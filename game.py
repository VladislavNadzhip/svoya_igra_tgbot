"""
Логика игры "Своя Игра" для Telegram.

Управляет состоянием игры: раунды, выбор вопросов, ответы,
подсчёт очков, таймауты, апелляции, пас, режим ведущего,
голосование за скип.
"""

import asyncio
import functools
import time
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple, Any
from siq_parser import GamePack, Round, Theme, Question


class GameState(Enum):
    IDLE = auto()
    LOBBY = auto()
    ROUND_START = auto()
    CHOOSING_QUESTION = auto()
    QUESTION_ASKED = auto()
    WAITING_ANSWER = auto()
    SHOWING_ANSWER = auto()
    APPEAL = auto()
    ROUND_END = auto()
    GAME_OVER = auto()
    SKIP_VOTE = auto()   # голосование за скип
    # Финал
    FINAL_THEME_ELIMINATION = auto()  # игроки по очереди убирают темы
    FINAL_BETTING = auto()            # ставки по очереди (тема известна, вопрос скрыт)
    FINAL_SHOWING_QUESTION = auto()   # показ вопроса
    FINAL_COUNTDOWN = auto()          # обратный отсчёт перед окном ответа
    FINAL_ANSWER_WINDOW = auto()      # окно для ввода ответа в чат
    FINAL_SHOWING_RESULTS = auto()    # показ результатов
    FINAL_APPEAL = auto()             # апелляция в финале


SKIP_VOTE_TIMEOUT = 20  # секунд на голосование за скип
APPEAL_TIMEOUT = 20

# Финал
FINAL_BET_TIMEOUT = 45        # секунд на ставку одного игрока
FINAL_COUNTDOWN_SECONDS = 5   # обратный отсчёт перед окном ответа
FINAL_ANSWER_WINDOW = 5       # окно для ввода ответа в чат (сек)
FINAL_APPEAL_TIMEOUT = 20     # время на голосование по апелляции в финале
FINAL_RESULTS_APPEAL_WINDOW = 30  # сколько ждать апелляций после результатов


def _locked(coro):
    """
    Сериализует вызов корутины через self._lock.

    Все публичные точки входа (нажатия кнопок, ответы, голосования),
    которые меняют состояние игры и при этом делают await (сетевые
    колбэки), должны идти строго по очереди — иначе одновременные
    нажатия двух игроков ломают очередь баззера, апелляции и счёт.

    Правило, исключающее дедлок: метод под @_locked НЕ должен вызывать
    другой метод под @_locked. Внутренние (_-методы) и обработчики
    таймеров лок не берут сами (таймер берёт его уже после sleep).
    """
    @functools.wraps(coro)
    async def wrapper(self, *args, **kwargs):
        async with self._lock:
            return await coro(self, *args, **kwargs)
    return wrapper


@dataclass
class Player:
    user_id: int
    username: str
    display_name: str
    score: int = 0


@dataclass
class AnswerAttempt:
    user_id: int
    text: str
    timestamp: float
    is_correct: bool = False
    processed: bool = False
    seq: int = 0  # монотонный порядок ответа на текущем вопросе


@dataclass
class Appeal:
    user_id: int
    answer_text: str
    price: int
    votes_for: set = field(default_factory=set)
    votes_against: set = field(default_factory=set)
    message_id: Optional[int] = None


@dataclass
class SkipVote:
    """\u0413олосование за скип раунда или темы."""
    skip_type: str          # 'round' или 'theme'
    theme_idx: Optional[int]  # только для 'theme'
    votes_for: set = field(default_factory=set)
    votes_against: set = field(default_factory=set)


class Game:
    def __init__(self, chat_id: int, pack: GamePack):
        self.chat_id: int = chat_id
        self.pack: GamePack = pack
        self.state: GameState = GameState.IDLE

        # Один лок на игру: все меняющие состояние действия идут по очереди.
        self._lock: asyncio.Lock = asyncio.Lock()
        self._attempt_counter: int = 0  # порядковый номер попыток ответа

        # Режим ведущего
        self.host_id: Optional[int] = None          # user_id ведущего (если есть)
        self.host_mode: bool = False                # True = есть ведущий

        self.players: Dict[int, Player] = {}

        self.current_round_index: int = 0
        self.current_round: Optional[Round] = None
        self.played_questions: set = set()
        self.skipped_themes: set = set()            # скипнутые темы (t_idx,)

        self.current_question: Optional[Question] = None
        self.current_theme_index: Optional[int] = None
        self.current_question_index: Optional[int] = None

        self.chooser_id: Optional[int] = None

        self.buzzer_queue: List[int] = []
        self.current_answerer_id: Optional[int] = None
        self.answer_attempts: List[AnswerAttempt] = []
        self.failed_answerers: set = set()
        self.passed_players: set = set()
        self.question_answered_correctly: bool = False
        self.correct_answerer_id: Optional[int] = None

        self.last_failed_answerers: set = set()
        self.last_answer_attempts: List[AnswerAttempt] = []
        self.last_question: Optional[Question] = None

        # Апелляция
        self.current_appeal: Optional[Appeal] = None
        self._appeal_task: Optional[asyncio.Task] = None
        self._state_before_appeal: Optional[GameState] = None
        self._appeal_question: Optional[Question] = None
        self._appeal_restore_active: bool = False

        # Скип-голосование
        self.current_skip_vote: Optional[SkipVote] = None
        self._skip_vote_task: Optional[asyncio.Task] = None
        self._state_before_skip: Optional[GameState] = None

        # Таймеры
        self.buzzer_timeout: float = 15.0
        self.answer_timeout: float = 20.0
        self._buzzer_task: Optional[asyncio.Task] = None
        self._answer_task: Optional[asyncio.Task] = None

        # Финал
        self.final_round: Optional[Round] = None       # раунд type="final" из пака
        self.final_themes: List[int] = []              # ещё не убранные темы (индексы)
        self.final_players: List[int] = []             # участники финала (счёт > 0), порядок
        self.final_eliminator_idx: int = 0             # чей сейчас ход убирать тему
        self.final_bet_idx: int = 0                    # чья сейчас очередь ставить
        self.final_question: Optional[Question] = None
        self.final_question_theme_idx: Optional[int] = None
        self.final_bets: Dict[int, int] = {}           # user_id -> ставка
        self.final_answers: Dict[int, str] = {}        # user_id -> текст ответа
        self.final_results: Dict[int, bool] = {}       # user_id -> ответ верный?
        self._final_task: Optional[asyncio.Task] = None

        # Callbacks
        self.send_callback = None
        self.send_photo_callback = None
        self.send_audio_callback = None
        self.send_video_callback = None
        self.show_board_callback = None
        self.show_buzzer_callback = None
        self.remove_buzzer_callback = None
        self.show_scores_callback = None
        self.announce_round_callback = None
        self.announce_game_over_callback = None
        self.show_appeal_callback = None
        self.remove_appeal_callback = None
        self.show_skip_vote_callback = None
        self.remove_skip_vote_callback = None

    # ==================== ИГРОКИ ====================

    def add_player(self, user_id: int, username: str, display_name: str) -> bool:
        # Allow joining in progress games but not in certain final states
        if self.state in (GameState.GAME_OVER, GameState.FINAL_SHOWING_RESULTS):
            return False
        if user_id in self.players:
            return False
        self.players[user_id] = Player(user_id=user_id, username=username,
                                       display_name=display_name, score=0)
        return True

    def remove_player(self, user_id: int) -> bool:
        if user_id in self.players:
            del self.players[user_id]
            return True
        return False

    def get_player(self, user_id: int) -> Optional[Player]:
        return self.players.get(user_id)

    def get_players_list(self) -> List[Player]:
        return list(self.players.values())

    def get_player_count(self) -> int:
        return len(self.players)

    def is_host(self, user_id: int) -> bool:
        return self.host_mode and self.host_id == user_id

    # ==================== ИГРА ====================

    def start_lobby(self):
        self.state = GameState.LOBBY

    @_locked
    async def start_game(self) -> bool:
        if self.state != GameState.LOBBY:
            return False
        if len(self.players) < 1:
            return False
        if not self.pack.rounds:
            return False
        self.current_round_index = 0
        await self._start_round()
        return True

    async def _start_round(self):
        if self.current_round_index >= len(self.pack.rounds):
            await self._end_game()
            return
        self.current_round = self.pack.rounds[self.current_round_index]
        self.played_questions.clear()
        self.skipped_themes.clear()
        self.state = GameState.ROUND_START
        self.chooser_id = self._get_first_chooser()
        if self.announce_round_callback:
            await self.announce_round_callback(self)
        # Финальный раунд из пака играется по особым правилам.
        if self.current_round.round_type == 'final':
            await self._start_final(self.current_round)
            return
        self.state = GameState.CHOOSING_QUESTION
        if self.show_board_callback:
            await self.show_board_callback(self)

    def _get_first_chooser(self) -> int:
        if not self.players:
            return 0
        return min(self.players.values(), key=lambda p: p.score).user_id

    @_locked
    async def select_question(self, user_id: int, theme_idx: int, question_idx: int) -> bool:
        if self.state != GameState.CHOOSING_QUESTION:
            return False
        if user_id != self.chooser_id:
            return False
        if self.current_round is None:
            return False
        if theme_idx < 0 or theme_idx >= len(self.current_round.themes):
            return False
        theme = self.current_round.themes[theme_idx]
        if question_idx < 0 or question_idx >= len(theme.questions):
            return False
        if (theme_idx, question_idx) in self.played_questions:
            return False

        self.current_theme_index = theme_idx
        self.current_question_index = question_idx
        self.current_question = theme.questions[question_idx]
        self.played_questions.add((theme_idx, question_idx))

        self.buzzer_queue.clear()
        self.current_answerer_id = None
        self.answer_attempts.clear()
        self._attempt_counter = 0
        self.failed_answerers.clear()
        self.passed_players.clear()
        self.question_answered_correctly = False
        self.correct_answerer_id = None

        await self._ask_question()
        return True

    async def _ask_question(self):
        self.state = GameState.QUESTION_ASKED
        if self.send_callback:
            theme = self.current_round.themes[self.current_theme_index]
            q = self.current_question
            header = f"🎯 *{theme.name}* за *{q.price}*"
            q_text = (q.text or '').strip()
            if not q_text:
                if q.image:
                    q_text = "🖼 Вопрос с изображением"
                elif q.audio:
                    q_text = "🎧 Вопрос с аудио"
                elif q.video:
                    q_text = "🎥 Вопрос с видео"
                else:
                    q_text = "❓ Вопрос без текста"
            await self.send_callback(self, f"{header}\n\n{q_text}")
        if self.current_question.image and self.send_photo_callback:
            await self.send_photo_callback(self, self.current_question.image,
                                           self.current_question.image_filename)
        if self.current_question.audio and self.send_audio_callback:
            await self.send_audio_callback(self, self.current_question.audio,
                                           self.current_question.audio_filename)
        if self.current_question.video and self.send_video_callback:
            await self.send_video_callback(self, self.current_question.video,
                                           self.current_question.video_filename)
        if self.show_buzzer_callback:
            await self.show_buzzer_callback(self)
        self._cancel_buzzer_timer()
        self._buzzer_task = asyncio.create_task(self._buzzer_timeout_handler())

    async def _buzzer_timeout_handler(self):
        try:
            await asyncio.sleep(self.buzzer_timeout)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.state == GameState.QUESTION_ASKED:
                await self._no_one_answered()

    async def _answer_timeout_handler(self):
        try:
            await asyncio.sleep(self.answer_timeout)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.state == GameState.WAITING_ANSWER and self.current_answerer_id:
                uid = self.current_answerer_id
                # Синтетическая попытка — чтобы таймаут тоже можно было
                # апеллировать и чтобы был корректный порядок ответов.
                self._attempt_counter += 1
                self.answer_attempts.append(AnswerAttempt(
                    user_id=uid, text="(не успел ответить)",
                    timestamp=time.time(), is_correct=False, processed=True,
                    seq=self._attempt_counter,
                ))
                await self._process_wrong_answer(uid)

    def _cancel_buzzer_timer(self):
        if self._buzzer_task and not self._buzzer_task.done():
            self._buzzer_task.cancel()

    def _cancel_answer_timer(self):
        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()

    def _cancel_appeal_timer(self):
        if self._appeal_task and not self._appeal_task.done():
            self._appeal_task.cancel()

    def _cancel_skip_vote_timer(self):
        if self._skip_vote_task and not self._skip_vote_task.done():
            self._skip_vote_task.cancel()

    @_locked
    async def press_buzzer(self, user_id: int) -> bool:
        if self.state not in (GameState.QUESTION_ASKED, GameState.WAITING_ANSWER):
            return False
        if user_id not in self.players:
            return False
        if user_id in self.failed_answerers:
            return False
        if user_id in self.passed_players:
            return False
        if user_id in self.buzzer_queue:
            return False
        if user_id == self.current_answerer_id:
            return False
        self.buzzer_queue.append(user_id)
        if self.state == GameState.QUESTION_ASKED and len(self.buzzer_queue) == 1:
            await self._give_answer_right(user_id)
            return True
        return False

    @_locked
    async def press_pass(self, user_id: int) -> bool:
        if self.state != GameState.QUESTION_ASKED:
            return False
        if user_id not in self.players:
            return False
        if user_id in self.failed_answerers:
            return False
        if user_id in self.passed_players:
            return False
        self.passed_players.add(user_id)
        active = [uid for uid in self.players
                  if uid not in self.failed_answerers and uid not in self.passed_players]
        if not active:
            self._cancel_buzzer_timer()
            if self.remove_buzzer_callback:
                await self.remove_buzzer_callback(self)
            await self._no_one_answered(skip_delay=True)
        return True

    async def _give_answer_right(self, user_id: int):
        self._cancel_buzzer_timer()
        self.current_answerer_id = user_id
        # Удаляем из очереди перед ответом
        if user_id in self.buzzer_queue:
            self.buzzer_queue.remove(user_id)
        self.state = GameState.WAITING_ANSWER
        player = self.players[user_id]
        if self.send_callback:
            name = player.display_name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
            await self.send_callback(
                self,
                f"⚡ *{name}* отвечает! ({self.answer_timeout:.0f} сек)"
            )
        self._cancel_answer_timer()
        self._answer_task = asyncio.create_task(self._answer_timeout_handler())

    @_locked
    async def submit_answer(self, user_id: int, answer_text: str) -> Optional[bool]:
        if self.state != GameState.WAITING_ANSWER:
            return None
        if user_id != self.current_answerer_id:
            return None
        if self.question_answered_correctly:
            return None
        self._cancel_answer_timer()
        self._attempt_counter += 1
        attempt = AnswerAttempt(user_id=user_id, text=answer_text,
                                timestamp=time.time(), seq=self._attempt_counter)
        is_correct = self._check_answer(answer_text, self.current_question.answer)
        attempt.is_correct = is_correct
        attempt.processed = True
        self.answer_attempts.append(attempt)
        if is_correct:
            await self._process_correct_answer(user_id)
            return True
        else:
            await self._process_wrong_answer(user_id)
            return False

    def _check_answer(self, user_answer: str, correct_answer: str) -> bool:
        user_clean = self._normalize(user_answer)
        if not user_clean:
            return False
        correct_variants = correct_answer.split('/')
        for variant in correct_variants:
            variant_clean = self._normalize(variant)
            if not variant_clean:
                continue
            if user_clean == variant_clean:
                return True
            if len(variant_clean) >= 3:
                if user_clean in variant_clean or variant_clean in user_clean:
                    shorter = min(len(user_clean), len(variant_clean))
                    longer = max(len(user_clean), len(variant_clean))
                    if shorter / longer >= 0.7:
                        return True
            distance = self._levenshtein(user_clean, variant_clean)
            max_len = max(len(user_clean), len(variant_clean))
            if max_len > 0 and distance / max_len <= 0.2:
                return True
        return False

    @staticmethod
    def _esc(text: str) -> str:
        """Экранирует спецсимволы MarkdownV1."""
        if not text:
            return ""
        return (text.replace("_", "\\_").replace("*", "\\*")
                    .replace("`", "\\`").replace("[", "\\["))

    def _safe_name(self, player: "Player") -> str:
        """Имя игрока, безопасное для Markdown."""
        return self._esc(player.display_name if player else "Игрок")

    @staticmethod
    def _normalize(text: str) -> str:
        import re
        text = text.lower().strip()
        text = re.sub(r'[^\w\s]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        text = text.replace('ё', 'е')
        return text

    @staticmethod
    def _levenshtein(s1: str, s2: str) -> int:
        if len(s1) < len(s2):
            return Game._levenshtein(s2, s1)
        if len(s2) == 0:
            return len(s1)
        prev_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            curr_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = prev_row[j + 1] + 1
                deletions = curr_row[j] + 1
                substitutions = prev_row[j] + (c1 != c2)
                curr_row.append(min(insertions, deletions, substitutions))
            prev_row = curr_row
        return prev_row[-1]

    async def _process_correct_answer(self, user_id: int):
        self.question_answered_correctly = True
        self.correct_answerer_id = user_id
        player = self.players[user_id]
        price = self.current_question.price
        player.score += price
        # Удаляем из очереди
        if user_id in self.buzzer_queue:
            self.buzzer_queue.remove(user_id)
        self.state = GameState.SHOWING_ANSWER
        if self.send_callback:
            name = player.display_name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
            await self.send_callback(
                self,
                f"✅ *{name}* отвечает правильно!\n"
                f"💰 +{price} очков (всего: {player.score})\n\n"
                f"📝 Правильный ответ: *{self.current_question.answer}*"
            )
        self.chooser_id = user_id
        if self.remove_buzzer_callback:
            await self.remove_buzzer_callback(self)
        self._save_last_question_data()
        await self._after_question()

    async def _process_wrong_answer(self, user_id: int):
        player = self.players[user_id]
        price = self.current_question.price
        player.score -= price
        self.failed_answerers.add(user_id)
        # Удаляем из очереди, чтобы другие могли отвечать
        if user_id in self.buzzer_queue:
            self.buzzer_queue.remove(user_id)
        self.current_answerer_id = None
        if self.send_callback:
            host_hint = " Ведущий может отменить штраф: /host_correct" if self.host_mode else ""
            name = player.display_name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
            await self.send_callback(
                self,
                f"❌ *{name}* отвечает неправильно!\n"
                f"💸 -{price} очков (всего: {player.score})\n"
                f"Можно подать /appeal если ответ верный по смыслу.{host_hint}"
            )
        available_players = [p for p in self.players
                             if p not in self.failed_answerers and p not in self.passed_players]
        if not available_players:
            await self._no_one_answered()
            return
        next_in_queue = None
        for uid in self.buzzer_queue:
            if uid not in self.failed_answerers and uid not in self.passed_players:
                next_in_queue = uid
                break
        if next_in_queue:
            await self._give_answer_right(next_in_queue)
        else:
            if self.remove_buzzer_callback:
                await self.remove_buzzer_callback(self)
            self.state = GameState.QUESTION_ASKED
            if self.show_buzzer_callback:
                await self.show_buzzer_callback(self)
            self._cancel_buzzer_timer()
            self._buzzer_task = asyncio.create_task(self._buzzer_timeout_handler())

    async def _no_one_answered(self, skip_delay: bool = False):
        self.state = GameState.SHOWING_ANSWER
        if self.remove_buzzer_callback:
            await self.remove_buzzer_callback(self)
        if self.send_callback:
            await self.send_callback(
                self,
                f"⏰ Время вышло! Никто не ответил.\n\n"
                f"📝 Правильный ответ: *{self.current_question.answer}*"
            )
        self._save_last_question_data()
        await self._after_question(skip_delay=skip_delay)

    def _save_last_question_data(self):
        self.last_failed_answerers = set(self.failed_answerers)
        self.last_answer_attempts = list(self.answer_attempts)
        self.last_question = self.current_question

    # ==================== АПЕЛЛЯЦИЯ ====================

    @_locked
    async def start_appeal(self, user_id: int, answer_text: str) -> bool:
        if self.current_appeal is not None:
            return False
        if user_id not in self.players:
            return False
        active_question_states = (
            GameState.QUESTION_ASKED,
            GameState.WAITING_ANSWER,
            GameState.SHOWING_ANSWER,
        )
        post_question_states = (GameState.CHOOSING_QUESTION,)
        if self.state in active_question_states:
            # Должен был ошибиться на текущем вопросе (в т.ч. по таймауту).
            if user_id not in self.failed_answerers:
                return False
            question = self.current_question
            attempts = self.answer_attempts
            self._cancel_buzzer_timer()
            self._cancel_answer_timer()
            if self.remove_buzzer_callback:
                await self.remove_buzzer_callback(self)
            self._state_before_appeal = self.state
            restore_to_active = True
        elif self.state in post_question_states:
            if user_id not in self.last_failed_answerers:
                return False
            if self.last_question is None:
                return False
            question = self.last_question
            attempts = self.last_answer_attempts
            self._state_before_appeal = self.state
            restore_to_active = False
        else:
            return False

        if question is None:
            return False

        # Последняя ошибочная попытка игрока (может отсутствовать при таймауте)
        last_attempt = None
        for att in reversed(attempts):
            if att.user_id == user_id and not att.is_correct:
                last_attempt = att
                break

        # Текст ответа: из аргумента (если игрок уточнил) или из попытки
        appeal_answer_text = answer_text or (last_attempt.text if last_attempt else "")
        if not appeal_answer_text:
            appeal_answer_text = "—"

        self.current_appeal = Appeal(
            user_id=user_id,
            answer_text=appeal_answer_text,
            price=question.price,
        )
        self._appeal_question = question
        self._appeal_restore_active = restore_to_active
        self.state = GameState.APPEAL
        player = self.players[user_id]
        if self.send_callback:
            name = self._safe_name(player)
            await self.send_callback(
                self,
                f"⚖️ *{name}* подаёт апелляцию!\n"
                f"Ответ: _{self._esc(appeal_answer_text)}_\n"
                f"Правильный ответ по паку: *{question.answer}*\n\n"
                f"Голосуйте! Засчитать ответ? ({APPEAL_TIMEOUT} сек)"
            )
        if self.show_appeal_callback:
            await self.show_appeal_callback(self)
        self._cancel_appeal_timer()
        self._appeal_task = asyncio.create_task(self._appeal_timeout_handler())
        return True

    @_locked
    async def vote_appeal(self, user_id: int, vote: bool) -> Optional[str]:
        if self.state not in (GameState.APPEAL, GameState.FINAL_APPEAL) \
                or self.current_appeal is None:
            return 'no_appeal'
        if user_id not in self.players:
            return 'not_player'
        appeal = self.current_appeal
        appeal.votes_for.discard(user_id)
        appeal.votes_against.discard(user_id)
        if vote:
            appeal.votes_for.add(user_id)
        else:
            appeal.votes_against.add(user_id)
        if self.show_appeal_callback:
            await self.show_appeal_callback(self)
        total = len(self.players)
        voted = len(appeal.votes_for) + len(appeal.votes_against)
        if voted >= total:
            self._cancel_appeal_timer()
            if self.state == GameState.FINAL_APPEAL:
                await self._resolve_final_appeal()
            else:
                await self._resolve_appeal()
        return 'voted'

    async def _appeal_timeout_handler(self):
        try:
            await asyncio.sleep(APPEAL_TIMEOUT)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.state == GameState.APPEAL:
                await self._resolve_appeal()

    async def _resolve_appeal(self):
        if self.state != GameState.APPEAL or self.current_appeal is None:
            return
        appeal = self.current_appeal
        question = getattr(self, '_appeal_question', self.current_question)
        restore_active = getattr(self, '_appeal_restore_active', False)
        for_votes = len(appeal.votes_for)
        against_votes = len(appeal.votes_against)
        total_voted = for_votes + against_votes
        accepted = for_votes >= against_votes and total_voted > 0
        if self.remove_appeal_callback:
            await self.remove_appeal_callback(self)
        player = self.players.get(appeal.user_id)
        price = appeal.price
        if accepted and player:
            name = self._safe_name(player)
            restore_active = getattr(self, '_appeal_restore_active', False)
            attempts = self.answer_attempts if restore_active else self.last_answer_attempts
            failed_set = self.failed_answerers if restore_active else self.last_failed_answerers

            # Граница: момент ошибочной попытки апеллянта. Все, кто
            # отвечал ПОСЛЕ него, не должны были вообще получить ход —
            # откатываем их очки (штрафы возвращаем, ошибочно начисленные
            # за «верный» ответ снимаем).
            s0 = None
            for att in reversed(attempts):
                if att.user_id == appeal.user_id and not att.is_correct:
                    s0 = att.seq
                    break

            refunded = []
            revoked = []
            if s0 is not None:
                for att in attempts:
                    if att.seq <= s0 or att.user_id == appeal.user_id:
                        continue
                    p = self.players.get(att.user_id)
                    if p is None:
                        continue
                    if att.is_correct:
                        p.score -= price
                        revoked.append(p)
                        if self.correct_answerer_id == att.user_id:
                            self.correct_answerer_id = None
                    else:
                        p.score += price
                        refunded.append(p)
                    failed_set.discard(att.user_id)

            # Награждаем апеллянта: вернуть штраф (если был) + начислить.
            if appeal.user_id in failed_set:
                player.score += price * 2
                failed_set.discard(appeal.user_id)
            else:
                player.score += price
            self.question_answered_correctly = True
            self.correct_answerer_id = appeal.user_id
            self.chooser_id = appeal.user_id

            if self.send_callback:
                extra = ""
                if refunded:
                    names = ", ".join(self._safe_name(p) for p in refunded)
                    extra += f"\n↩️ Возвращён штраф: {names}"
                if revoked:
                    names = ", ".join(self._safe_name(p) for p in revoked)
                    extra += f"\n↩️ Снят ошибочный балл: {names}"
                await self.send_callback(
                    self,
                    f"✅ Апелляция принята! ({for_votes} ЗА / {against_votes} ПРОТИВ)\n"
                    f"💰 *{name}* получает +{price} очков\n"
                    f"Итого: {player.score}{extra}"
                )
        else:
            result_text = "никто не проголосовал" if total_voted == 0 else f"{for_votes} ЗА / {against_votes} ПРОТИВ"
            if self.send_callback:
                await self.send_callback(self, f"❌ Апелляция отклонена ({result_text}).")
        self.current_appeal = None
        prev_state = self._state_before_appeal
        self._state_before_appeal = None
        self._appeal_question = None
        self._appeal_restore_active = None
        if accepted:
            self.last_failed_answerers = set()
            self.state = GameState.SHOWING_ANSWER
            # Принудительно вызываем колбэк доски в _after_question
            await self._after_question(skip_delay=True)
        elif prev_state == GameState.CHOOSING_QUESTION:
            self.last_failed_answerers = set()
            self.state = GameState.CHOOSING_QUESTION
            if self.show_board_callback:
                await self.show_board_callback(self)
        elif restore_active:
            available = [p for p in self.players
                         if p not in self.failed_answerers and p not in self.passed_players]
            if not available:
                self.state = GameState.SHOWING_ANSWER
                await self._after_question()
            else:
                if self.remove_buzzer_callback:
                    await self.remove_buzzer_callback(self)
                self.state = GameState.QUESTION_ASKED
                if self.show_buzzer_callback:
                    await self.show_buzzer_callback(self)
                self._cancel_buzzer_timer()
                self._buzzer_task = asyncio.create_task(self._buzzer_timeout_handler())
        else:
            self.state = GameState.SHOWING_ANSWER
            await self._after_question()

    # ==================== СКИП-ГОЛОСОВАНИЕ ====================

    @_locked
    async def start_skip_vote(self, initiator_id: int, skip_type: str,
                              theme_idx: Optional[int] = None) -> bool:
        """
        Инициировать голосование за скип.
        skip_type: 'round' | 'theme'
        theme_idx: индекс темы (только для skip_type='theme')
        """
        if self.state != GameState.CHOOSING_QUESTION:
            return False
        if initiator_id not in self.players:
            return False
        if self.current_skip_vote is not None:
            return False
        if skip_type == 'theme':
            if theme_idx is None or self.current_round is None:
                return False
            if theme_idx < 0 or theme_idx >= len(self.current_round.themes):
                return False
            # нельзя скипать уже скипнутую
            if theme_idx in self.skipped_themes:
                return False
        self._state_before_skip = self.state
        self.current_skip_vote = SkipVote(
            skip_type=skip_type,
            theme_idx=theme_idx,
            votes_for={initiator_id},
        )
        self.state = GameState.SKIP_VOTE
        player = self.players[initiator_id]
        if skip_type == 'round':
            label = f"раунд *{self.current_round.name}*"
        else:
            theme_name = self.current_round.themes[theme_idx].name
            label = f"тему *{theme_name}*"
        if self.send_callback:
            name = player.display_name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
            await self.send_callback(
                self,
                f"⏩ *{name}* предлагает пропустить {label}\n"
                f"Голосуйте! ({SKIP_VOTE_TIMEOUT} сек)"
            )
        if self.show_skip_vote_callback:
            await self.show_skip_vote_callback(self)
        self._cancel_skip_vote_timer()
        self._skip_vote_task = asyncio.create_task(self._skip_vote_timeout_handler())
        return True

    @_locked
    async def vote_skip(self, user_id: int, vote: bool) -> str:
        if self.state != GameState.SKIP_VOTE or self.current_skip_vote is None:
            return 'no_vote'
        if user_id not in self.players:
            return 'not_player'
        sv = self.current_skip_vote
        sv.votes_for.discard(user_id)
        sv.votes_against.discard(user_id)
        if vote:
            sv.votes_for.add(user_id)
        else:
            sv.votes_against.add(user_id)
        if self.show_skip_vote_callback:
            await self.show_skip_vote_callback(self)
        total = len(self.players)
        voted = len(sv.votes_for) + len(sv.votes_against)
        if voted >= total:
            self._cancel_skip_vote_timer()
            await self._resolve_skip_vote()
        return 'voted'

    async def _skip_vote_timeout_handler(self):
        try:
            await asyncio.sleep(SKIP_VOTE_TIMEOUT)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.state == GameState.SKIP_VOTE:
                await self._resolve_skip_vote()

    async def _resolve_skip_vote(self):
        if self.state != GameState.SKIP_VOTE or self.current_skip_vote is None:
            return
        sv = self.current_skip_vote
        for_v = len(sv.votes_for)
        against_v = len(sv.votes_against)
        total_voted = for_v + against_v
        accepted = for_v > against_v or (total_voted > 0 and against_v == 0)
        if self.remove_skip_vote_callback:
            await self.remove_skip_vote_callback(self)
        self.current_skip_vote = None
        self.state = self._state_before_skip or GameState.CHOOSING_QUESTION
        self._state_before_skip = None
        if accepted:
            if sv.skip_type == 'round':
                if self.send_callback:
                    await self.send_callback(self,
                        f"⏩ Раунд пропущен голосованием ({for_v} ЗА / {against_v} ПРОТИВ).")
                await self._end_round()
            else:
                # Скип темы: отмечаем все вопросы темы как сыгранные
                t_idx = sv.theme_idx
                self.skipped_themes.add(t_idx)
                if self.current_round:
                    theme = self.current_round.themes[t_idx]
                    for q_idx in range(len(theme.questions)):
                        self.played_questions.add((t_idx, q_idx))
                    if self.send_callback:
                        await self.send_callback(self,
                            f"⏩ Тема *{theme.name}* пропущена ({for_v} ЗА / {against_v} ПРОТИВ).")
                if self._is_round_complete():
                    await self._end_round()
                else:
                    self.state = GameState.CHOOSING_QUESTION
                    if self.show_board_callback:
                        await self.show_board_callback(self)
        else:
            result = "никто не проголосовал" if total_voted == 0 else f"{for_v} ЗА / {against_v} ПРОТИВ"
            if self.send_callback:
                await self.send_callback(self, f"❌ Скип отклонён ({result}).")
            self.state = GameState.CHOOSING_QUESTION
            if self.show_board_callback:
                await self.show_board_callback(self)

    # ==================== РЕЖИМ ВЕДУЩЕГО ====================

    def host_adjust_score(self, host_id: int, target_id: int, delta: int) -> bool:
        """Ведущий изменяет счёт игрока."""
        if not self.is_host(host_id):
            return False
        player = self.players.get(target_id)
        if player is None:
            return False
        player.score += delta
        return True

    @_locked
    async def host_mark_correct(self, host_id: int) -> bool:
        """
        Ведущий засчитывает ответ текущего отвечающего (или последнего
        ошибившегося) как верный. Полная обработка здесь, без логики в bot.py.
        """
        if not self.is_host(host_id):
            return False
        if self.state not in (GameState.WAITING_ANSWER, GameState.SHOWING_ANSWER,
                               GameState.QUESTION_ASKED):
            return False
        target_id = self.current_answerer_id
        if target_id is None:
            for att in reversed(self.answer_attempts):
                if not att.is_correct:
                    target_id = att.user_id
                    break
        if target_id is None or target_id not in self.players:
            return False
        q = self.current_question
        if q is None:
            return False
        self._cancel_answer_timer()
        self._cancel_buzzer_timer()
        if self.remove_buzzer_callback:
            await self.remove_buzzer_callback(self)
        player = self.players[target_id]
        if target_id in self.failed_answerers:
            player.score += q.price * 2  # вернуть штраф + начислить
            self.failed_answerers.discard(target_id)
        else:
            player.score += q.price
        self.question_answered_correctly = True
        self.correct_answerer_id = target_id
        self.chooser_id = target_id
        self.current_answerer_id = None
        self.state = GameState.SHOWING_ANSWER
        if self.send_callback:
            name = self._safe_name(player)
            await self.send_callback(
                self,
                f"✅ Ведущий засчитал ответ *{name}*!\n"
                f"💰 Счёт: {player.score}"
            )
        self._save_last_question_data()
        await self._after_question()
        return True

    @_locked
    async def host_mark_wrong(self, host_id: int) -> bool:
        """Ведущий снимает ответ текущего отвечающего как неверный."""
        if not self.is_host(host_id):
            return False
        if self.state != GameState.WAITING_ANSWER:
            return False
        target_id = self.current_answerer_id
        if target_id is None or target_id not in self.players:
            return False
        await self._process_wrong_answer(target_id)
        return True

    @_locked
    async def host_skip_round(self, host_id: int) -> bool:
        """Ведущий принудительно завершает раунд."""
        if not self.is_host(host_id):
            return False
        if self.state not in (GameState.CHOOSING_QUESTION, GameState.ROUND_START):
            return False
        await self._end_round()
        return True

    @_locked
    async def host_skip_theme(self, host_id: int, theme_idx: int) -> bool:
        """Ведущий принудительно скипает тему."""
        if not self.is_host(host_id):
            return False
        if self.state != GameState.CHOOSING_QUESTION or self.current_round is None:
            return False
        if theme_idx < 0 or theme_idx >= len(self.current_round.themes):
            return False
        self.skipped_themes.add(theme_idx)
        theme = self.current_round.themes[theme_idx]
        for q_idx in range(len(theme.questions)):
            self.played_questions.add((theme_idx, q_idx))
        if self._is_round_complete():
            await self._end_round()
        else:
            self.state = GameState.CHOOSING_QUESTION
            if self.show_board_callback:
                await self.show_board_callback(self)
        return True

    # ==================== ПОСЛЕ ВОПРОСА ====================

    async def _after_question(self, skip_delay: bool = False):
        if not skip_delay:
            await asyncio.sleep(2)
        if self._is_round_complete():
            await self._end_round()
        else:
            self.state = GameState.CHOOSING_QUESTION
            # Убеждаемся, что колбэк вызывается
            if self.show_board_callback:
                await self.show_board_callback(self)

    def _is_round_complete(self) -> bool:
        if self.current_round is None:
            return True
        total = sum(len(t.questions) for t in self.current_round.themes)
        return len(self.played_questions) >= total

    async def _end_round(self):
        self.state = GameState.ROUND_END
        if self.show_scores_callback:
            await self.show_scores_callback(self)
        self.current_round_index += 1
        await asyncio.sleep(3)
        if self.current_round_index < len(self.pack.rounds):
            # _start_round сам определит, что следующий раунд — финал.
            await self._start_round()
        else:
            await self._end_game()

    async def _end_game(self):
        self.state = GameState.GAME_OVER
        if self.announce_game_over_callback:
            await self.announce_game_over_callback(self)

    # ==================== ИНФОРМАЦИЯ ====================

    def get_board(self) -> List[dict]:
        if self.current_round is None:
            return []
        board = []
        for t_idx, theme in enumerate(self.current_round.themes):
            theme_data = {'theme_idx': t_idx, 'theme_name': theme.name, 'questions': [],
                          'skipped': t_idx in self.skipped_themes}
            for q_idx, question in enumerate(theme.questions):
                theme_data['questions'].append({
                    'q_idx': q_idx,
                    'price': question.price,
                    'played': (t_idx, q_idx) in self.played_questions
                })
            board.append(theme_data)
        return board

    _FINAL_STATES = (
        GameState.FINAL_THEME_ELIMINATION, GameState.FINAL_BETTING,
        GameState.FINAL_SHOWING_QUESTION, GameState.FINAL_COUNTDOWN,
        GameState.FINAL_ANSWER_WINDOW, GameState.FINAL_SHOWING_RESULTS,
        GameState.FINAL_APPEAL,
    )

    def get_board_text(self) -> str:
        # Для финала используем специальную функцию
        if self.state in self._FINAL_STATES:
            return self.get_final_board_text()
        
        board = self.get_board()
        if not board:
            return "Доска пуста"
        lines = [f"📋 *{self.current_round.name}*\n"]
        chooser = self.players.get(self.chooser_id)
        if chooser:
            c_name = chooser.display_name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
            lines.append(f"🎯 Выбирает: *{c_name}*\n")
        if self.host_mode and self.host_id:
            host_p = self.players.get(self.host_id)
            hname = host_p.display_name if host_p else str(self.host_id)
            hname = hname.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
            lines.append(f"🎤 Ведущий: *{hname}*\n")
        for theme_data in board:
            prices = []
            for q in theme_data['questions']:
                if q['played']:
                    prices.append("~~" + str(q['price']) + "~~")
                else:
                    prices.append(f"*{q['price']}*")
            skip_mark = " ⏩" if theme_data['skipped'] else ""
            lines.append(f"📌 {theme_data['theme_name']}{skip_mark}: {' | '.join(prices)}")
        return '\n'.join(lines)

    def get_scores_text(self) -> str:
        if not self.players:
            return "Нет игроков"
        sorted_players = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
        lines = ["🏆 *Счёт:*\n"]
        medals = ['🥇', '🥈', '🥉']
        for i, player in enumerate(sorted_players):
            medal = medals[i] if i < len(medals) else f"{i + 1}."
            # Экранируем имя игрока для Markdown
            name = player.display_name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
            lines.append(f"{medal} {name}: *{player.score}*")
        return '\n'.join(lines)

    def get_final_results_text(self) -> str:
        if not self.players:
            return "Нет игроков"
        sorted_players = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
        lines = ["🎉 *ИГРА ОКОНЧЕНА!*\n", "🏆 *Итоговый счёт:*\n"]
        medals = ['🥇', '🥈', '🥉']
        for i, player in enumerate(sorted_players):
            medal = medals[i] if i < len(medals) else f"{i + 1}."
            name = player.display_name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
            lines.append(f"{medal} {name}: *{player.score}*")
        if sorted_players:
            winner = sorted_players[0]
            w_name = winner.display_name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
            lines.append(f"\n👑 Победитель: *{w_name}*!")
        return '\n'.join(lines)

    def get_available_questions(self) -> List[Tuple[int, int, str, int]]:
        if self.current_round is None:
            return []
        available = []
        for t_idx, theme in enumerate(self.current_round.themes):
            for q_idx, question in enumerate(theme.questions):
                if (t_idx, q_idx) not in self.played_questions:
                    available.append((t_idx, q_idx, theme.name, question.price))
        return available

    def get_final_board(self) -> List[dict]:
        """Получить доску для финала - только оставшиеся темы."""
        if self.current_round is None or not self.final_themes:
            return []
            
        board = []
        for t_idx in self.final_themes:
            if t_idx < len(self.current_round.themes):
                theme = self.current_round.themes[t_idx]
                theme_data = {
                    'theme_idx': t_idx,
                    'theme_name': theme.name,
                    'questions': [],
                    'skipped': False
                }
                board.append(theme_data)
        return board

    def _build_final_keyboard(self) -> Any:
        """Клавиатура для выбора темы на исключение в финале."""
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        if self.state != GameState.FINAL_THEME_ELIMINATION:
            return InlineKeyboardMarkup(inline_keyboard=[])
            
        rows = []
        for t_idx in self.final_themes:
            if t_idx < len(self.current_round.themes):
                theme = self.current_round.themes[t_idx]
                rows.append([InlineKeyboardButton(
                    text="❌ {}".format(theme.name),
                    callback_data="final_eliminate_{}".format(t_idx)
                )])
        
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def get_appeal_status_text(self) -> str:
        if self.current_appeal is None:
            return ""
        a = self.current_appeal
        player = self.players.get(a.user_id)
        name = player.display_name if player else "Игрок"
        name = name.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
        for_v = len(a.votes_for)
        against_v = len(a.votes_against)
        total = len(self.players)
        return (
            f"⚖️ *Апелляция от {name}*\n"
            f"Ответ: _{a.answer_text}_\n\n"
            f"👍 За: {for_v}  |  👎 Против: {against_v}\n"
            f"Проголосовало: {for_v + against_v}/{total}"
        )

    def get_skip_vote_text(self) -> str:
        if self.current_skip_vote is None:
            return ""
        sv = self.current_skip_vote
        for_v = len(sv.votes_for)
        against_v = len(sv.votes_against)
        total = len(self.players)
        if sv.skip_type == 'round':
            label = f"раунд *{self.current_round.name}*"
        else:
            theme_name = self.current_round.themes[sv.theme_idx].name
            label = f"тему *{theme_name}*"
        return (
            f"⏩ *Голосование: пропустить {label}*\n\n"
            f"👍 За: {for_v}  |  👎 Против: {against_v}\n"
            f"Проголосовами: {for_v + against_v}/{total}"
        )

    def get_final_board_text(self) -> str:
        """Текст для отображения финальной доски."""
        if self.state not in self._FINAL_STATES:
            return ""

        board = self.get_final_board()
        if not board:
            return "Финальная доска пуста"

        lines = ["🎯 *Финальные темы:*\n"]

        if self.state == GameState.FINAL_THEME_ELIMINATION:
            uid = self._final_current_eliminator()
            eliminator = self.players.get(uid) if uid else None
            if eliminator:
                lines.append(f"❌ Убирает: *{self._safe_name(eliminator)}*\n")

        for theme_data in board:
            lines.append(f"📌 {theme_data['theme_name']}")

        if self.state == GameState.FINAL_THEME_ELIMINATION:
            lines.append("\n👇 Нажмите на тему, чтобы убрать её!")

        return '\n'.join(lines)

    # ==================== ОЧИСТКА ====================

    def cleanup(self):
        self._cancel_buzzer_timer()
        self._cancel_answer_timer()
        self._cancel_appeal_timer()
        self._cancel_skip_vote_timer()
        self._cancel_final_timer()

    def reset(self):
        self.cleanup()
        self.state = GameState.IDLE
        self.players.clear()
        self.host_id = None
        self.host_mode = False
        self.current_round_index = 0
        self.current_round = None
        self.played_questions.clear()
        self.skipped_themes.clear()
        self.current_question = None
        self.current_theme_index = None
        self.current_question_index = None
        self.chooser_id = None
        self.buzzer_queue.clear()
        self.current_answerer_id = None
        self.answer_attempts.clear()
        self.failed_answerers.clear()
        self.passed_players.clear()
        self.question_answered_correctly = False
        self.correct_answerer_id = None
        self.current_appeal = None
        self._state_before_appeal = None
        self.last_failed_answerers = set()
        self.last_answer_attempts = []
        self.last_question = None
        self.current_skip_vote = None
        self._state_before_skip = None
        self.cleanup_final()

    # ==================== ФИНАЛ ====================

    def _final_player_label(self, uid: int) -> str:
        p = self.players.get(uid)
        return self._safe_name(p) if p else "Игрок"

    async def _start_final(self, final_round: Round):
        """
        Запустить финал из пака (раунд type='final').

        Порядок: убираем темы по очереди -> ставки вслепую (тема
        известна, вопрос скрыт) -> показ вопроса -> отсчёт ->
        окно ответа в чат -> результаты -> апелляции.
        В финал проходят только игроки со счётом > 0.
        """
        self.final_round = final_round
        self.current_question = None
        self.current_answerer_id = None
        self.buzzer_queue.clear()
        self.answer_attempts.clear()
        self.failed_answerers.clear()
        self.passed_players.clear()
        self.current_appeal = None
        self.final_bets.clear()
        self.final_answers.clear()
        self.final_results.clear()
        self.final_eliminator_idx = 0
        self.final_bet_idx = 0

        eligible = [uid for uid, p in self.players.items() if p.score > 0]
        eligible.sort(key=lambda uid: self.players[uid].score)  # отстающий ходит первым
        self.final_players = eligible
        self.final_themes = [i for i, t in enumerate(final_round.themes) if t.questions]

        if not self.final_players:
            if self.send_callback:
                await self.send_callback(
                    self,
                    "🏁 *Финал*\nНи у кого нет положительного счёта — "
                    "финал не играется."
                )
            await self._end_game()
            return
        if not self.final_themes:
            if self.send_callback:
                await self.send_callback(
                    self, "🏁 Финал: в финальном раунде нет тем с вопросами.")
            await self._end_game()
            return

        if self.send_callback:
            names = ", ".join(self._final_player_label(u) for u in self.final_players)
            await self.send_callback(
                self,
                f"🏁 *ФИНАЛ!*\n\n"
                f"Участники (счёт > 0): {names}\n\n"
                f"Игроки по очереди убирают по одной теме, пока не "
                f"останется одна. Первым убирает отстающий."
            )

        if len(self.final_themes) <= 1:
            await self._final_start_betting()
        else:
            self.state = GameState.FINAL_THEME_ELIMINATION
            await self._final_prompt_eliminator()

    def _final_current_eliminator(self) -> Optional[int]:
        if not self.final_players:
            return None
        return self.final_players[self.final_eliminator_idx % len(self.final_players)]

    async def _final_prompt_eliminator(self):
        uid = self._final_current_eliminator()
        if uid is None:
            return
        if self.send_callback:
            await self.send_callback(
                self,
                f"❌ *{self._final_player_label(uid)}*, уберите одну тему "
                f"(осталось тем: {len(self.final_themes)})."
            )
        if self.show_board_callback:
            await self.show_board_callback(self)

    @_locked
    async def finalize_theme_elimination(self, user_id: int, theme_idx: int) -> bool:
        if self.state != GameState.FINAL_THEME_ELIMINATION:
            return False
        if user_id != self._final_current_eliminator():
            return False
        if theme_idx not in self.final_themes:
            return False
        self.final_themes.remove(theme_idx)
        theme_name = (self.final_round.themes[theme_idx].name
                      if self.final_round else str(theme_idx))
        if self.send_callback:
            await self.send_callback(
                self,
                f"➖ *{self._final_player_label(user_id)}* убирает тему: "
                f"~~{self._esc(theme_name)}~~"
            )
        if len(self.final_themes) <= 1:
            await self._final_start_betting()
        else:
            self.final_eliminator_idx += 1
            await self._final_prompt_eliminator()
        return True

    # ---------- Ставки (тема известна, вопрос скрыт) ----------

    async def _final_start_betting(self):
        self.state = GameState.FINAL_BETTING
        self.final_bet_idx = 0
        self.final_bets.clear()
        theme_idx = self.final_themes[0]
        self.final_question_theme_idx = theme_idx
        theme = self.final_round.themes[theme_idx]
        self.final_question = theme.questions[0] if theme.questions else None
        if self.send_callback:
            await self.send_callback(
                self,
                f"🎯 *Финальная тема: {self._esc(theme.name)}*\n\n"
                f"Ставки вслепую — вопрос ещё не показан.\n"
                f"Каждый по очереди отправляет в чат число — свою ставку."
            )
        await self._final_prompt_bettor()

    def _final_current_bettor(self) -> Optional[int]:
        if self.final_bet_idx >= len(self.final_players):
            return None
        return self.final_players[self.final_bet_idx]

    async def _final_prompt_bettor(self):
        uid = self._final_current_bettor()
        if uid is None:
            await self._final_show_question()
            return
        player = self.players.get(uid)
        if player is None:
            self.final_bet_idx += 1
            await self._final_prompt_bettor()
            return
        if self.send_callback:
            await self.send_callback(
                self,
                f"💰 *{self._safe_name(player)}*, ваша ставка?\n"
                f"Отправьте число от 1 до {player.score}. "
                f"({FINAL_BET_TIMEOUT} сек, иначе ставка = 1)"
            )
        self._cancel_final_timer()
        self._final_task = asyncio.create_task(
            self._final_bet_timeout(self.final_bet_idx))

    async def _final_bet_timeout(self, idx: int):
        try:
            await asyncio.sleep(FINAL_BET_TIMEOUT)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.state == GameState.FINAL_BETTING and self.final_bet_idx == idx:
                uid = self._final_current_bettor()
                if uid is not None:
                    self.final_bets[uid] = 1
                    if self.send_callback:
                        await self.send_callback(
                            self,
                            f"⏰ *{self._final_player_label(uid)}* не успел — "
                            f"ставка 1."
                        )
                    self.final_bet_idx += 1
                    await self._final_prompt_bettor()

    @_locked
    async def submit_final_bet(self, user_id: int, bet: int) -> Optional[str]:
        if self.state != GameState.FINAL_BETTING:
            return 'not_betting'
        if user_id != self._final_current_bettor():
            return 'not_your_turn'
        player = self.players.get(user_id)
        if player is None:
            return 'no_player'
        if bet < 1 or bet > player.score:
            return 'bad_amount'
        self.final_bets[user_id] = bet
        self._cancel_final_timer()
        if self.send_callback:
            await self.send_callback(
                self, f"✅ *{self._safe_name(player)}* поставил *{bet}*.")
        self.final_bet_idx += 1
        await self._final_prompt_bettor()
        return 'ok'

    # ---------- Вопрос -> отсчёт -> окно ответа ----------

    async def _final_show_question(self):
        q = self.final_question
        if q is None:
            if self.send_callback:
                await self.send_callback(self, "⚠️ В финальной теме нет вопроса.")
            await self._final_evaluate()
            return
        self.state = GameState.FINAL_SHOWING_QUESTION
        theme = self.final_round.themes[self.final_question_theme_idx]
        if self.send_callback:
            q_text = (q.text or '').strip()
            if not q_text:
                if q.image:
                    q_text = "🖼 Вопрос с изображением"
                elif q.audio:
                    q_text = "🎧 Вопрос с аудио"
                elif q.video:
                    q_text = "🎥 Вопрос с видео"
                else:
                    q_text = "❓ Вопрос без текста"
            await self.send_callback(
                self, f"🎯 *Финал — {self._esc(theme.name)}*\n\n{q_text}")
        if q.image and self.send_photo_callback:
            await self.send_photo_callback(self, q.image, q.image_filename)
        if q.audio and self.send_audio_callback:
            await self.send_audio_callback(self, q.audio, q.audio_filename)
        if q.video and self.send_video_callback:
            await self.send_video_callback(self, q.video, q.video_filename)
        await self._final_countdown()

    async def _final_countdown(self):
        self.state = GameState.FINAL_COUNTDOWN
        if self.send_callback:
            await self.send_callback(
                self,
                f"⏱️ Готовьтесь! Окно для ответа откроется через "
                f"{FINAL_COUNTDOWN_SECONDS} сек."
            )
        self._cancel_final_timer()
        self._final_task = asyncio.create_task(self._final_countdown_handler())

    async def _final_countdown_handler(self):
        try:
            await asyncio.sleep(FINAL_COUNTDOWN_SECONDS)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.state == GameState.FINAL_COUNTDOWN:
                await self._final_open_window()

    async def _final_open_window(self):
        self.state = GameState.FINAL_ANSWER_WINDOW
        self.final_answers.clear()
        if self.send_callback:
            await self.send_callback(
                self,
                f"✍️ *Пишите ответ в чат!* У вас {FINAL_ANSWER_WINDOW} секунд. "
                f"Засчитывается первое сообщение."
            )
        self._cancel_final_timer()
        self._final_task = asyncio.create_task(self._final_window_handler())

    async def _final_window_handler(self):
        try:
            await asyncio.sleep(FINAL_ANSWER_WINDOW)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.state == GameState.FINAL_ANSWER_WINDOW:
                await self._final_evaluate()

    @_locked
    async def record_final_answer(self, user_id: int, text: str) -> bool:
        """Игрок прислал ответ в окне финала (первое сообщение засчитывается)."""
        if self.state != GameState.FINAL_ANSWER_WINDOW:
            return False
        if user_id not in self.final_players:
            return False
        if user_id in self.final_answers:
            return False
        self.final_answers[user_id] = text
        return True

    # ---------- Результаты ----------

    async def _final_evaluate(self):
        self.state = GameState.FINAL_SHOWING_RESULTS
        q = self.final_question
        self.final_results.clear()
        for uid in self.final_players:
            ans = self.final_answers.get(uid, "")
            correct = bool(ans) and q is not None and self._check_answer(ans, q.answer)
            self.final_results[uid] = correct
            player = self.players.get(uid)
            if player is None:
                continue
            bet = self.final_bets.get(uid, 1)
            player.score += bet if correct else -bet
        await self._final_send_results(header="📊 *Результаты финала:*")
        self._arm_final_results_timer()

    def _arm_final_results_timer(self):
        self._cancel_final_timer()
        self._final_task = asyncio.create_task(self._final_results_timeout())

    async def _final_results_timeout(self):
        try:
            await asyncio.sleep(FINAL_RESULTS_APPEAL_WINDOW)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.state == GameState.FINAL_SHOWING_RESULTS:
                await self._end_game()

    async def _final_send_results(self, header: str):
        q = self.final_question
        lines = [header, ""]
        if q is not None:
            lines.append(f"📝 Правильный ответ: *{q.answer}*")
            lines.append("")
        for uid in self.final_players:
            player = self.players.get(uid)
            if player is None:
                continue
            correct = self.final_results.get(uid, False)
            bet = self.final_bets.get(uid, 1)
            ans = self.final_answers.get(uid) or "—"
            status = "✅" if correct else "❌"
            sign = "+" if correct else "−"
            lines.append(
                f"{status} *{self._safe_name(player)}*: _{self._esc(ans)}_ "
                f"({sign}{bet} → {player.score})"
            )
        lines.append("")
        lines.append("Не согласны? /appeal — голосование пересмотрит результат.")
        if self.send_callback:
            await self.send_callback(self, "\n".join(lines))

    # ---------- Апелляция в финале ----------

    @_locked
    async def start_final_appeal(self, user_id: int, answer_text: str) -> bool:
        if self.state != GameState.FINAL_SHOWING_RESULTS:
            return False
        if self.current_appeal is not None:
            return False
        if user_id not in self.final_players:
            return False
        if user_id not in self.final_results:
            return False
        self._cancel_final_timer()
        q = self.final_question
        ans = answer_text or self.final_answers.get(user_id, "") or "—"
        self.current_appeal = Appeal(
            user_id=user_id,
            answer_text=ans,
            price=self.final_bets.get(user_id, 1),
        )
        self._appeal_question = q
        self._state_before_appeal = GameState.FINAL_SHOWING_RESULTS
        self.state = GameState.FINAL_APPEAL
        if self.send_callback:
            await self.send_callback(
                self,
                f"⚖️ *Финальная апелляция от "
                f"{self._final_player_label(user_id)}*\n"
                f"Ответ: _{self._esc(ans)}_\n"
                f"Правильный по паку: *{q.answer if q else '—'}*\n\n"
                f"Голосуйте! ({FINAL_APPEAL_TIMEOUT} сек)"
            )
        if self.show_appeal_callback:
            await self.show_appeal_callback(self)
        self._cancel_appeal_timer()
        self._appeal_task = asyncio.create_task(self._final_appeal_timeout_handler())
        return True

    async def _final_appeal_timeout_handler(self):
        try:
            await asyncio.sleep(FINAL_APPEAL_TIMEOUT)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self.state == GameState.FINAL_APPEAL:
                await self._resolve_final_appeal()

    async def _resolve_final_appeal(self):
        if self.state != GameState.FINAL_APPEAL or self.current_appeal is None:
            return
        appeal = self.current_appeal
        for_v = len(appeal.votes_for)
        against_v = len(appeal.votes_against)
        total_voted = for_v + against_v
        accepted = total_voted > 0 and for_v >= against_v
        if self.remove_appeal_callback:
            await self.remove_appeal_callback(self)
        player = self.players.get(appeal.user_id)
        if accepted and player:
            old = self.final_results.get(appeal.user_id, False)
            new = not old
            self.final_results[appeal.user_id] = new
            bet = self.final_bets.get(appeal.user_id, 1)
            old_delta = bet if old else -bet
            new_delta = bet if new else -bet
            player.score += (new_delta - old_delta)
            if self.send_callback:
                await self.send_callback(
                    self,
                    f"✅ Апелляция принята ({for_v} ЗА / {against_v} ПРОТИВ). "
                    f"Ответ *{self._safe_name(player)}* теперь "
                    f"{'засчитан' if new else 'не засчитан'}."
                )
        else:
            txt = ("никто не проголосовал" if total_voted == 0
                   else f"{for_v} ЗА / {against_v} ПРОТИВ")
            if self.send_callback:
                await self.send_callback(self, f"❌ Апелляция отклонена ({txt}).")
        self.current_appeal = None
        self._appeal_question = None
        self._state_before_appeal = None
        self.state = GameState.FINAL_SHOWING_RESULTS
        await self._final_send_results(
            header="📊 *Результаты финала (после апелляции):*")
        self._arm_final_results_timer()

    def _cancel_final_timer(self):
        if self._final_task and not self._final_task.done():
            self._final_task.cancel()

    def cleanup_final(self):
        self._cancel_final_timer()
        self.final_round = None
        self.final_themes.clear()
        self.final_players.clear()
        self.final_eliminator_idx = 0
        self.final_bet_idx = 0
        self.final_question = None
        self.final_question_theme_idx = None
        self.final_bets.clear()
        self.final_answers.clear()
        self.final_results.clear()


class GameManager:
    def __init__(self):
        self.games: Dict[int, Game] = {}
        self.packs: Dict[int, GamePack] = {}

    def create_game(self, chat_id: int, pack: GamePack) -> Game:
        if chat_id in self.games:
            self.games[chat_id].cleanup()
        game = Game(chat_id=chat_id, pack=pack)
        self.games[chat_id] = game
        return game

    def get_game(self, chat_id: int) -> Optional[Game]:
        return self.games.get(chat_id)

    def remove_game(self, chat_id: int):
        if chat_id in self.games:
            self.games[chat_id].cleanup()
            del self.games[chat_id]

    def store_pack(self, chat_id: int, pack: GamePack):
        self.packs[chat_id] = pack

    def get_pack(self, chat_id: int) -> Optional[GamePack]:
        return self.packs.get(chat_id)

    def has_active_game(self, chat_id: int) -> bool:
        game = self.games.get(chat_id)
        if game is None:
            return False
        return game.state not in (GameState.IDLE, GameState.GAME_OVER)
