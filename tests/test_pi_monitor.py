import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


path = Path(__file__).resolve().parents[1] / "monitor" / "pi_monitor.py"
spec = importlib.util.spec_from_file_location("pi_monitor", path)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class Monitor(unittest.TestCase):
    def test_counter_deltas_handle_first_sample_and_reset(self):
        self.assertIsNone(monitor.cpu_percent(None, (10, 5)))
        self.assertIsNone(monitor.cpu_percent((10, 5), (1, 0)))
        self.assertEqual(monitor.cpu_percent((10, 5), (30, 10)), 75.0)
        self.assertEqual(
            monitor.network_rates({"eth0": (100, 200)}, {"eth0": (150, 1), "wlan0": (1, 1)}, 5),
            [
                {"interface": "eth0", "rx_bytes_per_s": 10, "tx_bytes_per_s": None},
                {"interface": "wlan0", "rx_bytes_per_s": None, "tx_bytes_per_s": None},
            ],
        )

    def test_only_explicit_dashboard_assets_and_snapshot_are_served(self):
        collector = SimpleNamespace(payload=None)
        handler_type = monitor.make_handler(collector)
        handler = handler_type.__new__(handler_type)
        responses = []
        handler.respond = lambda *args: responses.append(args)

        def get(path):
            handler.path = path
            handler.do_GET()
            return responses[-1]

        self.assertEqual(get("/monitor/api/v1/metrics")[0], 503)
        collector.payload = b'{"cpu_percent":25}'
        self.assertEqual(get("/monitor/api/v1/metrics"), (200, "application/json", collector.payload))
        self.assertIn(b"Raspberry Pi", get("/monitor/")[2])
        self.assertEqual(get("/monitor/../pi_monitor.py")[0], 404)
        self.assertEqual(get("/monitor/.env")[0], 404)
