# NetWatch — Процесс (Process Ongoing)

Документ фиксирует все выполненные шаги, статусы, ссылки и решения по проекту NetWatch на дату **2026-10-30**.

---

## 📅 Хронология выполненных шагов

| Дата/время | Шаг | Статус | Ответственный |
|------------|-----|--------|---------------|
| 2026-10-30T19:56 | Ознакомление с документацией (PROJECT_DOCUMENTATION.md, TARGET_ARCHITECTURE.md, MIGRATION_PLAN.md) | ✅ | AI-ассистент |
| 2026-10-30T19:59 | Проверка физического окружения: роутеры, сервисы, DNS | ✅ | AI-ассистент |
| 2026-10-30T20:05 | Создан `agent_b64.txt` (base64 агента) — 8108 символов (превышает лимит Telegram) | ✅ | AI-ассистент |
| 2026-10-30T20:10 | Обновлён `netwatch_telegram_bot.py`: добавлена команда `/get_agent`, кнопка "Получить агент как файл" | ✅ | AI-ассистент |
| 2026-10-30T20:15 | Проверка доступности Pi: HTTP → 308 Redirect, HTTPS → SSL Error | ✅ | AI-ассистент |
| 2026-10-30T20:25 | Согласован вариант: использовать GitHub репозиторий для хостинга агента | ✅ | Пользователь |
| 2026-10-30T20:30 | Создан GitHub репозиторий: https://github.com/zayacserj/netwatch-agent | ✅ | Пользователь |
| 2026-10-30T20:35 | Загружен `agent3.sh` в репо через веб-интерфейс | ✅ | Пользователь |
| 2026-10-30T20:38 | Тестирование git clone/ls-remote на Pi — успешно | ✅ | Пользователь |
| 2026-10-30T20:40 | Исправлен конфликт локального agent3.sh, выполнен git pull | ✅ | Пользователь |
| 2026-10-30T20:45 | Настройка git на Pi: инициализация репо, .gitignore | ✅ | Пользователь |
| 2026-10-30T20:48 | Загрузка `netwatch_telegram_bot.py` в репо на GitHub | ✅ | Пользователь |
| 2026-10-30T20:52 | Создан данный документ PROCESS_ONGOING.md | ✅ | AI-ассистент |

---

## 🌐 Репозитории и ссылки

### GitHub репозитории

| Репозиторий | URL | Содержимое | Статус |
|-------------|-----|------------|--------|
| netwatch-agent | https://github.com/zayacserj/netwatch-agent | Агент OpenWrt (agent3.sh) | ✅ Активный, main branch |
| netwatch-bot | https://github.com/zayacserj/netwatch-agent (файл bot.py) | Бот (bot.py = netwatch_telegram_bot.py) | ⏳ Планируется отдельный репо |

### Прямые ссылки

| Ссылка | Назначение | Статус |
|--------|------------|--------|
| https://raw.githubusercontent.com/zayacserj/netwatch-agent/main/agent3.sh | Скачивание агента для роутеров | ✅ Работает |
| https://github.com/zayacserj/netwatch-agent/blob/main/netwatch_telegram_bot.py | Просмотр кода бота в репо | ✅ Доступен |
| http://95.31.29.233/ | Raspberry Pi (публичный IP) | ✅ Доступен (HTTP 308 → HTTPS) |
| https://zayacserj.duckdns.org | DuckDNS домен Pi | ✅ Работает |

---

## 📁 Файлы в проекте

### На локальном компьютере (C:\Users\Sergey\Desktop\Netwatch)

| Файл | Назначение | Статус |
|------|------------|--------|
| `netwatch_telegram_bot.py` | Код Telegram-бота (Python, aiogram) | ✅ Обновлён, в git |
| `netwatch_agent_openwrt.sh` | Агент для OpenWrt (shell скрипт) | ✅ В git (как agent3.sh) |
| `agent_b64.txt` | Base64 код агента (для справки) | ✅ Создан |
| `PROJECT_DOCUMENTATION.md` | Документация проекта | ✅ Обновляется |
| `TARGET_ARCHITECTURE.md` | Целевая архитектура | ✅ Обновляется |
| `MIGRATION_PLAN.md` | План миграции | ✅ Обновляется |
| `PROCESS_ONGOING.md` | Этот документ — хронология | ✅ Создан |

### На Raspberry Pi (/opt/netwatch/)

| Файл/директория | Назначение | Статус |
|----------------|------------|--------|
| `/opt/netwatch/agent3.sh` | Агент OpenWrt (из git) | ✅ 6080 байт |
| `/opt/netwatch/bot.py` | Telegram-бот (старая версия) | ⚠️ Требуется обновление |
| `/opt/netwatch/netwatch_telegram_bot.py` | Обновлённый бот локально | ✅ Готово к установке |
| `/opt/netwatch/api.py` | HTTP API для агентов | ✅ Есть |
| `/opt/netwatch/db.py` | SQLite работа с БД | ✅ Есть |
| `/opt/netwatch/netwatch.db` | SQLite база данных | ✅ 65536 байт |
| `/opt/netwatch/.env` | Переменные окружения | ✅ Есть |
| `/opt/netwatch/.gitignore` | Исключения для git | ✅ Создан |
| `/opt/netwatch/.git/` | Git репо | ✅ Инициализирован |
| `/opt/netwatch/venv/` | Виртуальное окружение Python | ✅ Есть |

---

## 🔧 Предстоящие шаги

| Шаг | Описание | Статус |
|-----|----------|--------|
| 1 | Обновить `bot.py` на Pi свежей версией из `netwatch_telegram_bot.py` | ⏳ В очередь |
| 2 | Добавить `bot.py` в отдельный репо `zayacserj/netwatch-bot` или в текущий | ⏳ В очередь |
| 3 | Перезапустить бота на Pi (`sudo systemctl restart netwatch-bot`) | ⏳ В очередь |
| 4 | Установить агент на роутер TR3000 через GitHub raw URL | ⏳ В очередь |
| 5 | Проверить отображение роутера в `/routers` и `/devices` | ⏳ В очередь |
| 6 | Проверить уведомления о Wi-Fi событиях | ⏳ В очередь |
| 7 | Проверить `/health` команду бота | ⏳ В очередь |

---

## 📋 Инструкции для настройки

### Установка агента на роутер (новая команда через GitHub):

```bash
# Одной командой (скачать + установить + зарегистрировать):
curl -sS https://raw.githubusercontent.com/zayacserj/netwatch-agent/main/agent3.sh -o /tmp/agent3.sh && chmod +x /tmp/agent3.sh && /tmp/agent3.sh pair --code <КОД> --url http://95.31.29.233
```

Где `<КОД>` — код, который выдаст бот при `/add_router`.

### Обновление бота на Pi:

```bash
cd /opt/netwatch
cp netwatch_telegram_bot.py bot.py
python3 -m py_compile bot.py && echo "OK"
sudo systemctl restart netwatch-bot
sudo systemctl status netwatch-bot --no-pager
```

---

## ⚠️ Примечания

- Все изменения в коде бота синхронизируются через git
- Агент теперь доставляется через GitHub raw URL (обход проблем с DNS/HTTPS на Pi)
- На Pi можно использовать HTTP для парирования (обходит TLS-inspection ISP)
- Файл `agent_b64.txt` сохранён локально для справки, но не используется в продакшене
