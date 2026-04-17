"""
Telegram-бот "Своя Игра" на aiogram 3.x.
Поддерживает темы (topics), апелляции, маскировку аудио, пас,
пагинатор паков с inline-кнопками, режим ведущего, голосование за скип.
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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

manager = GameManager()
buzzer_messages: dict = {}
appeal_messages: dict = {}
skip_vote_messages: dict = {}

PACKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs")
os.makedirs(PACKS_DIR, exist_ok=True)

PACKS_PAGE_SIZE = 8

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
            chat_id=chat_id, text=text, reply_markup=reply_markup,
            parse_mode=parse_mode, message_thread_id=thread_id,
        )
    except Exception as e:
        logger.error("Send error (markdown): %s", e)
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup,
                message_thread_id=thread_id,
            )
        except Exception as e2:
            logger.error("Send error (plain): %s", e2)
            return None


# ==================== ПАГИНАТОР ПАКОВ ====================

def _get_siq_files() -> list[str]:
    if not os.path.isdir(PACKS_DIR):
        return []
    try:
        files = [f for f in os.listdir(PACKS_DIR) if f.lower().endswith('.siq')]
        return sorted(files)
    except Exception:
        return []


def _build_packs_keyboard(page: int, files: list[str]) -> InlineKeyboardMarkup:
    total = len(files)
    total_pages = max(1, (total + PACKS_PAGE_SIZE - 1) // PACKS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * PACKS_PAGE_SIZE
    end = start + PACKS_PAGE_SIZE
    page_files = files[start:end]

    rows = []
    for i, fname in enumerate(page_files):
        global_idx = start + i
        size_mb = 0.0
        try:
            size_mb = os.path.getsize(os.path.join(PACKS_DIR, fname)) / (1024 * 1024)
        except Exception:
            pass
        label = fname[:-4] if fname.lower().endswith('.siq') else fname
        label = label[:40] + "…" if len(label) > 40 else label
        btn_text = "📦 {} ({:.1f} МБ)".format(label, size_mb)
        rows.append([InlineKeyboardButton(
            text=btn_text,
            callback_data="loadpack_idx_{}".format(global_idx)
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data="packs_page_{}".format(page - 1)))
    nav.append(InlineKeyboardButton(text="{}/{}".format(page + 1, total_pages), callback_data="packs_noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data="packs_page_{}".format(page + 1)))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _packs_text(page: int, files: list[str]) -> str:
    total = len(files)
    total_pages = max(1, (total + PACKS_PAGE_SIZE - 1) // PACKS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    return "📁 *Доступные паки* (всего: {}, стр. {}/{}):\n\nНажмите на пак чтобы загрузить его:".format(
        total, page + 1, total_pages
    )


# ==================== CALLBACKS ДЛЯ GAME ====================

def _apply_callbacks(game: Game, bot: Bot, thread_id: int | None):
    game.buzzer_timeout = BUZZER_TIMEOUT
    game.answer_timeout = ANSWER_TIMEOUT

    async def send_callback(g, text):
        await safe_send(g.chat_id, text, bot, thread_id)

    async def send_photo_callback(g, photo_data: bytes, filename: str | None):
        fname = filename or "photo.jpg"
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
        fname = "audio.mp3"
        logger.info("Sending audio (masked): %s (%d bytes)", filename, len(audio_data))
        try:
            input_file = BufferedInputFile(audio_data, filename=fname)
            await bot.send_audio(
                chat_id=g.chat_id, audio=input_file,
                title="Своя Игра — Мелодия", performer="?",
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
        await safe_send(g.chat_id, g.get_board_text(), bot, thread_id, reply_markup=keyboard)

    async def show_buzzer_callback(g):
        rows = [[
            InlineKeyboardButton(text="🔔 Ответить!", callback_data="buzzer"),
            InlineKeyboardButton(text="🙅 Пас", callback_data="pass"),
        ]]
        if g.host_mode:
            rows.append([
                InlineKeyboardButton(text="✅ Засчитать ведущий", callback_data="host_mark_correct"),
                InlineKeyboardButton(text="❌ Снять ведущий", callback_data="host_mark_wrong"),
            ])
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
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
        text = g.get_appeal_status_text()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👍 За", callback_data="appeal_yes"),
            InlineKeyboardButton(text="👎 Против", callback_data="appeal_no"),
        ]])
        existing = appeal_messages.get(g.chat_id)
        if existing:
            try:
                await bot.edit_message_text(
                    chat_id=g.chat_id, message_id=existing, text=text,
                    reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
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

    async def show_skip_vote_callback(g):
        text = g.get_skip_vote_text()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👍 Пропустить", callback_data="skipvote_yes"),
            InlineKeyboardButton(text="👎 Играть", callback_data="skipvote_no"),
        ]])
        existing = skip_vote_messages.get(g.chat_id)
        if existing:
            try:
                await bot.edit_message_text(
                    chat_id=g.chat_id, message_id=existing, text=text,
                    reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
                )
                return
            except Exception:
                pass
        msg = await safe_send(g.chat_id, text, bot, thread_id, reply_markup=keyboard)
        if msg:
            skip_vote_messages[g.chat_id] = msg.message_id

    async def remove_skip_vote_callback(g):
        msg_id = skip_vote_messages.pop(g.chat_id, None)
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
        text = "🎦 *Раунд {}/{}: {}{}*\n\nТемы:\n{}".format(idx, total, r.name, rtype, themes)
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
    game.show_skip_vote_callback = show_skip_vote_callback
    game.remove_skip_vote_callback = remove_skip_vote_callback


def _build_board_keyboard(game: Game) -> InlineKeyboardMarkup:
    board = game.get_board()
    keyboard = []
    for theme_data in board:
        theme_name = theme_data['theme_name']
        short_name = theme_name[:18] + "…" if len(theme_name) > 18 else theme_name
        skip_mark = " ⏩" if theme_data.get('skipped') else ""
        row_label = [InlineKeyboardButton(
            text="📌 {}{}".format(short_name, skip_mark),
            callback_data="theme_info_{}".format(theme_data['theme_idx'])
        )]
        keyboard.append(row_label)
        price_row = []
        for q in theme_data['questions']:
            if q['played']:
                price_row.append(InlineKeyboardButton(
                    text="✖", callback_data="played_{}_{}".format(theme_data['theme_idx'], q['q_idx'])
                ))
            else:
                price_row.append(InlineKeyboardButton(
                    text=str(q['price']),
                    callback_data="q_{}_{}".format(theme_data['theme_idx'], q['q_idx'])
                ))
        keyboard.append(price_row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _build_host_score_keyboard(game: Game) -> InlineKeyboardMarkup:
    """Клавиатура для ведущего: выбор игрока и изменение очков."""
    rows = []
    for uid, player in game.players.items():
        rows.append([
            InlineKeyboardButton(
                text="➕100 {}".format(player.display_name),
                callback_data="hscore_{}_100".format(uid)
            ),
            InlineKeyboardButton(
                text="➖100 {}".format(player.display_name),
                callback_data="hscore_{}_-100".format(uid)
            ),
        ])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="hscore_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_host_skip_theme_keyboard(game: Game) -> InlineKeyboardMarkup:
    """Клавиатура для ведущего: скип темы."""
    if game.current_round is None:
        return InlineKeyboardMarkup(inline_keyboard=[])
    rows = []
    for t_idx, theme in enumerate(game.current_round.themes):
        if t_idx not in game.skipped_themes:
            rows.append([InlineKeyboardButton(
                text="⏩ {}".format(theme.name),
                callback_data="hskiptheme_{}".format(t_idx)
            )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="hskiptheme_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== КОМАНДЫ ====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "🎮 *Своя Игра — Telegram Bot*\n\n"
        "📦 *Как начать:*\n"
        "1. Отправьте .siq файл в этот чат ИЛИ используйте /listpacks\n"
        "2. /newgame — создать игру\n"
        "3. /join — присоединиться\n"
        "4. /startgame — начать!\n\n"
        "📋 *Команды:*\n"
        "/newgame — создать новую игру\n"
        "/join — присоединиться\n"
        "/leave — покинуть\n"
        "/startgame — начать\n"
        "/scores — счёт\n"
        "/stop — остановить\n"
        "/listpacks — список паков\n"
        "/packinfo — инфо о текущем паке\n"
        "/skipvote — голосование за скип раунда\n"
        "/skipthemevote — голосование за скип темы\n"
        "/help — помощь\n\n"
        "🎤 *Команды ведущего:*\n"
        "/host_score — изменить очки игроку\n"
        "/host_correct — засчитать последний ответ\n"
        "/host_skipround — пропустить раунд\n"
        "/host_skiptheme — пропустить тему"
    )
    await safe_send(message.chat.id, text, message.bot, get_thread_id(message))


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 *Правила Своей Игры:*\n\n"
        "• Игрок выбирает тему и стоимость вопроса\n"
        "• Бот задаёт вопрос\n"
        "• 🔔 Ответить — нажмите первым чтобы ответить\n"
        "• 🙅 Пас — пропустить вопрос без штрафа\n"
        "• Пока отвечает другой — можно нажать кнопку заранее (встанете в очередь)\n"
        "• Правильный ответ: +очки\n"
        "• Неправильный ответ: −очки\n"
        "• Если все спасовали — вопрос пропускается сразу\n\n"
        "⚖️ *Апелляция (/appeal):*\n"
        "Если бот не засчитал верный по смыслу ответ,\n"
        "напишите /appeal — все голосуют засчитывать ли.\n\n"
        "⏩ *Скип (/skipvote / /skipthemevote):*\n"
        "Предложить пропустить раунд или тему голосованием.\n\n"
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
        files = _get_siq_files()
        if files:
            keyboard = _build_packs_keyboard(0, files)
            await safe_send(chat_id, "📦 Сначала выберите пак:",
                            message.bot, thread_id, reply_markup=keyboard)
        else:
            await safe_send(
                chat_id,
                "📦 Сначала загрузите пак!\n"
                "Отправьте .siq файл в этот чат.\n\n"
                "Создать пак: vladimirkhil.com/si/siquester",
                message.bot, thread_id,
            )
        return
    # Выбор режима: с ведущим или без
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 Без ведущего", callback_data="newgame_nohost"),
        InlineKeyboardButton(text="🎤 Я — ведущий", callback_data="newgame_host"),
    ]])
    await safe_send(
        chat_id,
        "🎮 *Новая игра!*\n"
        "📦 Пак: {}\n\n"
        "Выберите режим:".format(pack.name),
        message.bot, thread_id, reply_markup=keyboard,
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
    # Ведущий не добавляется в players
    if game.host_mode and game.host_id == user.id:
        await safe_send(chat_id, "🎤 Вы ведущий, не участник!",
                        message.bot, thread_id)
        return
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
    host_line = ""
    if game.host_mode and game.host_id:
        host_line = "\n🎤 Ведущий присутствует"
    await safe_send(chat_id, "🚀 *Игра начинается!*{}".format(host_line),
                    message.bot, thread_id)
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
    in_current = user_id in game.failed_answerers
    in_last = user_id in game.last_failed_answerers
    if not in_current and not in_last:
        await safe_send(
            chat_id,
            "ℹ️ Апеллировать можно только после ошибочного ответа на текущий вопрос.",
            message.bot, thread_id,
        )
        return
    _apply_callbacks(game, message.bot, thread_id)
    success = await game.start_appeal(user_id, "")
    if not success:
        await safe_send(chat_id, "⚠️ Сейчас нельзя подать апелляцию.",
                        message.bot, thread_id)


@router.message(Command("skipvote"))
async def cmd_skipvote(message: Message):
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
    if game.current_skip_vote is not None:
        await safe_send(chat_id, "⚠️ Голосование за скип уже идёт!", message.bot, thread_id)
        return
    _apply_callbacks(game, message.bot, thread_id)
    success = await game.start_skip_vote(user_id, 'round')
    if not success:
        await safe_send(chat_id, "⚠️ Сейчас нельзя начать голосование за скип.",
                        message.bot, thread_id)


@router.message(Command("skipthemevote"))
async def cmd_skipthemevote(message: Message):
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
    if game.current_skip_vote is not None:
        await safe_send(chat_id, "⚠️ Голосование за скип уже идёт!", message.bot, thread_id)
        return
    if game.state != GameState.CHOOSING_QUESTION or game.current_round is None:
        await safe_send(chat_id, "⚠️ Сейчас нельзя.", message.bot, thread_id)
        return
    # Показываем клавиатуру выбора темы для скипа
    rows = []
    for t_idx, theme in enumerate(game.current_round.themes):
        if t_idx not in game.skipped_themes:
            rows.append([InlineKeyboardButton(
                text="⏩ {}".format(theme.name),
                callback_data="skipthemevote_{}".format(t_idx)
            )])
    if not rows:
        await safe_send(chat_id, "ℹ️ Все темы уже сыграны.", message.bot, thread_id)
        return
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="skipthemevote_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_send(chat_id, "⏩ Выберите тему для голосования за скип:",
                    message.bot, thread_id, reply_markup=keyboard)


# ==================== КОМАНДЫ ВЕДУЩЕГО ====================

@router.message(Command("host_score"))
async def cmd_host_score(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    user_id = message.from_user.id
    game = manager.get_game(chat_id)
    if game is None or not game.is_host(user_id):
        await safe_send(chat_id, "⚠️ Только ведущий может использовать эту команду.",
                        message.bot, thread_id)
        return
    if not game.players:
        await safe_send(chat_id, "ℹ️ Нет игроков.", message.bot, thread_id)
        return
    keyboard = _build_host_score_keyboard(game)
    await safe_send(chat_id, "🎤 *Ведущий: изменить очки*\n\n" + game.get_scores_text(),
                    message.bot, thread_id, reply_markup=keyboard)


@router.message(Command("host_correct"))
async def cmd_host_correct(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    user_id = message.from_user.id
    game = manager.get_game(chat_id)
    if game is None or not game.is_host(user_id):
        await safe_send(chat_id, "⚠️ Только ведущий.", message.bot, thread_id)
        return
    # Найдём последнего ошибшегося и вернём очки
    # Ищем в текущем вопросе
    target_id = None
    for att in reversed(game.answer_attempts):
        if not att.is_correct and att.user_id in game.failed_answerers:
            target_id = att.user_id
            break
    if target_id is None:
        # Ищем в прошлом вопросе
        for att in reversed(game.last_answer_attempts):
            if not att.is_correct and att.user_id in game.last_failed_answerers:
                target_id = att.user_id
                break
    if target_id is None:
        await safe_send(chat_id, "ℹ️ Не найден последний ошибившийся игрок.",
                        message.bot, thread_id)
        return
    player = game.players.get(target_id)
    if player is None:
        await safe_send(chat_id, "ℹ️ Игрок не найден.", message.bot, thread_id)
        return
    # Определяем цену вопроса
    q = game.current_question or game.last_question
    if q is None:
        await safe_send(chat_id, "ℹ️ Вопрос не найден.", message.bot, thread_id)
        return
    price = q.price
    # Возвращаем штраф и добавляем очки за правильный
    player.score += price * 2
    game.failed_answerers.discard(target_id)
    game.last_failed_answerers.discard(target_id)
    _apply_callbacks(game, message.bot, thread_id)
    await safe_send(
        chat_id,
        "✅ Ведущий засчитал ответ *{}*!\n"
        "💰 +{} очков (всего: {})".format(player.display_name, price, player.score),
        message.bot, thread_id,
    )


@router.message(Command("host_skipround"))
async def cmd_host_skipround(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    user_id = message.from_user.id
    game = manager.get_game(chat_id)
    if game is None or not game.is_host(user_id):
        await safe_send(chat_id, "⚠️ Только ведущий.", message.bot, thread_id)
        return
    _apply_callbacks(game, message.bot, thread_id)
    success = await game.host_skip_round(user_id)
    if not success:
        await safe_send(chat_id, "⚠️ Сейчас нельзя пропустить раунд.",
                        message.bot, thread_id)
    else:
        await safe_send(chat_id, "⏩ Ведущий пропустил раунд.", message.bot, thread_id)


@router.message(Command("host_skiptheme"))
async def cmd_host_skiptheme(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    user_id = message.from_user.id
    game = manager.get_game(chat_id)
    if game is None or not game.is_host(user_id):
        await safe_send(chat_id, "⚠️ Только ведущий.", message.bot, thread_id)
        return
    if game.state != GameState.CHOOSING_QUESTION or game.current_round is None:
        await safe_send(chat_id, "⚠️ Сейчас нельзя.", message.bot, thread_id)
        return
    keyboard = _build_host_skip_theme_keyboard(game)
    await safe_send(chat_id, "🎤 *Ведущий: выберите тему для пропуска:*",
                    message.bot, thread_id, reply_markup=keyboard)


@router.message(Command("listpacks"))
async def cmd_listpacks(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    files = _get_siq_files()
    if not files:
        await safe_send(
            chat_id,
            "📁 В папке packs/ нет .siq файлов.\n"
            "Отправьте .siq файл прямо в этот чат чтобы загрузить пак.",
            message.bot, thread_id,
        )
        return
    keyboard = _build_packs_keyboard(0, files)
    await safe_send(chat_id, _packs_text(0, files), message.bot, thread_id,
                    reply_markup=keyboard)


@router.message(Command("packinfo"))
async def cmd_packinfo(message: Message):
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    pack = manager.get_pack(chat_id)
    if pack is None:
        await safe_send(chat_id, "📦 Пак не загружен. Используйте /listpacks",
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
        await safe_send(chat_id, "✅ Пак загружен!\n\n{}\n\n/newgame — создать игру".format(info),
                        message.bot, thread_id, parse_mode=None)
        logger.info("Pack saved: %s", file_path)
    except ValueError as e:
        await safe_send(chat_id, "❌ Ошибка парсинга: {}".format(e), message.bot, thread_id)
    except Exception as e:
        logger.error("Pack load error: %s", e, exc_info=True)
        await safe_send(chat_id, "❌ Ошибка: {}".format(e), message.bot, thread_id)


# ==================== ВСПОМОГАТЕЛЬНАЯ ЗАГРУЗКА ПАКА ====================

async def _load_pack_by_filename(chat_id: int, thread_id, file_name: str, bot: Bot):
    file_path = os.path.join(PACKS_DIR, file_name)
    if not os.path.exists(file_path):
        await safe_send(chat_id, "❌ Файл не найден: {}".format(file_name),
                        bot, thread_id, parse_mode=None)
        return
    await safe_send(chat_id, "⏳ Загружаю пак *{}*...".format(
        file_name[:-4] if file_name.endswith('.siq') else file_name),
        bot, thread_id)
    try:
        pack = parse_siq(file_path)
        if not pack.rounds:
            await safe_send(chat_id, "❌ Пак пустой — нет раундов.", bot, thread_id)
            return
        total_q = sum(len(t.questions) for r in pack.rounds for t in r.themes)
        if total_q == 0:
            await safe_send(chat_id, "❌ Пак пустой — нет вопросов.", bot, thread_id)
            return
        manager.store_pack(chat_id, pack)
        await safe_send(
            chat_id,
            "✅ Пак *{}* загружен!\n"
            "Раундов: {}, вопросов: {}\n\n"
            "/newgame — создать игру".format(pack.name, len(pack.rounds), total_q),
            bot, thread_id,
        )
    except ValueError as e:
        await safe_send(chat_id, "❌ Ошибка парсинга: {}".format(e), bot, thread_id)
    except Exception as e:
        logger.error("Pack load error: %s", e, exc_info=True)
        await safe_send(chat_id, "❌ Ошибка: {}".format(e), bot, thread_id)


# ==================== INLINE КНОПКИ ====================

@router.callback_query()
async def handle_callback(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    data = callback.data
    thread_id = callback.message.message_thread_id
    game = manager.get_game(chat_id)

    # --- Пагинация паков ---
    if data.startswith("packs_page_"):
        try:
            page = int(data.split("_")[-1])
        except ValueError:
            await callback.answer()
            return
        files = _get_siq_files()
        if not files:
            await callback.answer("Паков нет", show_alert=True)
            return
        keyboard = _build_packs_keyboard(page, files)
        try:
            await callback.message.edit_text(
                _packs_text(page, files),
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await callback.answer()
        return

    if data == "packs_noop":
        await callback.answer()
        return

    # --- Загрузка пака по индексу ---
    if data.startswith("loadpack_idx_"):
        try:
            idx = int(data.split("_")[-1])
        except ValueError:
            await callback.answer("Ошибка", show_alert=True)
            return
        files = _get_siq_files()
        if idx < 0 or idx >= len(files):
            await callback.answer("Пак не найден", show_alert=True)
            return
        file_name = files[idx]
        await callback.answer("⏳ Загружаю...")
        await _load_pack_by_filename(chat_id, thread_id, file_name, callback.message.bot)
        return

    # --- Выбор режима новой игры ---
    if data in ("newgame_nohost", "newgame_host"):
        pack = manager.get_pack(chat_id)
        if pack is None:
            await callback.answer("Пак не выбран", show_alert=True)
            return
        if manager.has_active_game(chat_id):
            await callback.answer("Игра уже идёт", show_alert=True)
            return
        game = manager.create_game(chat_id, pack)
        _apply_callbacks(game, callback.message.bot, thread_id)
        if data == "newgame_host":
            game.host_mode = True
            game.host_id = user_id
            host_name = callback.from_user.first_name or "Ведущий"
            game.start_lobby()
            await callback.answer("Вы — ведущий!")
            await safe_send(
                chat_id,
                "🎮 *Новая игра!*\n"
                "📦 Пак: {}\n"
                "🎤 Ведущий: *{}*\n\n"
                "👥 Игроки: пока никого\n\n"
                "/join — присоединиться\n"
                "/startgame — начать игру".format(pack.name, host_name),
                callback.message.bot, thread_id,
            )
        else:
            game.start_lobby()
            await callback.answer()
            await safe_send(
                chat_id,
                "🎮 *Новая игра!*\n"
                "📦 Пак: {}\n\n"
                "👥 Игроки: пока никого\n\n"
                "/join — присоединиться\n"
                "/startgame — начать игру".format(pack.name),
                callback.message.bot, thread_id,
            )
        return

    # --- Buzzer ---
    if data == "buzzer":
        if game is None:
            await callback.answer("Нет игры")
            return
        if user_id not in game.players:
            await callback.answer("Вы не в игре! /join", show_alert=True)
            return
        if game.state not in (GameState.QUESTION_ASKED, GameState.WAITING_ANSWER):
            await callback.answer("Сейчас нельзя")
            return
        if user_id in game.failed_answerers:
            await callback.answer("Вы уже ошиблись на этом вопросе", show_alert=True)
            return
        if user_id in game.passed_players:
            await callback.answer("Вы спасовали на этом вопросе", show_alert=True)
            return
        if user_id == game.current_answerer_id:
            await callback.answer("Вы уже отвечаете!")
            return
        if user_id in game.buzzer_queue:
            await callback.answer("Вы уже в очереди")
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        success = await game.press_buzzer(user_id)
        if success:
            await callback.answer("Вы отвечаете!")
        else:
            await callback.answer("Встали в очередь — ответите после текущего")
        return

    # --- Пас ---
    if data == "pass":
        if game is None:
            await callback.answer("Нет игры")
            return
        if user_id not in game.players:
            await callback.answer("Вы не в игре! /join", show_alert=True)
            return
        if game.state != GameState.QUESTION_ASKED:
            await callback.answer("Сейчас нельзя")
            return
        if user_id in game.passed_players:
            await callback.answer("Вы уже спасовали", show_alert=True)
            return
        if user_id in game.failed_answerers:
            await callback.answer("Вы уже ошиблись", show_alert=True)
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        await game.press_pass(user_id)
        await callback.answer("🙅 Пас принят")
        return

    # --- Ведущий: засчитать ответ прямо из buzzer-кнопки ---
    if data == "host_mark_correct":
        if game is None or not game.is_host(user_id):
            await callback.answer("Только ведущий", show_alert=True)
            return
        # Ищем текущего отвечающего
        target_id = game.current_answerer_id
        if target_id is None:
            # последний ошибшийся
            for att in reversed(game.answer_attempts):
                if not att.is_correct:
                    target_id = att.user_id
                    break
        if target_id is None or target_id not in game.players:
            await callback.answer("Нет отвечающего игрока", show_alert=True)
            return
        player = game.players[target_id]
        q = game.current_question
        if q is None:
            await callback.answer("Нет текущего вопроса", show_alert=True)
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        game._cancel_answer_timer()
        game._cancel_buzzer_timer()
        if game.remove_buzzer_callback:
            await game.remove_buzzer_callback(game)
        # Засчитываем как верный
        game.question_answered_correctly = True
        game.correct_answerer_id = target_id
        if target_id in game.failed_answerers:
            player.score += q.price * 2  # вернуть штраф + дать очки
            game.failed_answerers.discard(target_id)
        else:
            player.score += q.price
        game.chooser_id = target_id
        game.state = GameState.SHOWING_ANSWER
        await safe_send(
            chat_id,
            "✅ Ведущий засчитал ответ *{}*!\n"
            "💰 Счёт: {}".format(player.display_name, player.score),
            callback.message.bot, thread_id,
        )
        game._save_last_question_data()
        await game._after_question()
        await callback.answer("Засчитано!")
        return

    if data == "host_mark_wrong":
        if game is None or not game.is_host(user_id):
            await callback.answer("Только ведущий", show_alert=True)
            return
        target_id = game.current_answerer_id
        if target_id is None or target_id not in game.players:
            await callback.answer("Нет отвечающего игрока", show_alert=True)
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        await game._process_wrong_answer(target_id)
        await callback.answer("Снято!")
        return

    # --- Ведущий: изменение очков ---
    if data.startswith("hscore_"):
        if game is None or not game.is_host(user_id):
            await callback.answer("Только ведущий", show_alert=True)
            return
        if data == "hscore_close":
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.answer()
            return
        parts = data.split("_")
        if len(parts) < 3:
            await callback.answer()
            return
        try:
            target_id = int(parts[1])
            delta = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if game.host_adjust_score(user_id, target_id, delta):
            player = game.players[target_id]
            sign = "+" if delta > 0 else ""
            await callback.answer("{}{} → {}".format(sign, delta, player.score))
            try:
                keyboard = _build_host_score_keyboard(game)
                await callback.message.edit_text(
                    "🎤 *Ведущий: изменить очки*\n\n" + game.get_scores_text(),
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
        else:
            await callback.answer("Ошибка", show_alert=True)
        return

    # --- Ведущий: скип темы ---
    if data.startswith("hskiptheme_"):
        if game is None or not game.is_host(user_id):
            await callback.answer("Только ведущий", show_alert=True)
            return
        suffix = data[len("hskiptheme_"):]
        if suffix == "cancel":
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.answer()
            return
        try:
            t_idx = int(suffix)
        except ValueError:
            await callback.answer()
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        success = await game.host_skip_theme(user_id, t_idx)
        if success:
            theme_name = game.current_round.themes[t_idx].name if game.current_round else str(t_idx)
            await safe_send(chat_id, "⏩ Ведущий пропустил тему *{}*.".format(theme_name),
                            callback.message.bot, thread_id)
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.answer("Тема пропущена")
        else:
            await callback.answer("Не удалось пропустить", show_alert=True)
        return

    # --- Голосование за скип темы (от игрока) ---
    if data.startswith("skipthemevote_"):
        suffix = data[len("skipthemevote_"):]
        if suffix == "cancel":
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.answer()
            return
        if game is None or user_id not in game.players:
            await callback.answer("Вы не в игре", show_alert=True)
            return
        try:
            t_idx = int(suffix)
        except ValueError:
            await callback.answer()
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        success = await game.start_skip_vote(user_id, 'theme', t_idx)
        if success:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.answer("Голосование начато!")
        else:
            await callback.answer("Не удалось начать голосование", show_alert=True)
        return

    # --- Голосование за скип: да/нет ---
    if data in ("skipvote_yes", "skipvote_no"):
        if game is None:
            await callback.answer("Нет игры")
            return
        if user_id not in game.players:
            await callback.answer("Вы не в игре!", show_alert=True)
            return
        if game.state != GameState.SKIP_VOTE:
            await callback.answer("Голосования нет")
            return
        _apply_callbacks(game, callback.message.bot, thread_id)
        vote = (data == "skipvote_yes")
        result = await game.vote_skip(user_id, vote)
        if result == 'voted':
            await callback.answer("👍 Голос учтён!" if vote else "👎 Голос учтён!")
        else:
            await callback.answer("Ошибка голосования")
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
                await callback.answer("{}:\n{}".format(theme.name, comment), show_alert=True)
            except (IndexError, ValueError):
                await callback.answer()
        else:
            await callback.answer()
        return

    await callback.answer()


# ==================== ТЕКСТОВЫЕ СООБЩЕНИЯ ====================

@router.message(F.text)
async def handle_text(message: Message):
    # Пропускаем команды — они обрабатываются своими хендлерами
    if message.text is None or message.text.startswith('/'):
        return
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return
    text = message.text.strip()
    if not text:
        return
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
    print("📁 Папка паков: {}".format(PACKS_DIR))
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
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
        BotCommand(command="skipvote", description="Голосование за скип раунда"),
        BotCommand(command="skipthemevote", description="Голосование за скип темы"),
        BotCommand(command="stop", description="Остановить игру"),
        BotCommand(command="listpacks", description="Список паков с выбором"),
        BotCommand(command="packinfo", description="Инфо о текущем паке"),
        BotCommand(command="host_score", description="[Ведущий] Изменить очки"),
        BotCommand(command="host_correct", description="[Ведущий] Засчитать последний ответ"),
        BotCommand(command="host_skipround", description="[Ведущий] Пропустить раунд"),
        BotCommand(command="host_skiptheme", description="[Ведущий] Пропустить тему"),
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
