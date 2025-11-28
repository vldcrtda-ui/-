import asyncio
import html
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from uuid import uuid4

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

DATA_FILE = Path("data.json")
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "bot.log"

load_dotenv()


def setup_logging() -> logging.Logger:
    handlers = [logging.StreamHandler()]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("moderation-bot")


logger = setup_logging()


def parse_chat_identifier(raw: str, field: str):
    value = (raw or "").strip()
    if value.startswith("http"):
        parsed = urlparse(value)
        tail = (parsed.path or "").split("/")[-1]
        if tail.startswith("+"):
            raise RuntimeError(f"{field} должен быть @username или числом (-100...), не invite-link.")
        value = tail or value
    if value.startswith("@"):
        return value
    if value.lstrip("-").isdigit():
        return int(value)
    return f"@{value}"


def load_config() -> Dict[str, Any]:
    token = os.environ.get("BOT_TOKEN")
    mod_chat = os.environ.get("MOD_CHAT_ID")
    public_chat = os.environ.get("PUBLIC_CHAT_ID")
    main_admin = os.environ.get("MAIN_ADMIN_ID")
    missing = [name for name, val in {
        "BOT_TOKEN": token,
        "MOD_CHAT_ID": mod_chat,
        "PUBLIC_CHAT_ID": public_chat,
        "MAIN_ADMIN_ID": main_admin,
    }.items() if not val]
    if missing:
        raise RuntimeError(f"Заполните переменные окружения: {', '.join(missing)}")
    return {
        "token": token,
        "mod_chat_id": parse_chat_identifier(mod_chat, "MOD_CHAT_ID"),
        "public_chat_id": parse_chat_identifier(public_chat, "PUBLIC_CHAT_ID"),
        "main_admin_id": int(main_admin),
    }


def load_state(main_admin_id: int) -> Dict[str, Any]:
    if not DATA_FILE.exists():
        return {"admins": [main_admin_id], "pending": {}, "last_sent": {}}
    with DATA_FILE.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    admins = set(data.get("admins") or [])
    admins.add(main_admin_id)
    return {
        "admins": list(admins),
        "pending": data.get("pending") or {},
        "last_sent": data.get("last_sent") or {},
    }


def save_state(state: Dict[str, Any]) -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)


STATE: Dict[str, Any] = {}
STATE_LOCK = asyncio.Lock()
CONFIG: Dict[str, Any] = {}


def moderator_only(user_id: int) -> bool:
    return user_id in STATE.get("admins", [])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Отправь сообщение, фото или видео - я передам его на модерацию.\n"
        "Используй /anon <текст>, если хочешь опубликовать анонимно."
    )


async def anon_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("После /anon укажи текст сообщения.")
        return
    text = " ".join(context.args).strip()
    await queue_message(update, context, text, force_anon=True)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Пустые сообщения не отправляются.")
        return
    await queue_message(update, context, text, force_anon=False)


async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return
    caption = (update.message.caption or "").strip()
    force_anon = False
    if caption.startswith("/anon"):
        force_anon = True
        caption = caption[len("/anon"):].lstrip()
    photo_id = update.message.photo[-1].file_id
    await queue_message(
        update,
        context,
        text=caption,
        force_anon=force_anon,
        message_type="photo",
        photo_id=photo_id,
    )


async def video_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.video:
        return
    caption = (update.message.caption or "").strip()
    force_anon = False
    if caption.startswith("/anon"):
        force_anon = True
        caption = caption[len("/anon"):].lstrip()
    video_id = update.message.video.file_id
    await queue_message(
        update,
        context,
        text=caption,
        force_anon=force_anon,
        message_type="video",
        video_id=video_id,
    )


async def queue_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    force_anon: bool,
    message_type: str = "text",
    photo_id: Optional[str] = None,
    video_id: Optional[str] = None,
) -> None:
    user = update.effective_user
    if not user:
        return
    request_id = str(uuid4())
    now = int(time.time())
    async with STATE_LOCK:
        last_sent = STATE.get("last_sent", {})
        last = last_sent.get(str(user.id))
        if last and now - last < 60:
            wait_for = 60 - (now - last)
            await update.message.reply_text(
                f"Можно отправлять одно сообщение в минуту. Подождите еще {wait_for} сек."
            )
            return
        STATE["last_sent"] = last_sent
    entry = {
        "type": message_type,
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "text": text,
        "force_anon": force_anon,
    }
    if message_type == "photo" and photo_id:
        entry["photo_id"] = photo_id
    if message_type == "video" and video_id:
        entry["video_id"] = video_id
    async with STATE_LOCK:
        STATE["last_sent"][str(user.id)] = now
        STATE["pending"][request_id] = entry
        save_state(STATE)
    await update.message.reply_text(
        "Отправлено на модерацию. После одобрения сообщение появится в канале."
    )
    await send_to_moderators(context, request_id, entry)


async def send_to_moderators(
    context: ContextTypes.DEFAULT_TYPE,
    request_id: str,
    entry: Dict[str, Any],
) -> None:
    username = entry.get("username")
    name_parts = [entry.get("first_name"), entry.get("last_name")]
    name = " ".join([p for p in name_parts if p]) or "пользователь"
    username_label = f"@{username}" if username else "без username"
    requested_anon = "да" if entry.get("force_anon") else "нет"
    header = (
        f"Новое сообщение #{request_id}\n"
        f"От: <a href=\"tg://user?id={entry['user_id']}\">{html.escape(name)}</a> "
        f"({html.escape(username_label)}, id={entry['user_id']})\n"
        f"Анонимность запрошена: {requested_anon}"
    )
    body = html.escape(entry.get("text") or "")
    full_text = f"{header}\n\n{body}" if body else header
    buttons = [
        [InlineKeyboardButton("Опубликовать", callback_data=f"approve:{request_id}")],
        [InlineKeyboardButton("Отклонить", callback_data=f"reject:{request_id}")],
    ]
    if entry.get("type") == "photo" and entry.get("photo_id"):
        await context.bot.send_photo(
            chat_id=CONFIG["mod_chat_id"],
            photo=entry["photo_id"],
            caption=full_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )
    elif entry.get("type") == "video" and entry.get("video_id"):
        await context.bot.send_video(
            chat_id=CONFIG["mod_chat_id"],
            video=entry["video_id"],
            caption=full_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )
    else:
        await context.bot.send_message(
            chat_id=CONFIG["mod_chat_id"],
            text=full_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    actor = query.from_user
    origin_chat_id = query.message.chat_id if query.message else None
    is_mod_chat = origin_chat_id == CONFIG.get("mod_chat_id")
    if not (moderator_only(actor.id) or is_mod_chat):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    data = (query.data or "").split(":")
    if len(data) < 2:
        return
    action, request_id = data[0], data[1]

    async with STATE_LOCK:
        entry = STATE.get("pending", {}).pop(request_id, None)
        if entry:
            save_state(STATE)

    if not entry:
        await query.answer("Заявка уже обработана", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if action == "approve":
        await publish(entry, context)
        await query.edit_message_reply_markup(reply_markup=None)
        status_note = f"\n\nСтатус: одобрено модератором {actor.id}"
        try:
            if query.message and query.message.caption:
                await query.edit_message_caption((query.message.caption or "") + status_note, reply_markup=None)
            elif query.message and query.message.text:
                await query.edit_message_text((query.message.text or "") + status_note, reply_markup=None)
            else:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            await query.edit_message_reply_markup(reply_markup=None)
    elif action == "reject":
        await notify_reject(context, entry)
        await query.edit_message_reply_markup(reply_markup=None)
        status_note = f"\n\nСтатус: отклонено модератором {actor.id}"
        try:
            if query.message and query.message.caption:
                await query.edit_message_caption((query.message.caption or "") + status_note, reply_markup=None)
            elif query.message and query.message.text:
                await query.edit_message_text((query.message.text or "") + status_note, reply_markup=None)
            else:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            await query.edit_message_reply_markup(reply_markup=None)


async def publish(
    entry: Dict[str, Any],
    context: ContextTypes.DEFAULT_TYPE,
    with_signature: bool = False,  # сохранено для совместимости
) -> None:
    text = entry.get("text") or ""
    if entry.get("type") == "photo" and entry.get("photo_id"):
        await context.bot.send_photo(
            chat_id=CONFIG["public_chat_id"],
            photo=entry["photo_id"],
            caption=text or None,
        )
    elif entry.get("type") == "video" and entry.get("video_id"):
        await context.bot.send_video(
            chat_id=CONFIG["public_chat_id"],
            video=entry["video_id"],
            caption=text or None,
        )
    else:
        await context.bot.send_message(chat_id=CONFIG["public_chat_id"], text=text or "-")
    try:
        await context.bot.send_message(
            chat_id=entry["user_id"],
            text="Ваше сообщение опубликовано. Спасибо!",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось уведомить автора: %s", exc)


async def notify_reject(context: ContextTypes.DEFAULT_TYPE, entry: Dict[str, Any]) -> None:
    try:
        await context.bot.send_message(
            chat_id=entry["user_id"],
            text="Сообщение отклонено модератором.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to notify author: %s", exc)


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != CONFIG["main_admin_id"]:
        await update.message.reply_text("Команда доступна только главному администратору.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /add_admin <id>")
        return
    try:
        new_admin = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")
        return
    async with STATE_LOCK:
        admins = set(STATE.get("admins", []))
        admins.add(new_admin)
        STATE["admins"] = list(admins)
        save_state(STATE)
    await update.message.reply_text(f"Администратор {new_admin} добавлен.")


async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != CONFIG["main_admin_id"]:
        await update.message.reply_text("Команда доступна только главному администратору.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /remove_admin <id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")
        return
    if target == CONFIG["main_admin_id"]:
        await update.message.reply_text("Нельзя удалить главного администратора.")
        return
    async with STATE_LOCK:
        admins = set(STATE.get("admins", []))
        admins.discard(target)
        STATE["admins"] = list(admins)
        save_state(STATE)
    await update.message.reply_text(f"Администратор {target} удален.")


async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not moderator_only(update.effective_user.id):
        await update.message.reply_text("Недостаточно прав.")
        return
    admins = ", ".join(str(a) for a in sorted(STATE.get("admins", [])))
    await update.message.reply_text(f"Текущие администраторы: {admins}")


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not moderator_only(update.effective_user.id):
        await update.message.reply_text("Недостаточно прав.")
        return
    count = len(STATE.get("pending", {}))
    await update.message.reply_text(f"Заявок в очереди: {count}")


def main() -> None:
    global CONFIG, STATE
    CONFIG = load_config()
    STATE = load_state(CONFIG["main_admin_id"])
    application = Application.builder().token(CONFIG["token"]).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("anon", anon_command))
    application.add_handler(CommandHandler("add_admin", add_admin))
    application.add_handler(CommandHandler("remove_admin", remove_admin))
    application.add_handler(CommandHandler("admins", list_admins))
    application.add_handler(CommandHandler("pending", pending))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.VIDEO, video_message))
    application.add_handler(MessageHandler(filters.PHOTO, photo_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    logger.info("Бот запущен")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
