"""
Тесты игровой логики "Своя Игра".

Стандартная библиотека (unittest + asyncio), без сторонних зависимостей:
    python -m unittest discover -s tests -v
    (или просто `python -m unittest` из корня)

Фоновые таймеры (create_task) и паузы (asyncio.sleep) замоканы, поэтому
сценарии полностью детерминированы и быстрые. Тайм-ауты при необходимости
имитируются прямым вызовом *_timeout_handler.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import game as game_mod
from game import Game, GameState
from siq_parser import GamePack, Round, Theme, Question


class _DummyTask:
    def done(self):
        return True

    def cancel(self):
        pass


def _fake_create_task(coro, *a, **k):
    # Фоновые задачи (таймеры/отсчёты) не запускаем — закрываем корутину,
    # чтобы не было предупреждения "coroutine was never awaited".
    try:
        coro.close()
    except Exception:
        pass
    return _DummyTask()


async def _fake_sleep(*a, **k):
    return


def make_pack(with_final=False):
    rounds = [
        Round(name="Раунд 1", round_type="standard", themes=[
            Theme(name="Арифметика", questions=[
                Question(price=100, text="2+2?", answer="4"),
                Question(price=200, text="Столица Франции?", answer="Париж"),
            ]),
        ]),
    ]
    if with_final:
        rounds.append(Round(name="Финал", round_type="final", themes=[
            Theme(name="ФинТема1", questions=[Question(price=0, text="Q1", answer="альфа")]),
            Theme(name="ФинТема2", questions=[Question(price=0, text="Q2", answer="бета")]),
            Theme(name="ФинТема3", questions=[Question(price=0, text="Q3", answer="гамма")]),
        ]))
    return GamePack(name="TestPack", rounds=rounds)


def attach_stub_callbacks(g: Game):
    sent = []

    async def send(_g, text):
        sent.append(text)

    async def noop1(_g):
        pass

    async def noop_media(_g, *a, **k):
        pass

    g.send_callback = send
    g.send_photo_callback = noop_media
    g.send_audio_callback = noop_media
    g.send_video_callback = noop_media
    g.show_board_callback = noop1
    g.show_buzzer_callback = noop1
    g.remove_buzzer_callback = noop1
    g.show_scores_callback = noop1
    g.announce_round_callback = noop1
    g.announce_game_over_callback = noop1
    g.show_appeal_callback = noop1
    g.remove_appeal_callback = noop1
    g.show_skip_vote_callback = noop1
    g.remove_skip_vote_callback = noop1
    return sent


class GameTestBase(unittest.IsolatedAsyncioTestCase):
    P1, P2, P3 = 1001, 1002, 1003

    async def asyncSetUp(self):
        for tgt, repl in (("create_task", _fake_create_task), ("sleep", _fake_sleep)):
            p = mock.patch.object(game_mod.asyncio, tgt, repl)
            p.start()
            self.addCleanup(p.stop)

    async def new_game(self, with_final=False, players=(P1, P2)):
        g = Game(chat_id=1, pack=make_pack(with_final))
        self.sent = attach_stub_callbacks(g)
        g.start_lobby()
        for i, uid in enumerate(players):
            g.add_player(uid, f"user{uid}", f"Игрок{i + 1}")
        ok = await g.start_game()
        self.assertTrue(ok)
        self.assertEqual(g.state, GameState.CHOOSING_QUESTION)
        return g

    async def answer(self, g, uid, text):
        """Выбрать вопрос (если нужно), нажать баззер и ответить."""
        await g.press_buzzer(uid)
        return await g.submit_answer(uid, text)


class TestBasicFlow(GameTestBase):
    async def test_correct_answer_scores_and_sets_chooser(self):
        g = await self.new_game()
        await g.select_question(g.chooser_id, 0, 0)  # price 100
        self.assertEqual(g.state, GameState.QUESTION_ASKED)
        res = await self.answer(g, self.P1, "4")
        self.assertTrue(res)
        self.assertEqual(g.players[self.P1].score, 100)
        self.assertEqual(g.chooser_id, self.P1)
        # 1 из 2 вопросов сыгран — раунд продолжается
        self.assertEqual(g.state, GameState.CHOOSING_QUESTION)

    async def test_wrong_answer_penalty(self):
        g = await self.new_game()
        await g.select_question(g.chooser_id, 0, 0)
        res = await self.answer(g, self.P1, "пять")
        self.assertFalse(res)
        self.assertEqual(g.players[self.P1].score, -100)
        self.assertIn(self.P1, g.failed_answerers)

    async def test_pass_by_all_skips_question(self):
        g = await self.new_game()
        await g.select_question(g.chooser_id, 0, 0)
        await g.press_pass(self.P1)
        await g.press_pass(self.P2)
        # вопрос закрыт, идём дальше (раунд ещё не закончен)
        self.assertEqual(g.state, GameState.CHOOSING_QUESTION)


class TestBuzzerQueue(GameTestBase):
    async def test_premove_queue_promotes_after_wrong(self):
        g = await self.new_game()
        await g.select_question(g.chooser_id, 0, 0)
        await g.press_buzzer(self.P1)
        self.assertEqual(g.current_answerer_id, self.P1)
        # P2 жмёт пока отвечает P1 — встаёт в очередь
        await g.press_buzzer(self.P2)
        self.assertIn(self.P2, g.buzzer_queue)
        # P1 отвечает неверно -> ход автоматически переходит к P2
        await g.submit_answer(self.P1, "неверно")
        self.assertEqual(g.current_answerer_id, self.P2)
        self.assertEqual(g.state, GameState.WAITING_ANSWER)
        await g.submit_answer(self.P2, "4")
        self.assertEqual(g.players[self.P2].score, 100)

    async def test_concurrent_buzzer_serialized_by_lock(self):
        import asyncio
        g = await self.new_game()
        await g.select_question(g.chooser_id, 0, 0)
        await asyncio.gather(g.press_buzzer(self.P1), g.press_buzzer(self.P2))
        # Ровно один отвечает, второй — в очереди (без гонки)
        self.assertIn(g.current_answerer_id, (self.P1, self.P2))
        other = self.P2 if g.current_answerer_id == self.P1 else self.P1
        self.assertIn(other, g.buzzer_queue)
        self.assertEqual(g.state, GameState.WAITING_ANSWER)


class TestAppeal(GameTestBase):
    async def _vote_accept(self, g):
        await g.vote_appeal(self.P1, True)
        await g.vote_appeal(self.P2, True)

    async def test_appeal_rolls_back_later_wrong_answerer(self):
        g = await self.new_game()
        await g.select_question(g.chooser_id, 0, 0)  # price 100
        # P1 отвечает неверно (но по сути верно), затем P2 тоже неверно
        await self.answer(g, self.P1, "четыре!")
        await self.answer(g, self.P2, "пять")
        self.assertEqual(g.players[self.P1].score, -100)
        self.assertEqual(g.players[self.P2].score, -100)
        self.assertEqual(g.state, GameState.CHOOSING_QUESTION)
        # P1 апеллирует и выигрывает голосование
        self.assertTrue(await g.start_appeal(self.P1, ""))
        await self._vote_accept(g)
        # P1 получает как за верный (+100 нетто), P2 — возврат штрафа
        self.assertEqual(g.players[self.P1].score, 100)
        self.assertEqual(g.players[self.P2].score, 0)
        self.assertEqual(g.chooser_id, self.P1)
        self.assertTrue(g.question_answered_correctly)

    async def test_appeal_revokes_correct_answer_made_after(self):
        g = await self.new_game()
        await g.select_question(g.chooser_id, 0, 0)
        await self.answer(g, self.P1, "четыре!")          # неверно по боту
        await self.answer(g, self.P2, "4")                 # верно -> +100
        self.assertEqual(g.players[self.P2].score, 100)
        self.assertEqual(g.chooser_id, self.P2)
        self.assertTrue(await g.start_appeal(self.P1, ""))
        await self._vote_accept(g)
        self.assertEqual(g.players[self.P1].score, 100)    # апеллянт — победитель
        self.assertEqual(g.players[self.P2].score, 0)      # ошибочный балл снят
        self.assertEqual(g.chooser_id, self.P1)

    async def test_appeal_rejected_keeps_scores(self):
        g = await self.new_game()
        await g.select_question(g.chooser_id, 0, 0)
        await self.answer(g, self.P1, "пять")
        self.assertTrue(await g.start_appeal(self.P1, ""))
        await g.vote_appeal(self.P1, True)
        await g.vote_appeal(self.P2, False)   # 1 ЗА / 1 ПРОТИВ -> принято (>=)
        # при равенстве голосов апелляция принимается
        self.assertEqual(g.players[self.P1].score, 100)

    async def test_appeal_after_timeout_allowed(self):
        g = await self.new_game()
        await g.select_question(g.chooser_id, 0, 0)
        await g.press_buzzer(self.P1)
        self.assertEqual(g.current_answerer_id, self.P1)
        # имитируем срабатывание таймаута ответа
        await g._answer_timeout_handler()
        self.assertIn(self.P1, g.failed_answerers)
        self.assertEqual(g.players[self.P1].score, -100)
        # апелляция по таймауту должна быть доступна
        ok = await g.start_appeal(self.P1, "4")
        self.assertTrue(ok)
        self.assertEqual(g.state, GameState.APPEAL)


class TestFinal(GameTestBase):
    async def _play_into_final(self):
        g = await self.new_game(with_final=True)
        # P1 берёт вопрос за 100
        await g.select_question(g.chooser_id, 0, 0)
        await self.answer(g, self.P1, "4")
        self.assertEqual(g.players[self.P1].score, 100)
        self.assertEqual(g.chooser_id, self.P1)
        # P1 выбирает вопрос за 200, отвечает P2
        await g.select_question(self.P1, 0, 1)
        await self.answer(g, self.P2, "Париж")
        self.assertEqual(g.players[self.P2].score, 200)
        # раунд закончен -> финал
        self.assertEqual(g.state, GameState.FINAL_THEME_ELIMINATION)
        self.assertEqual(set(g.final_players), {self.P1, self.P2})
        return g

    async def test_final_full_flow_with_appeal(self):
        g = await self._play_into_final()
        # отстающий ходит первым: P1 (100) < P2 (200)
        self.assertEqual(g._final_current_eliminator(), self.P1)
        self.assertEqual(len(g.final_themes), 3)
        first = g.final_themes[0]
        self.assertTrue(await g.finalize_theme_elimination(self.P1, first))
        self.assertEqual(g._final_current_eliminator(), self.P2)
        second = g.final_themes[0]
        self.assertTrue(await g.finalize_theme_elimination(self.P2, second))
        # осталась одна тема -> ставки
        self.assertEqual(g.state, GameState.FINAL_BETTING)
        remaining_theme = g.final_question_theme_idx
        correct = g.final_round.themes[remaining_theme].questions[0].answer

        # очередь ставок: сначала P1
        self.assertEqual(g._final_current_bettor(), self.P1)
        self.assertEqual(await g.submit_final_bet(self.P1, 50), 'ok')
        self.assertEqual(g._final_current_bettor(), self.P2)
        # слишком большая ставка отклоняется
        self.assertEqual(await g.submit_final_bet(self.P2, 999), 'bad_amount')
        self.assertEqual(await g.submit_final_bet(self.P2, 100), 'ok')

        # после всех ставок показывается вопрос и идёт отсчёт
        self.assertEqual(g.state, GameState.FINAL_COUNTDOWN)
        await g._final_open_window()
        self.assertEqual(g.state, GameState.FINAL_ANSWER_WINDOW)

        self.assertTrue(await g.record_final_answer(self.P1, correct))
        # после последнего ответа оценка запускается автоматически
        self.assertTrue(await g.record_final_answer(self.P2, "ерунда"))
        self.assertEqual(g.state, GameState.FINAL_SHOWING_RESULTS)
        # повторный ответ не принимается (игра уже оценена)
        self.assertFalse(await g.record_final_answer(self.P1, "другое"))
        self.assertEqual(g.players[self.P1].score, 150)   # 100 + 50
        self.assertEqual(g.players[self.P2].score, 100)    # 200 - 100

        # P2 апеллирует свой ответ как верный по смыслу
        self.assertTrue(await g.start_final_appeal(self.P2, "ерунда"))
        self.assertEqual(g.state, GameState.FINAL_APPEAL)
        await g.vote_appeal(self.P1, True)
        await g.vote_appeal(self.P2, True)
        # неверный -> верный: качель +2*ставка
        self.assertEqual(g.players[self.P2].score, 300)
        self.assertEqual(g.state, GameState.FINAL_SHOWING_RESULTS)

    async def test_final_skipped_when_no_positive_scores(self):
        g = await self.new_game(with_final=True)
        await g.select_question(g.chooser_id, 0, 0)
        await self.answer(g, self.P1, "неверно")   # P1 -100
        await self.answer(g, self.P2, "тоже нет")  # P2 -100
        # второй вопрос — оба пасуют, чтобы закрыть раунд
        await g.select_question(g.chooser_id, 0, 1)
        await g.press_pass(self.P1)
        await g.press_pass(self.P2)
        # ни у кого нет счёта > 0 -> финал не играется, игра завершена
        self.assertEqual(g.state, GameState.GAME_OVER)


if __name__ == "__main__":
    unittest.main()
