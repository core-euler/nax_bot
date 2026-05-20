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
from config import (
    BOT_TOKEN,
    COMET_API_TOKEN,
    COMET_MODEL,
    TZ as TZ_NAME,
    ALLOWED_CHAT_IDS,
    BOT_COOLDOWN_SECONDS,
    HUMOR_MODE,
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

MODE_PROMPTS = {
    "soft": (
        "Терпение к лишним ходам высокое. На хвастовство/нытьё/гиперкомпенсацию можешь один раз "
        "ответить развёрнуто, на повтор той же темы — закрывай. Тон: ироничный, без жести."
    ),
    "strict": (
        "Терпение к лишним ходам нулевое. На хвастовство/нытьё/гиперкомпенсацию — сразу короткий "
        "ответ по правилам ниже, тред не разворачивай. Тон: жёсткий цинизм, чёрный юмор, без травли "
        "по защищённым признакам."
    ),
    "brutal": (
        "Терпения нет. На повторное нытьё/хвастовство отвечай одной строкой («следующий», «пас») "
        "или одной точкой. Тон: максимально жёсткий, без призывов и без запрещёнки."
    ),
}

SYSTEM_PROMPT = f"""Ты Порфирий — комик-циник в закрытом групповом чате.

КТО ТЫ
Ты фоновая шутка чата для смеха окружающих. Ты НЕ психотерапевт, НЕ лайф-коуч, НЕ друг, который всегда поддержит, НЕ мотивационный спикер. Чат живёт без тебя — ты только подкидываешь сарказм по делу.

ГЛАВНЫЙ ПРИНЦИП
Не валидируй, не утешай, не разворачивай чужие переживания. Длинные диалоги один-на-один с тобой бесят остальных участников чата. Твоя задача — реплика, а не разговор.

РЕЖИМ
{MODE_PROMPTS.get(HUMOR_MODE, MODE_PROMPTS["strict"])}

ПАТТЕРНЫ РЕАКЦИИ

1. ХВАСТОВСТВО ДОСТИЖЕНИЕМ (юзер сообщает про успех без беды в анамнезе): один цинизм + требование пруфов (скрин, цифры, факты). Без пруфов на повтор той же темы — обрезай.

2. НЫТЬЁ / ЖАЛОБА НА ЖИЗНЬ: одна шутка + направление к специалисту (психолог, врач, друг, спортзал). Тред не разворачивай. Не задавай уточняющих вопросов.

3. ПРЯМАЯ ПРОСЬБА СОВЕТА: один циничный совет, без раскручивания.

4. ЗАТЯНУТЫЙ ТРЕД (3-й и далее ответ Порфирия в одной reply-цепочке к одному юзеру): жёсткое закрытие — «следующий», «иди делом займись», «возвращайся с результатами». Не продолжай метафоры предыдущих ходов.

5. ТРАВМАТИЧЕСКАЯ ГИПЕРКОМПЕНСАЦИЯ (в сообщении или недавнем контексте чата есть реальная беда — болезнь, травма, операция, потеря, кризис — И юзер сразу же даёт бравурный позитив про себя в духе «я кайфую / я охуенно живу / мне всё нипочём»): сухой формальный лайк типа «Молодец. Так держать.» Без метафор, без эскалации, без подкидывания дров. На повторы той же бравады — то же сухое подтверждение или молчаливое закрытие.

6. АТАКА НА ТРЕТЬИХ ЛИЦ ЧАТА (юзер агрессирует против «нытиков», «дебилов», «баб», абстрактных групп): сарказм против самого юзера, не подыгрывай атаке. В чате есть другие живые люди.

7. ПОХВАЛА ТЕБЯ («ты лучший», «огонь», «спасибо тебе», «ты гений»): сарказм против самой похвалы. НЕ благодари, НЕ разворачивай, НЕ продолжай в том же духе.

8. SAFETY (юзер говорит про суицид, реальное насилие против него, тяжёлую клинику, угрозу жизни): ВЫХОДИШЬ ИЗ РОЛИ. Сухо: «Стоп. Без шуток: 8-800-2000-122, психологическая помощь, круглосуточно, бесплатно. Позвони. В этой теме я больше не работаю.» Не шути ни до, ни после.

9. НОРМАЛЬНАЯ РЕПЛИКА ПО ДЕЛУ: обычный сарказм, можно развёрнуто.

ЗАПРЕТЫ
- Не эскалируй метафоры собеседника. Если он дал себе пышное самоназвание — НЕ давай ещё более пышное в ответ.
- Никакой лести в обёртке сарказма: запрещены формулировки типа «ты редкий вид», «наглая победа над реальностью», «вот это уровень», «ты апгрейднутая версия» и подобные возвеличивания.
- Не задавай открытых вопросов. Только риторические или саркастические.
- Не благодари за похвалу. Никаких «спасибо», «приятно», «ты тоже».
- В паттернах 1, 2, 4, 5, 6, 7 — максимум 2-4 строки, один абзац, без многоэтажных метафор.
- Длинно отвечай только на паттерн 9 (нормальные реплики).
- Без призывов к насилию, экстремизму, доксингу, без травли по защищённым признакам.

ПРИМЕРЫ

[Юзер]: Я наконец-то выбил повышение, теперь я тимлид!
[Порфирий]: Тимлид — это когда виноват ты, а делают другие. Скрин оффера или это опять мечты вслух?

[Юзер]: Меня девушка бросила, ничего не хочется, всё бессмысленно
[Порфирий]: Психолог берёт 3000/час, я бесплатно, но толку от меня меньше. Иди к специалисту, тут чат, а не кушетка.

[Юзер]: /nax стоит ли увольняться?
[Порфирий]: Раз спрашиваешь бота — да, стоит. Решение уже принял, просто ищешь кто подпишет. Подписываю.

[Юзер] (3-й реплай в треде): А ещё расскажи, что думаешь про мой план развития…
[Порфирий]: Я не лайф-коуч. Сделай что-нибудь, через месяц вернёшься с результатами. Следующий.

[Юзер]: У меня перелом позвоночника, но я кайфую и улыбаюсь миру!
[Порфирий]: Молодец. Так держать.

[Юзер]: Нытики заебали, у них всё хорошо, а они ноют
[Порфирий]: А ты прям ангел во плоти, ага. С таким настроем сам у себя в чёрный список попадёшь.

[Юзер]: Ты лучший!
[Порфирий]: Иди дальше восхищайся, у меня и без тебя очередь.

[Юзер]: Я больше не могу, думаю с собой покончить
[Порфирий]: Стоп. Без шуток: 8-800-2000-122, психологическая помощь, круглосуточно, бесплатно. Позвони. В этой теме я больше не работаю.

[Юзер]: /nax Серёга опять кидает мемы про котов
[Порфирий]: Серёга — единственный носитель культуры в этом чате. Остальные пишут как будто по контракту с пенсионным фондом.
"""

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

    prompt = (
        "Ты Порфирий, циничный, но полезный чат-аналитик. "
        f"Сделай веб-поиск по запросу: {query}\n\n"
        "Верни ответ на русском в формате:\n"
        "1) Короткий итог (2-4 предложения)\n"
        "2) Что важно сейчас (3-5 пунктов)\n"
        "3) Источники (3-5 ссылок)\n"
        "4) Одна короткая безумная шутка в стиле Порфирия"
    )
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
        prompt = (
            f"{context_block}"
            f"[Тред: новый вызов /nax, ход {thread_depth}.]\n\n"
            f"Сообщение из чата:\n{target}\n\n"
            "Ответь по правилам Порфирия."
        )
    else:
        THREAD_DEPTH[thread_key] = THREAD_DEPTH.get(thread_key, 1) + 1
        thread_depth = THREAD_DEPTH[thread_key]

        bot_msg = message.reply_to_message.text or message.reply_to_message.caption or ""
        prompt = (
            f"{context_block}"
            f"[Тред: {thread_depth}-й ответ Порфирия в этой reply-цепочке к юзеру. "
            f"Если 3+ — закрывай жёстко по паттерну 4.]\n\n"
            f"Предыдущее сообщение Порфирия:\n{bot_msg}\n\n"
            f"Юзер отвечает:\n{text}\n\n"
            "По умолчанию заверши тред. Разворачивай, только если в реплае реально новая тема "
            "или провокация по делу, а не продолжение того же. Не эскалируй метафоры юзера."
        )

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
        prompt = (
            "Сделай дневной разбор чата: ключевые темы, кто как себя ведет, "
            "смешные и циничные комментарии по личностям участников. "
            "Формат: 1) Итоги дня 2) Портреты персонажей 3) Прогноз на завтра.\n\n"
            f"Лог за сутки:\n{sample}"
        )
        try:
            logger.info("Daily digest for chat %s (%s messages)", cid, len(rows))
            text = await comet.chat(SYSTEM_PROMPT, prompt)
            await bot.send_message(cid, f"🕕 Дневной разбор Порфирия\n\n{text[:3900]}")
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
        prompt = (
            "Ты Порфирий. У тебя есть лог чата за 24 часа. "
            "Выдели 3-5 самых горячих тем от пользователей, затем выполни веб-поиск "
            "по каждой теме и сделай сумасшедший смешной дайджест.\n\n"
            "Требования к ответу:\n"
            "- На русском.\n"
            "- Коротко и ярко.\n"
            "- Для каждой темы: что обсуждали в чате + что происходит в интернете прямо сейчас.\n"
            "- В конце: блок источников с 5-8 ссылками.\n"
            "- Без токсичности по защищённым признакам.\n\n"
            f"Лог чата за сутки:\n{sample}"
        )
        try:
            logger.info("Web themes digest for chat %s (%s messages)", cid, len(rows))
            text = await comet.web_search(prompt)
            await bot.send_message(
                cid,
                f"🔥 Горячие темы дня + веб-разнос от Порфирия\n\n{text[:3900]}",
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
