"""
NetWatch HTTP API для агентов (aiohttp)
Endpoints:
- POST /event — событие Wi-Fi connect/disconnect
- POST /devices — снимок устройств (cron)
- POST /health — health-check от роутера
- GET /agent.sh — отдача скрипта агента
"""
import os
import json
import time
import sqlite3
import secrets
import re
import asyncio
from pathlib import Path

# Загрузка переменных окружения из .env (если есть)
# Позволяет работать без systemd EnvironmentFile
ENV_PATH = Path("/opt/netwatch/.env")
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from aiohttp import web

from db import (
    get_pairing_code, consume_pairing_code, upsert_router,
    add_event, can_notify_device, mark_device_notified, init_db, db_session
)

DB_PATH = Path("/opt/netwatch/netwatch.db")
AGENT_SECRET = os.getenv("AGENT_SECRET", "changeme")
AGENT_SCRIPT_PATH = Path("/opt/netwatch/agent3.sh")
ROUTER_OFFLINE_AFTER = 7 * 60

# --- Telegram notification helper ---
def send_telegram_notification(chat_id, text):
    """Отправляет уведомление в Telegram"""
    import urllib.request
    import os as _os
    import logging
    logger = logging.getLogger(__name__)
    token = _os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — notification skipped")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:
        logger.error(f"Telegram notification failed: {e}")


# --- Middleware: проверка X-Agent-Secret ---
@web.middleware
async def auth_middleware(request, handler):
    # /agent.sh and /pair don't require authorization
    if request.path in ("/agent.sh", "/pair") and request.method in ("GET", "POST"):
        return await handler(request)

    secret = request.headers.get("X-Agent-Secret")
    if not secret:
        return web.json_response({"error": "Forbidden: invalid agent secret"}, status=403)
    with db_session() as conn:
        router = conn.execute(
            "SELECT router_id FROM router_secrets WHERE secret=?", (secret,)
        ).fetchone()
    if router is None:
        return web.json_response({"error": "Forbidden: invalid agent secret"}, status=403)
    if request.method == "POST" and request.path in ("/event", "/devices", "/health"):
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            return web.json_response({"error": "Bad JSON"}, status=400)
        if not isinstance(data, dict) or "router_id" not in data:
            return web.json_response({"error": "Missing router_id"}, status=400)
        if data["router_id"] != router["router_id"]:
            return web.json_response({"error": "Forbidden: wrong router"}, status=403)
    return await handler(request)


def record_router_report(conn, router_id, notifications):
    router = conn.execute("""
        SELECT r.name, r.chat_id, r.online, r.podkop_proxy_tag, r.podkop_proxy_sample_at,
               s.watch_podkop_proxy, s.watch_router_online
        FROM routers r LEFT JOIN user_settings s ON s.chat_id=r.chat_id
        WHERE r.router_id=?
    """, (router_id,)).fetchone()
    if router is not None:
        conn.execute("UPDATE routers SET last_seen=unixepoch(), online=1 WHERE router_id=?", (router_id,))
        if not router["online"]:
            add_event(router_id, None, "router_online", conn=conn)
            if router["watch_router_online"]:
                notifications.append((router["chat_id"], f"🟢 Роутер онлайн: {router['name'] or router_id}"))
    return router


def record_device_arrival(conn, router_id, chat_id, mac, previous):
    if previous is None:
        known = conn.execute("""
            SELECT 1 FROM devices d JOIN routers r ON r.router_id=d.router_id
            WHERE d.mac=? COLLATE NOCASE AND r.chat_id=?
            LIMIT 1
        """, (mac, chat_id)).fetchone()
        add_event(router_id, mac, "device_online" if known else "device_new", conn=conn)
    elif not previous["online"]:
        add_event(router_id, mac, "device_online", conn=conn)

# --- POST /event ---
async def post_event(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Bad JSON"}, status=400)

    required = ["router_id", "mac", "event"]
    if not all(k in data for k in required):
        return web.json_response({"error": "Missing fields: router_id, mac, event"}, status=400)

    notifications = []
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        mac = data["mac"].lower()
        event = data["event"]

        # Получаем chat_id пользователя этого роутера
        router = record_router_report(conn, data["router_id"], notifications)
        chat_id = router["chat_id"] if router else None

        # Обновляем состояние устройства
        now = int(time.time())
        event_upper = event.upper()
        # DISCONNECTED also contains CONNECTED; exclude it from this branch.
        if "CONNECTED" in event_upper and "DISCONNECTED" not in event_upper:
            # Проверяем предыдущий статус
            prev = conn.execute("SELECT online, watch_online, custom_name, hostname FROM devices WHERE mac=? AND router_id=?",
                                (mac, data["router_id"])).fetchone()
            was_online = prev["online"] if prev else 0
            watch_online = prev["watch_online"] if prev else 1
            record_device_arrival(conn, data["router_id"], chat_id, mac, prev)

            conn.execute("""
                INSERT INTO devices (mac, router_id, ip, hostname, custom_name, online, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(mac, router_id) DO UPDATE SET
                    online=1, last_seen=excluded.last_seen,
                    ip=excluded.ip, hostname=excluded.hostname,
                    custom_name=COALESCE(excluded.custom_name, devices.custom_name)
            """, (mac, data["router_id"], data.get("ip"), data.get("hostname"), data.get("custom_name"), now, now))

            # Уведомление о подключении, если был оффлайн (с дедупликацией)
            if chat_id and was_online == 0 and watch_online == 1:
                device_name = (data.get("custom_name") or (prev["custom_name"] if prev else None)
                               or data.get("hostname") or (prev["hostname"] if prev else None) or mac)
                if can_notify_device(chat_id, mac, data["router_id"], conn=conn, notification_state=1):
                    mark_device_notified(chat_id, mac, data["router_id"], conn=conn, notification_state=1)
                    label = "🆕 Новое устройство" if prev is None else "🟢 Устройство онлайн"
                    notifications.append((chat_id, f"{label}: `{mac}` — {device_name}"))

        elif "DISCONNECTED" in event_upper:
            # Проверяем предыдущий статус
            prev = conn.execute("SELECT online, watch_online FROM devices WHERE mac=? AND router_id=?",
                                (mac, data["router_id"])).fetchone()
            was_online = prev["online"] if prev else 0
            watch_online = prev["watch_online"] if prev else 1

            conn.execute("UPDATE devices SET online=0, last_seen=? WHERE mac=? AND router_id=?",
                         (now, mac, data["router_id"]))
            if was_online == 1:
                add_event(data["router_id"], mac, "device_offline", conn=conn)

            # Уведомление об отключении, если был онлайн и watch_online включен (с дедупликацией)
            if chat_id and was_online == 1 and watch_online == 1:
                device_name = conn.execute("SELECT custom_name, hostname FROM devices WHERE mac=? AND router_id=?",
                                           (mac, data["router_id"])).fetchone()
                name = (device_name["custom_name"] or device_name["hostname"] or mac) if device_name else mac
                if can_notify_device(chat_id, mac, data["router_id"], conn=conn, notification_state=0):
                    mark_device_notified(chat_id, mac, data["router_id"], conn=conn, notification_state=0)
                    notifications.append((chat_id, f"🔴 Устройство оффлайн: `{mac}` — {name}"))

    for chat_id, text in notifications:
        send_telegram_notification(chat_id, text)

    return web.json_response({"status": "ok", "saved": True})

# --- POST /devices ---
async def post_devices(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Bad JSON"}, status=400)

    if "router_id" not in data or "devices" not in data:
        return web.json_response({"error": "Missing router_id or devices"}, status=400)

    notifications = []
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        router = record_router_report(conn, data["router_id"], notifications)
        chat_id = router["chat_id"] if router else None

        saved = 0
        new_devices = []

        for dev in data["devices"]:
            mac = dev.get("mac", "").lower()
            if not mac:
                continue

            # Check if device is new
            prev = conn.execute("SELECT online, watch_online, custom_name, hostname FROM devices WHERE mac=? AND router_id=?", (mac, data["router_id"])).fetchone()
            is_new = prev is None
            record_device_arrival(conn, data["router_id"], chat_id, mac, prev)

            conn.execute("""
                INSERT INTO devices (mac, router_id, ip, hostname, custom_name, online, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(mac, router_id) DO UPDATE SET
                    ip=excluded.ip,
                    hostname=excluded.hostname,
                    custom_name=COALESCE(excluded.custom_name, devices.custom_name),
                    online=1,
                    last_seen=excluded.last_seen
            """, (mac, data["router_id"], dev.get("ip"), dev.get("hostname"), dev.get("custom_name"), now, now))
            saved += 1

            if is_new and chat_id:
                device_name = dev.get("custom_name") or dev.get("hostname") or mac
                if can_notify_device(chat_id, mac, data["router_id"], conn=conn, notification_state=1):
                    mark_device_notified(chat_id, mac, data["router_id"], conn=conn, notification_state=1)
                    notifications.append((chat_id, f"🆕 Новое устройство: `{mac}` — {device_name}"))
                new_devices.append(mac)
            elif chat_id and prev["online"] == 0 and prev["watch_online"] == 1:
                device_name = (dev.get("custom_name") or prev["custom_name"]
                               or dev.get("hostname") or prev["hostname"] or mac)
                if can_notify_device(chat_id, mac, data["router_id"], conn=conn, notification_state=1):
                    mark_device_notified(chat_id, mac, data["router_id"], conn=conn, notification_state=1)
                    notifications.append((chat_id, f"🟢 Устройство онлайн: `{mac}` — {device_name}"))

    for chat_id, text in notifications:
        send_telegram_notification(chat_id, text)

    return web.json_response({"status": "ok", "saved_devices": saved, "new_devices": new_devices})

# --- POST /health ---
async def post_health(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Bad JSON"}, status=400)

    required = ["router_id", "internet", "wifi", "cpu", "ram", "wifi_clients", "podkop_status", "podkop_test"]
    if not all(k in data for k in required):
        return web.json_response({"error": "Missing health fields"}, status=400)

    latency = data.get("podkop_latency_ms")
    if latency is not None and (type(latency) is not int or not 0 <= latency <= 60000):
        return web.json_response({"error": "Invalid proxy latency"}, status=400)

    notifications = []
    with db_session() as conn:
        timestamp = data.get("timestamp", int(time.time()))
        conn.execute("""
            INSERT INTO health_checks (router_id, internet, wifi, cpu, ram, wifi_clients, podkop_status, podkop_test, timestamp, podkop_latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (data["router_id"],
              1 if data["internet"] else 0,
              1 if data["wifi"] else 0,
              data["cpu"], data["ram"], data["wifi_clients"],
              data["podkop_status"],
              None if data["podkop_test"] is None else (1 if data["podkop_test"] else 0),
              timestamp, latency))

        router_id = data["router_id"]
        router = record_router_report(conn, router_id, notifications)
        proxy_tag = data.get("podkop_proxy_tag")
        if (router and data["podkop_status"] == "running"
                and isinstance(proxy_tag, str) and 0 < len(proxy_tag) <= 200 and proxy_tag.isprintable()
                and type(timestamp) is int and timestamp > 0
                and (router["podkop_proxy_sample_at"] is None or timestamp > router["podkop_proxy_sample_at"])):
            previous = router["podkop_proxy_tag"]
            conn.execute("""
                UPDATE routers SET podkop_proxy_tag=?, podkop_proxy_sample_at=? WHERE router_id=?
            """, (proxy_tag, timestamp, router_id))
            if previous and previous != proxy_tag and router["watch_podkop_proxy"]:
                name = router["name"] or router_id
                notifications.append((router["chat_id"], f"🔀 Podkop сменил прокси на {name}\n{previous} → {proxy_tag}"))

    for chat_id, text in notifications:
        send_telegram_notification(chat_id, text)

    return web.json_response({"status": "ok", "saved": True})

# --- GET /agent.sh ---
async def get_agent_script(request):
    if not AGENT_SCRIPT_PATH.exists():
        return web.Response(text="# Agent script not found", status=404)
    return web.Response(text=AGENT_SCRIPT_PATH.read_text(), content_type="application/x-sh")

async def get_agent_identity(request):
    """Identify this secret without exposing another user's routers or secrets."""
    secret = request.headers.get("X-Agent-Secret")
    with db_session() as conn:
        row = conn.execute("SELECT router_id FROM router_secrets WHERE secret=?", (secret,)).fetchone()
    if row is None:
        return web.json_response({"error": "Invalid agent secret"}, status=403)
    return web.json_response({"router_id": row["router_id"]})


# --- POST /pair (регистрация роутера по pairing коду) ---
async def post_pair(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Bad JSON"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"error": "Expected JSON object"}, status=400)

    code = data.get("code")
    router_id = data.get("router_id")
    mac = data.get("mac")
    name = data.get("name", "OpenWrt Router")
    model = data.get("model", "unknown")

    if (not isinstance(code, str) or not 0 < len(code) <= 128
            or not isinstance(router_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", router_id)):
        return web.json_response({"error": "Invalid code or router_id"}, status=400)
    if any(value is not None and (not isinstance(value, str) or len(value) > 255)
           for value in (name, model)):
        return web.json_response({"error": "Invalid router metadata"}, status=400)
    if mac == "unknown":
        mac = None
    if mac is not None:
        if not isinstance(mac, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
            return web.json_response({"error": "Invalid router MAC"}, status=400)
        mac = mac.lower()

    # One writer claims the code, resolves the router and stores its secret.
    # A failed commit must not consume the code or leave a partial registration.
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        pc = conn.execute("""SELECT chat_id FROM pairing_codes
            WHERE code=? AND used=0 AND expires_at>?""", (code, int(time.time()))).fetchone()
        if pc is None:
            return web.json_response({"error": "Invalid or expired pairing code"}, status=403)
        chat_id = pc["chat_id"]
        owner = conn.execute("SELECT chat_id FROM routers WHERE router_id=?", (router_id,)).fetchone()
        if owner is not None and owner["chat_id"] != chat_id:
            return web.json_response({"error": "Router belongs to another user"}, status=409)

        router_id = upsert_router(router_id, chat_id, name=name, mac=mac, model=model, conn=conn)
        router_secret = secrets.token_hex(16)
        conn.execute("""
            INSERT OR REPLACE INTO router_secrets (router_id, secret) VALUES (?, ?)
        """, (router_id, router_secret))
        conn.execute("UPDATE pairing_codes SET used=1, router_id=? WHERE code=?", (router_id, code))

    return web.json_response({
        "status": "paired",
        "router_id": router_id,
        "agent_secret": router_secret,
        "api_url": "http://" + request.host
    })

# --- Background task: check for offline devices ---
async def check_offline_devices(app):
    """Periodically check for devices with stale last_seen and mark them offline"""
    # Ensure device_notifications table exists (init_db might have failed silently)
    with db_session() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_notifications (
                chat_id INTEGER,
                mac TEXT,
                router_id TEXT,
                last_notified_at INTEGER DEFAULT (unixepoch()),
                PRIMARY KEY (chat_id, mac, router_id),
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE,
                FOREIGN KEY (mac, router_id) REFERENCES devices(mac, router_id) ON DELETE CASCADE
            )
        """)

    while True:
        try:
            notifications = []
            with db_session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                now = int(time.time())
                threshold = now - 120  # 2 minutes
                stale_routers = conn.execute("""
                    UPDATE routers SET online=0
                    WHERE online=1 AND last_seen < ?
                    RETURNING router_id, name, chat_id
                """, (now - ROUTER_OFFLINE_AFTER,)).fetchall()
                for router in stale_routers:
                    add_event(router["router_id"], None, "router_offline", timestamp=now, conn=conn)
                    setting = conn.execute("""
                        SELECT watch_router_online FROM user_settings WHERE chat_id=?
                    """, (router["chat_id"],)).fetchone()
                    if setting and setting["watch_router_online"]:
                        name = router["name"] or router["router_id"]
                        notifications.append((router["chat_id"], f"🔴 Роутер оффлайн: {name}"))

                # Update status for every stale device; watch_online controls messages only.
                stale_devices = conn.execute("""
                    SELECT d.mac, d.router_id, d.custom_name, d.hostname, d.watch_online, r.chat_id
                    FROM devices d
                    JOIN routers r ON d.router_id = r.router_id
                    WHERE d.online=1 AND d.last_seen < ?
                """, (threshold,)).fetchall()

                for dev in stale_devices:
                    mac = dev["mac"]
                    router_id = dev["router_id"]
                    chat_id = dev["chat_id"]

                    # Mark as offline
                    conn.execute("UPDATE devices SET online=0 WHERE mac=? AND router_id=?", (mac, router_id))
                    add_event(router_id, mac, "device_offline", timestamp=now, conn=conn)

                    # Send notification if not recently notified
                    if (chat_id and dev["watch_online"] == 1
                            and can_notify_device(chat_id, mac, router_id, conn=conn, notification_state=0)):
                        mark_device_notified(chat_id, mac, router_id, conn=conn, notification_state=0)
                        device_name = dev["custom_name"] or dev["hostname"] or mac
                        notifications.append((chat_id, f"🔴 Устройство оффлайн (таймаут): `{mac}` — {device_name}"))

            for chat_id, text in notifications:
                send_telegram_notification(chat_id, text)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Offline check error: {e}")

        await asyncio.sleep(60)  # Check every minute


async def start_offline_check(app):
    """Start background offline checker on app startup"""
    asyncio.create_task(check_offline_devices(app))


# --- App factory ---
def create_app():
    # Инициализация БД (создание таблиц, миграции)
    init_db()

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_post("/event", post_event)
    app.router.add_post("/devices", post_devices)
    app.router.add_post("/health", post_health)
    app.router.add_get("/agent.sh", get_agent_script)
    app.router.add_get("/agent/identity", get_agent_identity)
    app.router.add_post("/pair", post_pair)

    # Start background task for offline device checking (after event loop starts)
    app.on_startup.append(start_offline_check)

    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8080)
