import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "ops" / "restore_netwatch.py"
spec = importlib.util.spec_from_file_location("restore_netwatch", source)
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


class Restore(unittest.TestCase):
    def test_failed_preflight_prevents_package_installation(self):
        with patch.object(restore, "check_target", side_effect=RuntimeError("occupied")), \
             patch.object(restore, "install_packages") as packages, \
             patch.object(restore, "install_files") as files:
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                restore.apply_restore(Path("/staging"), {"python": "3.13"})
        packages.assert_not_called()
        files.assert_not_called()

    def test_caddy_and_bot_start_only_after_local_route_checks(self):
        operations = []
        with patch.object(restore, "check_target"), \
             patch.object(restore, "install_packages"), \
             patch.object(restore, "install_files"), \
             patch.object(restore.time, "sleep"), \
             patch.object(restore, "run", side_effect=lambda *args: operations.append(args)), \
             patch.object(restore, "wait_for_local_routes",
                          side_effect=lambda routes: operations.append(("probe", routes))):
            restore.apply_restore(Path("/staging"), {"python": "3.13"})
        probe = next(i for i, operation in enumerate(operations) if operation[0] == "probe")
        self.assertIn(("systemctl", "start", "netwatch-api", "netwatch-monitor"), operations[:probe])
        self.assertEqual(operations[probe][1]["http://127.0.0.1:8080/agent/identity"], 403)
        self.assertIn(("systemctl", "start", "caddy", "netwatch-bot"), operations[probe + 1:])
        self.assertEqual(operations[-1], ("systemctl", "enable", "caddy", *restore.NETWATCH_UNITS))

    def test_service_uses_isolated_venv_and_separate_sqlite_path(self):
        unit = restore.app_unit("api.py")
        self.assertIn("User=netwatch-app\n", unit)
        self.assertIn("Environment=NETWATCH_DB_PATH=/var/lib/netwatch/netwatch.db\n", unit)
        self.assertIn("ExecStart=/opt/netwatch/venv/bin/python -B /opt/netwatch/api.py\n", unit)
