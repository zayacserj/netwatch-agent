"""Read-only Raspberry Pi metrics and dashboard, served on loopback."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time


ASSETS = Path(__file__).parent
CORE_SERVICES = ("caddy.service", "netwatch-api.service", "netwatch-bot.service")
SAMPLE_SECONDS = 5
SERVICE_SECONDS = 30


def cpu_counters():
    fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:9]
    counters = [int(field) for field in fields]
    return sum(counters), counters[3] + counters[4]


def cpu_percent(before, after):
    if before is None:
        return None
    total, idle = (after[i] - before[i] for i in range(2))
    if total <= 0 or not 0 <= idle <= total:
        return None
    return round(100 * (total - idle) / total, 1)


def memory():
    counters = {
        key: int(value.split()[0]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        for key, _, value in (line.partition(":"),)
        if key in ("MemTotal", "MemAvailable")
    }
    total = counters["MemTotal"]
    available = counters["MemAvailable"]
    return {"total_bytes": total, "available_bytes": available, "used_bytes": total - available}


def disk(path):
    try:
        stat = os.statvfs(path)
    except OSError:
        return {"mount": path, "available": False}
    return {
        "mount": path, "available": True,
        "total_bytes": stat.f_blocks * stat.f_frsize,
        "used_bytes": (stat.f_blocks - stat.f_bfree) * stat.f_frsize,
        "available_bytes": stat.f_bavail * stat.f_frsize,
    }


def usb_block_devices():
    devices = {}
    for entry in Path("/sys/class/block").glob("*"):
        try:
            node = entry.resolve(strict=True)
            if not any((parent / "subsystem").resolve() == Path("/sys/bus/usb")
                       for parent in node.parents):
                continue
            drive = node.parent.name if (node / "partition").exists() else node.name
            devices[node.name] = (drive, (node / "dev").read_text().strip())
        except OSError:
            continue
    return devices


def storage(mounts):
    result = [disk(path) for path in mounts]
    devices = usb_block_devices()
    if not devices:
        return result
    drives = {drive for drive, _ in devices.values()}
    by_number = {number: name for name, (_, number) in devices.items()}
    seen = set()
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        left, _, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if len(fields) < 6 or len(filesystem) < 2 or fields[3] != "/":
            continue
        name = by_number.get(fields[2])
        if name is None and filesystem[1].startswith("/dev/"):
            name = Path(filesystem[1]).resolve().name
        if name not in devices or name in seen:
            continue
        path = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        try:
            device = os.stat(path).st_dev
            if fields[2] != f"{os.major(device)}:{os.minor(device)}":
                continue
            row = disk(path)
            if os.stat(path).st_dev != device:
                continue
        except OSError:
            continue
        seen.add(name)
        drives.discard(devices[name][0])
        if path not in mounts:
            result.append({**row, "label": f"USB · {name} · {path}"})
    for drive in sorted(drives):
        result.append({"mount": None, "available": False, "label": f"USB · {drive}",
                       "mounted": False})
    return result


def cpu_temperature():
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            kind = (zone / "type").read_text().strip().lower()
            if "cpu" in kind or kind.startswith(("bcm2835", "bcm2711", "bcm2712")):
                return round(int((zone / "temp").read_text()) / 1000, 1)
        except (OSError, ValueError):
            continue
    return None


def network_counters():
    counters = {}
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, _, fields = line.partition(":")
        values = fields.split()
        if name.strip() != "lo" and len(values) >= 16:
            counters[name.strip()] = (int(values[0]), int(values[8]))
    return counters


def network_rates(before, after, elapsed):
    result = []
    for name, counters in sorted(after.items()):
        previous = before.get(name)
        rates = [None, None]
        if previous is not None and elapsed > 0:
            rates = [
                round((new - old) / elapsed) if new >= old else None
                for old, new in zip(previous, counters)
            ]
        result.append({"interface": name, "rx_bytes_per_s": rates[0], "tx_bytes_per_s": rates[1]})
    return result


def systemctl(*args):
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", *args], capture_output=True, text=True,
            timeout=2, env={**os.environ, "LC_ALL": "C"},
        )
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def services():
    output = systemctl("show", "--no-pager", "-p", "Id", "-p", "ActiveState", *CORE_SERVICES)
    states = {}
    for block in (output or "").split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in fields:
            states[fields["Id"]] = fields.get("ActiveState", "unknown")

    running = systemctl("list-units", "--type=service", "--state=running",
                        "--no-pager", "--no-legend", "--plain")
    return {
        "core": [{"name": unit, "state": states.get(unit, "unknown")} for unit in CORE_SERVICES],
        "running": None if running is None else [
            line.split()[0] for line in running.splitlines() if line.split()
        ],
    }


def containers():
    root = Path("/sys/fs/cgroup")
    groups = []
    for pattern, runtime in (
        ("system.slice/docker-*.scope", "Docker"),
        ("docker/*", "Docker"),
        ("system.slice/libpod-*.scope", "Podman"),
        ("user.slice/user-*.slice/user@*.service/app.slice/libpod-*.scope", "Podman"),
    ):
        for path in root.glob(pattern):
            ident = re.search(r"[a-f0-9]{12,64}", path.name)
            if ident is None:
                continue
            try:
                if "populated 1" in (path / "cgroup.events").read_text().splitlines():
                    groups.append((runtime, ident.group()[:12]))
            except OSError:
                continue
    return [{"runtime": runtime, "id": ident} for runtime, ident in sorted(set(groups))]


class Collector:
    def __init__(self, mounts):
        self.mounts = mounts
        self.payload = None
        self.stop = threading.Event()

    def collect(self):
        previous_cpu = None
        previous_network = {}
        previous_time = 0
        next_services = 0
        slow = {}
        while not self.stop.is_set():
            try:
                now = time.monotonic()
                cpu, network = cpu_counters(), network_counters()
                if now >= next_services:
                    slow = {"services": services(), "containers": containers()}
                    next_services = now + SERVICE_SECONDS
                snapshot = {
                    "sampled_at": int(time.time()),
                    "cpu_percent": cpu_percent(previous_cpu, cpu),
                    "temperature_c": cpu_temperature(),
                    "uptime_seconds": int(float(Path("/proc/uptime").read_text().split()[0])),
                    "memory": memory(),
                    "storage": storage(self.mounts),
                    "network": network_rates(previous_network, network, now - previous_time),
                    **slow,
                }
                self.payload = json.dumps(snapshot, separators=(",", ":")).encode()
                previous_cpu, previous_network, previous_time = cpu, network, now
            except (OSError, ValueError, IndexError, KeyError):
                self.payload = None
            self.stop.wait(SAMPLE_SECONDS)


def make_handler(collector):
    pages = {
        "/monitor/": ("text/html; charset=utf-8", "index.html"),
        "/monitor/style.css": ("text/css; charset=utf-8", "style.css"),
        "/monitor/dashboard.js": ("text/javascript; charset=utf-8", "dashboard.js"),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.partition("?")[0]
            if path == "/monitor":
                self.send_response(308)
                self.send_header("Location", "/monitor/")
                self.end_headers()
                return
            if path == "/monitor/api/v1/metrics":
                payload = collector.payload
                self.respond(200 if payload else 503, "application/json", payload or b'{"error":"unavailable"}')
            elif path in pages:
                content_type, filename = pages[path]
                self.respond(200, content_type, (ASSETS / filename).read_bytes())
            else:
                self.respond(404, "text/plain", b"Not found")

        def respond(self, status, content_type, body):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--mount", action="append", default=[], help="additional mount point to report")
    args = parser.parse_args()
    collector = Collector(list(dict.fromkeys(["/", *args.mount])))
    with HTTPServer(("127.0.0.1", args.port), make_handler(collector)) as server:
        threading.Thread(target=collector.collect, daemon=True).start()
        try:
            server.serve_forever()
        finally:
            collector.stop.set()


if __name__ == "__main__":
    main()
