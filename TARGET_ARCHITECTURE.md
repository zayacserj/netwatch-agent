# NetWatch — Целевая архитектура (Raspberry Pi + Telegram + Agent)

## 📊 Текущий статус проекта (2026-10-30)

| Компонент | Статус | Детали |
|-----------|--------|--------|
| Raspberry Pi OS + Caddy | ✅ Готово | HTTPS, Let's Encrypt, проброс портов 443/80 |
| Telegram Bot (`bot.py`) | ✅ Готово | Все команды реализованы: /start, /add_router, /routers, /devices, /settings, /guide, /history, /health, /admin |
| HTTP API (`api.py`) | ✅ Готово | /event, /devices, /health, /agent.sh, защита X-Agent-Secret |
| SQLite БД (`db.py`) | ✅ Готово | Таблицы: users, routers, devices, events, health_checks |
| Агент OpenWrt (`agent3.sh`) | ✅ Готов | pair, run, wifi-monitor, health-check, только Wi-Fi |
| Установка на роутере TR3000 | ❌ Проблема | DNS/HTTPS проблемы при загрузке agent.sh |
| Установка на роутере WR3000 | ⏳ В ожидании | После TR3000 |

### Ключевые решения (2026-10-30):
1. **Агент доставляется через GitHub:** файл `agent3.sh` хостится в репозитории https://github.com/zayacserj/netwatch-agent
   - Пользователь на роутере скачивает агента командой: `curl -sS https://raw.githubusercontent.com/zayacserj/netwatch-agent/main/agent3.sh -o /tmp/agent3.sh`
   - Это обходит проблемы DNS резолвинга (netwatchagent.duckdns.org → локальный IPv6) и TLS-inspection ISP
2. **Команда `/add_router`** обновлена: выдаёт готовую однокомандную установку
3. **Команда `/get_agent`** добавлена: отправка агента как документа в Telegram (fallback-вариант)

### Ключевые проблемы (устранены путём использования GitHub):
1. **DNS:** `netwatchagent.duckdns.org` резолвится в локальный IPv6 (`fdf5:ec45:d241::1`) вместо публичного IP
   - **Решение:** Используем GitHub raw URL для загрузки агента, DNS проблема не влияет
2. **SSL:** TLS-inspection провайдер выдаёт самоподписанный сертификат CN=OpenWrt
   - **Решение:** GitHub использует корректные сертификаты, SSL проблема решена для загрузки агента
3. **Caddy:** `/agent.sh` возвращает 308 Redirect (HTTP→HTTPS), сертификат Pi не работает для внешних клиентов
   - **Решение:** Агент доставляется через GitHub, Caddy больше не участвует в доставке агента

---

### Обновлённая команда `/add_router` (бот):

```python
# Быстрая установка (одна команда на роутере):
curl -sS https://raw.githubusercontent.com/zayacserj/netwatch-agent/main/agent3.sh -o /tmp/agent3.sh && chmod +x /tmp/agent3.sh && /tmp/agent3.sh pair --code {code} --url http://95.31.29.233
```

## 1. Общая схема

```
┌─────────────┐     HTTPS/WSS      ┌─────────────────────────┐
│  Пользователь │◄───────────────────►│  Telegram Bot API       │
│  (Telegram)   │                  │  (api.telegram.org)     │
└─────────────┘     Long Polling   └───────────┬─────────────┘
                                                 │
                                                 │ HTTPS
                                                 ▼
                                         ┌─────────────────┐
                                         │  Raspberry Pi   │
                                         │  (3 b+)         │
                                         │                 │
                                         │  ┌───────────┐  │
                                         │  │ Telegram  │  │
                                         │  │ Бот       │  │
                                         │  │ (long     │  │
                                         │  │ polling)  │  │
                                         │  └───────────┘  │
                                         │  ┌───────────┐  │
                                         │  │ HTTP API  │  │
                                         │  │ для       │  │
                                         │  │ агентов   │  │
                                         │  │ /event    │  │
                                         │  │ /devices  │  │
                                         │  │ /health   │  │
                                         │  └───────────┘  │
                                         │  ┌───────────┐  │
                                         │  │ SQLite    │  │
                                         │  │ БД        │  │
                                         │  │ (регистр) │  │
                                         │  └───────────┘  │
                                         │  ┌───────────┐  │
                                         │  │ Caddy     │  │
                                         │  │ HTTPS +   │  │
                                         │  │ LE сертиф │  │
                                         │  └───────────┘  │
                                         └────────┬────────┘
                                                  │ HTTPS POST (события)
                                                  ▼
                                         ┌─────────────────┐
                                         │  OpenWrt Роуте  │
                                         │  ry (до 30)   │
                                         │  • wlan0/wlan1│
                                         │  • hostapd_cli│
                                         │  • curl       │
                                         │  • DHCP leases│
                                         │  (только Wi-Fi)│
                                         └─────────────────┘

Внешний доступ:
- DuckDNS: zayacserj.duckdns.org → статический/динамический IP дома
- Проброс портов 443 (HTTPS) → Raspberry Pi (порт 8443 или 443)
- Сертификат: Let's Encrypt через Caddy
- Cloudflare: опционально (Tunnel или DNS proxy), не обязателен
```

---

## 2. Компоненты и обязанности

### 2.1 Raspberry Pi (Хаб)
| Компонент | Ответственность |
|-----------|-----------------|
| Telegram Bot | Приём команд от пользователей (`/start`, `/add_router`, `/routers`, `/devices`, `/settings`, `/guide`, `/history`, `/health`, `/admin`), отправка уведомлений |
| HTTP API для агентов | Приём событий `POST /event` (подключение/отключение Wi-Fi), приём снимков устройств `POST /devices`, приём health-check `POST /health`, раздача скрипта агента `GET /agent.sh` |
| База данных SQLite | Пользователи (chat_id), роутеры (router_id → chat_id), устройства (mac → router, online, watch_online), события (events, история за 24ч), результаты health-check |
| Обратный прокси (Caddy) | HTTPS терминация, автосертификат Let's Encrypt, проброс запросов на локальный порт бота/HTTP API |
| Веб-сервер для агентов | Раздача скрипта агента (`wget https://pi/agent.sh`) |

### 2.2 OpenWrt Агент (на роутере)
| Компонент | Ответственность |
|-----------|-----------------|
| `hostapd_cli` | События Wi-Fi подключения/отключения в реальном времени (AP-STA-CONNECTED / AP-STA-DISCONNECTED) |
| `/tmp/dhcp.leases` | Список активных DHCP-клиентов (IP, hostname, MAC) |
| `curl` | Отправка событий на Raspberry Pi (HTTP POST JSON) |
| cron | Периодический снимок устройств (раз в 1–2 мин), periodic health-check |
| `agent3.sh` | Реализация команд: `pair`, `run`, `wifi-monitor`, `health-check` |
| `/etc/netwatch/` | Конфиг (PI_URL, ROUTER_ID, AGENT_SECRET), creds, логи |
| **Wi-Fi только** | Агент анализирует и передаёт информацию только о беспроводных (Wi-Fi) подключениях; проводные подключения не учитываются |

### 2.3 Внешняя сеть
| Компонент | Ответственность |
|-----------|-----------------|
| DuckDNS | `zayacserj.duckdns.org` → домашний IP (динамический или статический) |
| Проброс портов | 443 → Raspberry Pi:443 (или 8443) |
| Сертификат TLS | Let's Encrypt (через Caddy) |
| Cloudflare | Опционально: Tunnel (если нельзя пробросить порт) или только DNS |

---

## 3. Протокол взаимодействия

### 3.1 Событие от роутера к Pi (Wi-Fi)
```
POST https://zayacserj.duckdns.org/event
Content-Type: application/json
X-Agent-Secret: <shared_secret>

{
  "router_id": "rtr_abc123",
  "mac": "aa:bb:cc:dd:ee:ff",
  "hostname": "iPhone",
  "ip": "192.168.10.50",
  "event": "AP-STA-CONNECTED",   // или AP-STA-DISCONNECTED
  "timestamp": 1730000000,
  "source": "hostapd"
}
```

### 3.2 Сnapshot устройств (cron)
```
POST https://zayacserj.duckdns.org/devices
Content-Type: application/json
X-Agent-Secret: <shared_secret>

{
  "router_id": "rtr_abc123",
  "devices": [
    {"mac": "...", "ip": "...", "hostname": "...", "connected": true}
  ],
  "timestamp": 1730000000
}
```

### 3.3 Health-check от роутера к Pi
```
POST https://zayacserj.duckdns.org/health
Content-Type: application/json
X-Agent-Secret: <shared_secret>

{
  "router_id": "rtr_abc123",
  "internet": true,
  "wifi": true,
  "cpu": "15.2",
  "ram": "245MB/980MB",
  "wifi_clients": 12,
  "podkop_status": "running",
  "podkop_test": true,
  "timestamp": 1730000000
}
```

### 3.4 Ответ бота пользователю
- `/devices` — бот читает из SQLite последние снимки по роутерам пользователя, отдаёт список (только Wi-Fi устройства)
- `/history` — бот читает таблицу `events` за последние 24 часа по всем роутерам пользователя
- `/health` — бот читает последние результаты health-check по роутерам пользователя
- `/admin` — админ-консоль владельца бота: статус всех роутеров, health-check, управление данными
- Уведомления — Pi шлёт в Telegram `sendMessage` при событии connect/disconnect, если `watch_online=true` для устройства

---

## 4. Модель данных (SQLite)

```sql
-- Пользователи Telegram
CREATE TABLE users (
  chat_id INTEGER PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  is_admin INTEGER DEFAULT 0,
  created_at INTEGER DEFAULT (unixepoch())
);

-- Роутеры
CREATE TABLE routers (
  router_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  name TEXT,
  mac TEXT,
  model TEXT,
  openwrt_version TEXT,
  online INTEGER DEFAULT 0,
  last_seen INTEGER,
  created_at INTEGER DEFAULT (unixepoch()),
  FOREIGN KEY (chat_id) REFERENCES users(chat_id)
);

-- Устройства (кэш состояния, только Wi-Fi)
CREATE TABLE devices (
  mac TEXT,
  router_id TEXT,
  ip TEXT,
  hostname TEXT,
  online INTEGER DEFAULT 0,
  watch_online INTEGER DEFAULT 0,
  label TEXT,
  first_seen INTEGER,
  last_seen INTEGER,
  PRIMARY KEY (mac, router_id),
  FOREIGN KEY (router_id) REFERENCES routers(router_id)
);

-- Логи событий (история за 24ч + ротация)
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  router_id TEXT,
  mac TEXT,
  event TEXT,
  timestamp INTEGER,
  FOREIGN KEY (router_id) REFERENCES routers(router_id)
);

-- Результаты health-check
CREATE TABLE health_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  router_id TEXT,
  internet INTEGER DEFAULT 0,
  wifi INTEGER DEFAULT 0,
  cpu TEXT,
  ram TEXT,
  wifi_clients INTEGER DEFAULT 0,
  podkop_status TEXT,
  podkop_test INTEGER DEFAULT 0,
  timestamp INTEGER,
  FOREIGN KEY (router_id) REFERENCES routers(router_id)
);
```

---

## 5. Функциональность

| Команда/событие | Описание | Реализация |
|-----------------|----------|------------|
| `/start` | Регистрация пользователя, приветствие | Бот пишет в users |
| `/add_router` | Генерация pairing-кода, инструкция установки агента | Бот генерирует код, хранит в памяти/БД |
| `/routers` | Список роутеров пользователя со статусом | SELECT из routers WHERE chat_id |
| `/devices` | Список всех Wi-Fi устройств пользователя со всех роутеров | SELECT из devices JOIN routers (только Wi-Fi) |
| `/settings` | Вкл/выкл уведомлений для устройств | UPDATE devices.watch_online |
| `/guide` | Инструкция | Текстовое сообщение |
| `/history` | История событий пользователя за последние 24 часа (по всем роутерам) | SELECT из events за последние 24ч WHERE chat_id |
| `/health` | Статус здоровья роутеров (интернет, Wi-Fi, CPU, RAM, клиенты, Podkop) | SELECT из health_checks |
| `/admin` | Админ-консоль владельца бота: управление данными (список всех пользователей, удалить пользователя, очистить данные по пользователю) | Доступно только для is_admin=1 |
| Событие Wi-Fi connect | Роутер шлёт POST /event → Pi ищет chat_id → шлёт уведомление в Telegram | HTTP + SQLite + Telegram API |
| Событие Wi-Fi disconnect | То же, с флагом оффлайн | То же |
| Установка агента | `wget -O /usr/bin/netwatch-agent https://pi/agent.sh` | Веб-сервер на Pi раздаёт скрипт |
| Health-check от роутера | POST /health → сохранение результата в health_checks | HTTP + SQLite |

---

## 6. Ограничения и риски

| Риск | Митигация |
|------|-----------|
| Домашний интернет недоступен | Роутеры буферизуют события локально, шлют при восстановлении |
| Pi перезагрузка | systemd service для бота + HTTP API, автозапуск |
| Безопасность (публичный URL) | Shared secret `X-Agent-Secret` на каждый запрос от роутера |
| Сертификат | Let's Encrypt через Caddy (автопродление) |
| До 10 пользователей × 3 роутера | SQLite Leicht, не более 30 роутеров, записей мало |
| NAT у роутеров друзей | Роутеры шлют исходящий HTTPS на публичный URL Pi (работает через NAT) |
| Только Wi-Fi анализируются | Проводные подключения игнорируются (по требованию заказчика) |

---

## 7. Отличия от текущей реализации (Cloudflare KV)

| Характеристика | Было (Cloudflare KV) | Стало (Raspberry Pi + SQLite) |
|----------------|----------------------|-------------------------------|
| Хранилище | Cloudflare KV (лимиты 429) | SQLite на Pi (без квот) |
| Центр управления | Worker (облако) | Raspberry Pi (дома) |
| Протокол | POST на Worker API | POST напрямую на Pi (HTTPS) |
| On-demand `/devices` | Из KV (только push от агента) | Из SQLite (кэш снимков от агентов) |
| `/history` | Нет | Да — события за 24 часа из таблицы events |
| `/health` | Нет | Да — health-check роутеров |
| `/admin` | Нет | Да — админ-консоль для владельца бота |
| Стоимость | Бесплатно, но с лимитами | 0 $ после покупки Pi |
| Приватность | Данные в облаке Cloudflare | Данные дома |
| Зависимость | Cloudflare uptime | Домашний интернет + Pi |
| Анализ подключений | Wi-Fi + проводные | Только Wi-Fi (проводные игнорируются) |