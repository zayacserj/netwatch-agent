"""Verify and install NetWatch from a backup or from the clean starter archive."""
import argparse
import getpass
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location(
    "restore_netwatch", Path(__file__).resolve().with_name("restore_netwatch.py"),
)
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


def configure(stage):
    domain = input("Публичный DNS-домен NetWatch без https://: ").strip().lower()
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if (len(domain) > 253 or not re.fullmatch(rf"(?:{label}\.)+[a-z]{{2,63}}", domain)):
        raise ValueError("Укажите публичное DNS-имя, например netwatch.example.org.")
    admin = input("Telegram ID администратора (Enter — пропустить): ").strip()
    if admin and not re.fullmatch(r"[1-9][0-9]{0,18}", admin):
        raise ValueError("Нужен числовой Telegram ID.")
    token = getpass.getpass("Токен бота из BotFather: ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Неверный формат токена Telegram.")
    password = getpass.getpass("Новый пароль монитора (логин monitor): ")
    if not 12 <= len(password) or len(password.encode()) > 72:
        raise ValueError("Нужно минимум 12 символов и максимум 72 байта.")
    if password != getpass.getpass("Повторите пароль монитора: "):
        raise ValueError("Пароли различаются.")
    return domain, admin, token, password


def write_config(stage, domain, admin, token, password):
    result = subprocess.run(
        ["caddy", "hash-password", "--algorithm", "bcrypt"],
        input=password + "\n", text=True, capture_output=True, check=True,
    )
    digest = result.stdout.strip()
    if not re.fullmatch(r"\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}", digest):
        raise RuntimeError("Caddy не вернул корректный bcrypt-хеш.")
    (stage / "opt/netwatch/.env").write_text(
        f"TELEGRAM_BOT_TOKEN={token}\nADMIN_IDS={admin}\n"
        f"PI_URL_PUBLIC=https://{domain}\nPI_MONITOR_URL=https://{domain}/monitor/\n",
        encoding="utf-8",
    )
    config = stage / "etc/caddy/Caddyfile"
    config.write_text(
        config.read_text(encoding="utf-8").replace("DOMAIN_PLACEHOLDER", domain)
        .replace("HASH_PLACEHOLDER", digest), encoding="utf-8",
    )
    for name in ("opt/netwatch/agent3.sh", "var/www/netwatch/agent.sh"):
        agent = stage / name
        agent.write_bytes(agent.read_bytes().replace(b"DOMAIN_PLACEHOLDER", domain.encode("ascii")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="verified NetWatch backup from Raspberry Pi")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    archive = args.archive or Path(__file__).resolve().with_name("starter.tar.gz")
    with tempfile.TemporaryDirectory(prefix="netwatch-starter-") as tmp:
        stage, profile = restore.backup_netwatch.prepare_restore(
            archive.resolve(), Path(tmp),
        )
        if args.archive is not None:
            print("Архив Pi проверен. Для восстановления будут использованы настройки и базы из архива.", flush=True)
            if args.apply:
                restore.apply_restore(stage, profile)
            return

        print("Архив проверен. В архиве чистая база и шаблоны настроек.", flush=True)
        if args.apply:
            if not os.isatty(0):
                raise RuntimeError("Для ввода секретов нужен интерактивный SSH-сеанс.")
            restore.check_target(profile)
            settings = configure(stage)
            restore.apply_restore(stage, profile, before_install=lambda: write_config(stage, *settings))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, EOFError) as exc:
        raise SystemExit(f"STOP: {exc}")
