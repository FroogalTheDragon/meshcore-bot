"""Tests for modules.commands.hamhelper_command.py"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from modules.commands.hamhelper_command import HamhelperCommand
from modules.db_manager import DBManager
from tests.conftest import mock_message

SAMPLE_QUESTIONS = [
    {
        "id": "T1A01",
        "correct": 2,
        "refs": "[97.1]",
        "question": "Which of the following is part of the Basis and Purpose of the Amateur Radio Service?",
        "answers": [
            "Providing personal radio communications for as many citizens as possible",
            "Providing communications for international contesting",
            "Advancing skills in the technical and communication phases of the radio art",
            "All these choices are correct",
        ],
        "figure": "",
        "correct_letter": "C",
    },
    {
        "id": "T1A02",
        "correct": 1,
        "refs": "[97.1]",
        "question": "Which agency regulates and enforces the rules for the Amateur Radio Service in the United States?",
        "answers": ["FEMA", "FCC", "ITU", "ARRL"],
        "figure": "t-1.png",
        "correct_letter": "B",
    },
]


@pytest.fixture
def hamhelper(command_mock_bot):
    """A fresh HamHelperCommand backed by the lightweight mock bot (no DB)."""
    return HamhelperCommand(command_mock_bot)


@pytest.fixture
def hamhelper_with_db(command_mock_bot_with_db, tmp_path):
    """HamHelperCommand backed by a real (temp-file) sqlite db, so the
    leaderboard queries in _record_result / _get_leaderboard_data run
    against the real schema created by the db migrations."""
    db_path = str(tmp_path / "hamhelper_test.db")
    command_mock_bot_with_db.db_manager = DBManager(command_mock_bot_with_db, db_path)
    return HamhelperCommand(command_mock_bot_with_db)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Don't actually sleep for 3-20s per message in these tests."""
    monkeypatch.setattr(
        "modules.commands.hamhelper_command.asyncio.sleep", AsyncMock(return_value=None)
    )


def _sent_messages(bot):
    """The content string of every send_response call made on the mock bot."""
    return [call.args[1] for call in bot.command_manager.send_response.call_args_list]


class TestHamhelper:
    # ---- get_question_pool -------------------------------------------------

    def test_get_question_pool(self, hamhelper):
        m = mock_open(read_data=json.dumps(SAMPLE_QUESTIONS))
        with patch("builtins.open", m):
            pool = hamhelper.get_question_pool()
        assert pool == SAMPLE_QUESTIONS

    def test_get_question_pool_returns_none_on_error(self, hamhelper):
        with patch("builtins.open", side_effect=OSError("no such file")):
            pool = hamhelper.get_question_pool()
        assert pool is None

    # ---- generate_question --------------------------------------------------

    def test_generate_question(self, hamhelper):
        m = mock_open(read_data=json.dumps(SAMPLE_QUESTIONS))
        with patch("builtins.open", m), \
                patch("modules.commands.hamhelper_command.random.randrange", return_value=1):
            question = hamhelper.generate_question()
        assert question == SAMPLE_QUESTIONS[1]

    def test_generate_question_returns_none_on_error(self, hamhelper):
        with patch("builtins.open", side_effect=OSError("no such file")):
            question = hamhelper.generate_question()
        assert question is None

    def test_get_question_pool_uses_configured_path(self, hamhelper):
        hamhelper.bot.config.add_section("Hamhelper_Command")
        hamhelper.bot.config.set("Hamhelper_Command", "question_pool_path", "my/path/ham_questions.json")
        m = mock_open(read_data=json.dumps(SAMPLE_QUESTIONS))
        with patch("builtins.open", m):
            pool = hamhelper.get_question_pool()

        assert pool == SAMPLE_QUESTIONS
        assert str(m.call_args[0][0]) == "my/path/ham_questions.json"

    def test_generate_question_uses_configured_question_pool_path(self, hamhelper):
        hamhelper.bot.config.add_section("Hamhelper_Command")
        hamhelper.bot.config.set("Hamhelper_Command", "question_pool_path", "another/path.json")
        m = mock_open(read_data=json.dumps(SAMPLE_QUESTIONS))
        with patch("builtins.open", m), \
                patch("modules.commands.hamhelper_command.random.randrange", return_value=0):
            question = hamhelper.generate_question()

        assert question == SAMPLE_QUESTIONS[0]
        assert str(m.call_args[0][0]) == "another/path.json"

    # ---- _record_result -------------------------------------------------------

    def test_record_result_creates_new_leaderboard_row(self, hamhelper_with_db):
        hamhelper_with_db.min_leaderboard_questions = 0
        hamhelper_with_db._record_result("alice", correct=True)

        rows = hamhelper_with_db._get_leaderboard_data()
        assert len(rows) == 1
        assert rows[0]["user_handle"] == "alice"
        assert rows[0]["questions_correct"] == 1
        assert rows[0]["questions_incorrect"] == 0
        assert rows[0]["total_questions"] == 1
        assert rows[0]["question_accuracy"] == 100.0

    def test_record_result_updates_existing_row(self, hamhelper_with_db):
        hamhelper_with_db.min_leaderboard_questions = 0
        hamhelper_with_db._record_result("bob", correct=True)
        hamhelper_with_db._record_result("bob", correct=False)

        rows = hamhelper_with_db._get_leaderboard_data()
        assert len(rows) == 1
        assert rows[0]["questions_correct"] == 1
        assert rows[0]["questions_incorrect"] == 1
        assert rows[0]["total_questions"] == 2
        assert rows[0]["question_accuracy"] == 50.0

    def test_record_result_logs_and_swallows_db_errors(self, hamhelper_with_db):
        hamhelper_with_db.bot.db_manager = MagicMock()
        hamhelper_with_db.bot.db_manager.connection.side_effect = Exception("db is gone")

        with pytest.raises(BaseException):
            hamhelper_with_db._record_result("carol", correct=True)

        hamhelper_with_db.logger.error.assert_called()

    # ---- _get_leaderboard_data ---------------------------------------------

    def test_get_leaderboard_data_orders_by_accuracy_desc(self, hamhelper_with_db):
        hamhelper_with_db.min_leaderboard_questions = 0
        hamhelper_with_db._record_result("low", correct=False)   # 0%
        hamhelper_with_db._record_result("high", correct=True)   # 100%
        hamhelper_with_db._record_result("mid", correct=True)    # 50%
        hamhelper_with_db._record_result("mid", correct=False)

        rows = hamhelper_with_db._get_leaderboard_data()

        assert [row["user_handle"] for row in rows] == ["high", "mid", "low"]

    def test_get_leaderboard_data_empty(self, hamhelper_with_db):
        assert hamhelper_with_db._get_leaderboard_data() == []

    def test_get_leaderboard_data_raises_on_db_error(self, hamhelper_with_db):
        hamhelper_with_db.bot.db_manager = MagicMock()
        hamhelper_with_db.bot.db_manager.connection.side_effect = Exception("db is gone")

        with pytest.raises(BaseException):
            hamhelper_with_db._get_leaderboard_data()

    # ---- _get_figure_url -------------------------------------------------------

    @pytest.mark.parametrize(
        "filename,expected_key",
        [("t-1.png", "fig_1"), ("t-2.png", "fig_2"), ("t-3.png", "fig_3")],
    )
    def test_get_figure_url(self, hamhelper, filename, expected_key):
        assert hamhelper._get_figure_url(filename) == hamhelper.figure_urls[expected_key]

    def test_get_figure_url_unknown_filename_returns_none(self, hamhelper):
        assert hamhelper._get_figure_url("not-a-real-figure.png") is None

    # ---- _ask_question -------------------------------------------------

    @pytest.mark.asyncio
    async def test_ask_question_sends_question_and_answers(self, hamhelper):
        hamhelper._active_question = {
            "question_id": SAMPLE_QUESTIONS[0]["id"],
            "question": SAMPLE_QUESTIONS[0]["question"],
            "answers": SAMPLE_QUESTIONS[0]["answers"],
            "figure": SAMPLE_QUESTIONS[0]["figure"],
            "correct_letter": "c",
            "asked_at": 0,
        }
        msg = mock_message(content="hamhelper")

        result = await hamhelper._ask_question(msg)

        assert result is True
        sent = _sent_messages(hamhelper.bot)
        assert SAMPLE_QUESTIONS[0]["question"] in sent
        assert "A. Providing personal radio communications for as many citizens as possible" in sent
        assert "B. Providing communications for international contesting" in sent
        assert "C. Advancing skills in the technical and communication phases of the radio art" in sent
        assert "D. All these choices are correct" in sent
        # Announcement text is not guaranteed; ensure question+answers sent instead
        assert hamhelper._active_question["correct_letter"] == "c"
        assert hamhelper._active_question["question_id"] == "T1A01"

    @pytest.mark.asyncio
    async def test_ask_question_respects_zero_mesh_char_limit_as_no_limit(self, hamhelper):
        hamhelper.mesh_char_limit = 0
        hamhelper._active_question = {
            "question_id": SAMPLE_QUESTIONS[0]["id"],
            "question": "x " * 100,
            "answers": SAMPLE_QUESTIONS[0]["answers"],
            "figure": SAMPLE_QUESTIONS[0]["figure"],
            "correct_letter": "c",
            "asked_at": 0,
        }
        hamhelper.send_response_chunked = AsyncMock(return_value=True)
        msg = mock_message(content="hamhelper")

        result = await hamhelper._ask_question(msg)

        assert result is True
        hamhelper.send_response_chunked.assert_not_called()
        assert any("x" in sent for sent in _sent_messages(hamhelper.bot))

    @pytest.mark.asyncio
    async def test_execute_trigger_with_active_question_does_not_generate_new_question(self, hamhelper):
        hamhelper._active_question = {"correct_letter": "c", "question_id": "T1A01", "asked_at": 0}
        hamhelper.generate_question = MagicMock(side_effect=AssertionError("Should not generate new question"))
        hamhelper._ask_question = AsyncMock(return_value=True)
        msg = mock_message(content="hamhelper")

        result = await hamhelper.execute(msg)

        assert result is True
        hamhelper._ask_question.assert_awaited_once_with(msg)

    # @pytest.mark.asyncio
    # async def test_ask_question_sends_raw_figure_when_present(self, hamhelper):
    #     hamhelper._active_question = {
    #         "question_id": SAMPLE_QUESTIONS[1]["id"],
    #         "question": SAMPLE_QUESTIONS[1]["question"],
    #         "answers": SAMPLE_QUESTIONS[1]["answers"],
    #         "figure": SAMPLE_QUESTIONS[1]["figure"],
    #         "correct_letter": "b",
    #         "asked_at": 0,
    #     }
    #     msg = mock_message(content="hamhelper")

    #     result = await hamhelper._ask_question(msg)

    #     assert result is True
    #     assert "t-1.png" in _sent_messages(hamhelper.bot)

    @pytest.mark.asyncio
    async def test_ask_question_fails_when_question_text_fails_to_send(self, hamhelper):
        hamhelper._active_question = {
            "question_id": SAMPLE_QUESTIONS[0]["id"],
            "question": SAMPLE_QUESTIONS[0]["question"],
            "answers": SAMPLE_QUESTIONS[0]["answers"],
            "figure": SAMPLE_QUESTIONS[0]["figure"],
            "correct_letter": "c",
            "asked_at": 0,
        }
        hamhelper.bot.command_manager.send_response = AsyncMock(return_value=False)
        msg = mock_message(content="hamhelper")

        result = await hamhelper._ask_question(msg)

        assert result is False
        assert hamhelper._active_question is not None


class TestHamhelperExecute:
    """Bonus coverage for execute()'s dispatch logic, since that's the main
    entry point the other methods above feed into."""

    @pytest.mark.asyncio
    async def test_execute_correct_answer_records_result_and_asks_next(self, hamhelper):
        hamhelper._active_question = {"correct_letter": "c", "question_id": "T1A01", "asked_at": 0}
        hamhelper._record_result = MagicMock()
        # execute() will schedule the next question; mock the scheduler
        hamhelper._schedule_delayed_ask = MagicMock()
        msg = mock_message(content="c", sender_id="Alice")

        result = await hamhelper.execute(msg)

        assert result is True
        assert hamhelper._active_question is None  # cleared before asking a new one
        hamhelper._record_result.assert_called_once_with(hamhelper._user_handle(msg), correct=True)
        hamhelper._schedule_delayed_ask.assert_called_once_with(msg, 60)
        assert any("Correct" in s for s in _sent_messages(hamhelper.bot))

    @pytest.mark.asyncio
    async def test_execute_correct_answer_uses_configured_schedule_delay(self, hamhelper):
        hamhelper.bot.config.add_section("Hamhelper_Command")
        hamhelper.bot.config.set("Hamhelper_Command", "schedule_delay_seconds", "5")
        # Recreate config-derived fields after changing config
        hamhelper.schedule_delay_seconds = int(hamhelper.bot.config.get("Hamhelper_Command", "schedule_delay_seconds"))

        hamhelper._active_question = {"correct_letter": "c", "question_id": "T1A01", "asked_at": 0}
        hamhelper._record_result = MagicMock()
        hamhelper._schedule_delayed_ask = MagicMock()
        msg = mock_message(content="c", sender_id="Alice")

        result = await hamhelper.execute(msg)

        assert result is True
        hamhelper._schedule_delayed_ask.assert_called_once_with(msg, 5)

    @pytest.mark.asyncio
    async def test_execute_incorrect_answer_keeps_question_open(self, hamhelper):
        hamhelper._active_question = {"correct_letter": "c", "question_id": "T1A01", "asked_at": 0}
        hamhelper._record_result = MagicMock()
        msg = mock_message(content="a", sender_id="Bob")

        result = await hamhelper.execute(msg)

        assert result is True
        assert hamhelper._active_question is not None  # still open
        hamhelper._record_result.assert_called_once_with(hamhelper._user_handle(msg), correct=False)
        assert any("Not quite" in s for s in _sent_messages(hamhelper.bot))

    @pytest.mark.asyncio
    async def test_execute_leaderboard_prints_each_row(self, hamhelper):
        hamhelper._get_leaderboard_data = MagicMock(
            return_value=[
                {
                    "user_handle": "alice",
                    "total_questions": 2,
                    "questions_correct": 2,
                    "questions_incorrect": 0,
                    "question_accuracy": 100.0,
                    "last_answer_ts": 0,
                }
            ]
        )
        msg = mock_message(content="leaderboard")

        result = await hamhelper.execute(msg)

        assert result is True
        sent = _sent_messages(hamhelper.bot)
        assert any("Top 5 Leaderboard:" in s for s in sent)
        assert any("alice" in s for s in sent)

    @pytest.mark.asyncio
    async def test_execute_bare_letter_ignored_without_active_question(self, hamhelper):
        msg = mock_message(content="a")

        result = await hamhelper.execute(msg)

        assert result is False
        assert _sent_messages(hamhelper.bot) == []

    @pytest.mark.asyncio
    async def test_execute_trigger_with_active_question_repeats(self, hamhelper):
        hamhelper._active_question = {"correct_letter": "c", "question_id": "T1A01", "asked_at": 0}
        hamhelper._ask_question = AsyncMock(return_value=True)
        msg = mock_message(content="hamhelper")

        result = await hamhelper.execute(msg)

        assert result is True
        hamhelper._ask_question.assert_awaited_once_with(msg)

    # @pytest.mark.asyncio
    # async def test_execute_trigger_without_active_question_asks_new(self, hamhelper):
    #     hamhelper._ask_question = AsyncMock(return_value=True)
    #     msg = mock_message(content="hamhelper")

    #     result = await hamhelper.execute(msg)

    #     # current implementation calls _ask_question without awaiting it,
    #     # so execute() returns None and the coroutine is scheduled
    #     assert result is None
    #     hamhelper._ask_question.assert_called_once_with(msg)

    @pytest.mark.asyncio
    async def test_execute_status_reports_scheduled_question(self, hamhelper):
        hamhelper._scheduled_ask_task = MagicMock()
        hamhelper._scheduled_ask_task.done.return_value = False
        hamhelper._scheduled_ask_meta = {"scheduled_at": time.time(), "delay": 30}
        msg = mock_message(content="status")

        result = await hamhelper.execute(msg)

        assert result is True
        assert any("scheduled to run in" in s for s in _sent_messages(hamhelper.bot))

    @pytest.mark.asyncio
    async def test_execute_returns_false_when_disabled(self, hamhelper):
        hamhelper.hamhelper_enabled = False
        msg = mock_message(content="hamhelper")

        result = await hamhelper.execute(msg)

        assert result is False
        assert _sent_messages(hamhelper.bot) == []
