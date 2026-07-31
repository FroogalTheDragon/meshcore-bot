"""Tests for modules.commands.hamhelper_command.py"""

import json
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from modules.commands.hamhelper_command import HamHelperCommand
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
    return HamHelperCommand(command_mock_bot)


@pytest.fixture
def hamhelper_with_db(command_mock_bot_with_db, tmp_path):
    """HamHelperCommand backed by a real (temp-file) sqlite db, so the
    leaderboard queries in _record_result / _get_leaderboard_data run
    against the real schema created by the db migrations."""
    db_path = str(tmp_path / "hamhelper_test.db")
    command_mock_bot_with_db.db_manager = DBManager(command_mock_bot_with_db, db_path)
    return HamHelperCommand(command_mock_bot_with_db)


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

    # ---- _record_result -------------------------------------------------------

    def test_record_result_creates_new_leaderboard_row(self, hamhelper_with_db):
        hamhelper_with_db._record_result("alice", correct=True)

        rows = hamhelper_with_db._get_leaderboard_data()
        assert len(rows) == 1
        assert rows[0]["user_handle"] == "alice"
        assert rows[0]["questions_correct"] == 1
        assert rows[0]["questions_incorrect"] == 0
        assert rows[0]["total_questions"] == 1
        assert rows[0]["question_accuracy"] == 100.0

    def test_record_result_updates_existing_row(self, hamhelper_with_db):
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

        hamhelper_with_db._record_result("carol", correct=True)  # must not raise

        hamhelper_with_db.logger.error.assert_called()

    # ---- _get_leaderboard_data ---------------------------------------------

    def test_get_leaderboard_data_orders_by_accuracy_desc(self, hamhelper_with_db):
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

        with pytest.raises(Exception):
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

    # ---- _ask_new_question -------------------------------------------------

    @pytest.mark.asyncio
    async def test_ask_new_question_sends_question_and_answers(self, hamhelper):
        hamhelper.generate_question = MagicMock(return_value=SAMPLE_QUESTIONS[0])
        msg = mock_message(content="hamhelper")

        result = await hamhelper._ask_new_question(msg)

        assert result is True
        sent = _sent_messages(hamhelper.bot)
        assert SAMPLE_QUESTIONS[0]["question"] in sent
        assert "A. Providing personal radio communications for as many citizens as possible" in sent
        assert "B. Providing communications for international contesting" in sent
        assert "C. Advancing skills in the technical and communication phases of the radio art" in sent
        assert "D. All these choices are correct" in sent
        assert any("Anyone can answer" in s for s in sent)
        assert hamhelper._active_question["correct_letter"] == "c"
        assert hamhelper._active_question["question_id"] == "T1A01"

    @pytest.mark.asyncio
    async def test_ask_new_question_sends_raw_figure_when_present(self, hamhelper):
        # NOTE: _ask_new_question sends the raw figure filename as-is (unlike
        # _repeat_question, which resolves it to a full URL via _get_figure_url).
        hamhelper.generate_question = MagicMock(return_value=SAMPLE_QUESTIONS[1])
        msg = mock_message(content="hamhelper")

        result = await hamhelper._ask_new_question(msg)

        assert result is True
        assert "t-1.png" in _sent_messages(hamhelper.bot)

    @pytest.mark.asyncio
    async def test_ask_new_question_fails_when_no_question_available(self, hamhelper):
        hamhelper.generate_question = MagicMock(return_value=None)
        msg = mock_message(content="hamhelper")

        result = await hamhelper._ask_new_question(msg)

        assert result is False
        assert hamhelper._active_question is None
        assert any("Failed to load question" in s for s in _sent_messages(hamhelper.bot))

    @pytest.mark.asyncio
    async def test_ask_new_question_stops_if_question_text_fails_to_send(self, hamhelper):
        hamhelper.generate_question = MagicMock(return_value=SAMPLE_QUESTIONS[0])
        hamhelper.bot.command_manager.send_response = AsyncMock(return_value=False)
        msg = mock_message(content="hamhelper")

        result = await hamhelper._ask_new_question(msg)

        assert result is False
        assert hamhelper._active_question is None

    # ---- _repeat_question ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_repeat_question_resends_active_question(self, hamhelper):
        hamhelper.get_question_pool = MagicMock(return_value=SAMPLE_QUESTIONS)
        hamhelper._active_question = {
            "correct_letter": "b",
            "question_id": "T1A02",
            "asked_at": 0,
        }
        msg = mock_message(content="hamhelper")

        result = await hamhelper._repeat_question(msg)

        assert result is True
        sent = _sent_messages(hamhelper.bot)
        assert SAMPLE_QUESTIONS[1]["question"] in sent
        # Figure filenames are resolved to a full URL on repeat.
        assert hamhelper.figure_urls["fig_1"] in sent
        assert any("Anyone can answer" in s for s in sent)

    @pytest.mark.asyncio
    async def test_repeat_question_fails_when_question_not_in_pool(self, hamhelper):
        hamhelper.get_question_pool = MagicMock(return_value=SAMPLE_QUESTIONS)
        hamhelper._active_question = {
            "correct_letter": "c",
            "question_id": "does-not-exist",
            "asked_at": 0,
        }
        msg = mock_message(content="hamhelper")

        result = await hamhelper._repeat_question(msg)

        assert result is False
        assert any("Failed to load question" in s for s in _sent_messages(hamhelper.bot))

    @pytest.mark.asyncio
    async def test_repeat_question_fails_when_figure_url_unknown(self, hamhelper):
        bad_question = dict(SAMPLE_QUESTIONS[1])
        bad_question["figure"] = "not-a-known-figure.png"
        hamhelper.get_question_pool = MagicMock(return_value=[bad_question])
        hamhelper._active_question = {
            "correct_letter": "b",
            "question_id": "T1A02",
            "asked_at": 0,
        }
        msg = mock_message(content="hamhelper")

        result = await hamhelper._repeat_question(msg)

        assert result is False


class TestHamhelperExecute:
    """Bonus coverage for execute()'s dispatch logic, since that's the main
    entry point the other methods above feed into."""

    @pytest.mark.asyncio
    async def test_execute_correct_answer_records_result_and_asks_next(self, hamhelper):
        hamhelper._active_question = {"correct_letter": "c", "question_id": "T1A01", "asked_at": 0}
        hamhelper._record_result = MagicMock()
        hamhelper._ask_new_question = AsyncMock(return_value=True)
        msg = mock_message(content="c", sender_id="Alice")

        result = await hamhelper.execute(msg)

        assert result is True
        assert hamhelper._active_question is None  # cleared before asking a new one
        hamhelper._record_result.assert_called_once_with(hamhelper._user_handle(msg), correct=True)
        hamhelper._ask_new_question.assert_awaited_once()
        assert any("Correct" in s for s in _sent_messages(hamhelper.bot))

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
        assert "Question Leaderboard:" in sent
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
        hamhelper._repeat_question = AsyncMock(return_value=True)
        msg = mock_message(content="hamhelper")

        result = await hamhelper.execute(msg)

        assert result is True
        hamhelper._repeat_question.assert_awaited_once_with(msg)

    @pytest.mark.asyncio
    async def test_execute_trigger_without_active_question_asks_new(self, hamhelper):
        hamhelper._ask_new_question = AsyncMock(return_value=True)
        msg = mock_message(content="hamhelper")

        result = await hamhelper.execute(msg)

        assert result is True
        hamhelper._ask_new_question.assert_awaited_once_with(msg)
