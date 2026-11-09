"""Exercise the actual snapshot shell function with real jq and fake radio/HTTP.

Requires POSIX sh (or NETWATCH_TEST_SHELL pointing to BusyBox) and jq.
No router, live HTTP requests, config writes, or cron changes.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


AGENT = Path(__file__).resolve().parents[1] / 'deploy_files' / 'agent3_fixed.sh'
SHELL = os.environ.get('NETWATCH_TEST_SHELL') or shutil.which('sh')
JQ = shutil.which('jq')


@unittest.skipUnless(SHELL and JQ, 'snapshot integration tests require sh and jq')
class Snapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.leases = self.root / 'leases'
        self.leases.write_text('')
        self.args = self.root / 'args'
        self.header = self.root / 'header'
        source = AGENT.read_text(encoding='utf-8')
        start = source.index('cmd_snapshot() {')
        end = source.index('\n}\n', start) + 3
        function = source[start:end].replace('/tmp/dhcp.leases', self.leases.as_posix())
        self.script = self.root / 'snapshot.sh'
        self.script.write_text('''set -e
log() { printf '%s\\n' "$*" >&2; }
get_router_id() { printf '%s' "$TEST_ROUTER"; }
iw() { printf '%s\\n' "$WIRELESS"; }
iwinfo() {
    [ "${RADIO_FAIL:-0}" = 0 ] || return 1
    case "$1" in
        phy0-ap0) printf '%s\\n' "$ASSOC0" ;;
        phy1-ap0) printf '%s\\n' "$ASSOC1" ;;
        *) return 1 ;;
    esac
}
ip() { printf '%s\\n' "$NEIGHBORS"; }
hostapd_cli() { echo 'hostapd_cli must not be used' >&2; return 99; }
curl() {
    printf '%s\\n' "$@" > "$CAPTURE_ARGS"
    cat > "$CAPTURE_HEADER"
    return "${CURL_EXIT:-0}"
}
''' + function + '\ncmd_snapshot\n', encoding='utf-8', newline='\n')
        self.env = dict(os.environ, TEST_ROUTER='rt_test',
                        PI_URL='http://example.invalid:8080/', AGENT_SECRET='test-only-secret',
                        WIRELESS='phy#0\n\tInterface phy0-ap0\nphy#1\n\tInterface phy1-ap0',
                        ASSOC0='', ASSOC1='', NEIGHBORS='', RADIO_FAIL='0', CURL_EXIT='0',
                        CAPTURE_ARGS=self.args.as_posix(), CAPTURE_HEADER=self.header.as_posix())

    def shell(self):
        return [SHELL, 'sh'] if 'busybox' in Path(SHELL).name.lower() else [SHELL]

    def run_snapshot(self, **env):
        return subprocess.run(self.shell() + [str(self.script)], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=15)

    def payload(self):
        args = self.args.read_text().splitlines()
        self.assertIn('--fail', args)
        self.assertIn('--max-time', args)
        self.assertEqual(args[-1], 'http://example.invalid:8080/devices')
        self.assertNotIn('test-only-secret', '\n'.join(args))
        self.assertEqual(self.header.read_text().strip(), 'X-Agent-Secret: test-only-secret')
        return json.loads(args[args.index('--data-binary') + 1])

    def test_six_clients_without_hostapd(self):
        assoc = '\n'.join(f'AA:BB:CC:DD:EE:{i:02X} -55 dBm / -90 dBm' for i in range(6))
        result = self.run_snapshot(ASSOC0=assoc)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.payload()
        self.assertEqual(data['router_id'], 'rt_test')
        self.assertEqual(len(data['devices']), 6)
        self.assertEqual(data['devices'][0]['mac'], 'aa:bb:cc:dd:ee:00')
        self.assertIsInstance(data['timestamp'], int)

    def test_empty_radios_send_empty_snapshot(self):
        result = self.run_snapshot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload()['devices'], [])
        self.assertEqual(self.payload()['snapshot_interval'], 60)

    def test_fast_cron_marks_its_snapshot_interval(self):
        result = self.run_snapshot(NETWATCH_SNAPSHOT_INTERVAL='20')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload()['snapshot_interval'], 20)

    def test_dedup_and_json_escaping(self):
        hostname = 'device"with\\quotes'
        self.leases.write_text(f'0 aa:bb:cc:dd:ee:01 192.0.2.10 {hostname} *\n')
        assoc = 'AA:BB:CC:DD:EE:01 -55 dBm'
        result = self.run_snapshot(ASSOC0=assoc, ASSOC1=assoc)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload()['devices'], [dict(mac='aa:bb:cc:dd:ee:01',
                                                       ip='192.0.2.10', hostname=hostname)])

    def test_neighbor_fallback_and_unknown_hostname(self):
        self.leases.write_text('0 aa:bb:cc:dd:ee:01 192.0.2.10 * *\n')
        result = self.run_snapshot(ASSOC0='AA:BB:CC:DD:EE:01 -55 dBm\nAA:BB:CC:DD:EE:02 -60 dBm',
                                  NEIGHBORS='192.0.2.11 dev br-lan lladdr aa:bb:cc:dd:ee:02 REACHABLE')
        self.assertEqual(result.returncode, 0, result.stderr)
        devices = self.payload()['devices']
        self.assertEqual(devices[0]['hostname'], 'unknown')
        self.assertEqual(devices[1]['ip'], '192.0.2.11')

    def test_http_error_propagates(self):
        result = self.run_snapshot(CURL_EXIT='22')
        self.assertEqual(result.returncode, 22)

    def test_radio_failure_prevents_partial_send(self):
        result = self.run_snapshot(RADIO_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.args.exists())

    def test_no_interfaces_prevents_send(self):
        result = self.run_snapshot(WIRELESS='')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.args.exists())

    def test_unpaired_prevents_send(self):
        result = self.run_snapshot(TEST_ROUTER='')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.args.exists())

    def test_agent_shell_syntax(self):
        result = subprocess.run(self.shell() + ['-n', str(AGENT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
