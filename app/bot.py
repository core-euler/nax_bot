import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ChatMemberUpdated,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from storage import bind_chat, is_bound, log_message, load_chats, read_last_24h, read_last_n
from comet import CometClient
from prompts import (
    SYSTEM_PROMPT,
    build_nax_prompt,
    build_reply_prompt,
    build_find_prompt,
    build_daily_digest_prompt,
    build_web_themes_prompt,
)
from config import (
    BOT_TOKEN,
    COMET_API_TOKEN,
    COMET_MODEL,
    TZ as TZ_NAME,
    ALLOWED_CHAT_IDS,
    BOT_COOLDOWN_SECONDS,
    WEB_DIGEST_HOUR,
    WEB_DIGEST_MINUTE,
)

TZ = ZoneInfo(TZ_NAME)

if not BOT_TOKEN or not COMET_API_TOKEN:
    raise RuntimeError("Set BOT_TOKEN and COMET_API_TOKEN in .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("porfiriy")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
comet = CometClient(COMET_API_TOKEN, model=COMET_MODEL)

LAST_CALL: dict[int, float] = {}
THREAD_DEPTH: dict[tuple[int, int], int] = {}


# ---------------------------------------------------------------------------
# Личка — команды
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Привязать чат", callback_data="bind_chat")
    ]])
    await message.answer(
        "Добавь меня в группу как администратора, затем напиши /bind прямо в той группе.\n"
        "Или нажми кнопку и перешли сообщение из группы сюда (работает только если "
        "у отправителя открытая пересылка).\n\nКоманда вызова в чате: /nax",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "bind_chat")
async def bind_button(callback: CallbackQuery):
    await callback.message.answer(
        "Перешли мне любое сообщение из группы.\n"
        "Если не работает — напиши /bind прямо в той группе."
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Авто-привязка при добавлении бота в группу
# ---------------------------------------------------------------------------

@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    new_status = event.new_chat_member.status
    logger.info(
        "my_chat_member: chat=%s (%s) new_status=%s",
        chat.id, chat.title, new_status,
    )
    if new_status in {"member", "administrator"}:
        if ALLOWED_CHAT_IDS and chat.id not in ALLOWED_CHAT_IDS:
            logger.warning("my_chat_member: chat %s not in ALLOWED_CHAT_IDS, skip", chat.id)
            return
        bind_chat(chat.id, chat.title)
        logger.info("Auto-bound chat %s (%s) via my_chat_member", chat.id, chat.title)
        try:
            await bot.send_message(
                chat.id,
                f"Привязан. chat_id={chat.id}. Зови через /nax."
            )
        except Exception:
            logger.exception("Failed to send welcome to chat %s", chat.id)
    elif new_status in {"left", "kicked", "restricted"}:
        logger.info("Bot removed from chat %s (%s)", chat.id, chat.title)


# ---------------------------------------------------------------------------
# Привязка через /bind прямо в группе
# ---------------------------------------------------------------------------

@dp.message(Command("bind"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_bind_in_group(message: Message):
    chat = message.chat
    logger.info("cmd_bind_in_group: chat=%s (%s)", chat.id, chat.title)
    if ALLOWED_CHAT_IDS and chat.id not in ALLOWED_CHAT_IDS:
        logger.warning("cmd_bind_in_group: chat %s not in ALLOWED_CHAT_IDS", chat.id)
        await message.reply("Этот чат не в списке разрешённых.")
        return
    bind_chat(chat.id, chat.title)
    await message.reply(f"Привязан. chat_id={chat.id}. Зови через /nax.")


# ---------------------------------------------------------------------------
# Привязка через forward в личке (legacy + новый API)
# ---------------------------------------------------------------------------

async def _bind_chat_from_forward(message: Message, chat_id: int, title: str | None):
    bind_chat(chat_id, title)
    logger.info("Chat bound via forward: %s (%s)", title, chat_id)
    await message.answer(f"Готово. Привязал чат: {title or chat_id} ({chat_id})")


@dp.message(F.forward_from_chat)
async def bind_by_forward_legacy(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        src = message.forward_from_chat
        logger.info(
            "bind_by_forward_legacy: src.id=%s src.type=%s src.title=%r",
            src.id, src.type, src.title,
        )
        if src.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.answer("Нужен forward именно из группы.")
            return
        await _bind_chat_from_forward(message, src.id, src.title)
    except Exception:
        logger.exception("bind_by_forward_legacy failed")
        await message.answer("Ошибка при обработке forward (legacy). Смотри логи.")


@dp.message(F.chat.type == ChatType.PRIVATE, F.forward_origin)
async def bind_by_forward_new(message: Message):
    try:
        origin = getattr(message, "forward_origin", None)
        logger.info(
            "bind_by_forward_new: has_origin=%s origin_type=%s msg_text=%r",
            origin is not None,
            type(origin).__name__ if origin else "—",
            (message.text or "")[:80],
        )
        if not origin:
            return

        src_chat = getattr(origin, "chat", None)
        logger.info(
            "bind_by_forward_new: src_chat=%s src_chat_type=%s",
            getattr(src_chat, "id", None),
            getattr(src_chat, "type", None),
        )
        if src_chat and src_chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await _bind_chat_from_forward(message, src_chat.id, src_chat.title)
            return

        # MessageOriginHiddenUser или MessageOriginUser — chat_id не доступен
        logger.warning(
            "bind_by_forward_new: origin_type=%s — cannot extract chat_id",
            type(origin).__name__,
        )
        await message.answer(
            f"Не могу получить chat_id из этого forward (тип: {type(origin).__name__}).\n"
            "Telegram скрывает источник из-за настроек приватности отправителя.\n\n"
            "Используй команду /bind прямо в группе — это надёжнее."
        )
    except Exception:
        logger.exception("bind_by_forward_new failed")
        await message.answer("Ошибка при обработке forward. Смотри логи.")


@dp.message(F.chat.type == ChatType.PRIVATE, ~F.text.regexp(r"^/"))
async def private_fallback(message: Message):
    logger.info(
        "private_fallback (unhandled): text=%r has_forward_origin=%s "
        "has_forward_from_chat=%s forward_origin_type=%s",
        (message.text or "")[:80],
        getattr(message, "forward_origin", None) is not None,
        message.forward_from_chat is not None,
        type(getattr(message, "forward_origin", None)).__name__,
    )


# ---------------------------------------------------------------------------
# Ручной веб-поиск
# ---------------------------------------------------------------------------

def _is_find_command(text: str) -> bool:
    if not text:
        return False
    first = text.split(maxsplit=1)[0].lower()
    return first == "/find" or first.startswith("/find@")


async def _handle_find(message: Message):
    started_at = datetime.now().timestamp()
    logger.info(
        "cmd_find.enter chat=%s type=%s user=%s text=%r",
        message.chat.id,
        message.chat.type,
        message.from_user.id if message.from_user else "unknown",
        (message.text or "")[:120],
    )
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
            logger.warning("cmd_find.blocked reason=not_allowed chat=%s", message.chat.id)
            await message.reply("Этот чат не в списке разрешённых для /find.")
            return
        if not is_bound(message.chat.id):
            logger.warning("cmd_find.blocked reason=not_bound chat=%s", message.chat.id)
            await message.reply("Сначала привяжи чат: /bind")
            return

    text = message.text or ""
    parts = text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query and message.reply_to_message:
        query = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
        logger.info("cmd_find.query_from_reply chat=%s query=%r", message.chat.id, query[:120])
    if not query:
        logger.warning("cmd_find.blocked reason=empty_query chat=%s", message.chat.id)
        await message.reply("Используй: /find <запрос> или реплай на сообщение с /find")
        return
    logger.info("cmd_find.query_ready chat=%s query=%r", message.chat.id, query[:200])

    prompt = build_find_prompt(query)
    try:
        logger.info("cmd_find.search_start chat=%s", message.chat.id)
        result = await comet.web_search(prompt)
        elapsed_ms = int((datetime.now().timestamp() - started_at) * 1000)
        logger.info(
            "cmd_find.search_ok chat=%s result_len=%s elapsed_ms=%s",
            message.chat.id,
            len(result),
            elapsed_ms,
        )
        await message.reply(result[:4000], disable_web_page_preview=True)
        logger.info("cmd_find.reply_sent chat=%s reply_len=%s", message.chat.id, min(len(result), 4000))
    except Exception as e:
        elapsed_ms = int((datetime.now().timestamp() - started_at) * 1000)
        logger.exception("cmd_find failed in chat %s", message.chat.id)
        logger.error("cmd_find.search_fail chat=%s elapsed_ms=%s error=%r", message.chat.id, elapsed_ms, e)
        await message.reply(f"Поиск сломался: {e}")


@dp.message(Command("find"))
@dp.message(F.text.regexp(r"^/find(?:@[A-Za-z0-9_]+)?(?:\s+|$)"))
async def cmd_find(message: Message):
    await _handle_find(message)


# ---------------------------------------------------------------------------
# Групповой слушатель — /nax и логирование
# ---------------------------------------------------------------------------

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_listener(message: Message):
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        return
    if not is_bound(message.chat.id):
        return

    text = message.text or message.caption or ""
    if text:
        user = message.from_user.full_name if message.from_user else "unknown"
        log_message(message.chat.id, user, text)

    if _is_find_command(text):
        await _handle_find(message)
        return

    is_nax = text.startswith("/nax")
    is_reply_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.id == bot.id
        and bool(text)
    )

    if not is_nax and not is_reply_to_bot:
        return

    now_ts = datetime.now().timestamp()
    last = LAST_CALL.get(message.chat.id, 0)
    if now_ts - last < BOT_COOLDOWN_SECONDS:
        wait_s = int(BOT_COOLDOWN_SECONDS - (now_ts - last))
        logger.info("Cooldown hit in chat %s, wait=%ss", message.chat.id, wait_s)
        await message.reply(f"Остынь. Следующий вызов через {wait_s} сек.")
        return
    LAST_CALL[message.chat.id] = now_ts

    recent = read_last_n(message.chat.id, n=10)
    context_block = ""
    if recent:
        lines = "\n".join(f"  {r['user']}: {r['text']}" for r in recent)
        context_block = f"Последние сообщения в чате:\n{lines}\n\n"

    user_id = message.from_user.id if message.from_user else 0
    thread_key = (message.chat.id, user_id)

    if is_nax:
        # Новый вызов /nax — сбрасываем тред и считаем как первый ход.
        THREAD_DEPTH[thread_key] = 1
        thread_depth = 1

        target = text.replace("/nax", "", 1).strip()
        if not target and message.reply_to_message:
            target = message.reply_to_message.text or message.reply_to_message.caption or ""
        if not target:
            await message.reply("Дай текст после /nax или ответь реплаем на сообщение.")
            return
        prompt = build_nax_prompt(context_block, target, thread_depth)
    else:
        THREAD_DEPTH[thread_key] = THREAD_DEPTH.get(thread_key, 1) + 1
        thread_depth = THREAD_DEPTH[thread_key]

        bot_msg = message.reply_to_message.text or message.reply_to_message.caption or ""
        prompt = build_reply_prompt(context_block, bot_msg, text, thread_depth)

    try:
        logger.info(
            "reply triggered in chat %s by user %s (nax=%s, reply_to_bot=%s)",
            message.chat.id,
            message.from_user.id if message.from_user else "unknown",
            is_nax,
            is_reply_to_bot,
        )
        answer = await comet.chat(SYSTEM_PROMPT, prompt)
        await message.reply(answer[:4000])
    except Exception as e:
        logger.exception("reply handler failed in chat %s", message.chat.id)
        await message.reply(f"Что-то пошло не так: {e}")


# ---------------------------------------------------------------------------
# Ежедневный дайджест
# ---------------------------------------------------------------------------

async def daily_digest():
    chats = load_chats()
    for cid_str, meta in chats.items():
        cid = int(cid_str)
        if ALLOWED_CHAT_IDS and cid not in ALLOWED_CHAT_IDS:
            continue
        rows = read_last_24h(cid)
        if not rows:
            continue
        sample = "\n".join([f"- {r['user']}: {r['text']}" for r in rows[-200:]])
        prompt = build_daily_digest_prompt(sample)
        try:
            logger.info("Daily digest for chat %s (%s messages)", cid, len(rows))
            text = await comet.chat(SYSTEM_PROMPT, prompt)
            await bot.send_message(cid, f"🕕 Дневной разбор Накса\n\n{text[:3900]}")
        except Exception as e:
            logger.exception("Daily digest failed for chat %s", cid)
            await bot.send_message(cid, f"Не смог собрать разбор: {e}")


# ---------------------------------------------------------------------------
# Веб-дайджест горячих тем в 12:00
# ---------------------------------------------------------------------------

async def daily_web_themes_digest():
    chats = load_chats()
    for cid_str, meta in chats.items():
        cid = int(cid_str)
        if ALLOWED_CHAT_IDS and cid not in ALLOWED_CHAT_IDS:
            continue

        rows = read_last_24h(cid)
        if not rows:
            continue

        sample = "\n".join([f"- {r['user']}: {r['text']}" for r in rows[-250:]])
        prompt = build_web_themes_prompt(sample)
        try:
            logger.info("Web themes digest for chat %s (%s messages)", cid, len(rows))
            text = await comet.web_search(prompt)
            await bot.send_message(
                cid,
                f"🔥 Горячие темы дня + веб-разнос от Накса\n\n{text[:3900]}",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.exception("Web digest failed for chat %s", cid)
            await bot.send_message(cid, f"Не смог сделать веб-дайджест: {e}")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def main():
    logger.info("Starting Porfiriy bot...")
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(daily_digest, "cron", hour=18, minute=0)
    scheduler.add_job(daily_web_themes_digest, "cron", hour=WEB_DIGEST_HOUR, minute=WEB_DIGEST_MINUTE)
    scheduler.start()
    logger.info(
        "Scheduler started (daily digest at 18:00 %s, web digest at %02d:%02d %s)",
        TZ,
        WEB_DIGEST_HOUR,
        WEB_DIGEST_MINUTE,
        TZ,
    )
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
