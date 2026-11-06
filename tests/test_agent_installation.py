"""Real installer shell, fake HTTP/crontab, private filesystem and real jq.

No router, host cron, package manager, credentials, or live HTTP is used.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


AGENT = Path(__file__).resolve().parents[1] / 'deploy_files' / 'agent3_fixed.sh'
SHELL = os.environ.get('NETWATCH_TEST_SHELL') or shutil.which('sh')


@unittest.skipUnless(SHELL and shutil.which('jq'), 'installation tests require sh and jq')
class Installation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'netwatch'
        self.config = self.state / 'config'
        self.installed = self.state / 'agent3.sh'
        self.cron = self.root / 'crontab-root'
        self.original_cron = '*/7 * * * * /usr/bin/podkop list_update\n# keep me\n0 4 * * * /usr/bin/backup\n'
        self.cron.write_text(self.original_cron, newline='\n')
        self.lock = self.root / 'install.lock'
        self.calls = self.root / 'http.jsonl'
        self.marker = self.root / 'first-run'
        self.http = self.root / 'fake_http.py'
        self.http.write_text('''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
url=args[-1]
def flag(name, default=None):
    return args[args.index(name)+1] if name in args else default
body=None
if '--data-binary' in args:
    body=json.loads(Path(flag('--data-binary')[1:]).read_text())
headers=sys.stdin.read() if flag('--header')=='@-' else ''
with open(os.environ['HTTP_CALLS'],'a',encoding='utf-8') as stream:
    stream.write(json.dumps({'url':url,'body':body,'headers':headers})+'\\n')
identity=url.endswith('/agent/identity')
code=os.environ.get('IDENTITY_HTTP','200') if identity else os.environ.get('PAIR_HTTP','200')
response={'router_id':os.environ.get('IDENTITY_ID','rt_old')} if identity else {
    'status':'paired','router_id':os.environ.get('CANONICAL_ID') or body['router_id'],
    'agent_secret':os.environ.get('PAIR_SECRET','a'*32)}
if code!='200': response={'error':'Rejected'}
Path(flag('--output')).write_text(json.dumps(response),encoding='utf-8')
sys.stdout.write(code)
sys.exit(int(os.environ.get('CURL_EXIT','0')))
''', encoding='utf-8')
        uuid = self.root / 'uuid'
        uuid.write_text('01234567-89ab-cdef-0123-456789abcdef\n')
        mac = self.root / 'mac'
        mac.write_text('AA:BB:CC:DD:EE:FF\n')
        model = self.root / 'model'
        model.write_text('Cudy "Router"\nSecond line', newline='\n')
        source = AGENT.read_text(encoding='utf-8')
        for old, path in (('/etc/netwatch', self.state), ('/etc/crontabs/root', self.cron),
                          ('/tmp/netwatch-install.lock', self.lock),
                          ('/proc/sys/kernel/random/uuid', uuid),
                          ('/sys/class/net/br-lan/address', mac),
                          ('/sys/class/net/eth0/address', self.root / 'missing-mac'),
                          ('/tmp/sysinfo/model', model)):
            source = source.replace(old, path.as_posix())
        start = source.index('cmd_run() {')
        end = source.index('\n}\n', start) + 3
        source = source[:start] + '''cmd_run() {
    printf '%s\\n' "$(get_router_id)" >> "$INITIAL_RUN_LOG"
    return "${INITIAL_RUN_EXIT:-0}"
}
''' + source[end:]
        self.prefix = '''id() { echo 0; }
iw() { :; }
iwinfo() { :; }
logger() { :; }
crontab() {
    [ "${CRON_FAIL:-0}" = 0 ] || return 1
    cp "$1" "$CRON_PATH"
}
curl() { ''' + shlex.quote(Path(sys.executable).as_posix()) + ' ' + shlex.quote(self.http.as_posix()) + ''' "$@"; }
'''
        self.source = source
        self.agent = self.root / 'bootstrap.sh'
        self.agent.write_text(self.prefix + source, encoding='utf-8', newline='\n')
        self.env = dict(os.environ, CRON_PATH=self.cron.as_posix(), HTTP_CALLS=self.calls.as_posix(),
                        INITIAL_RUN_LOG=self.marker.as_posix(), INITIAL_RUN_EXIT='0',
                        CRON_FAIL='0', IDENTITY_HTTP='200', IDENTITY_ID='rt_old',
                        PAIR_HTTP='200', PAIR_SECRET='a'*32, CANONICAL_ID='', CURL_EXIT='0')

    def shell(self):
        return [SHELL, 'sh'] if 'busybox' in Path(SHELL).name.lower() else [SHELL]

    def run_agent(self, *args, path=None, **env):
        path = path or self.agent
        # Windows cannot replace an open script, unlike Linux. Keep the real
        # source path in $0, but supply its code through stdin for this harness.
        return subprocess.run(self.shell() + ['-c', 'eval "$(cat)"', str(path), *args],
                              input=path.read_text(encoding='utf-8'),
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=20)

    def pair(self, **env):
        return self.run_agent('pair', '--code', '11223344', '--url', 'http://pi.invalid:8080', **env)

    def seed_existing(self, identity='rt_old'):
        self.state.mkdir(exist_ok=True)
        self.config.write_text(f'PI_URL="http://pi.invalid:8080"\nROUTER_ID="{identity}"\n'
                               f'AGENT_SECRET="{"b"*32}"\nPODKOP_GROUP="custom-out"\n', newline='\n')
        self.installed.write_bytes(self.agent.read_bytes())

    def requests(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def assert_cron(self):
        text = self.cron.read_text()
        self.assertTrue(text.startswith(self.original_cron))
        self.assertEqual(text.count(str(self.installed.as_posix()) + ' snapshot'), 1)
        self.assertEqual(text.count(str(self.installed.as_posix()) + ' run'), 1)
        self.assertNotIn('/mnt/usb', text)
        self.assertIn('logger -t netwatch-agent', text)

    def test_first_install_preserves_unrelated_cron_and_uses_server_id(self):
        result = self.pair(CANONICAL_ID='rt_canonical')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assert_cron()
        self.assertEqual(self.marker.read_text().strip(), 'rt_canonical')
        self.assertIn('ROUTER_ID="rt_canonical"', self.config.read_text())
        self.assertEqual(self.installed.read_bytes(), self.agent.read_bytes())
        self.assertNotIn('a'*32, result.stdout + result.stderr)
        request = self.requests()[0]['body']
        self.assertEqual(request['mac'], 'aa:bb:cc:dd:ee:ff')
        self.assertEqual(request['model'], 'Cudy "Router"\nSecond line')
        self.assertEqual(len(request['router_id']), 19)
        self.assertFalse(self.lock.exists())

    def test_repeat_from_installed_file_reuses_binding_and_is_idempotent(self):
        self.seed_existing()
        original = self.config.read_bytes()
        first = self.run_agent('pair', '--code', '11223344', '--url', 'http://pi.invalid:8080', path=self.installed)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        cron = self.cron.read_bytes()
        second = self.run_agent('pair', '--code', '55667788', '--url', 'http://pi.invalid:8080', path=self.installed)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.cron.read_bytes(), cron)
        self.assertTrue(all(r['body'] is None for r in self.requests()))
        self.assertNotIn('b'*32, first.stdout + second.stdout)
        self.assert_cron()

    def test_cron_only_install_is_idempotent(self):
        self.state.mkdir()
        first = self.run_agent('install-cron')
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        cron = self.cron.read_bytes()
        second = self.run_agent('install-cron')
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual(self.cron.read_bytes(), cron)
        self.assert_cron()

    def test_expired_code_does_not_replace_files_or_cron(self):
        self.seed_existing()
        original = self.config.read_bytes(), self.installed.read_bytes(), self.cron.read_bytes()
        result = self.pair(IDENTITY_HTTP='403', PAIR_HTTP='403')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.config.read_bytes(), self.installed.read_bytes(), self.cron.read_bytes()), original)

    def test_cron_failure_restores_existing_runtime_files(self):
        self.seed_existing()
        original = self.config.read_bytes(), self.installed.read_bytes(), self.cron.read_bytes()
        result = self.pair(IDENTITY_HTTP='403', CANONICAL_ID='rt_old', CRON_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.config.read_bytes(), self.installed.read_bytes(), self.cron.read_bytes()), original)
        self.assertFalse(self.marker.exists())

    def test_first_install_cron_failure_leaves_no_runtime_binding(self):
        result = self.pair(CRON_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.installed.exists())
        self.assertEqual(self.cron.read_text(), self.original_cron)

    def test_secret_identity_mismatch_does_not_repair_or_replace_silently(self):
        self.seed_existing()
        result = self.pair(IDENTITY_ID='rt_other')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.requests()), 1)
        self.assertFalse(self.marker.exists())
        self.assertIn('ROUTER_ID="rt_old"', self.config.read_text())

    def test_uncertain_http_result_is_not_automatically_retried(self):
        result = self.pair(CURL_EXIT='28')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.requests()), 1)
        self.assertFalse(self.config.exists())
        self.assertTrue((self.state / 'pairing.pending_id').exists())

    def test_missing_dependency_stops_before_code_consumption(self):
        self.agent.write_text('command() { [ "$2" != jq ]; }\n' + self.prefix + self.source,
                              encoding='utf-8', newline='\n')
        result = self.pair()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing dependency: jq', result.stdout)
        self.assertEqual(self.requests(), [])
        self.assertFalse(self.config.exists())

    def test_installation_lock_stops_concurrent_installer(self):
        self.lock.mkdir()
        result = self.pair()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.requests(), [])

    def test_invalid_returned_secret_is_not_printed_or_installed(self):
        result = self.pair(PAIR_SECRET='not-a-valid-secret')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.config.exists())
        self.assertNotIn('not-a-valid-secret', result.stdout + result.stderr)

    def test_initial_delivery_failure_does_not_undo_successful_install(self):
        result = self.pair(INITIAL_RUN_EXIT='9')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(self.config.exists())
        self.assert_cron()
        self.assertIn('initial delivery failed', result.stdout)

    def test_different_api_requires_explicit_repair(self):
        self.seed_existing()
        result = self.run_agent('pair', '--code', '11223344', '--url', 'http://other.invalid')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.requests(), [])

    def test_config_is_source_of_router_id_even_with_legacy_file(self):
        self.seed_existing()
        (self.state / 'router_id').write_text('rt_stale\n')
        result = self.pair()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.marker.read_text().strip(), 'rt_old')

    def test_missing_uuid_cannot_register_a_shared_placeholder_id(self):
        (self.root / 'uuid').write_text('')
        result = self.pair()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.requests(), [])

    def test_invalid_target_type_is_rejected_before_pairing(self):
        self.state.mkdir()
        self.config.mkdir()
        result = self.pair()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.requests(), [])
