"""
Telegram-бот "Своя Игра" на aiogram 3.x.
Поддерживает темы (topics), апелляции, маскировку аудио-метаданных.
"""

import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, CommandStart
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BufferedInputFile,
)
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN, ANSWER_TIMEOUT, BUZZER_TIMEOUT
from siq_parser import parse_siq, get_pack_info, GamePack
from game import Game, GameManager, GameState

# ==================== ЛОГИРОВАНИЕ ====================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== ГЛОБАЛЬНЫЕ ====================

manager = GameManager()
buzzer_messages: dict = {}   # chat_id -> message_id
appeal_messages: dict = {}   # chat_id -> message_id

PACKS_DIR = "packs"
os.makedirs(PACKS_DIR, exist_ok=True)

router = Router()


# ==================== УТИЛИТЫ ====================

def get_thread_id(message: Message) -> int | None:
    return message.message_thread_id


async def safe_send(chat_id: int, text: str, bot: Bot,
                    thread_id: int | None = None,
                    reply_markup=None,
                    parse_mode=ParseMode.MARKDOWN):
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            message_thread_id=thread_id,
        )
    except Exception as e:
        logger.error("Send error (markdown): %s", e)
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                message_thread_id=thread_id,
            )
        except Exception as e2:
            logger.error("Send error (plain): %s", e2)
            return None


# ==================== CALLBACKS ДЛЯ GAME ====================

def _apply_callbacks(game: Game, bot: Bot, thread_id: int | None):
    game.buzzer_timeout = BUZZER_TIMEOUT
    game.answer_timeout = ANSWER_TIMEOUT

    async def send_callback(g, text):
        await safe_send(g.chat_id, text, bot, thread_id)

    async def send_photo_callback(g, photo_data: bytes, filename: str | None):
        fname = filename or "photo.jpg"
        logger.info("Sending photo: %s (%d bytes)", fname, len(photo_data))
        try:
            input_file = BufferedInputFile(photo_data, filename=fname)
            await bot.send_photo(chat_id=g.chat_id, photo=input_file,
                                 message_thread_id=thread_id)
        except Exception as e:
            logger.error("send_photo failed [%s]: %s", fname, e)
            try:
                input_file2 = BufferedInputFile(photo_data, filename=fname)
                await bot.send_document(chat_id=g.chat_id, document=input_file2,
                                        message_thread_id=thread_id)
            except Exception as e2:
                logger.error("send_document photo fallback failed: %s", e2)

    async def send_audio_callback(g, audio_data: bytes, filename: str | None):
        """
        Аудио всегда отправляется под анонимным именем, чтобы не
        раскрывать исполнителя/название трека участникам.
        """
        fname = "audio.mp3"          # фиксированное имя — скрывает оригинал
        logger.info("Sending audio (masked): %s (%d bytes)", filename, len(audio_data))
        try:
            input_file = BufferedInputFile(audio_data, filename=fname)
            await bot.send_audio(
                chat_id=g.chat_id,
                audio=input_file,
                title="Своя Игра — Мелодия",
                performer="?",
                message_thread_id=thread_id,
            )
        except Exception as e:
            logger.error("send_audio failed: %s", e)
            try:
                input_file2 = BufferedInputFile(audio_data, filename=fname)
                await bot.send_voice(chat_id=g.chat_id, voice=input_file2,
                                     message_thread_id=thread_id)
            except Exception as e2:
                logger.error("send_voice fallback failed: %s", e2)

    async def send_video_callback(g, video_data: bytes, filename: str | None):
        fname = filename or "video.mp4"
        logger.info("Sending video: %s (%d bytes)", fname, len(video_data))
        try:
            input_file = BufferedInputFile(video_data, filename=fname)
            await bot.send_video(chat_id=g.chat_id, video=input_file,
                                 message_thread_id=thread_id)
        except Exception as e:
            logger.error("send_video failed: %s", e)
            try:
                input_file2 = BufferedInputFile(video_data, filename=fname)
                await bot.send_document(chat_id=g.chat_id, document=input_file2,
                                        message_thread_id=thread_id)
            except Exception as e2:
                logger.error("send_document video fallback failed: %s", e2)

    async def show_board_callback(g):
        keyboard = _build_board_keyboard(g)
        await safe_send(g.chat_id, g.get_board_text(), bot, thread_id,
                        reply_markup=keyboard)

    async def show_buzzer_callback(g):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Ответить!", callback_data="buzzer")]
        ])
        msg = await safe_send(g.chat_id, "⏳ Кто хочет ответить? Жмите кнопку!",
                              bot, thread_id, reply_markup=keyboard)
        if msg:
            buzzer_messages[g.chat_id] = msg.message_id

    async def remove_buzzer_callback(g):
        msg_id = buzzer_messages.pop(g.chat_id, None)
        if msg_id:
            try:
                await bot.delete_message(chat_id=g.chat_id, message_id=msg_id)
            except Exception:
                pass

    async def show_appeal_callback(g):
        """Отправляет или редактирует сообщение с кнопками апелляции."""
        text = g.get_appeal_status_text()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👍 За", callback_data="appeal_yes"),
            InlineKeyboardButton(text="👎 Против", callback_data="appeal_no"),
        ]])
        existing = appeal_messages.get(g.chat_id)
        if existing:
            try:
                await bot.edit_message_text(
                    chat_id=g.chat_id,
                    message_id=existing,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            except Exception:
                pass
        msg = await safe_send(g.chat_id, text, bot, thread_id, reply_markup=keyboard)
        if msg:
            appeal_messages[g.chat_id] = msg.message_id

    async def remove_appeal_callback(g):
        msg_id = appeal_messages.pop(g.chat_id, None)
        if msg_id:
            try:
                await bot.delete_message(chat_id=g.chat_id, message_id=msg_id)
            except Exception:
                pass

    async def show_scores_callback(g):
        round_name = g.current_round.name if g.current_round else "Раунд"
        header = "📊 *Конец раунда: {}*\n\n".format(round_name)
        await safe_send(g.chat_id, header + g.get_scores_text(), bot, thread_id)

    async def announce_round_callback(g):
        r = g.current_round
        idx = g.current_round_index + 1
        total = len(g.pack.rounds)
        rtype = " (Финал)" if r.round_type == 'final' else ""
        themes = '\n'.join("  - {}".format(t.name) for t in r.themes)
        text = "🎬 *Раунд {}/{}: {}{}*\n\nТемы:\n{}".format(idx, total, r.name, rtype, themes)
        await safe_send(g.chat_id, text, bot, thread_id)
        await asyncio.sleep(2)

    async def announce_game_over_callback(g):
        await safe_send(g.chat_id, g.get_final_results_text(), bot, thread_id)

    game.send_callback = send_callback
    game.send_photo_callback = send_photo_callback
    game.send_audio_callback = send_audio_callback
    game.send_video_callback = send_video_callback
    game.show_board_callback = show_board_callback
    game.show_buzzer_callback = show_buzzer_callback
    game.remove_buzzer_callback = remove_buzzer_callback
    game.show_scores_callback = show_scores_callback
    game.announce_round_callback = announce_round_callback
    game.announce_game_over_callback = announce_game_over_callback
    game.show_appeal_callback = show_appeal_callback
    game.remove_appeal_callback = remove_appeal_callback


def _build_board_keyboard(game):
    board = game.get_board()
    keyboard = []
    for theme_data in board:
        theme_name = theme_data['theme_name']
        short_name = theme_name[:18] + "…" if len(theme_name) > 18 else theme_name
        row_label = [InlineKeyboardButton(
            text="📌 {}".format(short_name),
            callback_data="theme_info_{}".format(theme_data['theme_idx'])
        )]
        keyboard.append(row_label)
        price_row = []
        for q in theme_data['questions']:
            if q['played']:
                price_row.append(InlineKeyboardButton(
                    text="✖",
                    callback_data="played_{}_{}".format(theme_data['theme_idx'], q['q_idx'])
                ))
            else:
                price_row.append(InlineKeyboardButton(
                    text=str(q['price']),
                    callback_data="q_{}_{}".format(theme_data['theme_idx'], q['q_idx'])
                ))
        keyboard.append(price_row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# ==================== КОМАНДЫ ====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "🎮 *Своя Игра — Telegram Bot*\n\n"
        "Играйте в Свою Игру прямо в Telegram!\n\n"
        "📦 *Как начать:*\n"
        "1. Создайте пакет вопросов на сайте:\n"
        "   vladimirkhil.com/si/siquester\n"
        "2. Скачайте .siq файл\n"
        "3. Отправьте его в этот чат ИЛИ положите в папку packs/\n"
        "4. /newgame — создать игру\n"
        "5. /join — присоединиться\n"
        "6. /startgame — начать!\n\n"
        "📋 *Команды:*\n"
        "/newgame — создать новую игру\n"
        "/join — присоединиться\n"
        "/leave — покинуть\n"
        "/startgame — начать\n"
        "/scores — счёт\n"
        "/stop — остановить\n"
        "/listpacks — список локальных паков\n"
        "/loadpack — загрузить пак из packs/\n"
        "/packinfo — инфо о паке\n"
        "/help — помощь"
    )
    await safe_send(message.chat.id, text, message.bot, get_thread_id(message))


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 *Правила Своей Игры:*\n\n"
        "• Игрок выбирает тему и стоимость вопроса\n"
        "• Бот задаёт вопрос\n"
        "• Кто хочет ответить — жмёт 🔔\n"
        "• Первый нажавший получает право ответа\n"
        "• Правильный ответ: +очки\n"
        "• Неправильный ответ: −очки\n"
        "• После ошибки другие могут попробовать\n"
        "• Правильно ответивший выбирает следующий вопрос\n"
        "• После каждого раунда — таблица очков\n\n"
        "⚖️ *Апелляция:*\n"
        "Если бот не засчитал верный по смыслу ответ,\n"
        "игрок может написать /appeal — все проголосуют,\n"
        "засчитывать ли его. Большинство ЗА — ответ принят.\n\n"
        "⏱ *Таймауты:*\n"
        "На кнопку: {} сек\n"
        "На ответ: {} сек"
    ).format(BUZZER_TIMEOUT, ANSWER_TIMEOUT)
    await safe_send(message.chat.id, text, message.bot, get_thread_id(message))


@router.message(Command("newgame"))
async def cmd_newgame(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)

    if manager.has_active_game(chat_id):
        await safe_send(chat_id, "⚠️ Уже идёт игра! /stop чтобы остановить.",
                        message.bot, thread_id)
        return

    pack = manager.get_pack(chat_id)
    if pack is None:
        await safe_send(
            chat_id,
            "📦 Сначала загрузите пак!\n"
            "Отправьте .siq файл в этот чат ИЛИ используйте /loadpack\n\n"
            "Создать пак: vladimirkhil.com/si/siquester",
            message.bot, thread_id,
        )
        return

    game = manager.create_game(chat_id, pack)
    _apply_callbacks(game, message.bot, thread_id)
    game.start_lobby()

    await safe_send(
        chat_id,
        "🎮 *Новая игра!*\n"
        "📦 Пак: {}\n\n"
        "👥 Игроки: пока никого\n\n"
        "/join — присоединиться\n"
        "/startgame — начать игру".format(pack.name),
        message.bot, thread_id,
    )


@router.message(Command("join"))
async def cmd_join(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    user = message.from_user

    game = manager.get_game(chat_id)
    if game is None or game.state != GameState.LOBBY:
        await safe_send(chat_id, "⚠️ Нет лобби. Используйте /newgame",
                        message.bot, thread_id)
        return

    display_name = user.first_name or "Player"
    if user.last_name:
        display_name += " {}".format(user.last_name)

    if game.add_player(user.id, user.username or "", display_name):
        players_list = '\n'.join(
            "  {}. {}".format(i + 1, p.display_name)
            for i, p in enumerate(game.get_players_list())
        )
        await safe_send(
            chat_id,
            "✅ *{}* присоединился!\n\n👥 Игроки ({}):\n{}".format(
                display_name, game.get_player_count(), players_list
            ),
            message.bot, thread_id,
        )
    else:
        await safe_send(chat_id, "ℹ️ {}, вы уже в игре!".format(display_name),
                        message.bot, thread_id)


@router.message(Command("leave"))
async def cmd_leave(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    user = message.from_user
    game = manager.get_game(chat_id)
    if game is None:
        return
    if game.remove_player(user.id):
        await safe_send(chat_id, "👋 *{}* покинул игру.".format(user.first_name),
                        message.bot, thread_id)
    else:
        await safe_send(chat_id, "ℹ️ Вы не в игре.", message.bot, thread_id)


@router.message(Command("startgame"))
async def cmd_startgame(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    game = manager.get_game(chat_id)
    if game is None or game.state != GameState.LOBBY:
        await safe_send(chat_id, "⚠️ Нет лобби. Используйте /newgame",
                        message.bot, thread_id)
        return
    if game.get_player_count() < 1:
        await safe_send(chat_id, "⚠️ Нужен хотя бы 1 игрок! /join",
                        message.bot, thread_id)
        return
    _apply_callbacks(game, message.bot, thread_id)
    await safe_send(chat_id, "🚀 *Игра начинается!*", message.bot, thread_id)
    await asyncio.sleep(1)
    success = await game.start_game()
    if not success:
        await safe_send(chat_id, "❌ Не удалось начать игру.", message.bot, thread_id)


@router.message(Command("scores"))
async def cmd_scores(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    game = manager.get_game(chat_id)
    if game is None or not game.players:
        await safe_send(chat_id, "ℹ️ Нет активной игры.", message.bot, thread_id)
        return
    await safe_send(chat_id, game.get_scores_text(), message.bot, thread_id)


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    game = manager.get_game(chat_id)
    if game is None:
        await safe_send(chat_id, "ℹ️ Нет активной игры.", message.bot, thread_id)
        return
    if game.players:
        await safe_send(chat_id, game.get_scores_text(), message.bot, thread_id)
    manager.remove_game(chat_id)
    await safe_send(chat_id, "🛑 Игра остановлена.", message.bot, thread_id)


@router.message(Command("appeal"))
async def cmd_appeal(message: Message):
    """Игрок подаёт апелляцию на свой последний неправильный ответ."""
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    user_id = message.from_user.id

    game = manager.get_game(chat_id)
    if game is None:
        await safe_send(chat_id, "ℹ️ Нет активной игры.", message.bot, thread_id)
        return

    if user_id not in game.players:
        await safe_send(chat_id, "⚠️ Вы не в игре! /join", message.bot, thread_id)
        return

    if game.current_appeal is not None:
        await safe_send(chat_id, "⚠️ Апелляция уже идёт!", message.bot, thread_id)
        return

    if user_id not in game.failed_answerers:
        await safe_send(
            chat_id,
            "ℹ️ Апеллировать можно только после ошибочного ответа на текущий вопрос.",
            message.bot, thread_id,
        )
        return

    _apply_callbacks(game, message.bot, thread_id)
    success = await game.start_appeal(user_id, "")
    if not success:
        await safe_send(
            chat_id,
            "⚠️ Сейчас нельзя подать апелляцию.",
            message.bot, thread_id,
        )


@router.message(Command("listpacks"))
async def cmd_listpacks(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    if not os.path.exists(PACKS_DIR):
        await safe_send(chat_id, "📁 Папка packs/ не создана.", message.bot, thread_id)
        return
    files = [f for f in os.listdir(PACKS_DIR) if f.lower().endswith('.siq')]
    if not files:
        await safe_send(
            chat_id,
            "📁 Папка packs/ пуста.\nПоложите туда .siq файлы и используйте /loadpack <имя>",
            message.bot, thread_id,
        )
        return
    lines = ["📁 *Доступные паки в packs/:*\n"]
    for i, f in enumerate(files, 1):
        size_mb = os.path.getsize(os.path.join(PACKS_DIR, f)) / (1024 * 1024)
        lines.append("{}. `{}` ({:.1f} МБ)".format(i, f, size_mb))
    lines.append("\n💡 Используйте /loadpack <имя_файла> для загрузки")
    await safe_send(chat_id, "\n".join(lines), message.bot, thread_id,
                    parse_mode=ParseMode.MARKDOWN)


@router.message(Command("loadpack"))
async def cmd_loadpack(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await safe_send(
            chat_id,
            "📦 Использование: /loadpack <имя_файла.siq>\n\nДоступные паки: /listpacks",
            message.bot, thread_id,
        )
        return
    file_name = parts[1].strip()
    file_path = os.path.join(PACKS_DIR, file_name)
    if not os.path.exists(file_path):
        await safe_send(chat_id, "❌ Файл `{}` не найден в packs/.".format(file_name),
                        message.bot, thread_id, parse_mode=ParseMode.MARKDOWN)
        return
    if not file_name.lower().endswith('.siq'):
        await safe_send(chat_id, "❌ Файл должен быть в формате .siq",
                        message.bot, thread_id)
        return
    await safe_send(chat_id, "⏳ Загружаю и обрабатываю пак...", message.bot, thread_id)
    try:
        pack = parse_siq(file_path)
        if not pack.rounds:
            await safe_send(chat_id, "❌ Пак пустой — нет раундов.", message.bot, thread_id)
            return
        total_q = sum(len(t.questions) for r in pack.rounds for t in r.themes)
        if total_q == 0:
            await safe_send(chat_id, "❌ Пак пустой — нет вопросов.", message.bot, thread_id)
            return
        manager.store_pack(chat_id, pack)
        info = get_pack_info(pack)
        await safe_send(
            chat_id,
            "✅ Пак загружен!\n\n{}\n\n/newgame — создать игру".format(info),
            message.bot, thread_id, parse_mode=None,
        )
    except ValueError as e:
        await safe_send(chat_id, "❌ Ошибка парсинга: {}".format(e), message.bot, thread_id)
    except Exception as e:
        logger.error("Pack load error: %s", e, exc_info=True)
        await safe_send(chat_id, "❌ Ошибка: {}".format(e), message.bot, thread_id)


@router.message(Command("packinfo"))
async def cmd_packinfo(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    pack = manager.get_pack(chat_id)
    if pack is None:
        await safe_send(chat_id, "📦 Пак не загружен. Отправьте .siq файл или используйте /loadpack",
                        message.bot, thread_id)
        return
    await safe_send(chat_id, get_pack_info(pack), message.bot, thread_id, parse_mode=None)


# ==================== ЗАГРУЗКА ФАЙЛОВ ====================

@router.message(F.document)
async def handle_document(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    document = message.document
    if document is None:
        return
    file_name = document.file_name or ""
    if not file_name.lower().endswith('.siq'):
        return
    await safe_send(chat_id, "⏳ Загружаю и обрабатываю пак...", message.bot, thread_id)
    file_path = os.path.join(PACKS_DIR, "{}_{}".format(chat_id, file_name))
    try:
        tg_file = await message.bot.get_file(document.file_id)
        await tg_file.download_to_file(file_path)
        pack = parse_siq(file_path)
        if not pack.rounds:
            await safe_send(chat_id, "❌ Пак пустой — нет раундов.", message.bot, thread_id)
            return
        total_q = sum(len(t.questions) for r in pack.rounds for t in r.themes)
        if total_q == 0:
            await safe_send(chat_id, "❌ Пак пустой — нет вопросов.", message.bot, thread_id)
            return
        manager.store_pack(chat_id, pack)
        info = get_pack_info(pack)
        await safe_send(
            chat_id,
            "✅ Пак загружен!\n\n{}\n\n/newgame — создать игру".format(info),
            message.bot, thread_id, parse_mode=None,
        )
        logger.info("Pack saved: %s", file_path)
    except ValueError as e:
        await safe_send(chat_id, "❌ Ошибка парсинга: {}".format(e), message.bot, thread_id)
    except Exception as e:
        logger.error("Pack load error: %s", e, exc_info=True)
        await safe_send(chat_id, "❌ Ошибка: {}".format(e), message.bot, thread_id)


# ==================== INLINE КНОПКИ ====================

@router.callback_query()
async def handle_callback(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    data = callback.data
    thread_id = callback.message.message_thread_id
    game = manager.get_game(chat_id)

    # --- Buzzer ---
    if data == "buzzer":
        if game is None:
            await callback.answer("Нет игры")
            return
        if user_id not in game.players:
            await callback.answer("Вы не в игре! /join", show_alert=True)
            return
        if game.state != GameState.QUESTION_ASKED:
            await callback.answer("Сейчас нельзя")
            return
        if user_id in game.failed_answerers:
            await callback.answer("Вы уже ошиблись на этом вопросе", show_alert=True)
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        success = await game.press_buzzer(user_id)
        await callback.answer("Вы отвечаете!" if success else "Кто-то уже отвечает")
        return

    # --- Апелляция: голосование ---
    if data in ("appeal_yes", "appeal_no"):
        if game is None:
            await callback.answer("Нет игры")
            return
        if user_id not in game.players:
            await callback.answer("Вы не в игре!", show_alert=True)
            return
        if game.state != GameState.APPEAL:
            await callback.answer("Апелляции нет")
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        vote = (data == "appeal_yes")
        result = await game.vote_appeal(user_id, vote)
        if result == 'voted':
            await callback.answer("👍 Голос учтён!" if vote else "👎 Голос учтён!")
        else:
            await callback.answer("Ошибка голосования")
        return

    # --- Выбор вопроса ---
    if data.startswith("q_"):
        if game is None:
            await callback.answer("Нет игры")
            return
        if game.state != GameState.CHOOSING_QUESTION:
            await callback.answer("Сейчас нельзя выбирать", show_alert=True)
            return
        if user_id != game.chooser_id:
            chooser = game.get_player(game.chooser_id)
            name = chooser.display_name if chooser else "другой игрок"
            await callback.answer("Сейчас выбирает {}".format(name), show_alert=True)
            return
        try:
            parts = data.split("_")
            theme_idx = int(parts[1])
            question_idx = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer("Ошибка")
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        success = await game.select_question(user_id, theme_idx, question_idx)
        await callback.answer() if success else await callback.answer("Вопрос уже сыгран",
                                                                       show_alert=True)
        return

    if data.startswith("played_"):
        await callback.answer("Уже сыграно")
        return

    if data.startswith("theme_info_"):
        if game and game.current_round:
            try:
                t_idx = int(data.split("_")[2])
                theme = game.current_round.themes[t_idx]
                comment = theme.comment if theme.comment else "Нет описания"
                await callback.answer("{}\n{}".format(theme.name, comment), show_alert=True)
            except (IndexError, ValueError):
                await callback.answer()
        else:
            await callback.answer()
        return

    await callback.answer()


# ==================== ТЕКСТОВЫЕ СООБЩЕНИЯ ====================

@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    if message.text is None or message.text.startswith('/'):
        return
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    user_id = message.from_user.id
    text = message.text

    game = manager.get_game(chat_id)
    if game is None:
        return
    if game.state != GameState.WAITING_ANSWER:
        return
    if user_id != game.current_answerer_id:
        return

    _apply_callbacks(game, message.bot, thread_id)
    await game.submit_answer(user_id, text)


# ==================== MAIN ====================

async def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Укажите BOT_TOKEN в config.py!")
        return

    print("🚀 Запуск бота Своя Игра (aiogram)...")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
    )
    dp = Dispatcher()
    dp.include_router(router)

    commands = [
        BotCommand(command="start", description="Начать / Помощь"),
        BotCommand(command="help", description="Правила игры"),
        BotCommand(command="newgame", description="Создать новую игру"),
        BotCommand(command="join", description="Присоединиться"),
        BotCommand(command="leave", description="Покинуть игру"),
        BotCommand(command="startgame", description="Начать игру"),
        BotCommand(command="scores", description="Текущий счёт"),
        BotCommand(command="appeal", description="Подать апелляцию"),
        BotCommand(command="stop", description="Остановить игру"),
        BotCommand(command="listpacks", description="Список локальных паков"),
        BotCommand(command="loadpack", description="Загрузить пак из packs/"),
        BotCommand(command="packinfo", description="Инфо о паке"),
    ]
    await bot.set_my_commands(commands)

    print("✅ Бот запущен!")
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен вручную.")
    except Exception as e:
        logger.critical("Critical error: %s", e, exc_info=True)
