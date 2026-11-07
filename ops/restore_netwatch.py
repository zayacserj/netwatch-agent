"""Restore NetWatch to a clean Raspberry Pi OS installation."""
import argparse
import importlib.util
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


ROOT = Path("/opt/netwatch")
STATE = Path("/var/lib/netwatch")
WEB = Path("/var/www/netwatch")
SYSTEMD = Path("/etc/systemd/system")
BACKUP_DIR = Path("/usr/local/lib/netwatch")
APP_USER = "netwatch-app"
METRICS_USER = "netwatch-metrics"
CADDYFILE = Path("/etc/caddy/Caddyfile")
BUNDLE = Path(__file__).resolve().parent
backup_spec = importlib.util.spec_from_file_location("backup_netwatch", BUNDLE / "backup_netwatch.py")
backup_netwatch = importlib.util.module_from_spec(backup_spec)
backup_spec.loader.exec_module(backup_netwatch)
APP_FILES = ("api.py", "db.py", "netwatch_telegram_bot.py", "agent3.sh")
MONITOR_FILES = ("pi_monitor.py", "index.html", "style.css", "dashboard.js")
NETWATCH_UNITS = ("netwatch-api", "netwatch-bot", "netwatch-monitor")
CADDY_KEYRING = Path("/usr/share/keyrings/caddy-stable-archive-keyring.gpg")
CADDY_SOURCES = Path("/etc/apt/sources.list.d/caddy-stable.list")
CADDY_REPOSITORY = "https://dl.cloudsmith.io/public/caddy/stable"


def run(*args, umask=-1):
    subprocess.run(args, check=True, umask=umask)


def public_directory(path):
    if not path.exists():
        public_directory(path.parent)
        path.mkdir()
        path.chmod(0o755)


def install(source, target, mode=0o644, uid=0, gid=0):
    public_directory(target.parent)
    shutil.copyfile(source, target)
    os.chown(target, uid, gid)
    target.chmod(mode)


def clean_target_paths():
    return (
        ROOT, STATE, WEB, BACKUP_DIR, CADDYFILE, CADDY_KEYRING, CADDY_SOURCES,
        SYSTEMD / "caddy.service",
        Path("/etc/netwatch-backup.conf"),
        *(SYSTEMD / f"{unit}.service" for unit in NETWATCH_UNITS),
        SYSTEMD / "netwatch-backup.service", SYSTEMD / "netwatch-backup.timer",
    )


def existing_paths(paths):
    return [path for path in paths if path.exists() or path.is_symlink()]


def check_target(profile):
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("Запустите --apply от root.")
    import pwd
    os_release = Path("/etc/os-release").read_text()
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if (not re.search(r'(?m)^VERSION_ID="?13"?$', os_release)
            or platform.machine() != "aarch64"
            or profile.get("machine") != "aarch64"
            or profile["python"] != python):
        raise RuntimeError("Нужна чистая Debian/Raspberry Pi OS 13 arm64 с Python версии из архива.")
    present = existing_paths(clean_target_paths())
    if present:
        raise RuntimeError("Целевая система не чистая, уже существует: " + ", ".join(map(str, present)))
    for name in (APP_USER, METRICS_USER):
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        raise RuntimeError(f"Системная учётная запись {name} уже существует.")
    for unit in (*NETWATCH_UNITS, "caddy", "nginx", "apache2", "netwatch-backup.timer"):
        if subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0:
            raise RuntimeError(f"Служба уже запущена: {unit}")
    for name in ("netwatch-monitor.service", "netwatch-backup.service", "netwatch-backup.timer"):
        if not (BUNDLE / name).is_file():
            raise RuntimeError(f"Отсутствует файл комплекта восстановления: {name}")


def install_packages(stage):
    run("apt-get", "-o", "APT::Update::Error-Mode=any", "update", umask=0o022)
    run("apt-get", "install", "-y", "--no-install-recommends",
        "ca-certificates", "curl", "gnupg", "python3-venv", "git", umask=0o022)
    key = stage / "caddy-stable.gpg.key"
    run("curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "60",
        "--proto", "=https", "--proto-redir", "=https", "--output", str(key),
        f"{CADDY_REPOSITORY}/gpg.key")
    run("gpg", "--batch", "--dearmor", "--output", str(CADDY_KEYRING), str(key))
    CADDY_KEYRING.chmod(0o644)
    CADDY_SOURCES.write_text(
        f"deb [signed-by={CADDY_KEYRING}] {CADDY_REPOSITORY}/deb/debian any-version main\n",
        encoding="utf-8",
    )
    CADDY_SOURCES.chmod(0o644)
    run("apt-get", "-o", "APT::Update::Error-Mode=any", "update", umask=0o022)
    # Leave Caddy stopped until the archived HTTPS configuration is validated.
    run("systemctl", "mask", "caddy.service")
    run("apt-get", "install", "-y", "--no-install-recommends", "caddy", umask=0o022)


def app_unit(script):
    root = ROOT.as_posix()
    state = STATE.as_posix()
    return f"""[Unit]
Description=NetWatch {script}
After=network-online.target
Wants=network-online.target

[Service]
User={APP_USER}
Group={APP_USER}
WorkingDirectory={root}
Environment=NETWATCH_DB_PATH={state}/netwatch.db
ExecStart={root}/venv/bin/python -B {root}/{script}
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""


def write_units(stage):
    for unit, script in (("netwatch-api", "api.py"), ("netwatch-bot", "netwatch_telegram_bot.py")):
        path = SYSTEMD / f"{unit}.service"
        path.write_text(app_unit(script), encoding="utf-8")
        path.chmod(0o644)
    for name in ("netwatch-monitor.service", "netwatch-backup.service", "netwatch-backup.timer"):
        install(BUNDLE / name, SYSTEMD / name)
    settings = stage / "etc/netwatch-backup.conf"
    if settings.is_file():
        install(settings, Path("/etc/netwatch-backup.conf"), 0o600)


def install_files(stage):
    import grp
    import pwd

    for name in (APP_USER, METRICS_USER):
        run("useradd", "--system", "--user-group", "--no-create-home",
            "--shell", "/usr/sbin/nologin", name)
    app = pwd.getpwnam(APP_USER)
    source = stage / "opt/netwatch"
    public_directory(ROOT)
    STATE.mkdir()
    os.chown(STATE, app.pw_uid, app.pw_gid)
    STATE.chmod(0o750)
    for name in APP_FILES:
        install(source / name, ROOT / name)
    install(source / ".env", ROOT / ".env", 0o640, gid=app.pw_gid)
    for name in ("netwatch.db", "netwatch_notify.db"):
        if (source / name).is_file():
            install(source / name, STATE / name, 0o600, app.pw_uid, app.pw_gid)
            (ROOT / name).symlink_to(STATE / name)
    for name in MONITOR_FILES:
        install(source / "monitor" / name, ROOT / "monitor" / name)
    for path in (stage / "var/www/netwatch").rglob("*"):
        if path.is_file():
            install(path, WEB / path.relative_to(stage / "var/www/netwatch"))
    public_directory(Path("/var/www/acme"))
    install(stage / "etc/caddy/Caddyfile", CADDYFILE, 0o640, gid=grp.getgrnam("caddy").gr_gid)
    install(BUNDLE / "backup_netwatch.py", BACKUP_DIR / "backup_netwatch.py", 0o700)
    (ROOT / "backups").mkdir(mode=0o700)
    run(sys.executable, "-m", "venv", str(ROOT / "venv"), umask=0o022)
    run(str(ROOT / "venv/bin/python"), "-m", "pip", "--isolated", "install",
        "--disable-pip-version-check", "--no-input", "--requirement", str(stage / "requirements.txt"),
        umask=0o022)
    run("git", "init", "--initial-branch=master", str(ROOT))
    run("git", "-C", str(ROOT), "remote", "add", "origin", "https://github.com/zayacserj/netwatch-agent.git")
    write_units(stage)


def http_status(url):
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except (URLError, TimeoutError):
        return None


def wait_for_local_routes(routes):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if all(http_status(url) == expected for url, expected in routes.items()):
            return
        time.sleep(1)
    raise RuntimeError("Локальная проверка API или монитора не прошла.")


def apply_restore(stage, profile):
    check_target(profile)
    install_packages(stage)
    install_files(stage)
    run("caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile")
    run("systemd-analyze", "verify", *(str(SYSTEMD / f"{unit}.service") for unit in NETWATCH_UNITS))
    run("systemctl", "daemon-reload")
    started = ["netwatch-api", "netwatch-monitor"]
    enabling = False
    try:
        run("systemctl", "start", *started)
        wait_for_local_routes({
            "http://127.0.0.1:8080/agent.sh": 200,
            "http://127.0.0.1:8080/agent/identity": 403,
            "http://127.0.0.1:8090/monitor/api/v1/metrics": 200,
        })
        run("systemctl", "unmask", "caddy.service")
        started.extend(["caddy", "netwatch-bot"])
        run("systemctl", "start", "caddy", "netwatch-bot")
        time.sleep(3)
        for unit in ("caddy", *NETWATCH_UNITS):
            run("systemctl", "is-active", unit)
        enabling = True
        run("systemctl", "enable", "caddy", *NETWATCH_UNITS)
    except BaseException:
        subprocess.run(["systemctl", "stop", *started], check=False)
        if enabling:
            subprocess.run(["systemctl", "disable", *started], check=False)
        raise
    print("NetWatch восстановлен. Локальные API и монитор отвечают, четыре службы активны.")
    print("Проверьте HTTPS из внешней сети и /health в Telegram. Таймер бэкапа подключается после проверки USB.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--stage-parent", type=Path, default=Path("/var/tmp"))
    parser.add_argument("--apply", action="store_true", help="install on a clean Pi")
    args = parser.parse_args()
    stage, profile = backup_netwatch.prepare_restore(args.archive.resolve(), args.stage_parent.resolve())
    print(f"Архив проверен, подготовленные файлы: {stage}", flush=True)
    if args.apply:
        apply_restore(stage, profile)
    else:
        existing = existing_paths(clean_target_paths())
        if existing:
            print("На этой системе доступна проверка архива. Для установки нужна чистая Pi OS.")
        else:
            print("Архив готов. Для установки на чистой Pi повторите команду с --apply.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"STOP: {exc}")
