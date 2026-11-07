"""
NetWatch database layer (sqlite3)
"""
import sqlite3
from contextlib import contextmanager
import os
import time
from pathlib import Path

DB_PATH = Path(os.getenv("NETWATCH_DB_PATH", "/opt/netwatch/netwatch.db"))


def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def db_session(conn=None):
    """Commit/rollback and close only connections owned by this scope."""
    owned = conn is None
    if owned:
        conn = get_conn()
    try:
        yield conn
        if owned:
            conn.commit()
    except BaseException:
        if owned:
            conn.rollback()
        raise
    finally:
        if owned:
            conn.close()


def init_db():
    with db_session() as conn:
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
            podkop_proxy_tag TEXT,
            podkop_proxy_sample_at INTEGER,
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
            podkop_latency_ms INTEGER,
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
            watch_podkop_proxy INTEGER NOT NULL DEFAULT 0,
            watch_router_online INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS device_notifications (
            chat_id INTEGER,
            mac TEXT,
            router_id TEXT,
            last_notified_at INTEGER DEFAULT (unixepoch()),
            last_notified_state INTEGER,
            PRIMARY KEY (chat_id, mac, router_id),
            FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE,
            FOREIGN KEY (mac, router_id) REFERENCES devices(mac, router_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS device_history_presence (
            chat_id INTEGER NOT NULL,
            mac TEXT NOT NULL,
            online INTEGER NOT NULL,
            PRIMARY KEY (chat_id, mac),
            FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
        );
        """)

    # Миграция: добавить новые колонки если таблица уже существует
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            INSERT OR IGNORE INTO device_history_presence (chat_id, mac, online)
            SELECT r.chat_id, lower(d.mac), MAX(CASE WHEN d.online=1 THEN 1 ELSE 0 END)
            FROM devices d JOIN routers r ON r.router_id=d.router_id
            GROUP BY r.chat_id, lower(d.mac)
        """)
        settings_columns = {col["name"] for col in conn.execute("PRAGMA table_info(user_settings)")}
        if "watch_podkop_proxy" not in settings_columns:
            conn.execute("ALTER TABLE user_settings ADD COLUMN watch_podkop_proxy INTEGER NOT NULL DEFAULT 0")
        if "watch_router_online" not in settings_columns:
            conn.execute("ALTER TABLE user_settings ADD COLUMN watch_router_online INTEGER NOT NULL DEFAULT 0")
        router_columns = {col["name"] for col in conn.execute("PRAGMA table_info(routers)")}
        if "podkop_proxy_tag" not in router_columns:
            conn.execute("ALTER TABLE routers ADD COLUMN podkop_proxy_tag TEXT")
        if "podkop_proxy_sample_at" not in router_columns:
            conn.execute("ALTER TABLE routers ADD COLUMN podkop_proxy_sample_at INTEGER")
        health_columns = [col["name"] for col in conn.execute("PRAGMA table_info(health_checks)").fetchall()]
        if "podkop_latency_ms" not in health_columns:
            conn.execute("ALTER TABLE health_checks ADD COLUMN podkop_latency_ms INTEGER")
        columns = [col["name"] for col in conn.execute("PRAGMA table_info(devices)").fetchall()]
        if "custom_name" not in columns:
            conn.execute("ALTER TABLE devices ADD COLUMN custom_name TEXT")
        if "watch_online" not in columns:
            conn.execute("ALTER TABLE devices ADD COLUMN watch_online INTEGER DEFAULT 1")
        notification_columns = [col["name"] for col in conn.execute("PRAGMA table_info(device_notifications)").fetchall()]
        if "last_notified_state" not in notification_columns:
            conn.execute("ALTER TABLE device_notifications ADD COLUMN last_notified_state INTEGER")
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


def get_all_users(limit=-1, offset=0):
    with db_session() as conn:
        rows = conn.execute("""
            SELECT chat_id, username, first_name, is_admin
            FROM users ORDER BY chat_id LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_admin_user(chat_id):
    with db_session() as conn:
        row = conn.execute("""
            SELECT u.chat_id, u.username, u.first_name,
                   (SELECT COUNT(*) FROM routers WHERE chat_id=u.chat_id) AS router_count
            FROM users u WHERE u.chat_id=?
        """, (chat_id,)).fetchone()
        return dict(row) if row else None


def _remove_user_router_data(conn, chat_id):
    conn.execute("""
        DELETE FROM pairing_codes WHERE chat_id=? OR router_id IN (
            SELECT router_id FROM routers WHERE chat_id=?
        )
    """, (chat_id, chat_id))
    conn.execute("DELETE FROM routers WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM device_history_presence WHERE chat_id=?", (chat_id,))


def delete_user(chat_id):
    with db_session() as conn:
        _remove_user_router_data(conn, chat_id)
        return conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,)).rowcount == 1


def clean_user_data(chat_id):
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)).fetchone() is None:
            return False
        _remove_user_router_data(conn, chat_id)
        return True


# --- notification settings ---

_NOTIFICATION_COLUMNS = {
    "podkop": "watch_podkop_proxy",
    "router": "watch_router_online",
}


def get_notification_settings(chat_id):
    with db_session() as conn:
        row = conn.execute("""
            SELECT watch_podkop_proxy, watch_router_online
            FROM user_settings WHERE chat_id=?
        """, (chat_id,)).fetchone()
    return {key: bool(row and row[column]) for key, column in _NOTIFICATION_COLUMNS.items()}


def toggle_notification_setting(chat_id, key):
    column = _NOTIFICATION_COLUMNS[key]
    with db_session() as conn:
        conn.execute("INSERT OR IGNORE INTO user_settings (chat_id) VALUES (?)", (chat_id,))
        row = conn.execute(f"""
            UPDATE user_settings SET {column}=1-{column}
            WHERE chat_id=? RETURNING {column}
        """, (chat_id,)).fetchone()
        return bool(row[column])


def disable_device_notifications(chat_id):
    with db_session() as conn:
        return conn.execute("""
            UPDATE devices SET watch_online=0
            WHERE watch_online=1 AND router_id IN (
                SELECT router_id FROM routers WHERE chat_id=?
            )
        """, (chat_id,)).rowcount


# --- routers ---

def upsert_router(router_id, chat_id, name=None, mac=None, model=None, conn=None):
    with db_session(conn) as conn:

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
                        name=COALESCE(NULLIF(name, ''), ?),
                        model=COALESCE(?, model),
                        last_seen=unixepoch()
                    WHERE router_id=?
                """, (name, model, router_id))
                return router_id

        # Иначе создать новый
        conn.execute("""
            INSERT INTO routers (router_id, chat_id, name, mac, model, online, last_seen)
            VALUES (?, ?, ?, ?, ?, 0, unixepoch())
            ON CONFLICT(router_id) DO UPDATE SET
                name=COALESCE(NULLIF(routers.name, ''), excluded.name),
                mac=COALESCE(excluded.mac, routers.mac),
                model=COALESCE(excluded.model, routers.model)
        """, (router_id, chat_id, name, mac, model))
        return router_id


def get_user_routers(chat_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM routers WHERE chat_id=? ORDER BY created_at DESC", (chat_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def rename_user_router(chat_id, router_id, name):
    with db_session() as conn:
        return conn.execute(
            "UPDATE routers SET name=? WHERE router_id=? AND chat_id=?",
            (name, router_id, chat_id),
        ).rowcount == 1


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


def get_user_by_router_id(router_id, conn=None):
    """Возвращает chat_id пользователя, которому принадлежит роутер"""
    with db_session(conn) as conn:
        row = conn.execute("SELECT chat_id FROM routers WHERE router_id=?", (router_id,)).fetchone()
        return row["chat_id"] if row else None


# --- devices ---

_DEVICE_HOSTNAME_SQL = """
    CASE WHEN lower(trim(d.hostname)) NOT IN ('', '*', 'unknown') THEN trim(d.hostname)
         ELSE (
            SELECT trim(other.hostname)
            FROM devices other
            JOIN routers owner ON owner.router_id=other.router_id
            WHERE owner.chat_id=r.chat_id
              AND other.mac=d.mac COLLATE NOCASE
              AND lower(trim(other.hostname)) NOT IN ('', '*', 'unknown')
            ORDER BY other.last_seen DESC, other.router_id
            LIMIT 1
         )
    END AS known_hostname
"""


def get_user_devices(chat_id, router_id=None):
    conn = get_conn()
    query = f"""
        SELECT d.*, r.name AS router_name, r.router_id, {_DEVICE_HOSTNAME_SQL}
        FROM devices d
        JOIN routers r ON d.router_id = r.router_id
        WHERE r.chat_id=?
    """
    params = (chat_id,)
    if router_id:
        query += " AND d.router_id=?"
        params += (router_id,)
    query += " ORDER BY d.last_seen DESC"
    rows = conn.execute(query, params).fetchall()
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


def get_device(chat_id, mac, router_id, *, conn=None):
    """Get single device by MAC and router for user"""
    with db_session(conn) as session:
        row = session.execute(f"""
            SELECT d.*, r.name AS router_name, {_DEVICE_HOSTNAME_SQL}
            FROM devices d
            JOIN routers r ON d.router_id = r.router_id
            WHERE r.chat_id=? AND d.mac=? AND d.router_id=?
        """, (chat_id, mac.lower(), router_id)).fetchone()
    return dict(row) if row else None


def set_device_custom_name(mac, router_id, custom_name, *, chat_id=None):
    with db_session() as conn:
        return conn.execute("""
            UPDATE devices SET custom_name=? WHERE mac=? AND router_id=?
              AND (? IS NULL OR router_id IN (SELECT router_id FROM routers WHERE chat_id=?))
        """, (custom_name, mac.lower(), router_id, chat_id, chat_id)).rowcount == 1


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
    with db_session() as conn:
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

        can_notify = can_notify_device(chat_id, mac, router_id, conn=conn)
        if can_notify:
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
                    chat_id = get_user_by_router_id(router_id, conn=conn)
                    if chat_id:
                        mark_device_notified(chat_id, mac, router_id, conn=conn)

    return can_notify


# --- events ---

def add_event(router_id, mac, event, timestamp=None, conn=None):
    with db_session(conn) as conn:
        ts = timestamp if timestamp is not None else int(time.time())
        conn.execute("INSERT INTO events (router_id, mac, event, timestamp) VALUES (?, ?, ?, ?)",
                     (router_id, mac.lower() if mac is not None else None, event, ts))


def get_user_events(chat_id, hours=24, limit=-1):
    conn = get_conn()
    since_ts = int(time.time()) - hours * 3600
    rows = conn.execute(f"""
        SELECT e.*, r.name AS router_name, d.custom_name, {_DEVICE_HOSTNAME_SQL}
        FROM events e
        JOIN routers r ON e.router_id = r.router_id
        LEFT JOIN devices d ON d.mac=e.mac COLLATE NOCASE AND d.router_id=e.router_id
        WHERE r.chat_id=? AND e.timestamp >= ?
          AND upper(e.event) IN (
              'DEVICE_NEW', 'DEVICE_ONLINE', 'DEVICE_OFFLINE', 'ROUTER_ONLINE', 'ROUTER_OFFLINE',
              'CONNECTED', 'DISCONNECTED', 'AP-STA-CONNECTED', 'AP-STA-DISCONNECTED'
          )
        ORDER BY e.timestamp DESC, e.id DESC
        LIMIT ?
    """, (chat_id, since_ts, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- health checks ---

def add_health_check(router_id, internet, wifi, cpu, ram, wifi_clients, podkop_status, podkop_test, timestamp=None, podkop_latency_ms=None):
    conn = get_conn()
    ts = timestamp or int(time.time())
    conn.execute("""
        INSERT INTO health_checks (router_id, internet, wifi, cpu, ram, wifi_clients, podkop_status, podkop_test, timestamp, podkop_latency_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (router_id, 1 if internet else 0, 1 if wifi else 0, cpu, ram, wifi_clients, podkop_status,
          None if podkop_test is None else (1 if podkop_test else 0), ts, podkop_latency_ms))
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
    with db_session() as conn:
        row = conn.execute("""
            SELECT * FROM pairing_codes
            WHERE code=? AND used=0 AND expires_at > ?
        """, (code, int(time.time()))).fetchone()
        return dict(row) if row else None


def consume_pairing_code(code, router_id):
    with db_session() as conn:
        conn.execute("UPDATE pairing_codes SET used=1, router_id=? WHERE code=?", (router_id, code))


# --- cascade delete ---

def delete_router_complete(router_id):
    conn = get_conn()
    conn.execute("DELETE FROM health_checks WHERE router_id=?", (router_id,))
    conn.execute("DELETE FROM events WHERE router_id=?", (router_id,))
    conn.execute("DELETE FROM devices WHERE router_id=?", (router_id,))
    conn.execute("DELETE FROM router_secrets WHERE router_id=?", (router_id,))
    conn.execute("""
        DELETE FROM device_history_presence
        WHERE chat_id=(SELECT chat_id FROM routers WHERE router_id=?)
          AND NOT EXISTS (
              SELECT 1 FROM devices d JOIN routers r ON r.router_id=d.router_id
              WHERE r.chat_id=device_history_presence.chat_id
                AND d.mac=device_history_presence.mac COLLATE NOCASE
          )
    """, (router_id,))
    conn.commit()
    conn.close()


def is_valid_router_secret(secret):
    """Check if a secret is valid (exists in router_secrets table)"""
    with db_session() as conn:
        row = conn.execute(
            "SELECT router_id FROM router_secrets WHERE secret=?",
            (secret,)
        ).fetchone()
        return row is not None


def can_notify_device(chat_id, mac, router_id, min_interval=300, conn=None, notification_state=None):
    """Check if we can send a notification for this device (deduplication)"""
    with db_session(conn) as conn:
        now = int(time.time())
        row = conn.execute(
            "SELECT last_notified_at, last_notified_state FROM device_notifications WHERE chat_id=? AND mac=? AND router_id=?",
            (chat_id, mac, router_id)
        ).fetchone()
        if not row:
            return True
        # An opposite status is a new transition, not a duplicate notification.
        if (notification_state is not None and row["last_notified_state"] is not None
                and notification_state != row["last_notified_state"]):
            return True
        last_notified_at = row["last_notified_at"]
        return (now - last_notified_at) >= min_interval


def mark_device_notified(chat_id, mac, router_id, conn=None, notification_state=None):
    """Mark that we've sent a notification for this device"""
    with db_session(conn) as conn:
        now = int(time.time())
        conn.execute(
            "INSERT OR REPLACE INTO device_notifications (chat_id, mac, router_id, last_notified_at, last_notified_state) VALUES (?, ?, ?, ?, ?)",
            (chat_id, mac, router_id, now, notification_state)
        )
