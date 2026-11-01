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

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
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
@dp.message(Command("add_router"))
async def cmd_add_router(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    code = secrets.token_hex(4)  # 8 hex chars = 4 bytes
    create_pairing_code(code, message.from_user.id, expires_sec=900)

    github_agent_url = "https://raw.githubusercontent.com/zayacserj/netwatch-agent/master/deploy_files/agent3_fixed.sh"
    pi_url = os.getenv("PI_URL", "http://192.168.10.222:8080")
    pi_url_public = os.getenv("PI_URL_PUBLIC", "http://95.31.29.233:8080")
    
    text = (
        f"🔐 Код подключения роутера:\n\n"
        f"`{code}`\n\n"
        f"Действует 15 минут.\n\n"
        f"🚀 Быстрая установка (локальная сеть):\n"
        f"```\n"
        f"curl -sS {github_agent_url} -o /tmp/agent3.sh && chmod +x /tmp/agent3.sh && /tmp/agent3.sh pair --code {code} --url {pi_url}\n"
        f"```\n\n"
        f"🌐 Для роутеров в другой сети (интернет):\n"
        f"```\n"
        f"curl -sS {github_agent_url} -o /tmp/agent3.sh && chmod +x /tmp/agent3.sh && /tmp/agent3.sh pair --code {code} --url {pi_url_public}\n"
        f"```\n"
    )
    await message.reply(text)


# --- /routers ---
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
        builder.button(text=f"🗑 {r['name'] or r['router_id'][:10]}", callback_data=f"del_router_{r['router_id']}")
    await message.reply("\n".join(lines), parse_mode="Markdown", reply_markup=builder.as_markup())


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
        parse_mode="Markdown",
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
    await callback.message.edit_text(rm_cmd, parse_mode="Markdown")
    await callback.answer("✅ Роутер удалён")


@dp.callback_query(F.data == "cancel_del")
async def callback_cancel_delete(callback: CallbackQuery):
    await callback.message.edit_text("❌ Удаление отменено")
    await callback.answer()


# --- /devices ---
@dp.message(Command("devices"))
async def cmd_devices(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    routers = get_user_routers(message.from_user.id)
    if not routers:
        await message.reply("📭 У вас нет роутеров. Добавьте: /add_router")
        return
    builder = InlineKeyboardBuilder()
    lines = ["📱 *Мои Wi-Fi устройства:*"]
    seen_macs = set()
    for r in routers:
        router_name = r["name"] or r["router_id"]
        devs = get_user_devices(message.from_user.id, r["router_id"])
        if not devs:
            continue
        lines.append(f"\n🔹 *{router_name}:*")
        for d in devs:
            mac_key = d["mac"].lower()
            if mac_key in seen_macs:
                continue
            seen_macs.add(mac_key)
            status = "🟢" if d["online"] else "🔴"
            name = d["custom_name"] or d["hostname"] or "неизвестно"
            lines.append(f"{status} `{d['mac']}` — {name}")
            builder.button(
                text=f"{status} {name or d['mac']} ({d['mac']}) [{router_name}]{' 🔔' if d.get('watch_online', 1) else ' 🔕'}",
                callback_data=f"device_detail|{d['mac']}|{r['router_id']}"
            )
    if not lines[-1].startswith("🔹"):
        lines.append("Нет устройств.")
    builder.adjust(1)
    await message.reply("\n".join(lines), parse_mode="Markdown", reply_markup=builder.as_markup())


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
    name = device["custom_name"] or device["hostname"] or "неизвестно"
    
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
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()


# --- Device back to list ---
@dp.callback_query(F.data == "device_back_list")
async def callback_device_back(callback: CallbackQuery):
    devices = get_user_devices(callback.from_user.id)
    if not devices:
        await callback.message.edit_text("📭 Нет Wi-Fi устройств.")
        await callback.answer()
        return
    builder = InlineKeyboardBuilder()
    lines = ["📱 *Мои Wi-Fi устройства:*"]
    for d in devices:
        status = "🟢" if d["online"] else "🔴"
        name = d["custom_name"] or d["hostname"] or "неизвестно"
        router_name = d.get("router_name", "н/д")
        lines.append(f"{status} `{d['mac']}` — {name} ({router_name})")
        builder.button(
            text=f"{status} {name or d['mac']} ({d['mac']}) [{router_name}]{' 🔔' if d.get('watch_online', 1) else ' 🔕'}",
            callback_data=f"device_detail|{d['mac']}|{d['router_id']}"
        )
    builder.adjust(1)
    await callback.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=builder.as_markup())
    await callback.answer()


# --- Device rename: ask for new name ---
@dp.callback_query(F.data.startswith("device_rename|"))
async def callback_device_rename(callback: CallbackQuery):
    parts = callback.data.split("|")
    mac = parts[1]
    router_id = parts[2]
    
    device = get_device(callback.from_user.id, mac.lower(), router_id)
    if not device:
        await callback.answer("⛔ Нет доступа")
        return
    
    await callback.message.edit_text(
        f"✏️ Введите новое имя для устройства `{mac}`:\n\n"
        f"Отправьте сообщение с новым именем.",
        parse_mode="Markdown"
    )
    # Store state: user is waiting to rename this device
    callback.from_user._pending_rename = {"mac": mac, "router_id": router_id}
    await callback.answer()


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
        name = device["custom_name"] or device["hostname"] or "неизвестно"
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
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())


# --- Handle text input for rename ---
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_pending_rename(message: Message):
    user = message.from_user
    pending = getattr(user, "_pending_rename", None)
    if not pending:
        return
    
    mac = pending["mac"]
    router_id = pending["router_id"]
    new_name = message.text.strip()
    
    if not new_name:
        await message.reply("❌ Имя не может быть пустым")
        return
    
    set_device_custom_name(mac, router_id, new_name)
    user._pending_rename = None
    
    await message.reply(f"✅ Устройство `{mac}` переименовано в *{new_name}*", parse_mode="Markdown")


# --- /settings ---
@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    devices = get_user_devices(message.from_user.id)
    if not devices:
        await message.reply("📭 Нет устройств для настройки.")
        return
    builder = InlineKeyboardBuilder()
    for d in devices:
        watch = "🔔 Отключить" if d.get("watch_online", 1) else "🔔 Включить"
        builder.button(text=f"{d['mac']} — {watch}", callback_data=f"toggle_notify|{d['mac']}|{d['router_id']}")
    await message.reply("⚙️ Настройка уведомлений:", reply_markup=builder.as_markup())


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
    
    # Refresh settings list
    devices = get_user_devices(callback.from_user.id)
    builder = InlineKeyboardBuilder()
    for d in devices:
        watch = "🔔 Отключить" if d.get("watch_online", 1) else "🔔 Включить"
        builder.button(text=f"{d['mac']} — {watch}", callback_data=f"toggle_notify|{d['mac']}|{d['router_id']}")
    builder.adjust(1)
    await callback.message.edit_text("⚙️ Настройка уведомлений:", reply_markup=builder.as_markup())


# --- /guide ---
@dp.message(Command("guide"))
async def cmd_guide(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text = (
        "📖 *Инструкция NetWatch*\n\n"
        "1. Добавьте роутер: /add_router\n"
        "2. Подключитесь к роутеру по SSH и выполните команду с кодом\n"
        "3. Через 1 минуту роутер появится в списке: /routers\n"
        "4. Просматривайте устройства: /devices\n"
        "5. История событий: /history\n"
        "6. Статус здоровья: /health\n"
    )
    await message.reply(text, parse_mode="Markdown")


# --- /history ---
@dp.message(Command("history"))
async def cmd_history(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    events = get_user_events(message.from_user.id, hours=24)
    if not events:
        await message.reply("📭 Нет событий за последние 24 часа.")
        return
    lines = ["📜 *История событий (24ч):*"]
    for e in events[:50]:
        t = fmt_time(e["timestamp"])
        evt = "🟢 подключился" if "CONNECTED" in e["event"].upper() else "🔴 отключился"
        lines.append(f"{t} — {e.get('router_name', e['router_id'])}: {e['mac']} {evt}")
    await message.reply("\n".join(lines), parse_mode="Markdown")


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
            pk = "✅" if h["podkop_test"] else "⚠️"
            lines.append(
                f"• {r['name'] or r['router_id']}:\n"
                f"  Интернет: {net} | Wi-Fi: {wf} | CPU: {h['cpu'] or 'н/д'} | "
                f"RAM: {h['ram'] or 'н/д'} | Клиенты: {h['wifi_clients']}\n"
                f"  Podkop: {h['podkop_status'] or 'н/д'} (test: {pk})"
            )
        else:
            lines.append(f"• {r['name'] or r['router_id']}: ⏳ нет данных о health-check")
    await message.reply("\n".join(lines), parse_mode="Markdown")


# --- /admin ---
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Доступ запрещён. Только для администратора.")
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Список пользователей", callback_data="admin_list_users")
    builder.button(text="🧹 Очистить данные пользователя", callback_data="admin_clean_prompt")
    await message.reply("🔐 Админ-панель:\nВыберите действие:", reply_markup=builder.as_markup())


# --- Admin callbacks ---
@dp.callback_query(F.data == "admin_list_users")
async def admin_list_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа")
        return
    users = get_all_users()
    if not users:
        await callback.message.edit_text("📭 Нет пользователей.")
        await callback.answer()
        return
    lines = ["👥 *Все пользователи:*"]
    for u in users:
        admin_tag = " (👑 админ)" if u["is_admin"] else ""
        lines.append(f"`{u['chat_id']}` {u['username'] or ''} {u['first_name'] or ''}{admin_tag}")
    lines.append("\nУдалить пользователя:\n`/admin_clean_user <chat_id> del`")
    lines.append("Очистить данные:\n`/admin_clean_user <chat_id> clean`")
    await callback.message.edit_text("\n".join(lines), parse_mode="Markdown")
    await callback.answer()


@dp.message(F.text.regexp(r"^/admin_clean_user\s+\d+\s+(del|clean)$"))
async def admin_clean_input(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    try:
        chat_id = int(parts[1])
        action = parts[2]
        if action == "del":
            delete_user(chat_id)
            await message.reply(f"✅ Пользователь {chat_id} удалён вместе с данными.")
        elif action == "clean":
            clean_user_data(chat_id)
            await message.reply(f"🧹 Данные пользователя {chat_id} очищены (роутеры, устройства, события, health-check).")
    except ValueError:
        await message.reply("❌ Неверный chat_id")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


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
