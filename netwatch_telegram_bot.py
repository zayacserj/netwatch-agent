"""
NetWatch Telegram Bot (aiogram)
Commands: /start, /add_router, /routers, /devices, /settings, /guide, /history, /health, /admin
"""
import os
import asyncio
import secrets
import time
import logging
from datetime import datetime
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

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импорты из db.py (должен быть в той же папке)
import sys
sys.path.insert(0, str(Path(__file__).parent))

from db import (
    init_db, upsert_user, is_admin, set_admin, get_all_users, delete_user, clean_user_data,
    upsert_router, get_user_routers, get_all_routers, set_router_online,
    get_user_devices, get_user_devices_count, set_device_online,
    get_device, set_device_custom_name, toggle_device_watch_online,
    add_event, get_user_events,
    get_latest_health, get_user_health, add_health_check,
    create_pairing_code, get_pairing_code, consume_pairing_code,
    delete_router_complete,
    disable_device_notifications, get_notification_settings, toggle_notification_setting,
    rename_user_router,
    get_admin_user,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var")

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

bot = Bot(token=TOKEN)
dp = Dispatcher()


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_online(online: int) -> str:
    return "🟢 онлайн" if online else "🔴 оффлайн"


# --- /start ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if message.from_user.id in ADMIN_IDS:
        set_admin(message.from_user.id, True)
    text = (
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Это бот NetWatch для мониторинга Wi-Fi сетей через OpenWrt-роутеры.\n\n"
        "Команды:\n"
        "• /add_router — добавить роутер\n"
        "• /routers — мои роутеры\n"
        "• /devices — подключённые Wi-Fi устройства\n"
        "• /history — история событий (24ч)\n"
        "• /health — статус здоровья роутеров\n"
        "• /settings — настройки уведомлений\n"
        "• /guide — инструкция\n"
    )
    if is_admin(message.from_user.id):
        text += "\n🔐 /admin — админ-панель"
    await message.reply(text)


# --- /add_router ---
def _agent_install_command(code, url):
    from ipaddress import ip_address
    from shlex import quote
    from urllib.parse import urlsplit

    release_ref = "82221b18ae1b8c17c2ab4d18549d46a6381f76d3"
    agent_sha256 = "2b5763f77db64abcd4f788045d1a8f66b0283868ad75a8426abc2f2b33b5ae04"
    if (not isinstance(code, str) or len(code) != 8
            or any(c not in "0123456789abcdefABCDEF" for c in code)):
        raise ValueError("Invalid pairing code")
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("Invalid API address")
    if any(c in url for c in "\"'`$\\"):
        raise ValueError("Invalid API address")
    url = url.rstrip("/")
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError("Invalid API address")
    try:
        ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Use the canonical API hostname, not an IP address")
    if parts.hostname.lower() == "localhost" or parts.hostname.lower().endswith((".local", ".localhost")):
        raise ValueError("Use the public API hostname")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("Invalid API port")
    source = f"https://raw.githubusercontent.com/zayacserj/netwatch-agent/{release_ref}/deploy_files/agent3_fixed.sh"
    return (
        "(\n"
        "  set -e\n"
        "  umask 077\n"
        "  [ -r /etc/openwrt_release ] && [ \"$(id -u)\" = 0 ] || { echo 'Run as root on OpenWrt' >&2; exit 1; }\n"
        "  nw_install_dir=$(mktemp -d /tmp/netwatch-install.XXXXXX)\n"
        "  agent=\"$nw_install_dir/agent.sh\"\n"
        "  trap 'rm -f \"$agent\"; rmdir \"$nw_install_dir\" 2>/dev/null || true' 0\n"
        f"  source={quote(source)}\n"
        "\n"
        "  download() { curl --disable -fsS --max-time 30 --proto '=https' -o \"$agent\" \"$source\"; }\n"
        "  if command -v curl >/dev/null 2>&1 && [ -s /etc/ssl/certs/ca-certificates.crt ]; then\n"
        "    download\n"
        "  elif command -v uclient-fetch >/dev/null 2>&1 && uclient-fetch -q -T 30 -O \"$agent\" \"$source\"; then\n"
        "    :\n"
        "  else\n"
        "    if command -v apk >/dev/null 2>&1; then\n"
        "      apk update && apk add curl ca-bundle || exit 1\n"
        "    else\n"
        "      opkg update && opkg install curl ca-bundle || exit 1\n"
        "    fi\n"
        "    download\n"
        "  fi\n"
        "\n"
        f"  printf '%s  %s\\n' {quote(agent_sha256)} \"$agent\" | sha256sum -c -\n"
        "  sh -n \"$agent\"\n"
        f"  sh \"$agent\" pair --install-deps --code {quote(code)} --url {quote(url)}\n"
        ")"
    )


@dp.message(Command("add_router"))
async def cmd_add_router(message: Message):
    from html import escape

    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    code = secrets.token_hex(4)
    api_url = os.getenv("PI_URL_PUBLIC", "")
    try:
        install_command = _agent_install_command(code, api_url)
    except ValueError:
        await message.reply("Подключение временно недоступно: единый HTTPS-адрес сервера NetWatch не настроен.")
        return

    text = (
        "<b>📡 Подключение роутера NetWatch</b>\n\n"
        "Скопируйте весь блок в терминал OpenWrt под root. Код действует 15 минут.\n\n"
        f"<pre>{escape(install_command, quote=False)}</pre>\n"
    )
    text += "\nАгент установит недостающие пакеты и сохранит действующую привязку."
    create_pairing_code(code, message.from_user.id, expires_sec=900)
    await message.reply(text, parse_mode="HTML")


# --- /routers ---
class RouterRename(StatesGroup):
    waiting_for_name = State()


@dp.message(Command("routers"))
async def cmd_routers(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    routers = get_user_routers(message.from_user.id)
    if not routers:
        await message.reply("📭 У вас ещё нет роутеров. Добавьте: /add_router")
        return
    lines = ["🖥️ *Мои роутеры:*"]
    builder = InlineKeyboardBuilder()
    for r in routers:
        online = fmt_online(r['online'])
        dev_count = get_user_devices_count(message.from_user.id, r['router_id'])
        lines.append(f"• {r['name'] or r['router_id']} — {online} (устройств: {dev_count}, MAC: {r['mac'] or 'н/д'})")
        label = r["name"] or r["router_id"]
        builder.button(text=f"✏️ {label}", callback_data=f"rename_router|{r['router_id']}")
        builder.button(text=f"🗑 {label}", callback_data=f"del_router_{r['router_id']}")
    builder.adjust(2)
    await message.reply("\n".join(lines),  reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("rename_router|"))
async def callback_rename_router(callback: CallbackQuery, state: FSMContext):
    router_id = callback.data.split("|", 1)[1]
    router = next(
        (r for r in get_user_routers(callback.from_user.id) if r["router_id"] == router_id),
        None,
    )
    if router is None:
        await callback.answer("⛔ Роутер недоступен")
        return
    await state.set_data({"router_id": router_id})
    await state.set_state(RouterRename.waiting_for_name)
    await callback.answer()
    await callback.message.answer(
        f"✏️ Текущее имя: {router['name'] or router_id}\n"
        "Отправь новое имя одним сообщением, до 64 символов. Для отмены отправь /cancel."
    )


@dp.message(RouterRename.waiting_for_name, Command("cancel"))
async def cancel_router_rename(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Переименование отменено.")


@dp.message(RouterRename.waiting_for_name, F.text & ~F.text.startswith("/"))
async def save_router_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 1 <= len(name) <= 64 or not name.isprintable():
        await message.reply("Введи имя из 1–64 символов в одну строку или отправь /cancel.")
        return
    router_id = (await state.get_data()).get("router_id")
    changed = rename_user_router(message.from_user.id, router_id, name)
    await state.clear()
    if changed:
        await message.reply(f"✅ Новое имя роутера: {name}\nСписок роутеров — /routers")
    else:
        await message.reply("Роутер больше недоступен. Открой /routers.")


# --- Удаление роутера с подтверждением ---
@dp.callback_query(F.data.startswith("del_router_"))
async def callback_del_router(callback: CallbackQuery):
    router_id = callback.data.replace("del_router_", "")
    
    # Проверка: роутер должен принадлежать этому пользователю
    user_routers = get_user_routers(callback.from_user.id)
    router = next((r for r in user_routers if r["router_id"] == router_id), None)
    if not router:
        await callback.answer("⛔ У вас нет прав на удаление этого роутера")
        return
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"confirm_del_{router_id}")
    builder.button(text="❌ Отмена", callback_data="cancel_del")
    await callback.message.edit_text(
        f"⚠️ Удалить роутер `{router['name'] or router_id}` (`{router_id}`)?\nВсе данные (устройства, события, health-check) будут удалены.",
        
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_del_"))
async def callback_confirm_delete(callback: CallbackQuery):
    router_id = callback.data.replace("confirm_del_", "")
    
    # Проверка: роутер должен принадлежать этому пользователю
    user_routers = get_user_routers(callback.from_user.id)
    if not any(r["router_id"] == router_id for r in user_routers):
        await callback.answer("⛔ У вас нет прав на удаление этого роутера")
        return
    
    # Удалить роутер и все связанные данные
    delete_router_complete(router_id)
    
    # Показать команду для ручного удаления агента
    rm_cmd = (
        f"📋 *Роутер `{router_id}` удалён из базы.*\n\n"
        f"Для полного удаления агента с роутера выполните на нём:\n"
        f"```\n"
        f"rm -f /etc/netwatch/config\n"
        f"rm -f /tmp/agent3.sh\n"
        f"rm -f /mnt/usb/log/netwatch/netwatch-agent.log\n"
        f"rm -f /var/run/netwatch-wifi-monitor.pid\n"
        f"```\n"
        f"ИЛИ просто переподключите роутер:"
    )
    await callback.message.edit_text(rm_cmd, )
    await callback.answer("✅ Роутер удалён")


@dp.callback_query(F.data == "cancel_del")
async def callback_cancel_delete(callback: CallbackQuery):
    await callback.message.edit_text("❌ Удаление отменено")
    await callback.answer()


# --- /devices ---
DEVICES_PER_PAGE = 10


def unique_devices(rows):
    """Prefer the most recently seen online record for each MAC."""
    ordered = sorted(rows, key=lambda row: (
        not row["online"], -(row["last_seen"] or 0), row["router_id"],
    ))
    selected = {}
    for row in ordered:
        selected.setdefault(row["mac"].lower(), row)
    return list(selected.values())


def render_devices_page(chat_id, page=0):
    devices = unique_devices(get_user_devices(chat_id))
    if not devices:
        return "📭 Нет Wi-Fi устройств.", None

    pages = (len(devices) + DEVICES_PER_PAGE - 1) // DEVICES_PER_PAGE
    page = max(0, min(page, pages - 1))
    start = page * DEVICES_PER_PAGE
    lines = [f"📱 Мои Wi-Fi устройства · {len(devices)}", f"Страница {page + 1} из {pages}", ""]
    builder = InlineKeyboardBuilder()
    for device in devices[start:start + DEVICES_PER_PAGE]:
        mac = device["mac"]
        router_id = device["router_id"]
        name = " ".join((device["custom_name"] or device.get("known_hostname") or mac).split())[:64] or mac
        router_name = " ".join((device["router_name"] or router_id).split())[:64]
        status = "🟢" if device["online"] else "🔴"
        bell = "🔔" if device.get("watch_online", 1) else "🔕"
        lines.append(f"{status} {name} · {router_name}\n   {mac}")
        data = f"device_detail|{mac}|{router_id}"
        if len(data.encode("utf-8")) <= 64:
            builder.button(text=f"{status} {name} {bell}", callback_data=data)
    builder.adjust(1)
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"device_page|{page - 1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="Далее ▶", callback_data=f"device_page|{page + 1}"))
    if navigation:
        builder.row(*navigation)
    return "\n".join(lines), builder.as_markup()


@dp.message(Command("devices"))
async def cmd_devices(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text, markup = render_devices_page(message.from_user.id)
    if markup is None and not get_user_routers(message.from_user.id):
        await message.reply("📭 У вас нет роутеров. Добавьте: /add_router")
        return
    await message.reply(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("device_page|"))
async def callback_device_page(callback: CallbackQuery):
    try:
        page = int(callback.data.split("|", 1)[1])
    except ValueError:
        await callback.answer("Страница недоступна")
        return
    text, markup = render_devices_page(callback.from_user.id, page)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


# --- Device detail callback ---
@dp.callback_query(F.data.startswith("device_detail|"))
async def callback_device_detail(callback: CallbackQuery):
    parts = callback.data.split("|")
    mac = parts[1]
    router_id = parts[2]
    
    device = get_device(callback.from_user.id, mac.lower(), router_id)
    if not device:
        await callback.answer("❌ Устройство не найдено")
        return
    
    status = "🟢 онлайн" if device["online"] else "🔴 оффлайн"
    watch = "🔔 Уведомления: ВКЛ" if device.get("watch_online", 1) else "🔕 Уведомления: ВЫКЛ"
    name = device["custom_name"] or device.get("known_hostname") or "неизвестно"
    
    text = (
        f"📱 *Устройство*\n\n"
        f"• MAC: `{device['mac']}`\n"
        f"• Имя: {name}\n"
        f"• IP: {device['ip'] or 'н/д'}\n"
        f"• Роутер: {device['router_name'] or router_id}\n"
        f"• Статус: {status}\n"
        f"• {watch}\n"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Переименовать", callback_data=f"device_rename|{mac}|{router_id}")
    builder.button(text=watch.split(":")[0].strip() + (" Выключить" if device.get("watch_online", 1) else " Включить"), callback_data=f"device_notify|{mac}|{router_id}")
    builder.button(text="◀ Назад к списку", callback_data="device_back_list")
    builder.adjust(2, 1)
    
    await callback.message.edit_text(text,  reply_markup=builder.as_markup())
    await callback.answer()


# --- Device back to list ---
@dp.callback_query(F.data == "device_back_list")
async def callback_device_back(callback: CallbackQuery):
    text, markup = render_devices_page(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


# --- Device rename: ask for new name ---
class DeviceRename(StatesGroup):
    waiting_for_name = State()


@dp.callback_query(F.data.startswith("device_rename|"))
async def callback_device_rename(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("|", 2)
    if len(parts) != 3:
        await callback.answer("Устройство недоступно")
        return
    _, mac, router_id = parts
    device = get_device(callback.from_user.id, mac, router_id)
    if not device:
        await callback.answer("⛔ Нет доступа")
        return

    await state.set_data({"mac": device["mac"], "router_id": router_id})
    await state.set_state(DeviceRename.waiting_for_name)
    await callback.answer()
    await callback.message.answer(
        f"✏️ Введите новое имя для устройства {device['mac']}.\n"
        "Отправьте имя одной строкой, до 64 символов, или /cancel для отмены."
    )


@dp.message(DeviceRename.waiting_for_name, Command("cancel"))
async def cancel_device_rename(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Переименование отменено.")


@dp.message(DeviceRename.waiting_for_name, F.text & ~F.text.startswith("/"))
async def save_device_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 1 <= len(name) <= 64 or not name.isprintable():
        await message.reply("Введите имя из 1–64 символов в одну строку или /cancel.")
        return

    data = await state.get_data()
    changed = set_device_custom_name(
        data["mac"], data["router_id"], name, chat_id=message.from_user.id,
    )
    await state.clear()
    if changed:
        await message.reply(f"✅ Новое имя устройства: {name}\nОбновить список — /devices")
    else:
        await message.reply("Устройство больше недоступно. Откройте /devices.")


# --- Device toggle notifications ---
@dp.callback_query(F.data.startswith("device_notify|"))
async def callback_device_notify(callback: CallbackQuery):
    parts = callback.data.split("|")
    mac = parts[1]
    router_id = parts[2]
    
    new_val = toggle_device_watch_online(mac, router_id)
    status = "ВКЛ" if new_val else "ВЫКЛ"
    await callback.answer(f"Уведомления {status}")
    
    # Refresh detail view
    device = get_device(callback.from_user.id, mac.lower(), router_id)
    if device:
        watch = "🔔 Уведомления: ВКЛ" if device.get("watch_online", 1) else "🔔 Уведомления: ВЫКЛ"
        name = device["custom_name"] or device.get("known_hostname") or "неизвестно"
        stat = "🟢 онлайн" if device["online"] else "🔴 оффлайн"
        text = (
            f"📱 *Устройство*\n\n"
            f"• MAC: `{device['mac']}`\n"
            f"• Имя: {name}\n"
            f"• IP: {device['ip'] or 'н/д'}\n"
            f"• Роутер: {device['router_name'] or router_id}\n"
            f"• Статус: {stat}\n"
            f"• {watch}\n"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="✏️ Переименовать", callback_data=f"device_rename|{mac}|{router_id}")
        builder.button(text=watch.split(":")[0].strip() + (" Выключить" if device.get("watch_online", 1) else " Включить"), callback_data=f"device_notify|{mac}|{router_id}")
        builder.button(text="◀ Назад к списку", callback_data="device_back_list")
        builder.adjust(2, 1)
        await callback.message.edit_text(text,  reply_markup=builder.as_markup())


# --- /settings ---
def notification_settings_keyboard(chat_id):
    settings = get_notification_settings(chat_id)
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔕 Отключить онлайн/оффлайн у всех устройств",
        callback_data="notify_settings|disable_devices",
    )
    for key, label in (("podkop", "🔀 Podkop: смена прокси"), ("router", "📡 Статус роутеров")):
        state = "ВКЛ" if settings[key] else "ВЫКЛ"
        builder.button(text=f"{label} · {state}", callback_data=f"notify_settings|{key}")
    builder.adjust(1)
    return builder.as_markup()


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.reply(
        "⚙️ Уведомления NetWatch\n\n"
        "О новых устройствах бот сообщает всегда. "
        "Уведомления отдельных устройств можно включить в /devices.",
        reply_markup=notification_settings_keyboard(message.from_user.id),
    )


@dp.callback_query(F.data.startswith("notify_settings|"))
async def callback_notification_settings(callback: CallbackQuery):
    user = callback.from_user
    action = callback.data.split("|", 1)[1]
    if action == "disable_devices":
        count = disable_device_notifications(user.id)
        await callback.answer(f"Отключены уведомления у устройств: {count}")
        return
    if action not in ("podkop", "router"):
        await callback.answer("Настройка недоступна")
        return
    upsert_user(user.id, user.username, user.first_name)
    enabled = toggle_notification_setting(user.id, action)
    await callback.answer("Уведомления включены" if enabled else "Уведомления отключены")
    await callback.message.edit_reply_markup(reply_markup=notification_settings_keyboard(user.id))


# --- Toggle notifications (from /settings) ---
@dp.callback_query(F.data.startswith("toggle_notify|"))
async def callback_toggle_notify(callback: CallbackQuery):
    parts = callback.data.split("|")
    mac = parts[1]
    router_id = parts[2]
    
    user_routers = get_user_routers(callback.from_user.id)
    if not any(r["router_id"] == router_id for r in user_routers):
        await callback.answer("⛔ Нет доступа")
        return
    
    new_val = toggle_device_watch_online(mac, router_id)
    status = "ВКЛ" if new_val else "ВЫКЛ"
    await callback.answer(f"Уведомления {status}")
    
    await callback.message.edit_text(
        "⚙️ Уведомления NetWatch",
        reply_markup=notification_settings_keyboard(callback.from_user.id),
    )


# --- /guide ---
@dp.message(Command("guide"))
async def cmd_guide(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text = (
        "<b>📖 Как пользоваться NetWatch</b>\n\n"
        "<b>1. Подключите роутер</b>\n"
        "Отправьте /add_router. Бот выдаст блок установки с кодом на 15 минут. "
        "Откройте терминал OpenWrt по SSH под пользователем root и вставьте весь блок. "
        "Роутеру нужен интернет для загрузки агента и недостающих пакетов. "
        "После успешной установки откройте /routers.\n\n"
        "<b>2. Посмотрите роутеры и устройства</b>\n"
        "• /routers — список роутеров и их статус. Кнопка ✏️ меняет имя. "
        "Кнопка 🗑 удаляет роутер и его историю после подтверждения.\n"
        "• /devices — список Wi-Fi-устройств. Нажмите на устройство, чтобы увидеть "
        "его IP, роутер и статус, задать имя или переключить уведомления онлайн/оффлайн. "
        "Ввод имени можно отменить командой /cancel.\n\n"
        "<b>3. Настройте уведомления</b>\n"
        "В /settings можно разом выключить уведомления онлайн/оффлайн у текущих устройств. "
        "Сообщения о новых устройствах продолжат приходить. Там же можно отдельно "
        "включить уведомления о статусе роутеров и о смене прокси Podkop, если он установлен.\n\n"
        "<b>4. Проверьте состояние сети</b>\n"
        "• /health — интернет, Wi-Fi, число клиентов, загрузка CPU, память и состояние Podkop.\n"
        "• /history — события подключения и отключения, полученные за последние 24 часа.\n\n"
        "/start — вернуться к списку команд."
    )
    await message.reply(text, parse_mode="HTML")


# --- /history ---
def format_history_event(event):
    kind = event["event"].upper()
    labels = {
        "DEVICE_NEW": "🆕 Новое устройство",
        "DEVICE_ONLINE": "🟢 Устройство онлайн",
        "DEVICE_OFFLINE": "🔴 Устройство оффлайн",
        "CONNECTED": "🟢 Устройство онлайн",
        "DISCONNECTED": "🔴 Устройство оффлайн",
        "AP-STA-CONNECTED": "🟢 Устройство онлайн",
        "AP-STA-DISCONNECTED": "🔴 Устройство оффлайн",
        "ROUTER_ONLINE": "🟢 Роутер онлайн",
        "ROUTER_OFFLINE": "🔴 Роутер оффлайн",
    }
    time = fmt_time(event["timestamp"])
    router = " ".join((event["router_name"] or event["router_id"]).split())[:64]
    label = labels[kind]
    if kind.startswith("ROUTER_"):
        return f"{time} · {label} · {router}"
    mac = (event["mac"] or "н/д")[:32]
    name = event["custom_name"] or event["known_hostname"] or mac
    name = " ".join(name.split())[:64] or mac
    device = name if name == mac else f"{name} ({mac})"
    return f"{time} · {label} · {device} · {router}"


@dp.message(Command("history"))
async def cmd_history(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    events = get_user_events(message.from_user.id, hours=24, limit=50)
    if not events:
        await message.reply("📭 Нет событий за последние 24 часа.")
        return
    page = "📜 История за 24 часа"
    for event in events:
        line = format_history_event(event)
        if len((page + "\n" + line).encode("utf-16-le")) // 2 > 4000:
            await message.reply(page)
            page = "📜 История за 24 часа · продолжение"
        page += "\n" + line
    await message.reply(page)


# --- /health ---
@dp.message(Command("health"))
async def cmd_health(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    routers = get_user_routers(message.from_user.id)
    if not routers:
        await message.reply("📭 У вас нет роутеров.")
        return
    lines = ["🩺 *Статус здоровья роутеров:*"]
    for r in routers:
        h = get_latest_health(r["router_id"])
        if h:
            net = "✅" if h["internet"] else "❌"
            wf = "✅" if h["wifi"] else "❌"
            pk = "не проверялся" if h["podkop_test"] is None else ("✅" if h["podkop_test"] else "⚠️")
            if h["podkop_test"] and h.get("podkop_latency_ms") is not None:
                pk += f" ({h['podkop_latency_ms']} мс)"
            podkop_status = {
                "running": "sing-box работает",
                "stopped": "sing-box остановлен",
                "not_installed": "не установлен",
                "unknown": "неизвестно",
            }.get(h["podkop_status"], h["podkop_status"] or "н/д")
            lines.append(
                f"• {r['name'] or r['router_id']}:\n"
                f"  Интернет: {net} | Wi-Fi: {wf} (клиентов: {h['wifi_clients']}) | CPU: {h['cpu'] or 'н/д'} | "
                f"RAM: {h['ram'] or 'н/д'}\n"
                f"  Podkop: {podkop_status} | Прокси: {pk}"
            )
        else:
            lines.append(f"• {r['name'] or r['router_id']}: ⏳ нет данных о health-check")
    await message.reply("\n".join(lines), )


# --- /admin ---
class AdminFlow(StatesGroup):
    broadcast_text = State()
    confirming = State()


ADMIN_USERS_PER_PAGE = 12
_broadcast_lock = asyncio.Lock()


def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Список пользователей", callback_data="admin_users|0")
    builder.button(text="🖥 Мониторинг Pi", callback_data="admin_pi")
    builder.button(text="📣 Сообщение всем пользователям", callback_data="admin_broadcast")
    builder.adjust(1)
    return builder.as_markup()


def admin_confirm_keyboard(token, label):
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data=f"admin_confirm|{token}")
    builder.button(text="Отмена", callback_data="admin_home")
    builder.adjust(2)
    return builder.as_markup()


def admin_message_allowed(message):
    return message.chat.type == "private" and is_admin(message.from_user.id)


async def admin_callback_allowed(callback):
    if (callback.message is not None and callback.message.chat.type == "private"
            and is_admin(callback.from_user.id)):
        return True
    await callback.answer("Панель доступна администраторам в личном чате.", show_alert=True)
    return False


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not admin_message_allowed(message):
        await message.reply("⛔ Панель доступна администраторам в личном чате.")
        return
    await state.clear()
    await message.reply("🔐 Админ-панель NetWatch", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin_home")
async def admin_home(callback: CallbackQuery, state: FSMContext):
    if not await admin_callback_allowed(callback):
        return
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("🔐 Админ-панель NetWatch", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin_pi")
async def admin_pi(callback: CallbackQuery):
    if await admin_callback_allowed(callback):
        await callback.answer("Мониторинг Pi пока не настроен.", show_alert=True)


@dp.callback_query(F.data.startswith("admin_users|"))
async def admin_list_users(callback: CallbackQuery, state: FSMContext):
    if not await admin_callback_allowed(callback):
        return
    raw = callback.data.split("|", 1)[1]
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 8:
        await callback.answer("Страница недоступна")
        return
    page = int(raw)
    users = get_all_users(limit=ADMIN_USERS_PER_PAGE + 1, offset=page * ADMIN_USERS_PER_PAGE)
    if page and not users:
        page = 0
        users = get_all_users(limit=ADMIN_USERS_PER_PAGE + 1)
    await state.clear()
    builder = InlineKeyboardBuilder()
    for user in users[:ADMIN_USERS_PER_PAGE]:
        name = " ".join((user["username"] or user["first_name"] or "").split())[:32]
        label = f"{user['chat_id']} · {name}" if name else str(user["chat_id"])
        builder.button(text=label, callback_data=f"admin_user|{user['chat_id']}")
    builder.adjust(1)
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"admin_users|{page - 1}"))
    if len(users) > ADMIN_USERS_PER_PAGE:
        navigation.append(InlineKeyboardButton(text="Далее ▶", callback_data=f"admin_users|{page + 1}"))
    if navigation:
        builder.row(*navigation)
    builder.row(InlineKeyboardButton(text="В админ-панель", callback_data="admin_home"))
    text = f"👥 Пользователи · страница {page + 1}" if users else "📭 Нет пользователей."
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=builder.as_markup())


def admin_target(raw):
    if not raw.isascii() or not raw.isdecimal() or not 1 <= len(raw) <= 18:
        return None
    return get_admin_user(int(raw))


@dp.callback_query(F.data.startswith("admin_user|"))
async def admin_user_card(callback: CallbackQuery, state: FSMContext):
    if not await admin_callback_allowed(callback):
        return
    user = admin_target(callback.data.split("|", 1)[1])
    if user is None:
        await callback.answer("Пользователь не найден")
        return
    await state.clear()
    chat_id = user["chat_id"]
    name = user["first_name"] or "н/д"
    username = "@" + user["username"] if user["username"] else "н/д"
    builder = InlineKeyboardBuilder()
    builder.button(text="🧹 Удалить данные пользователя", callback_data=f"admin_action|clean|{chat_id}")
    builder.button(text="🗑 Удалить пользователя", callback_data=f"admin_action|delete|{chat_id}")
    builder.button(text="К списку пользователей", callback_data="admin_users|0")
    builder.adjust(1)
    await callback.answer()
    await callback.message.edit_text(
        f"👤 Пользователь {chat_id}\nИмя: {name}\nTelegram: {username}\n"
        f"Роутеров: {user['router_count']}",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data.startswith("admin_action|"))
async def admin_user_action(callback: CallbackQuery, state: FSMContext):
    if not await admin_callback_allowed(callback):
        return
    parts = callback.data.split("|")
    user = admin_target(parts[2]) if len(parts) == 3 and parts[1] in ("clean", "delete") else None
    if user is None:
        await callback.answer("Действие недоступно")
        return
    action, chat_id = parts[1], user["chat_id"]
    token = secrets.token_hex(8)
    await state.set_data({"token": token, "action": action, "chat_id": chat_id})
    await state.set_state(AdminFlow.confirming)
    scope = "Запись пользователя и его настройки тоже будут удалены." if action == "delete" else (
        "Запись пользователя и его настройки сохранятся."
    )
    await callback.answer()
    await callback.message.edit_text(
        f"Подтвердите удаление данных пользователя {chat_id}.\n\n"
        f"Будут удалены привязки ({user['router_count']}), устройства, история и коды привязки.\n"
        + scope,
        reply_markup=admin_confirm_keyboard(token, "Подтвердить удаление"),
    )


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_prompt(callback: CallbackQuery, state: FSMContext):
    if not await admin_callback_allowed(callback):
        return
    await state.clear()
    await state.set_state(AdminFlow.broadcast_text)
    await callback.answer()
    await callback.message.edit_text("📣 Отправьте текст рассылки одним сообщением. Для отмены — /cancel.")


@dp.message(AdminFlow.broadcast_text, Command("cancel"))
@dp.message(AdminFlow.confirming, Command("cancel"))
async def cancel_admin_action(message: Message, state: FSMContext):
    if admin_message_allowed(message):
        await state.clear()
        await message.reply("Действие отменено.", reply_markup=admin_keyboard())


@dp.message(AdminFlow.broadcast_text, F.text & ~F.text.startswith("/"))
async def admin_broadcast_preview(message: Message, state: FSMContext):
    if not admin_message_allowed(message):
        await state.clear()
        return
    text = message.text
    if not text.strip() or len(text.encode("utf-16-le")) // 2 > 4096:
        await message.reply("Введите текст рассылки длиной до 4096 символов или /cancel.")
        return
    count = len(get_all_users())
    if not count:
        await state.clear()
        await message.reply("📭 Получателей пока нет.", reply_markup=admin_keyboard())
        return
    token = secrets.token_hex(8)
    await state.set_data({"token": token, "action": "broadcast", "text": text})
    await state.set_state(AdminFlow.confirming)
    preview = text if len(text) <= 800 else text[:800] + "…"
    await message.reply(
        f"📣 Предпросмотр рассылки · пользователей в базе: {count}\n\n{preview}",
        reply_markup=admin_confirm_keyboard(token, "Отправить всем"),
    )


async def send_admin_broadcast(telegram_bot, users, text):
    sent = failed = 0
    for user in users:
        chat_id = user["chat_id"]
        try:
            try:
                await telegram_bot.send_message(chat_id, text, parse_mode=None)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                await telegram_bot.send_message(chat_id, text, parse_mode=None)
        except TelegramAPIError:
            failed += 1
        else:
            sent += 1
        await asyncio.sleep(0.05)
    return sent, failed


@dp.callback_query(F.data.startswith("admin_confirm|"))
async def admin_confirm(callback: CallbackQuery, state: FSMContext):
    if not await admin_callback_allowed(callback):
        return
    data = await state.get_data()
    if (await state.get_state() != AdminFlow.confirming.state
            or data.get("token") != callback.data.split("|", 1)[1]):
        await callback.answer("Подтверждение уже использовано или отменено.")
        return
    action = data["action"]
    if action == "broadcast" and _broadcast_lock.locked():
        await callback.answer("Уже идёт другая рассылка. Повторите чуть позже.")
        return
    await state.clear()
    await callback.answer()
    if action == "broadcast":
        async with _broadcast_lock:
            users = get_all_users()
            await callback.message.edit_text("📣 Рассылка запущена.")
            sent, failed = await send_admin_broadcast(callback.bot, users, data["text"])
        result = f"📣 Рассылка завершена. Отправлено: {sent}. Не доставлено: {failed}."
    else:
        chat_id = data["chat_id"]
        if action == "clean":
            changed = clean_user_data(chat_id)
            result = f"🧹 Данные пользователя {chat_id} удалены. Учётная запись сохранена."
        elif action == "delete":
            changed = delete_user(chat_id)
            result = f"🗑 Пользователь {chat_id} удалён вместе с данными."
        else:
            return
        if not changed:
            result = "Пользователь не найден."
    await callback.message.edit_text(result, reply_markup=admin_keyboard())


# --- Bot commands setup ---
async def set_commands():
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Начать работу"),
        types.BotCommand(command="add_router", description="Добавить роутер"),
        types.BotCommand(command="routers", description="Мои роутеры"),
        types.BotCommand(command="devices", description="Wi-Fi устройства"),
        types.BotCommand(command="settings", description="Настройки уведомлений"),
        types.BotCommand(command="guide", description="Инструкция"),
        types.BotCommand(command="history", description="История событий (24ч)"),
        types.BotCommand(command="health", description="Статус здоровья роутеров"),
        types.BotCommand(command="admin", description="Админ-панель"),
    ])


if __name__ == "__main__":
    async def main():
        init_db()
        for aid in ADMIN_IDS:
            set_admin(aid, True)
        await set_commands()
        logging.info("NetWatch bot starting...")
        await dp.start_polling(bot)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
