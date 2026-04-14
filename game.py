"""
Логика игры "Своя Игра" для Telegram.

Управляет состоянием игры: раунды, выбор вопросов, ответы,
подсчёт очков, таймауты.
"""

import asyncio
import time
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple
from siq_parser import GamePack, Round, Theme, Question


class GameState(Enum):
    """Состояния игры."""
    IDLE = auto()              # Игра не начата
    LOBBY = auto()             # Набор игроков
    ROUND_START = auto()       # Начало раунда
    CHOOSING_QUESTION = auto() # Игрок выбирает вопрос
    QUESTION_ASKED = auto()    # Вопрос задан, ждём нажатия кнопки
    WAITING_ANSWER = auto()    # Кто-то нажал кнопку, ждём текстовый ответ
    SHOWING_ANSWER = auto()    # Показ правильного ответа
    ROUND_END = auto()         # Конец раунда
    GAME_OVER = auto()         # Игра окончена


@dataclass
class Player:
    """Игрок."""
    user_id: int
    username: str
    display_name: str
    score: int = 0


@dataclass
class AnswerAttempt:
    """Попытка ответа."""
    user_id: int
    text: str
    timestamp: float
    is_correct: bool = False
    processed: bool = False


class Game:
    """Основной класс игры."""

    def __init__(self, chat_id: int, pack: GamePack):
        self.chat_id: int = chat_id
        self.pack: GamePack = pack
        self.state: GameState = GameState.IDLE

        # Игроки: user_id -> Player
        self.players: Dict[int, Player] = {}

        # Текущий раунд
        self.current_round_index: int = 0
        self.current_round: Optional[Round] = None

        # Отслеживание отыгранных вопросов: (theme_index, question_index)
        self.played_questions: set = set()

        # Текущий вопрос
        self.current_question: Optional[Question] = None
        self.current_theme_index: Optional[int] = None
        self.current_question_index: Optional[int] = None

        # Кто выбирает вопрос (user_id)
        self.chooser_id: Optional[int] = None

        # Ответы на текущий вопрос
        self.buzzer_queue: List[int] = []  # очередь нажавших кнопку
        self.current_answerer_id: Optional[int] = None
        self.answer_attempts: List[AnswerAttempt] = []
        self.failed_answerers: set = set()  # кто уже ошибся на этом вопросе
        self.question_answered_correctly: bool = False
        self.correct_answerer_id: Optional[int] = None

        # Таймеры
        self.buzzer_timeout: float = 15.0   # секунд на нажатие кнопки
        self.answer_timeout: float = 20.0   # секунд на ввод ответа
        self._buzzer_task: Optional[asyncio.Task] = None
        self._answer_task: Optional[asyncio.Task] = None

        # Callback для отправки сообщений (устанавливается ботом)
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

    # ==================== УПРАВЛЕНИЕ ИГРОКАМИ ====================

    def add_player(self, user_id: int, username: str, display_name: str) -> bool:
        """Добавляет игрока. Возвращает True если успешно."""
        if self.state != GameState.LOBBY:
            return False
        if user_id in self.players:
            return False

        self.players[user_id] = Player(
            user_id=user_id,
            username=username,
            display_name=display_name,
            score=0
        )
        return True

    def remove_player(self, user_id: int) -> bool:
        """Удаляет игрока."""
        if user_id in self.players:
            del self.players[user_id]
            return True
        return False

    def get_player(self, user_id: int) -> Optional[Player]:
        """Возвращает игрока по ID."""
        return self.players.get(user_id)

    def get_players_list(self) -> List[Player]:
        """Возвращает список игроков."""
        return list(self.players.values())

    def get_player_count(self) -> int:
        """Количество игроков."""
        return len(self.players)

    # ==================== УПРАВЛЕНИЕ ИГРОЙ ====================

    def start_lobby(self):
        """Открывает лобби для набора игроков."""
        self.state = GameState.LOBBY

    async def start_game(self) -> bool:
        """Начинает игру. Возвращает True если успешно."""
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
        """Начинает текущий раунд."""
        if self.current_round_index >= len(self.pack.rounds):
            await self._end_game()
            return

        self.current_round = self.pack.rounds[self.current_round_index]
        self.played_questions.clear()
        self.state = GameState.ROUND_START

        # Выбираем первого игрока для выбора вопроса
        # (игрок с наименьшим счётом, или первый)
        self.chooser_id = self._get_first_chooser()

        if self.announce_round_callback:
            await self.announce_round_callback(self)

        self.state = GameState.CHOOSING_QUESTION

        if self.show_board_callback:
            await self.show_board_callback(self)

    def _get_first_chooser(self) -> int:
        """Определяет кто первый выбирает вопрос."""
        if not self.players:
            return 0
        # Игрок с наименьшим счётом
        return min(self.players.values(), key=lambda p: p.score).user_id

    async def select_question(self, user_id: int, theme_idx: int, question_idx: int) -> bool:
        """
        Игрок выбирает вопрос.
        Возвращает True если выбор валиден.
        """
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

        # Устанавливаем текущий вопрос
        self.current_theme_index = theme_idx
        self.current_question_index = question_idx
        self.current_question = theme.questions[question_idx]
        self.played_questions.add((theme_idx, question_idx))

        # Сбрасываем состояние ответов
        self.buzzer_queue.clear()
        self.current_answerer_id = None
        self.answer_attempts.clear()
        self.failed_answerers.clear()
        self.question_answered_correctly = False
        self.correct_answerer_id = None

        await self._ask_question()
        return True

    async def _ask_question(self):
        """Задаёт текущий вопрос."""
        self.state = GameState.QUESTION_ASKED

        if self.send_callback:
            theme = self.current_round.themes[self.current_theme_index]
            q = self.current_question

            header = f"🎯 *{theme.name}* за *{q.price}*"

            q_text = (q.text or '').strip()

            if not q_text:
                # Вопрос содержит медиа — укажем тип медиа
                if q.image:
                    q_text = "🖼 Вопрос с изображением"
                elif q.audio:
                    q_text = "🎵 Вопрос с аудио"
                elif q.video:
                    q_text = "🎥 Вопрос с видео"
                else:
                    q_text = "❓ Вопрос без текста"

            await self.send_callback(self, f"{header}\n\n{q_text}")

        # Отправляем медиа если есть
        if self.current_question.image and self.send_photo_callback:
            await self.send_photo_callback(
                self,
                self.current_question.image,
                self.current_question.image_filename
            )

        if self.current_question.audio and self.send_audio_callback:
            await self.send_audio_callback(
                self,
                self.current_question.audio,
                self.current_question.audio_filename
            )

        if self.current_question.video and self.send_video_callback:
            await self.send_video_callback(
                self,
                self.current_question.video,
                self.current_question.video_filename
            )

        # Показываем кнопку "Ответить"
        if self.show_buzzer_callback:
            await self.show_buzzer_callback(self)

        # Запускаем таймер на кнопку
        self._cancel_buzzer_timer()
        self._buzzer_task = asyncio.create_task(self._buzzer_timeout_handler())

    async def _buzzer_timeout_handler(self):
        """Таймаут ожидания нажатия кнопки."""
        try:
            await asyncio.sleep(self.buzzer_timeout)
            # Никто не нажал кнопку
            if self.state == GameState.QUESTION_ASKED:
                await self._no_one_answered()
        except asyncio.CancelledError:
            pass

    async def _answer_timeout_handler(self):
        """Таймаут ожидания текстового ответа."""
        try:
            await asyncio.sleep(self.answer_timeout)
            if self.state == GameState.WAITING_ANSWER and self.current_answerer_id:
                # Время вышло — считаем как неправильный ответ
                await self._process_wrong_answer(self.current_answerer_id)
        except asyncio.CancelledError:
            pass

    def _cancel_buzzer_timer(self):
        """Отменяет таймер кнопки."""
        if self._buzzer_task and not self._buzzer_task.done():
            self._buzzer_task.cancel()

    def _cancel_answer_timer(self):
        """Отменяет таймер ответа."""
        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()

    async def press_buzzer(self, user_id: int) -> bool:
        """
        Игрок нажимает кнопку "Ответить".
        Возвращает True если игрок получил право отвечать.
        """
        if self.state != GameState.QUESTION_ASKED:
            return False

        if user_id not in self.players:
            return False

        if user_id in self.failed_answerers:
            return False

        if user_id in self.buzzer_queue:
            return False

        self.buzzer_queue.append(user_id)

        # Первый нажавший получает право ответа
        if len(self.buzzer_queue) == 1:
            await self._give_answer_right(user_id)
            return True

        return False

    async def _give_answer_right(self, user_id: int):
        """Даёт игроку право ответить."""
        self._cancel_buzzer_timer()
        self.current_answerer_id = user_id
        self.state = GameState.WAITING_ANSWER

        player = self.players[user_id]
        if self.send_callback:
            await self.send_callback(
                self,
                f"⚡ *{player.display_name}* отвечает! ({self.answer_timeout:.0f} сек)"
            )

        # Запускаем таймер на ответ
        self._cancel_answer_timer()
        self._answer_task = asyncio.create_task(self._answer_timeout_handler())

    async def submit_answer(self, user_id: int, answer_text: str) -> Optional[bool]:
        """
        Игрок отправляет текстовый ответ.
        Возвращает:
          True — правильный ответ
          False — неправильный ответ
          None — ответ не принят (не тот игрок, не то состояние)
        """
        if self.state != GameState.WAITING_ANSWER:
            return None

        if user_id != self.current_answerer_id:
            return None

        if self.question_answered_correctly:
            return None

        self._cancel_answer_timer()

        attempt = AnswerAttempt(
            user_id=user_id,
            text=answer_text,
            timestamp=time.time()
        )

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
        """
        Проверяет ответ игрока.
        Поддерживает множественные правильные ответы через '/'.
        Нечувствительна к регистру, убирает лишние пробелы.
        """
        user_clean = self._normalize(user_answer)

        if not user_clean:
            return False

        # Правильный ответ может содержать варианты через "/"
        correct_variants = correct_answer.split('/')

        for variant in correct_variants:
            variant_clean = self._normalize(variant)
            if not variant_clean:
                continue

            # Точное совпадение (без регистра)
            if user_clean == variant_clean:
                return True

            # Один содержит другой (для коротких ответов)
            if len(variant_clean) >= 3:
                if user_clean in variant_clean or variant_clean in user_clean:
                    # Проверяем что совпадение достаточно значимое
                    shorter = min(len(user_clean), len(variant_clean))
                    longer = max(len(user_clean), len(variant_clean))
                    if shorter / longer >= 0.7:
                        return True

            # Расстояние Левенштейна для опечаток
            distance = self._levenshtein(user_clean, variant_clean)
            max_len = max(len(user_clean), len(variant_clean))
            if max_len > 0 and distance / max_len <= 0.2:
                return True

        return False

    @staticmethod
    def _normalize(text: str) -> str:
        """Нормализует текст для сравнения."""
        import re
        text = text.lower().strip()
        # Убираем знаки препинания
        text = re.sub(r'[^\w\s]', '', text)
        # Убираем множественные пробелы
        text = re.sub(r'\s+', ' ', text).strip()
        # Убираем "ё" -> "е"
        text = text.replace('ё', 'е')
        return text

    @staticmethod
    def _levenshtein(s1: str, s2: str) -> int:
        """Расстояние Левенштейна."""
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
        """Обрабатывает правильный ответ."""
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

        # Этот игрок выбирает следующий вопрос
        self.chooser_id = user_id

        if self.remove_buzzer_callback:
            await self.remove_buzzer_callback(self)

        await self._after_question()

    async def _process_wrong_answer(self, user_id: int):
        """Обрабатывает неправильный ответ."""
        player = self.players[user_id]
        price = self.current_question.price
        player.score -= price
        self.failed_answerers.add(user_id)

        if self.send_callback:
            await self.send_callback(
                self,
                f"❌ *{player.display_name}* отвечает неправильно!\n"
                f"💸 -{price} очков (всего: {player.score})"
            )

        self.current_answerer_id = None

        # Проверяем, есть ли ещё кто-то кто может ответить
        available_players = [
            p for p in self.players
            if p not in self.failed_answerers
        ]

        if not available_players:
            # Никто больше не может ответить
            await self._no_one_answered()
            return

        # Проверяем очередь кнопки — может кто-то уже нажал
        next_in_queue = None
        for uid in self.buzzer_queue:
            if uid not in self.failed_answerers:
                next_in_queue = uid
                break

        if next_in_queue:
            # Даём право ответа следующему в очереди
            await self._give_answer_right(next_in_queue)
        else:
            # Возвращаемся к ожиданию кнопки
            self.state = GameState.QUESTION_ASKED

            if self.show_buzzer_callback:
                await self.show_buzzer_callback(self)

            self._cancel_buzzer_timer()
            self._buzzer_task = asyncio.create_task(self._buzzer_timeout_handler())

    async def _no_one_answered(self):
        """Никто не ответил на вопрос."""
        self.state = GameState.SHOWING_ANSWER

        if self.remove_buzzer_callback:
            await self.remove_buzzer_callback(self)

        if self.send_callback:
            await self.send_callback(
                self,
                f"⏰ Время вышло! Никто не ответил.\n\n"
                f"📝 Правильный ответ: *{self.current_question.answer}*"
            )

        # chooser не меняется
        await self._after_question()

    async def _after_question(self):
        """Действия после вопроса."""
        # Небольшая пауза
        await asyncio.sleep(2)

        # Проверяем, остались ли вопросы в раунде
        if self._is_round_complete():
            await self._end_round()
        else:
            self.state = GameState.CHOOSING_QUESTION
            if self.show_board_callback:
                await self.show_board_callback(self)

    def _is_round_complete(self) -> bool:
        """Проверяет, все ли вопросы в раунде отыграны."""
        if self.current_round is None:
            return True

        total = sum(len(t.questions) for t in self.current_round.themes)
        return len(self.played_questions) >= total

    async def _end_round(self):
        """Завершает раунд."""
        self.state = GameState.ROUND_END

        if self.show_scores_callback:
            await self.show_scores_callback(self)

        # Переходим к следующему раунду
        self.current_round_index += 1

        await asyncio.sleep(3)

        if self.current_round_index < len(self.pack.rounds):
            await self._start_round()
        else:
            await self._end_game()

    async def _end_game(self):
        """Завершает игру."""
        self.state = GameState.GAME_OVER

        if self.announce_game_over_callback:
            await self.announce_game_over_callback(self)

    # ==================== ИНФОРМАЦИЯ ====================

    def get_board(self) -> List[dict]:
        """
        Возвращает текущую доску вопросов.
        Каждый элемент: {
            'theme_idx': int,
            'theme_name': str,
            'questions': [
                {'q_idx': int, 'price': int, 'played': bool}, ...
            ]
        }
        """
        if self.current_round is None:
            return []

        board = []
        for t_idx, theme in enumerate(self.current_round.themes):
            theme_data = {
                'theme_idx': t_idx,
                'theme_name': theme.name,
                'questions': []
            }
            for q_idx, question in enumerate(theme.questions):
                theme_data['questions'].append({
                    'q_idx': q_idx,
                    'price': question.price,
                    'played': (t_idx, q_idx) in self.played_questions
                })
            board.append(theme_data)
        return board

    def get_board_text(self) -> str:
        """Возвращает текстовое представление доски."""
        board = self.get_board()
        if not board:
            return "Доска пуста"

        lines = []
        lines.append(f"📋 *{self.current_round.name}*\n")

        chooser = self.players.get(self.chooser_id)
        if chooser:
            lines.append(f"🎯 Выбирает: *{chooser.display_name}*\n")

        for theme_data in board:
            prices = []
            for q in theme_data['questions']:
                if q['played']:
                    prices.append("~~" + str(q['price']) + "~~")
                else:
                    prices.append(f"*{q['price']}*")

            lines.append(f"📌 {theme_data['theme_name']}: {' | '.join(prices)}")

        return '\n'.join(lines)

    def get_scores_text(self) -> str:
        """Возвращает текст с очками всех игроков."""
        if not self.players:
            return "Нет игроков"

        sorted_players = sorted(
            self.players.values(),
            key=lambda p: p.score,
            reverse=True
        )

        lines = ["🏆 *Счёт:*\n"]
        medals = ['🥇', '🥈', '🥉']

        for i, player in enumerate(sorted_players):
            medal = medals[i] if i < len(medals) else f"{i + 1}."
            lines.append(f"{medal} {player.display_name}: *{player.score}*")

        return '\n'.join(lines)

    def get_final_results_text(self) -> str:
        """Возвращает финальные результаты."""
        if not self.players:
            return "Нет игроков"

        sorted_players = sorted(
            self.players.values(),
            key=lambda p: p.score,
            reverse=True
        )

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
        """
        Возвращает список доступных вопросов.
        Каждый элемент: (theme_idx, question_idx, theme_name, price)
        """
        if self.current_round is None:
            return []

        available = []
        for t_idx, theme in enumerate(self.current_round.themes):
            for q_idx, question in enumerate(theme.questions):
                if (t_idx, q_idx) not in self.played_questions:
                    available.append((t_idx, q_idx, theme.name, question.price))
        return available

    # ==================== ОЧИСТКА ====================

    def cleanup(self):
        """Очищает таймеры и ресурсы."""
        self._cancel_buzzer_timer()
        self._cancel_answer_timer()

    def reset(self):
        """Полный сброс игры."""
        self.cleanup()
        self.state = GameState.IDLE
        self.players.clear()
        self.current_round_index = 0
        self.current_round = None
        self.played_questions.clear()
        self.current_question = None
        self.current_theme_index = None
        self.current_question_index = None
        self.chooser_id = None
        self.buzzer_queue.clear()
        self.current_answerer_id = None
        self.answer_attempts.clear()
        self.failed_answerers.clear()
        self.question_answered_correctly = False
        self.correct_answerer_id = None


class GameManager:
    """
    Менеджер игр. Хранит активные игры по chat_id.
    """

    def __init__(self):
        self.games: Dict[int, Game] = {}
        self.packs: Dict[int, GamePack] = {}  # chat_id -> загруженный пак

    def create_game(self, chat_id: int, pack: GamePack) -> Game:
        """Создаёт новую игру для чата."""
        # Если есть старая игра — очищаем
        if chat_id in self.games:
            self.games[chat_id].cleanup()

        game = Game(chat_id=chat_id, pack=pack)
        self.games[chat_id] = game
        return game

    def get_game(self, chat_id: int) -> Optional[Game]:
        """Возвращает игру для чата."""
        return self.games.get(chat_id)

    def remove_game(self, chat_id: int):
        """Удаляет игру."""
        if chat_id in self.games:
            self.games[chat_id].cleanup()
            del self.games[chat_id]

    def store_pack(self, chat_id: int, pack: GamePack):
        """Сохраняет загруженный пак для чата."""
        self.packs[chat_id] = pack

    def get_pack(self, chat_id: int) -> Optional[GamePack]:
        """Возвращает загруженный пак."""
        return self.packs.get(chat_id)

    def has_active_game(self, chat_id: int) -> bool:
    	"""Есть ли активная игра в чате."""
    	game = self.games.get(chat_id)
    	if game is None:
    		return False
    	return game.state not in (GameState.IDLE, GameState.GAME_OVER)
