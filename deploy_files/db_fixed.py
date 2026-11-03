"""
NetWatch database layer (sqlite3)
"""
import sqlite3
import os
import time
from pathlib import Path

DB_PATH = Path(os.getenv("NETWATCH_DB_PATH", "/opt/netwatch/netwatch.db"))


def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT (unixepoch())
    );

    CREATE TABLE IF NOT EXISTS routers (
        router_id TEXT PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        name TEXT,
        mac TEXT,
        model TEXT,
        online INTEGER DEFAULT 0,
        last_seen INTEGER DEFAULT (unixepoch()),
        created_at INTEGER DEFAULT (unixepoch()),
        FOREIGN KEY (chat_id) REFERENCES users(chat_id)
    );

    CREATE TABLE IF NOT EXISTS devices (
        mac TEXT,
        router_id TEXT,
        ip TEXT,
        hostname TEXT,
        online INTEGER DEFAULT 0,
        first_seen INTEGER DEFAULT (unixepoch()),
        last_seen INTEGER DEFAULT (unixepoch()),
        PRIMARY KEY (mac, router_id),
        FOREIGN KEY (router_id) REFERENCES routers(router_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        router_id TEXT,
        mac TEXT,
        event TEXT,
        timestamp INTEGER DEFAULT (unixepoch()),
        FOREIGN KEY (router_id) REFERENCES routers(router_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS health_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        router_id TEXT,
        internet INTEGER,
        wifi INTEGER,
        cpu TEXT,
        ram TEXT,
        wifi_clients INTEGER,
        podkop_status TEXT,
        podkop_test INTEGER,
        timestamp INTEGER DEFAULT (unixepoch()),
        FOREIGN KEY (router_id) REFERENCES routers(router_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS pairing_codes (
        code TEXT PRIMARY KEY,
        chat_id INTEGER,
        router_id TEXT,
        expires_at INTEGER,
        used INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT (unixepoch())
    );

    CREATE TABLE IF NOT EXISTS router_secrets (
        router_id TEXT PRIMARY KEY,
        secret TEXT NOT NULL,
        FOREIGN KEY (router_id) REFERENCES routers(router_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS user_settings (
        chat_id INTEGER PRIMARY KEY,
        watch_online INTEGER DEFAULT 1,
        FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS device_notifications (
        chat_id INTEGER,
        mac TEXT,
        router_id TEXT,
        last_notified_at INTEGER DEFAULT (unixepoch()),
        PRIMARY KEY (chat_id, mac, router_id),
        FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE,
        FOREIGN KEY (mac, router_id) REFERENCES devices(mac, router_id) ON DELETE CASCADE
    );
    """)
    conn.commit()
    conn.close()

    # Миграция: добавить новые колонки если таблица уже существует
    conn = get_conn()
    columns = [col["name"] for col in conn.execute("PRAGMA table_info(devices)").fetchall()]
    if "custom_name" not in columns:
        conn.execute("ALTER TABLE devices ADD COLUMN custom_name TEXT")
    if "watch_online" not in columns:
        conn.execute("ALTER TABLE devices ADD COLUMN watch_online INTEGER DEFAULT 1")
    # Проверяем наличие таблицы device_notifications
    tables = [row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "device_notifications" not in tables:
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


# --- users ---

def upsert_user(chat_id, username, first_name):
    conn = get_conn()
    conn.execute("""
        INSERT INTO users (chat_id, username, first_name) VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
    """, (chat_id, username, first_name))
    conn.commit()
    conn.close()


def is_admin(chat_id):
    import os as _os
    admin_ids = [int(x) for x in _os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    if chat_id in admin_ids:
        return True
    conn = get_conn()
    row = conn.execute("SELECT is_admin FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return bool(row and row["is_admin"])


def set_admin(chat_id, value=True):
    conn = get_conn()
    conn.execute("UPDATE users SET is_admin=? WHERE chat_id=?", (1 if value else 0, chat_id))
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_conn()
    rows = conn.execute("SELECT chat_id, username, first_name, is_admin FROM users ORDER BY chat_id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_user(chat_id):
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def clean_user_data(chat_id):
    conn = get_conn()
    routers = conn.execute("SELECT router_id FROM routers WHERE chat_id=?", (chat_id,)).fetchall()
    for r in routers:
        conn.execute("DELETE FROM routers WHERE router_id=?", (r["router_id"],))
    conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


# --- routers ---

def upsert_router(router_id, chat_id, name=None, mac=None, model=None):
    conn = get_conn()
    
    # Если есть MAC — проверить, есть ли уже роутер с таким MAC
    if mac:
        existing = conn.execute(
            "SELECT router_id FROM routers WHERE mac=? AND chat_id=?", 
            (mac.lower(), chat_id)
        ).fetchone()
        if existing:
            router_id = existing["router_id"]
            # Обновить имя/модель
            conn.execute("""
                UPDATE routers SET 
                    name=COALESCE(?, name),
                    model=COALESCE(?, model),
                    last_seen=unixepoch()
                WHERE router_id=?
            """, (name, model, router_id))
            conn.commit()
            conn.close()
            return router_id
    
    # Иначе создать новый
    conn.execute("""
        INSERT INTO routers (router_id, chat_id, name, mac, model, online, last_seen)
        VALUES (?, ?, ?, ?, ?, 0, unixepoch())
        ON CONFLICT(router_id) DO UPDATE SET
            name=COALESCE(excluded.name, routers.name),
            mac=COALESCE(excluded.mac, routers.mac),
            model=COALESCE(excluded.model, routers.model)
    """, (router_id, chat_id, name, mac, model))
    conn.commit()
    conn.close()
    return router_id


def get_user_routers(chat_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM routers WHERE chat_id=? ORDER BY created_at DESC", (chat_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_routers():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM routers ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_router_online(router_id, online=True):
    conn = get_conn()
    conn.execute("UPDATE routers SET online=?, last_seen=unixepoch() WHERE router_id=?", (1 if online else 0, router_id))
    conn.commit()
    conn.close()


def get_user_by_router_id(router_id):
    """Возвращает chat_id пользователя, которому принадлежит роутер"""
    conn = get_conn()
    row = conn.execute("SELECT chat_id FROM routers WHERE router_id=?", (router_id,)).fetchone()
    conn.close()
    return row["chat_id"] if row else None


# --- devices ---

def get_user_devices(chat_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT d.*, r.name AS router_name, r.router_id
        FROM devices d
        JOIN routers r ON d.router_id = r.router_id
        WHERE r.chat_id=?
        ORDER BY d.last_seen DESC
    """, (chat_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_devices_count(chat_id, router_id):
    conn = get_conn()
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM devices d
        JOIN routers r ON d.router_id = r.router_id
        WHERE r.chat_id=? AND d.router_id=?
    """, (chat_id, router_id)).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def set_device_online(mac, router_id, online=True):
    conn = get_conn()
    conn.execute("UPDATE devices SET online=? WHERE mac=? AND router_id=?", (1 if online else 0, mac, router_id))
    conn.commit()
    conn.close()


def get_device(chat_id, mac, router_id):
    """Get single device by MAC and router for user"""
    conn = get_conn()
    row = conn.execute("""
        SELECT d.*, r.name AS router_name
        FROM devices d
        JOIN routers r ON d.router_id = r.router_id
        WHERE r.chat_id=? AND d.mac=? AND d.router_id=?
    """, (chat_id, mac.lower(), router_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_device_custom_name(mac, router_id, custom_name):
    conn = get_conn()
    conn.execute("UPDATE devices SET custom_name=? WHERE mac=? AND router_id=?", (custom_name, mac.lower(), router_id))
    conn.commit()
    conn.close()


def toggle_device_watch_online(mac, router_id):
    conn = get_conn()
    row = conn.execute("SELECT watch_online FROM devices WHERE mac=? AND router_id=?", (mac.lower(), router_id)).fetchone()
    if row:
        new_val = 0 if row["watch_online"] else 1
        conn.execute("UPDATE devices SET watch_online=? WHERE mac=? AND router_id=?", (new_val, mac.lower(), router_id))
        conn.commit()
    conn.close()
    return new_val if row else None


def upsert_device_with_notification(chat_id, mac, router_id, data):
    conn = get_conn()
    now = int(time.time())

    conn.execute("""
        INSERT INTO devices (mac, router_id, ip, hostname, custom_name, online, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(mac, router_id) DO UPDATE SET
            ip=excluded.ip,
            hostname=excluded.hostname,
            custom_name=excluded.custom_name,
            online=1,
            last_seen=excluded.last_seen
    """, (mac, router_id, data.get("ip"), data.get("hostname"), data.get("custom_name"), now, now))

    if not can_notify_device(chat_id, mac, router_id):
        conn.close()
        return False

    send_notification = True
    if "online_change" in data:
        prev = conn.execute("SELECT online FROM devices WHERE mac=? AND router_id=?", (mac, router_id)).fetchone()
        if prev and prev["online"] == 1 and data["online_change"] == 0:
            pass
        else:
            send_notification = False

    if send_notification:
        send_telegram_notification = False
        if data.get("event") == "CONNECTED":
            send_telegram_notification = True
        elif data.get("event") == "DISCONNECTED":
            send_telegram_notification = data.get("was_online", 0) == 1

        if send_telegram_notification:
            chat_id = get_user_by_router_id(router_id)
            if chat_id:
                mark_device_notified(chat_id, mac, router_id)

    conn.commit()
    conn.close()
    return True


# --- events ---

def add_event(router_id, mac, event, timestamp=None):
    conn = get_conn()
    ts = timestamp or int(time.time())
    conn.execute("INSERT INTO events (router_id, mac, event, timestamp) VALUES (?, ?, ?, ?)",
                 (router_id, mac.lower(), event, ts))
    conn.commit()
    conn.close()


def get_user_events(chat_id, hours=24):
    conn = get_conn()
    since_ts = int(time.time()) - hours * 3600
    rows = conn.execute("""
        SELECT e.*, r.name AS router_name
        FROM events e
        JOIN routers r ON e.router_id = r.router_id
        WHERE r.chat_id=? AND e.timestamp >= ?
        ORDER BY e.timestamp DESC
    """, (chat_id, since_ts)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- health checks ---

def add_health_check(router_id, internet, wifi, cpu, ram, wifi_clients, podkop_status, podkop_test, timestamp=None):
    conn = get_conn()
    ts = timestamp or int(time.time())
    conn.execute("""
        INSERT INTO health_checks (router_id, internet, wifi, cpu, ram, wifi_clients, podkop_status, podkop_test, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (router_id, 1 if internet else 0, 1 if wifi else 0, cpu, ram, wifi_clients, podkop_status, 1 if podkop_test else 0, ts))
    conn.commit()
    conn.close()


def get_latest_health(router_id):
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM health_checks
        WHERE router_id=?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (router_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_health(chat_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT hc.*, r.name AS router_name
        FROM health_checks hc
        JOIN routers r ON hc.router_id = r.router_id
        WHERE r.chat_id=?
        ORDER BY hc.timestamp DESC
    """, (chat_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- pairing codes ---

def create_pairing_code(code, chat_id, expires_sec=900):
    conn = get_conn()
    expires_at = int(time.time()) + expires_sec
    conn.execute("INSERT OR REPLACE INTO pairing_codes (code, chat_id, expires_at) VALUES (?, ?, ?)",
                 (code, chat_id, expires_at))
    conn.commit()
    conn.close()


def get_pairing_code(code):
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM pairing_codes
        WHERE code=? AND used=0 AND expires_at > ?
    """, (code, int(time.time()))).fetchone()
    conn.close()
    return dict(row) if row else None


def consume_pairing_code(code, router_id):
    conn = get_conn()
    conn.execute("UPDATE pairing_codes SET used=1, router_id=? WHERE code=?", (router_id, code))
    conn.commit()
    conn.close()


# --- cascade delete ---

def delete_router_complete(router_id):
    conn = get_conn()
    conn.execute("DELETE FROM health_checks WHERE router_id=?", (router_id,))
    conn.execute("DELETE FROM events WHERE router_id=?", (router_id,))
    conn.execute("DELETE FROM devices WHERE router_id=?", (router_id,))
    conn.execute("DELETE FROM router_secrets WHERE router_id=?", (router_id,))
    conn.commit()
    conn.close()


def is_valid_router_secret(secret):
    """Check if a secret is valid (exists in router_secrets table)"""
    conn = get_conn()
    row = conn.execute(
        "SELECT router_id FROM router_secrets WHERE secret=?", 
        (secret,)
    ).fetchone()
    conn.close()
    return row is not None


def can_notify_device(chat_id, mac, router_id, min_interval=300):
    """Check if we can send a notification for this device (deduplication)"""
    conn = get_conn()
    now = int(time.time())
    row = conn.execute(
        "SELECT last_notified_at FROM device_notifications WHERE chat_id=? AND mac=? AND router_id=?",
        (chat_id, mac, router_id)
    ).fetchone()
    conn.close()
    if not row:
        return True
    last_notified_at = row["last_notified_at"]
    return (now - last_notified_at) >= min_interval


def mark_device_notified(chat_id, mac, router_id):
    """Mark that we've sent a notification for this device"""
    conn = get_conn()
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO device_notifications (chat_id, mac, router_id, last_notified_at) VALUES (?, ?, ?, ?)",
        (chat_id, mac, router_id, now)
    )
    conn.commit()
    conn.close()
