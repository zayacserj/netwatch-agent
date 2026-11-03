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
    get_pairing_code, consume_pairing_code, upsert_router, get_conn,
    is_valid_router_secret, get_user_by_router_id,
    can_notify_device, mark_device_notified, init_db
)

DB_PATH = Path("/opt/netwatch/netwatch.db")
AGENT_SECRET = os.getenv("AGENT_SECRET", "changeme")
AGENT_SCRIPT_PATH = Path("/opt/netwatch/agent3.sh")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.error(f"Telegram notification failed: {e}")


# --- Middleware: проверка X-Agent-Secret ---
@web.middleware
async def auth_middleware(request, handler):
    # /agent.sh and /pair don't require authorization
    if request.path in ("/agent.sh", "/pair") and request.method in ("GET", "POST"):
        return await handler(request)
    
    secret = request.headers.get("X-Agent-Secret")
    if not secret or not is_valid_router_secret(secret):
        return web.json_response({"error": "Forbidden: invalid agent secret"}, status=403)
    return await handler(request)

# --- POST /event ---
async def post_event(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Bad JSON"}, status=400)
    
    required = ["router_id", "mac", "event"]
    if not all(k in data for k in required):
        return web.json_response({"error": "Missing fields: router_id, mac, event"}, status=400)
    
    conn = get_conn()
    mac = data["mac"].lower()
    event = data["event"]
    timestamp = data.get("timestamp", int(time.time()))
    
    conn.execute("INSERT INTO events (router_id, mac, event, timestamp) VALUES (?, ?, ?, ?)",
                 (data["router_id"], mac, event, timestamp))
    
    # Получаем chat_id пользователя этого роутера
    chat_id = get_user_by_router_id(data["router_id"])
    
    # Обновляем состояние устройства
    now = int(time.time())
    if "CONNECTED" in event.upper():
        # Проверяем предыдущий статус
        prev = conn.execute("SELECT online FROM devices WHERE mac=? AND router_id=?",
                            (mac, data["router_id"])).fetchone()
        was_online = prev["online"] if prev else 0

        conn.execute("""
            INSERT INTO devices (mac, router_id, ip, hostname, custom_name, online, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(mac, router_id) DO UPDATE SET
                online=1, last_seen=excluded.last_seen,
                ip=excluded.ip, hostname=excluded.hostname, custom_name=excluded.custom_name
        """, (mac, data["router_id"], data.get("ip"), data.get("hostname"), data.get("custom_name"), now, now))

        # Уведомление о подключении, если был оффлайн (с дедупликацией)
        if chat_id and was_online == 0:
            device_name = data.get("custom_name") or data.get("hostname") or mac
            if can_notify_device(chat_id, mac, data["router_id"]):
                mark_device_notified(chat_id, mac, data["router_id"])
                send_telegram_notification(chat_id, f"🟢 Устройство онлайн: `{mac}` — {device_name}")

    elif "DISCONNECTED" in event.upper():
        # Проверяем предыдущий статус
        prev = conn.execute("SELECT online, watch_online FROM devices WHERE mac=? AND router_id=?",
                            (mac, data["router_id"])).fetchone()
        was_online = prev["online"] if prev else 0
        watch_online = prev["watch_online"] if prev else 1

        conn.execute("UPDATE devices SET online=0, last_seen=? WHERE mac=? AND router_id=?",
                     (now, mac, data["router_id"]))

        # Уведомление об отключении, если был онлайн и watch_online включен (с дедупликацией)
        if chat_id and was_online == 1 and watch_online == 1:
            device_name = conn.execute("SELECT custom_name, hostname FROM devices WHERE mac=? AND router_id=?",
                                       (mac, data["router_id"])).fetchone()
            name = (device_name["custom_name"] or device_name["hostname"] or mac) if device_name else mac
            if can_notify_device(chat_id, mac, data["router_id"]):
                mark_device_notified(chat_id, mac, data["router_id"])
                send_telegram_notification(chat_id, f"🔴 Устройство оффлайн: `{mac}` — {name}")
    
    conn.commit()
    conn.close()
    
    return web.json_response({"status": "ok", "saved": True})

# --- POST /devices ---
async def post_devices(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Bad JSON"}, status=400)
    
    if "router_id" not in data or "devices" not in data:
        return web.json_response({"error": "Missing router_id or devices"}, status=400)
    
    conn = get_conn()
    now = int(time.time())
    chat_id = get_user_by_router_id(data["router_id"])
    
    saved = 0
    new_devices = []
    
    for dev in data["devices"]:
        mac = dev.get("mac", "").lower()
        if not mac:
            continue
        
        # Check if device is new
        prev = conn.execute("SELECT mac FROM devices WHERE mac=? AND router_id=?", (mac, data["router_id"])).fetchone()
        is_new = prev is None
        
        conn.execute("""
            INSERT INTO devices (mac, router_id, ip, hostname, custom_name, online, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(mac, router_id) DO UPDATE SET
                ip=excluded.ip,
                hostname=excluded.hostname,
                custom_name=excluded.custom_name,
                online=1,
                last_seen=excluded.last_seen
        """, (mac, data["router_id"], dev.get("ip"), dev.get("hostname"), dev.get("custom_name"), now, now))
        saved += 1
        
        if is_new and chat_id:
            device_name = dev.get("custom_name") or dev.get("hostname") or mac
            if can_notify_device(chat_id, mac, data["router_id"]):
                mark_device_notified(chat_id, mac, data["router_id"])
                send_telegram_notification(chat_id, f"🆕 Новое устройство: `{mac}` — {device_name}")
            new_devices.append(mac)
    
    conn.commit()
    conn.close()
    
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
    
    conn = get_conn()
    timestamp = data.get("timestamp", int(time.time()))
    conn.execute("""
        INSERT INTO health_checks (router_id, internet, wifi, cpu, ram, wifi_clients, podkop_status, podkop_test, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (data["router_id"], 
          1 if data["internet"] else 0,
          1 if data["wifi"] else 0,
          data["cpu"], data["ram"], data["wifi_clients"],
          data["podkop_status"], 1 if data["podkop_test"] else 0,
          timestamp))
    conn.commit()
    conn.close()
    
    # Обновляем last_seen роутера
    conn = get_conn()
    conn.execute("UPDATE routers SET last_seen=unixepoch(), online=1 WHERE router_id=?", (data["router_id"],))
    conn.commit()
    conn.close()
    
    return web.json_response({"status": "ok", "saved": True})

# --- GET /agent.sh ---
async def get_agent_script(request):
    if not AGENT_SCRIPT_PATH.exists():
        return web.Response(text="# Agent script not found", status=404)
    return web.Response(text=AGENT_SCRIPT_PATH.read_text(), content_type="application/x-sh")

# --- POST /pair (регистрация роутера по pairing коду) ---
async def post_pair(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Bad JSON"}, status=400)

    code = data.get("code")
    router_id = data.get("router_id")
    mac = data.get("mac", "unknown")
    name = data.get("name", "OpenWrt Router")
    model = data.get("model", "unknown")

    if not code or not router_id:
        return web.json_response({"error": "Missing fields: code, router_id"}, status=400)

    pc = get_pairing_code(code)
    if not pc:
        return web.json_response({"error": "Invalid or expired pairing code"}, status=403)

    chat_id = pc["chat_id"]
    consume_pairing_code(code, router_id)

    # Регистрируем роутер (обновляем router_id на случай если MAC уже существует)
    router_id = upsert_router(router_id, chat_id, name=name, mac=mac, model=model)

    # Генерируем уникальный секрет для этого роутера
    router_secret = secrets.token_hex(16)

    # Сохраняем секрет в конфиге (для последующих запросов)
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS router_secrets (
            router_id TEXT PRIMARY KEY,
            secret TEXT NOT NULL,
            FOREIGN KEY (router_id) REFERENCES routers(router_id)
        )
    """)
    conn.execute("""
        INSERT OR REPLACE INTO router_secrets (router_id, secret) VALUES (?, ?)
    """, (router_id, router_secret))
    conn.commit()
    conn.close()

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
    conn = get_conn()
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
    conn.commit()
    conn.close()
    
    while True:
        try:
            conn = get_conn()
            now = int(time.time())
            threshold = now - 120  # 2 minutes
            
            # Find devices that are still marked online but haven't been seen in 2 minutes
            stale_devices = conn.execute("""
                SELECT d.mac, d.router_id, d.custom_name, d.hostname, r.chat_id
                FROM devices d
                JOIN routers r ON d.router_id = r.router_id
                WHERE d.online=1 AND d.last_seen < ? AND d.watch_online=1
            """, (threshold,)).fetchall()
            
            for dev in stale_devices:
                mac = dev["mac"]
                router_id = dev["router_id"]
                chat_id = dev["chat_id"]
                
                # Mark as offline
                conn.execute("UPDATE devices SET online=0 WHERE mac=? AND router_id=?", (mac, router_id))
                
                # Send notification if not recently notified
                if chat_id and can_notify_device(chat_id, mac, router_id):
                    mark_device_notified(chat_id, mac, router_id)
                    device_name = dev["custom_name"] or dev["hostname"] or mac
                    send_telegram_notification(chat_id, f"🔴 Устройство оффлайн (таймаут): `{mac}` — {device_name}")
            
            conn.commit()
            conn.close()
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
    app.router.add_post("/pair", post_pair)
    
    # Start background task for offline device checking (after event loop starts)
    app.on_startup.append(start_offline_check)
    
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8080)
