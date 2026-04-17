"""
Логика игры "Своя Игра" для Telegram.

Управляет состоянием игры: раунды, выбор вопросов, ответы,
подсчёт очков, таймауты, апелляции, пас, режим ведущего,
голосование за скип.
"""

import asyncio
import time
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple
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


SKIP_VOTE_TIMEOUT = 20  # секунд на голосование за скип
APPEAL_TIMEOUT = 20


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
        if self.state != GameState.LOBBY:
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
        self.state = GameState.CHOOSING_QUESTION
        if self.show_board_callback:
            await self.show_board_callback(self)

    def _get_first_chooser(self) -> int:
        if not self.players:
            return 0
        return min(self.players.values(), key=lambda p: p.score).user_id

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
            if self.state == GameState.QUESTION_ASKED:
                await self._no_one_answered()
        except asyncio.CancelledError:
            pass

    async def _answer_timeout_handler(self):
        try:
            await asyncio.sleep(self.answer_timeout)
            if self.state == GameState.WAITING_ANSWER and self.current_answerer_id:
                await self._process_wrong_answer(self.current_answerer_id)
        except asyncio.CancelledError:
            pass

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
        self.state = GameState.WAITING_ANSWER
        player = self.players[user_id]
        if self.send_callback:
            await self.send_callback(
                self,
                f"⚡ *{player.display_name}* отвечает! ({self.answer_timeout:.0f} сек)"
            )
        self._cancel_answer_timer()
        self._answer_task = asyncio.create_task(self._answer_timeout_handler())

    async def submit_answer(self, user_id: int, answer_text: str) -> Optional[bool]:
        if self.state != GameState.WAITING_ANSWER:
            return None
        if user_id != self.current_answerer_id:
            return None
        if self.question_answered_correctly:
            return None
        self._cancel_answer_timer()
        attempt = AnswerAttempt(user_id=user_id, text=answer_text, timestamp=time.time())
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
        self.state = GameState.SHOWING_ANSWER
        if self.send_callback:
            await self.send_callback(
                self,
                f"✅ *{player.display_name}* отвечает правильно!\n"
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
        self.current_answerer_id = None
        if self.send_callback:
            host_hint = " Ведущий может отменить штраф: /host_correct" if self.host_mode else ""
            await self.send_callback(
                self,
                f"❌ *{player.display_name}* отвечает неправильно!\n"
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
            if user_id not in self.failed_answerers:
                return False
            last_attempt = None
            for att in reversed(self.answer_attempts):
                if att.user_id == user_id and not att.is_correct:
                    last_attempt = att
                    break
            if last_attempt is None:
                return False
            question = self.current_question
            self._cancel_buzzer_timer()
            self._cancel_answer_timer()
            self._state_before_appeal = self.state
            restore_to_active = True
        elif self.state in post_question_states:
            if user_id not in self.last_failed_answerers:
                return False
            if self.last_question is None:
                return False
            question = self.last_question
            # Ищем последнюю попытку игрока (может отсутствовать при таймауте)
            last_attempt = None
            for att in reversed(self.last_answer_attempts):
                if att.user_id == user_id and not att.is_correct:
                    last_attempt = att
                    break
            self._state_before_appeal = self.state
            restore_to_active = False
        else:
            return False

        # Текст ответа для апелляции: из попытки или из переданного answer_text
        appeal_answer_text = last_attempt.text if last_attempt else (answer_text or "—")

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
            await self.send_callback(
                self,
                f"⚖️ *{player.display_name}* подаёт апелляцию!\n"
                f"Ответ: _{appeal_answer_text}_\n"
                f"Правильный ответ по паку: *{question.answer}*\n\n"
                f"Голосуйте! Засчитать ответ? ({APPEAL_TIMEOUT} сек)"
            )
        if self.show_appeal_callback:
            await self.show_appeal_callback(self)
        self._cancel_appeal_timer()
        self._appeal_task = asyncio.create_task(self._appeal_timeout_handler())
        return True

    async def vote_appeal(self, user_id: int, vote: bool) -> Optional[str]:
        if self.state != GameState.APPEAL or self.current_appeal is None:
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
            await self._resolve_appeal()
        return 'voted'

    async def _appeal_timeout_handler(self):
        try:
            await asyncio.sleep(APPEAL_TIMEOUT)
            if self.state == GameState.APPEAL:
                await self._resolve_appeal()
        except asyncio.CancelledError:
            pass

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
            player.score += price * 2
            self.question_answered_correctly = True
            self.correct_answerer_id = appeal.user_id
            self.chooser_id = appeal.user_id
            if self.send_callback:
                await self.send_callback(
                    self,
                    f"✅ Апелляция принята! ({for_votes} ЗА / {against_votes} ПРОТИВ)\n"
                    f"💰 *{player.display_name}* получает +{price} очков\n"
                    f"Итого: {player.score}"
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
            await self._after_question()
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
            await self.send_callback(
                self,
                f"⏩ *{player.display_name}* предлагает пропустить {label}\n"
                f"Голосуйте! ({SKIP_VOTE_TIMEOUT} сек)"
            )
        if self.show_skip_vote_callback:
            await self.show_skip_vote_callback(self)
        self._cancel_skip_vote_timer()
        self._skip_vote_task = asyncio.create_task(self._skip_vote_timeout_handler())
        return True

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
            if self.state == GameState.SKIP_VOTE:
                await self._resolve_skip_vote()
        except asyncio.CancelledError:
            pass

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

    def host_force_correct(self, host_id: int) -> bool:
        """Ведущий засчитывает последний неправильный ответ как верный (аргумент для текущего вопроса)."""
        if not self.is_host(host_id):
            return False
        # Ищем последнего ошибшегося из SHOWING_ANSWER или CHOOSING_QUESTION
        return True  # проверка пройдена, обработка в bot.py

    async def host_skip_round(self, host_id: int) -> bool:
        """Ведущий принудительно завершает раунд."""
        if not self.is_host(host_id):
            return False
        if self.state not in (GameState.CHOOSING_QUESTION, GameState.ROUND_START):
            return False
        await self._end_round()
        return True

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

    def get_board_text(self) -> str:
        board = self.get_board()
        if not board:
            return "Доска пуста"
        lines = [f"📋 *{self.current_round.name}*\n"]
        chooser = self.players.get(self.chooser_id)
        if chooser:
            lines.append(f"🎯 Выбирает: *{chooser.display_name}*\n")
        if self.host_mode and self.host_id:
            host_p = self.players.get(self.host_id)
            hname = host_p.display_name if host_p else str(self.host_id)
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
            lines.append(f"{medal} {player.display_name}: *{player.score}*")
        return '\n'.join(lines)

    def get_final_results_text(self) -> str:
        if not self.players:
            return "Нет игроков"
        sorted_players = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
        lines = ["🎉 *ИГРА ОКОНЧЕНА!*\n", "🏆 *Итоговый счёт:*\n"]
        medals = ['🥇', '🥈', '🥉']
        for i, player in enumerate(sorted_players):
            medal = medals[i] if i < len(medals) else f"{i + 1}."
            lines.append(f"{medal} {player.display_name}: *{player.score}*")
        if sorted_players:
            winner = sorted_players[0]
            lines.append(f"\n👑 Победитель: *{winner.display_name}*!")
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

    def get_appeal_status_text(self) -> str:
        if self.current_appeal is None:
            return ""
        a = self.current_appeal
        player = self.players.get(a.user_id)
        name = player.display_name if player else "Игрок"
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

    # ==================== ОЧИСТКА ====================

    def cleanup(self):
        self._cancel_buzzer_timer()
        self._cancel_answer_timer()
        self._cancel_appeal_timer()
        self._cancel_skip_vote_timer()

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
