import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "monitor" / "pi_monitor.py"
spec = importlib.util.spec_from_file_location("pi_monitor", path)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class Monitor(unittest.TestCase):
    def test_usb_storage_requires_matching_mount(self):
        mount = "/mnt/netwatch-backup"
        with patch.object(monitor, "disk", side_effect=lambda path: {"mount": path, "available": True}), \
                patch.object(monitor, "usb_block_devices", return_value={"sda": ("sda", "8:0"), "sda1": ("sda", "8:1")}), \
                patch.object(monitor.Path, "read_text", return_value="10 1 179:2 / / rw - ext4 /dev/root rw"), \
                patch.object(monitor.os, "stat", return_value=SimpleNamespace(st_dev=42)), \
                patch.object(monitor.os, "major", return_value=8, create=True), \
                patch.object(monitor.os, "minor", return_value=1, create=True):
            self.assertEqual([row["mount"] for row in monitor.storage(["/"])], ["/", None])
            with patch.object(monitor.Path, "read_text", return_value=f"11 10 8:1 / {mount} rw - ntfs3 /dev/sda1 rw"):
                self.assertEqual([row["mount"] for row in monitor.storage(["/"])], ["/", mount])

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
