"""Run the actual health collection block with simulated Linux counters/radios."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


AGENT = Path(__file__).resolve().parents[1] / 'deploy_files' / 'agent3_fixed.sh'
SHELL = os.environ.get('NETWATCH_TEST_SHELL') or shutil.which('sh')


@unittest.skipUnless(SHELL and shutil.which('jq'), 'health tests require sh and jq')
class HealthMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stat = self.root / 'stat'
        self.after = self.root / 'stat.after'
        self.mem = self.root / 'meminfo'
        self.stat.write_text('cpu 100 0 100 800 0 0 0 0 40 0\n')
        self.after.write_text('cpu 150 0 150 900 0 0 0 0 140 0\n')
        self.mem.write_text('MemTotal: 524288 kB\nMemAvailable: 393216 kB\nMemFree: 100000 kB\n')
        source = AGENT.read_text(encoding='utf-8')
        start = source.index('cmd_run() {')
        end = source.index('    # Экранируем строковые значения', start)
        collection = source[start:end] + '}\n'
        collection = collection.replace('/proc/stat', self.stat.as_posix())
        collection = collection.replace('/proc/meminfo', self.mem.as_posix())
        self.script = self.root / 'health.sh'
        self.script.write_text('''set -e
get_router_id() { echo rt_test; }
log() { printf '%s\\n' "$*" >&2; }
ping() { return 0; }
iw() { printf '%s\\n' "$WIRELESS"; }
iwinfo() {
    [ "${RADIO_FAIL:-0}" = 0 ] || return 1
    case "$1" in
        phy0-ap0) printf '%s\\n' "$ASSOC0" ;;
        phy1-ap0) printf '%s\\n' "$ASSOC1" ;;
        *) return 1 ;;
    esac
}
sleep() { cp "$STAT_AFTER" "$STAT_FILE"; }
podkop() {
    [ "$1" = get_sing_box_status ] || return 99
    printf '%s' "$PODKOP_JSON"
    return "${PODKOP_EXIT:-0}"
}
capture() {
    jq -cn --arg cpu "$cpu" --arg ram "$ram" --argjson wifi "$wifi_status" \\
       --argjson clients "$wifi_clients" --arg podkop "$podkop_status" --argjson probe "$podkop_test" \\
       '{cpu:$cpu,ram:$ram,wifi:$wifi,clients:$clients,podkop:$podkop,probe:$probe}'
}
''' + collection.replace('\n}\n', '\n    capture\n}\n') + '\ncmd_run\n',
                               encoding='utf-8', newline='\n')
        self.env = dict(os.environ, STAT_AFTER=self.after.as_posix(), STAT_FILE=self.stat.as_posix(),
                        WIRELESS='phy#0\n Interface phy0-ap0\n type AP\nphy#1\n Interface phy1-ap0\n type AP',
                        ASSOC0='', ASSOC1='', RADIO_FAIL='0', PODKOP_JSON='{"running":1,"enabled":0}', PODKOP_EXIT='0')

    def run_health(self, **env):
        command = [SHELL, 'sh'] if 'busybox' in Path(SHELL).name.lower() else [SHELL]
        return subprocess.run(command + [str(self.script)], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=10)

    def metrics(self, **env):
        result = self.run_health(**env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_cpu_percentage_and_memory_units(self):
        values = self.metrics()
        self.assertEqual(values['cpu'], '50.0%')
        self.assertEqual(values['ram'], '128MiB/512MiB')

    def test_cpu_iowait_is_idle(self):
        self.after.write_text('cpu 150 0 100 900 50 0 0 0 40 0\n')
        self.assertEqual(self.metrics()['cpu'], '25.0%')

    def test_cpu_no_elapsed_ticks_reports_unavailable(self):
        self.after.write_bytes(self.stat.read_bytes())
        self.assertEqual(self.metrics()['cpu'], 'n/a')

    def test_cpu_counter_reset_reports_unavailable(self):
        self.after.write_text('cpu 1 0 0 2 0 0 0 0 0 0\n')
        self.assertEqual(self.metrics()['cpu'], 'n/a')

    def test_memory_available_zero_is_not_missing(self):
        self.mem.write_text('MemTotal: 524288 kB\nMemAvailable: 0 kB\nMemFree: 100000 kB\n')
        self.assertEqual(self.metrics()['ram'], '512MiB/512MiB')

    def test_memory_fallback_excludes_reclaimable_cache(self):
        self.mem.write_text('MemTotal: 524288 kB\nMemFree: 102400 kB\nBuffers: 10240 kB\n'
                            'Cached: 51200 kB\nSReclaimable: 5120 kB\nShmem: 5120 kB\n')
        self.assertEqual(self.metrics()['ram'], '352MiB/512MiB')

    def test_invalid_memory_reports_unavailable(self):
        self.mem.write_text('MemTotal: 0 kB\n')
        self.assertEqual(self.metrics()['ram'], 'n/a')

    def test_six_clients_without_hostapd(self):
        assoc = '\n'.join(f'AA:BB:CC:DD:EE:{i:02X} -55 dBm' for i in range(6))
        values = self.metrics(ASSOC0=assoc)
        self.assertEqual(values['wifi'], 1)
        self.assertEqual(values['clients'], 6)

    def test_empty_radio_still_has_wifi(self):
        values = self.metrics()
        self.assertEqual((values['wifi'], values['clients']), (1, 0))

    def test_duplicate_client_on_two_radios_counts_once(self):
        values = self.metrics(ASSOC0='AA:BB:CC:DD:EE:01 -55 dBm',
                              ASSOC1='aa:bb:cc:dd:ee:01 -60 dBm')
        self.assertEqual(values['clients'], 1)

    def test_no_ap_interface_reports_wifi_off(self):
        values = self.metrics(WIRELESS='phy#0\n Interface phy0-sta0\n type managed')
        self.assertEqual((values['wifi'], values['clients']), (0, 0))

    def test_radio_error_does_not_report_fake_zero_clients(self):
        result = self.run_health(RADIO_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_podkop_running_with_disabled_autostart_is_not_stopped(self):
        values = self.metrics()
        self.assertEqual(values['podkop'], 'running')
        self.assertIsNone(values['probe'])

    def test_podkop_stopped_is_explicit(self):
        self.assertEqual(self.metrics(PODKOP_JSON='{"running":0,"enabled":1}')['podkop'], 'stopped')

    def test_podkop_failed_command_reports_unknown(self):
        self.assertEqual(self.metrics(PODKOP_EXIT='1')['podkop'], 'unknown')

    def test_podkop_invalid_response_reports_unknown(self):
        self.assertEqual(self.metrics(PODKOP_JSON='unsupported command')['podkop'], 'unknown')

    def test_podkop_missing_runtime_field_reports_unknown(self):
        self.assertEqual(self.metrics(PODKOP_JSON='{"enabled":1}')['podkop'], 'unknown')
