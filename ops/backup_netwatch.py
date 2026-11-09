"""Create and verify NetWatch backups without stopping the running services."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path("/opt/netwatch")
DESTINATION = ROOT / "backups" / "scheduled"
INSTALL_SCRIPT = Path("/usr/local/lib/netwatch/backup_netwatch.py")
UNITS = ("netwatch-api", "netwatch-bot", "netwatch-monitor", "caddy")
ARCHIVE_NAME = re.compile(r"netwatch-\d{8}T\d{12}Z-[a-f0-9]{8}\.tar\.gz")
WEB_ROOT = Path("/var/www/netwatch")


def live_files():
    paths = [ROOT / name for name in (
        "api.py", "db.py", "netwatch_telegram_bot.py", ".env", "agent3.sh",
        "monitor/pi_monitor.py", "monitor/index.html", "monitor/dashboard.js",
        "monitor/style.css",
    )]
    paths.append(INSTALL_SCRIPT)
    paths.append(Path("/etc/caddy/Caddyfile"))
    for directory, _, files in os.walk(WEB_ROOT, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                paths.append(path)
    for unit in UNITS:
        output = subprocess.check_output([
            "systemctl", "show", unit, "-p", "FragmentPath", "-p", "DropInPaths",
        ], text=True, timeout=10)
        props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if not props.get("FragmentPath"):
            raise RuntimeError(f"No unit file for {unit}")
        paths.append(Path(props["FragmentPath"]))
        paths.extend(Path(path) for path in shlex.split(props.get("DropInPaths", "")))
    for name in ("netwatch-backup.service", "netwatch-backup.timer"):
        path = Path("/etc/systemd/system") / name
        if path.is_file():
            paths.append(path)
    configuration = Path("/etc/netwatch-backup.conf")
    if configuration.is_file():
        paths.append(configuration)
    return {str(path).lstrip("/"): path for path in paths}


def runtime_profile():
    packages = {}
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    venv = ROOT / "venv" / "lib" / f"python{python}" / "site-packages"
    for unit, package in (("netwatch-api", "aiohttp"), ("netwatch-bot", "aiogram")):
        try:
            pid = int(subprocess.check_output([
                "systemctl", "show", unit, "-p", "MainPID", "--value",
            ], timeout=5))
            executable = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0].decode()
            if Path(executable).parent == ROOT / "venv" / "bin":
                distribution = next(metadata.distributions(name=package, path=[str(venv)]), None)
                if distribution is None:
                    raise metadata.PackageNotFoundError(package)
                packages[package] = distribution.version
            else:
                packages[package] = metadata.version(package)
        except (OSError, ValueError, subprocess.SubprocessError, metadata.PackageNotFoundError):
            print(f"Версию {package} получить не удалось.", file=sys.stderr)
    return {
        "machine": platform.machine(),
        "python": python,
        "packages": packages,
    }


def database_files():
    paths = {"opt/netwatch/netwatch.db": ROOT / "netwatch.db"}
    legacy = ROOT / "netwatch_notify.db"
    if legacy.is_file():
        paths["opt/netwatch/netwatch_notify.db"] = legacy
    return paths


def sqlite_check(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError(f"SQLite integrity check failed: {path.name}")


def sqlite_backup(source, target):
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    sqlite_check(target)


def digest(stream, output=None):
    sha = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        sha.update(chunk)
        if output is not None:
            output.write(chunk)
    return sha.hexdigest()


def verify_archive(path, scratch=None):
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        by_name = {item.name: item for item in members}
        if len(by_name) != len(members) or any(not item.isfile() for item in members):
            raise RuntimeError("Unexpected archive entries")
        manifest = json.load(archive.extractfile("manifest.json"))
        files = manifest["files"]
        databases = set(manifest["databases"])
        if (manifest["version"] != 1 or set(by_name) != {"manifest.json", *files}
                or not databases or not databases <= files.keys()):
            raise RuntimeError("Archive manifest does not match its contents")
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=scratch or path.parent) as tmp:
            for index, (name, expected) in enumerate(files.items()):
                with archive.extractfile(by_name[name]) as stream:
                    if name in databases:
                        restored = Path(tmp) / f"restored-{index}.sqlite"
                        with restored.open("wb") as output:
                            actual = digest(stream, output)
                        sqlite_check(restored)
                        restored.unlink()
                    else:
                        actual = digest(stream)
                if actual != expected:
                    raise RuntimeError(f"SHA-256 mismatch: {name}")
    return len(files), len(databases)


def create_archive(destination, files, databases, runtime=None):
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = destination.stat()
    if destination.is_symlink() or (os.name == "posix" and (
        info.st_mode & 0o077 or info.st_uid != os.geteuid()
    )):
        raise RuntimeError("Backup directory must be private and not a symlink")
    now = datetime.now(timezone.utc)
    name = f"netwatch-{now:%Y%m%dT%H%M%S%fZ}-{secrets.token_hex(4)}.tar.gz"
    with tempfile.TemporaryDirectory(prefix=".create-", dir=destination) as tmp:
        work = Path(tmp)
        checksums = {}
        pending = work / name
        with tarfile.open(pending, "w:gz", compresslevel=3) as archive:
            for relative, source in {**files, **databases}.items():
                target = work / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative in databases:
                    sqlite_backup(source, target)
                else:
                    shutil.copy2(source, target)
                with target.open("rb") as stream:
                    checksums[relative] = digest(stream)
                archive.add(target, arcname=relative, recursive=False)
            manifest = work / "manifest.json"
            manifest.write_text(json.dumps({
                "version": 1, "created_utc": now.isoformat(), "files": checksums,
                "databases": sorted(databases), "runtime": runtime or {},
            }, indent=2), encoding="utf-8")
            archive.add(manifest, arcname="manifest.json", recursive=False)
        verify_archive(pending)
        with pending.open("r+b") as stream:
            os.fsync(stream.fileno())
        result = destination / name
        os.replace(pending, result)
        sync_directory(destination)
    return result


def safe_restore_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or str(path) != name or "\\" in name or ":" in name:
        raise RuntimeError("Unsafe archive path")
    allowed = (
        "opt/netwatch/", "var/www/netwatch/", "etc/caddy/",
        "etc/systemd/system/netwatch-",
    )
    return name.startswith(allowed) or name in (
        "etc/netwatch-backup.conf", "usr/lib/systemd/system/caddy.service",
        "lib/systemd/system/caddy.service", "usr/local/lib/netwatch/backup_netwatch.py",
    )


def prepare_restore(path, parent):
    required = {
        "opt/netwatch/api.py", "opt/netwatch/db.py", "opt/netwatch/netwatch_telegram_bot.py",
        "opt/netwatch/.env", "opt/netwatch/netwatch.db", "opt/netwatch/agent3.sh",
        "opt/netwatch/monitor/pi_monitor.py", "opt/netwatch/monitor/index.html",
        "opt/netwatch/monitor/style.css", "opt/netwatch/monitor/dashboard.js",
        "etc/caddy/Caddyfile", "var/www/netwatch/agent.sh",
    }
    verify_archive(path, scratch=parent)
    with tarfile.open(path, "r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        missing = required - manifest["files"].keys()
        if missing:
            raise RuntimeError("Missing recovery files: " + ", ".join(sorted(missing)))
        profile = manifest.get("runtime", {})
        packages = profile.get("packages", {})
        for package in ("aiohttp", "aiogram"):
            version = packages.get(package, "")
            if not re.fullmatch(r"[0-9][A-Za-z0-9.+!-]*", version):
                raise RuntimeError(f"Missing valid version of {package} in backup")
        if not re.fullmatch(r"3\.\d+", profile.get("python", "")):
            raise RuntimeError("Missing Python runtime version in backup")

        selected = [name for name in manifest["files"] if safe_restore_path(name)]
        stage = Path(tempfile.mkdtemp(prefix="netwatch-restore-", dir=parent))
        try:
            for name in selected:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(name) as source, target.open("xb") as output:
                    if digest(source, output) != manifest["files"][name]:
                        raise RuntimeError(f"SHA-256 mismatch during extraction: {name}")
                target.chmod(0o600)
            (stage / "runtime.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
            (stage / "requirements.txt").write_text(
                "".join(f"{name}=={packages[name]}\n" for name in ("aiohttp", "aiogram")),
                encoding="utf-8",
            )
        except BaseException:
            if stage.resolve().parent == parent.resolve():
                shutil.rmtree(stage)
            raise
    return stage, profile


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mount_uuid(mount):
    output = subprocess.run([
        "findmnt", "--mountpoint", str(mount), "--noheadings", "--output", "UUID",
    ], capture_output=True, text=True, timeout=5)
    return output.stdout.strip().lower() if output.returncode == 0 else ""


def copy_to_usb(archive, mount, uuid, *, cutoff=None):
    if mount_uuid(mount) != uuid.lower():
        return False
    fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if (mount_uuid(mount) != uuid.lower()
                or os.fstat(fd).st_dev != mount.stat().st_dev):
            return False
        try:
            os.mkdir("netwatch-backups", mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        target = os.open("netwatch-backups", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            temporary = f".partial-{secrets.token_hex(8)}"
            output = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=target)
            try:
                with os.fdopen(output, "w+b") as stream, archive.open("rb") as source:
                    expected = digest(source, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.seek(0)
                    if digest(stream) != expected:
                        raise RuntimeError("USB archive SHA-256 mismatch")
                os.replace(temporary, archive.name, src_dir_fd=target, dst_dir_fd=target)
                os.fsync(target)
                if cutoff is not None:
                    for name in os.listdir(target):
                        if expired_archive(name, cutoff) and stat.S_ISREG(
                            os.stat(name, dir_fd=target, follow_symlinks=False).st_mode
                        ):
                            os.unlink(name, dir_fd=target)
                    os.fsync(target)
            finally:
                try:
                    os.unlink(temporary, dir_fd=target)
                except FileNotFoundError:
                    pass
        finally:
            os.close(target)
    finally:
        os.close(fd)
    return True


def expired_archive(name, cutoff):
    if not ARCHIVE_NAME.fullmatch(name):
        return False
    timestamp = name.removeprefix("netwatch-").split("-", 1)[0]
    try:
        created = datetime.strptime(timestamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return created < cutoff


def prune(destination, keep, *, cutoff=None):
    if keep < 1 and cutoff is None:
        return
    archives = sorted(path for path in destination.iterdir()
                      if ARCHIVE_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink())
    for index, path in enumerate(archives):
        if (keep > 0 and index < len(archives) - keep) or (
            cutoff is not None and expired_archive(path.name, cutoff)
        ):
            path.unlink()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("create")
    verify = sub.add_parser("verify")
    verify.add_argument("archive", type=Path)
    prepare = sub.add_parser("prepare-restore")
    prepare.add_argument("archive", type=Path)
    prepare.add_argument("--stage-parent", type=Path, default=Path("/var/tmp"))
    args = parser.parse_args()
    if args.action == "verify":
        files, databases = verify_archive(args.archive.resolve())
        print(f"Проверены файлы: {files}. SQLite восстановлены во временный каталог и проверены: {databases}.")
        return 0
    if args.action == "prepare-restore":
        stage, profile = prepare_restore(args.archive.resolve(), args.stage_parent.resolve())
        print(f"Архив проверен и распакован: {stage}")
        print(f"Python: {profile['python']}. Пакеты: "
              + ", ".join(f"{name}=={version}" for name, version in profile["packages"].items()))
        return 0

    keep_days = int(os.getenv("NETWATCH_BACKUP_KEEP_DAYS", "0"))
    keep = int(os.getenv("NETWATCH_BACKUP_KEEP", "0"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days) if keep_days > 0 else None
    archive = create_archive(DESTINATION, live_files(), database_files(), runtime=runtime_profile())
    print(f"Локальный архив проверен: {archive}", flush=True)
    usb = [os.getenv(key, "") for key in ("NETWATCH_USB_MOUNT", "NETWATCH_USB_UUID")]
    if any(usb):
        if not all(usb) or not Path(usb[0]).is_absolute():
            raise RuntimeError("Incomplete USB backup configuration")
        if not copy_to_usb(archive, Path(usb[0]), usb[1], cutoff=cutoff):
            print("USB-копия пропущена: настроенный носитель не смонтирован.", file=sys.stderr)
            return 2
        print(f"Копия на USB проверена по SHA-256: {archive.name}")
    prune(DESTINATION, keep, cutoff=cutoff)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
