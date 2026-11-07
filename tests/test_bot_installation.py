"""Test actual /add_router command generation and its shell bootstrap offline."""
import ast
import asyncio
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SHELL = os.environ.get('NETWATCH_TEST_SHELL') or shutil.which('sh')

OPENWRT = '''id() { echo 0; }
command() {
    [ "$1" = -v ] || return 1
    case "$2" in
        apk|opkg) [ "$2" = "$MOCK_PACKAGE_MANAGER" ]; return ;;
        uclient-fetch) [ "$MOCK_UCLIENT" = 1 ]; return ;;
        curl|jq|iw|iwinfo)
            case " $MOCK_MISSING " in
                *" $2 "*) [ "$packages_installed" = yes ]; return ;;
            esac ;;
    esac
    return 0
}
apk() {
    echo "apk $*" >&2
    if [ "$1" = add ]; then
        [ "$MOCK_INSTALL_FAIL" != 1 ] || return 7
        packages_installed=yes
    fi
}
opkg() {
    echo "opkg $*" >&2
    if [ "$1" = install ]; then
        [ "$MOCK_INSTALL_FAIL" != 1 ] || return 7
        packages_installed=yes
    fi
}
'''


def mock_openwrt_paths(command, missing_ca=False):
    return command.replace('/etc/openwrt_release', '"$ARTIFACT"').replace(
        '/etc/ssl/certs/ca-certificates.crt', '"${ARTIFACT}.missing"' if missing_ca else '"$ARTIFACT"'
    )


class BotInstallation(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / 'netwatch_telegram_bot.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name in ('_agent_install_command', 'cmd_add_router')]
        self.assertEqual(len(nodes), 2)
        for node in nodes:
            node.decorator_list = []
            for arg in node.args.args:
                arg.annotation = None
        self.codes = []
        namespace = dict(os=os, secrets=SimpleNamespace(token_hex=lambda count: '11223344'),
                         upsert_user=lambda *args: None,
                         create_pairing_code=lambda *args, **kwargs: self.codes.append((args, kwargs)))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot-installation', 'exec'), namespace)
        self.command = namespace['_agent_install_command']
        self.handler = namespace['cmd_add_router']
        self.replies = []

        async def reply(text, **kwargs):
            self.replies.append((text, kwargs))

        self.message = SimpleNamespace(from_user=SimpleNamespace(id=1, username='test', first_name='Test'),
                                       reply=reply)

    def render(self, url='https://netwatchagent.example', legacy='http://192.168.10.222:8080'):
        with patch.dict(os.environ, PI_URL=legacy, PI_URL_PUBLIC=url):
            asyncio.run(self.handler(self.message))
        return self.replies[-1]

    def test_command_pins_an_immutable_release_and_its_actual_checksum(self):
        command = self.command('11223344', 'https://pi.invalid:8080')
        self.assertRegex(command, r'/[0-9a-f]{40}/deploy_files/agent3_fixed\.sh')
        self.assertNotIn('/master/', command)
        self.assertNotIn('/main/', command)
        digest = hashlib.sha256((ROOT / 'deploy_files/agent3_fixed.sh').read_bytes()).hexdigest()
        self.assertIn(digest, command)
        self.assertLess(command.index('sha256sum -c -'), command.index(' pair --install-deps --code '))
        self.assertIn('-fsS', command)
        self.assertIn('--disable', command)
        self.assertNotIn(' -k ', command)

    def test_universal_command_is_rendered_as_copyable_code(self):
        text, kwargs = self.render()
        self.assertEqual(kwargs['parse_mode'], 'Markdown')
        self.assertIn('```\n(\n', text)
        self.assertEqual(len(self.codes), 1)
        self.assertEqual(self.codes[0], (('11223344', 1), {'expires_sec': 900}))
        self.assertEqual(text.count('pair --install-deps --code 11223344'), 1)
        self.assertNotIn('192.168.10.222', text)

    def test_single_canonical_command_ignores_legacy_local_address(self):
        text, _ = self.render(url='https://api.example.invalid', legacy='http://10.0.0.10:8080')
        self.assertIn('--url https://api.example.invalid', text)
        self.assertLessEqual(len(text), 4096)
        self.assertEqual(len(self.codes), 1)
        self.assertEqual(text.count('pair --install-deps --code 11223344'), 1)
        other, _ = self.render(url='https://api.example.invalid', legacy='http://192.168.99.99:8080')
        self.assertEqual(text, other)

    def test_old_public_http_default_is_not_offered(self):
        text, _ = self.render(url='http://95.31.29.233:8080')
        self.assertNotIn('95.31.29.233', text)
        self.assertIn('единый HTTPS-адрес', text)
        self.assertEqual(self.codes, [])

    def test_invalid_address_does_not_issue_code_or_expose_credentials(self):
        text, _ = self.render(url='https://admin:private-password@pi.invalid')
        self.assertEqual(self.codes, [])
        self.assertNotIn('private-password', text)

    def test_missing_canonical_address_never_falls_back_to_lan(self):
        text, _ = self.render(url='')
        self.assertEqual(self.codes, [])
        self.assertNotIn('192.168.10.222', text)

    def test_ip_literals_and_localhost_cannot_be_canonical_host(self):
        for url in ('https://192.168.10.222', 'https://95.31.29.233', 'https://localhost',
                    'https://[::1]', 'https://netwatch.local', 'http://public.example'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.command('11223344', url)

    def test_invalid_codes_and_urls_are_rejected(self):
        for code in ('', 'not-code', '1122;344'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.command(code, 'https://pi.invalid')
        for url in ('file:///tmp/agent.sh', 'https://pi.invalid?x=1', 'https://pi.invalid#x',
                    'https://pi.invalid:invalid', 'https://pi.invalid\ncommand'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.command('11223344', url)

    def shell(self):
        if not SHELL:
            self.skipTest('shell bootstrap checks require sh')
        return [SHELL, 'sh'] if 'busybox' in Path(SHELL).name.lower() else [SHELL]

    def bootstrap(self, artifact, *, correct_hash=True, curl_exit='0', url='https://pi.invalid:8080',
                  package_manager='', missing='', missing_ca=False, install_fail=False, uclient=False):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as name:
            folder = Path(name)
            download = folder / 'artifact.sh'
            download.write_text(artifact, encoding='utf-8', newline='\n')
            marker = folder / 'paired'
            command = self.command('11223344', url)
            if correct_hash:
                command = re.sub(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])',
                                 hashlib.sha256(download.read_bytes()).hexdigest(), command)
            command = command.replace('/tmp/netwatch-install.XXXXXX', (folder / 'download.XXXXXX').as_posix())
            mock = '''curl() {
    [ "$CURL_EXIT" = 0 ] || return "$CURL_EXIT"
    while [ "$#" -gt 0 ]; do
        case "$1" in -o|-O) cp "$ARTIFACT" "$2"; return ;; esac
        shift
    done
    return 99
}
uclient-fetch() { curl "$@"; }
'''
            result = subprocess.run(self.shell() + ['-c', OPENWRT + mock + mock_openwrt_paths(command, missing_ca)],
                                    capture_output=True, text=True,
                                    env=dict(os.environ, CURL_EXIT=curl_exit, ARTIFACT=download.as_posix(),
                                             PAIR_MARKER=marker.as_posix(),
                                             MOCK_PACKAGE_MANAGER=package_manager, MOCK_MISSING=missing,
                                             MOCK_INSTALL_FAIL=str(int(install_fail)),
                                             MOCK_UCLIENT=str(int(uclient))), timeout=15)
            args = marker.read_text().splitlines() if marker.exists() else None
            # Linux can unlink a script just executed by a child shell;
            # busybox-w32 may retain its handle until the parent unwinds.
            if os.name != 'nt':
                self.assertEqual(list(folder.glob('download.*')), [])
            return result, args

    def test_bootstrap_passes_code_and_url_without_shell_interpretation(self):
        url = 'https://pi.invalid/path;literal'
        result, args = self.bootstrap('printf "%s\\n" "$@" > "$PAIR_MARKER"\n', url=url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args, ['pair', '--install-deps', '--code', '11223344', '--url', url])
        self.assertEqual(result.stderr, '')

    def test_missing_tools_are_installed_before_pairing(self):
        from test_agent_installation import Installation
        for manager, verb in (('apk', 'add'), ('opkg', 'install')):
            for missing, missing_ca in (('curl jq iw iwinfo', True), ('jq', False), ('', False)):
                with self.subTest(manager=manager, missing=missing):
                    fixture = Installation()
                    fixture.setUp()
                    self.addCleanup(fixture.doCleanups)
                    fixture.agent.write_text(OPENWRT + fixture.prefix + fixture.source,
                                             encoding='utf-8', newline='\n')
                    if missing_ca:
                        fixture.ca_bundle.unlink()
                    result = fixture.run_agent(
                        'pair', '--install-deps', '--code', '11223344', '--url', 'https://pi.invalid',
                        MOCK_PACKAGE_MANAGER=manager, MOCK_MISSING=missing,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(fixture.requests()), 1)
                    packages = missing + (' ca-bundle' if missing_ca else '')
                    expected = [f'{manager} update', f'{manager} {verb} {packages}'] if packages else []
                    self.assertEqual(result.stderr.splitlines(), expected)

    def test_bootstrap_downloads_without_curl(self):
        artifact = 'echo executed > "$PAIR_MARKER"\n'
        for manager, verb in (('apk', 'add'), ('opkg', 'install')):
            result, args = self.bootstrap(artifact, missing='curl', uclient=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(args, ['executed'])
            self.assertEqual(result.stderr, '')
            result, args = self.bootstrap(artifact, package_manager=manager, missing='curl')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(args, ['executed'])
            self.assertEqual(result.stderr.splitlines(), [
                f'{manager} update', f'{manager} {verb} curl ca-bundle',
            ])

    def test_package_failure_stops_before_pairing(self):
        result, args = self.bootstrap('echo executed > "$PAIR_MARKER"\n',
                                      package_manager='apk', missing='curl', install_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(args)

    def test_checksum_mismatch_prevents_execution(self):
        result, args = self.bootstrap('echo executed > "$PAIR_MARKER"\n', correct_hash=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(args)

    def test_download_failure_prevents_execution(self):
        result, args = self.bootstrap('echo executed > "$PAIR_MARKER"\n', curl_exit='22')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(args)

    def test_invalid_shell_artifact_is_not_executed_even_with_matching_hash(self):
        result, args = self.bootstrap(') invalid shell\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(args)

    def test_generated_command_through_actual_installer_preserves_cron(self):
        from test_agent_installation import Installation
        fixture = Installation()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        command = self.command('11223344', 'https://pi.invalid:8080')
        # The harness adds fake radio/HTTP functions to the real release script.
        command = re.sub(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])',
                         hashlib.sha256(fixture.agent.read_bytes()).hexdigest(), command)
        command = command.replace('/tmp/netwatch-install.XXXXXX', (fixture.root / 'download.XXXXXX').as_posix())
        mock = '''curl() {
    while [ "$#" -gt 0 ]; do
        if [ "$1" = -o ]; then cp "$ARTIFACT" "$2"; return; fi
        shift
    done
    return 99
}
'''
        result = subprocess.run(self.shell() + ['-c', OPENWRT + mock + mock_openwrt_paths(command)],
                                capture_output=True, text=True,
                                env=dict(fixture.env, ARTIFACT=fixture.agent.as_posix(),
                                         CANONICAL_ID='rt_from_server', MOCK_PACKAGE_MANAGER=''),
                                timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        fixture.assert_cron()
        self.assertEqual(fixture.marker.read_text().strip(), 'rt_from_server')
        self.assertEqual(len(fixture.requests()), 1)
        self.assertNotIn('a'*32, result.stdout + result.stderr)
