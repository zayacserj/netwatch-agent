"""Build the Windows wizard and its allowlisted starter archive."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
TARGET = SOURCE / "dist/NetWatch-Pi-Wizard.exe"

CADDY = """DOMAIN_PLACEHOLDER {
    @monitor path /monitor /monitor/*
    handle @monitor {
        basic_auth {
            monitor HASH_PLACEHOLDER
        }
        reverse_proxy 127.0.0.1:8090 {
            header_up -Authorization
        }
    }
    @api path /pair /agent/identity /devices /health /event /agent.sh
    handle @api {
        reverse_proxy 127.0.0.1:8080
    }
    handle {
        respond 404
    }
}
"""


def starter(database):
    files = {
        "opt/netwatch/api.py": (SOURCE / "deploy_files/api_fixed.py").read_bytes(),
        "opt/netwatch/db.py": (SOURCE / "deploy_files/db_fixed.py").read_bytes(),
        "opt/netwatch/netwatch_telegram_bot.py": (SOURCE / "netwatch_telegram_bot.py").read_bytes(),
        "opt/netwatch/.env": b"# Filled interactively on Raspberry Pi.\n",
        "opt/netwatch/netwatch.db": database,
        "etc/caddy/Caddyfile": CADDY.encode(),
    }
    agent = (SOURCE / "deploy_files/agent3_fixed.sh").read_bytes()
    agent = agent.replace(b"https://netwatchagent.duckdns.org", b"https://DOMAIN_PLACEHOLDER")
    files["opt/netwatch/agent3.sh"] = agent
    files["var/www/netwatch/agent.sh"] = agent
    for name in ("pi_monitor.py", "index.html", "style.css", "dashboard.js"):
        files[f"opt/netwatch/monitor/{name}"] = (SOURCE / "monitor" / name).read_bytes()
    manifest = {
        "version": 1,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "databases": ["opt/netwatch/netwatch.db"],
        "runtime": {"machine": "aarch64", "python": "3.13",
                    "packages": {"aiohttp": "3.14.5", "aiogram": "3.31.0"}},
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in {**files, "manifest.json": json.dumps(manifest).encode()}.items():
            entry = tarfile.TarInfo(name)
            entry.mode = 0o600
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def restore_with_hook():
    source = (SOURCE / "ops/restore_netwatch.py").read_text(encoding="utf-8")
    old = "def apply_restore(stage, profile):\n    check_target(profile)\n    install_packages(stage)\n"
    if source.count(old) != 1:
        raise RuntimeError("The restore entry point changed")
    return source.replace(
        old, "def apply_restore(stage, profile, before_install=None):\n"
        "    check_target(profile)\n    install_packages(stage)\n"
        "    if before_install is not None:\n        before_install()\n",
    )


def build(tmp):
    database = tmp / "empty.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute("VACUUM")
        assert conn.execute("SELECT name FROM sqlite_schema").fetchall() == []
    finally:
        conn.close()
    payload = tmp / "payload.zip"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("starter.tar.gz", starter(database.read_bytes()))
        bundle.writestr("restore_netwatch.py", restore_with_hook())
        bundle.write(HERE / "fresh_install.py", "fresh_install.py")
        for name in ("backup_netwatch.py", "netwatch-backup.service", "netwatch-backup.timer"):
            bundle.write(SOURCE / "ops" / name, name)
        bundle.write(SOURCE / "monitor/netwatch-monitor.service", "netwatch-monitor.service")
    with zipfile.ZipFile(payload) as bundle:
        bundle.extractall(tmp / "test")
    subprocess.run(
        [sys.executable, "-IB", str(tmp / "test/fresh_install.py")], check=True,
        env={**os.environ, "TEMP": str(tmp), "TMP": str(tmp)},
    )
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    TARGET.parent.mkdir(exist_ok=True)
    subprocess.run([
        str(framework / "csc.exe"), "/nologo", "/target:winexe", "/codepage:65001",
        "/reference:System.IO.Compression.dll", "/reference:System.IO.Compression.FileSystem.dll",
        f"/resource:{payload},NetWatch.Payload", f"/out:{TARGET}", str(HERE / "Wizard.cs"),
    ], check=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(dir=HERE) as tmp:
        build(Path(tmp))
    print(TARGET)
