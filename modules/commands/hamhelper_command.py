"""
Sends random HAM radio practice questions and lets anyone in the channel/DM
answer them. First correct answer wins, gets logged, and a new question is
generated. Wrong answers are logged too but leave the question open.

Questions from https://github.com/russolsen/ham_radio_question_pool — thanks russolsen!
"""
import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path

from ..models import MeshMessage
from ..transmission_tracker import TransmissionTracker
from .base_command import BaseCommand


class HamHelperCommand(BaseCommand):
    name = "hamhelper"
    keywords = ['hamhelper', 'a', 'b', 'c', 'd', 'leaderboard', 'lb', 'manual', 'man']
    description = "Send a random question for the HAM radio license test."
    category = "education"
    cooldown_seconds = 3
    figure_base_url = "https://github.com/FroogalTheDragon/ham_radio_question_pool/blob/main/technician-2026-2030"
    help_string = "Hamhelper: HAM radio practice Q&A, answer A/B/C/D. 'leaderboard'/'lb' for scores, 'manual'/'man' for this help."
    figure_urls = {
        "fig_1": f"{figure_base_url}/t-1.png",
        "fig_2": f"{figure_base_url}/t-2.png",
        "fig_3": f"{figure_base_url}/t-3.png"
    }

    def __init__(self, bot):
        super().__init__(bot)
        self.transmission_tracker = TransmissionTracker(bot)
        # Single shared open question — anyone in the channel/DM can answer it.
        # {"correct_letter": "b", "question_id": ..., "asked_at": ts}
        self._active_question = None

    def get_repeats(self):
        repeat = self.transmission_tracker.get_repeat_info("hamhelper")
        print(repeat)

    def _user_handle(self, message: MeshMessage) -> str:
        # Same identity convention used elsewhere for per-user rate limiting: pubkey when
        # available, else display name.
        return message.sender_pubkey or message.sender_id or "unknown"

    def get_question_pool(self) -> dict:
        data_path = Path(__file__).resolve().parent.parent.parent
        try:
            if data_path.exists():
                with open(data_path / "data/randomlines/ham_questions.json") as data:
                    json_data = json.load(data)
                    return json_data
            else:
                raise BaseException("Couldn't load question pool!")
        except BaseException as e:
            print(f"Failed to load question pool: {e}")
            return None

    async def send_data(self, message: MeshMessage, data: str, attempts: int) -> bool:
        if not await self.send_response(message, data, skip_user_rate_limit=True):
            while attempts != 0:
                if not await self.send_response(message, data, skip_user_rate_limit=True):
                    attempts -= 1
                    print(f"Trying to send {attempts} more times")
                else:
                    return True
            return False
        return True

    def generate_question(self) -> dict or None:
        data_path = Path(__file__).resolve().parent.parent.parent
        try:
            if data_path.exists():
                with open(data_path / "data/randomlines/ham_questions.json") as data:
                    json_data = json.load(data)
                    question = json_data[random.randrange(0, len(json_data))]
                    return question
            else:
                raise BaseException(f"Couldn't find hamhelper question data at {data_path}")
        except BaseException as e:
            print(f"Failed to load question: {e}")
            return None

    def _record_result(self, user_handle: str, correct: bool) -> None:
        """Update hamhelper_leaderboard (table/columns from migration 0013)."""
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, questions_correct, questions_incorrect "
                    "FROM hamhelper_leaderboard WHERE user_handle = ?",
                    (user_handle,),
                )
                row = cursor.fetchone()
                now_ts = int(time.time())

                if row is None:
                    correct_count = 1 if correct else 0
                    incorrect_count = 0 if correct else 1
                    total = correct_count + incorrect_count
                    accuracy_pct = (correct_count / total) * 100
                    cursor.execute(
                        """
                        INSERT INTO hamhelper_leaderboard
                            (user_handle, questions_correct, questions_incorrect,
                             total_questions, question_accuracy, last_answer_ts)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (user_handle, correct_count, incorrect_count, total, accuracy_pct, now_ts),
                    )
                else:
                    correct_count = row["questions_correct"] + (1 if correct else 0)
                    incorrect_count = row["questions_incorrect"] + (0 if correct else 1)
                    total = correct_count + incorrect_count
                    accuracy_pct = (correct_count / total) * 100 if total else 0.0
                    cursor.execute(
                        """
                        UPDATE hamhelper_leaderboard
                        SET questions_correct = ?, questions_incorrect = ?,
                            total_questions = ?, question_accuracy = ?, last_answer_ts = ?
                        WHERE id = ?
                        """,
                        (correct_count, incorrect_count, total, accuracy_pct, now_ts, row["id"]),
                    )
                conn.commit()
        except Exception as e:
            self.logger.error(f"hamhelper: failed to record result for {user_handle}: {e}")

    def _get_leaderboard_data(self):
        try:
            rows = None
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT * FROM hamhelper_leaderboard ORDER BY question_accuracy DESC;
                    """
                )
                rows = cursor.fetchall()
            return rows

        except Exception as e:
            self.logger.error(f"Failed to get leaderboard data: {e}")
            raise

    def _get_figure_url(self, figure_file_name: str) -> str or None:
        if figure_file_name == "t-1.png":
            return self.figure_urls["fig_1"]
        elif figure_file_name == "t-2.png":
            return self.figure_urls["fig_2"]
        elif figure_file_name == "t-3.png":
            return self.figure_urls["fig_3"]
        else:
            return None

    async def _ask_new_question(self, message: MeshMessage) -> bool:
        question = self.generate_question()
        if not question:
            await self.send_response(
                message, "Failed to load question, check logs for more details...",
                skip_user_rate_limit=True,
            )
            return False

        question_text = question["question"]
        question_answers = question["answers"]
        question_figure = question["figure"]
        correct_letter = question["correct_letter"]

        if not await self.send_data(message, question_text, 3):
            return False
        await asyncio.sleep(12)
        labels = ["A", "B", "C", "D"]
        for index, answer in enumerate(question_answers):
            await asyncio.sleep(12)
            if index >= len(labels):
                print("Oops!! Too many options...")
                continue
            if not await self.send_data(message, f"{labels[index]}. {answer}", 3):
                print("Failed to send answers after 3 tries")
                return False

        if question_figure:
            await asyncio.sleep(12)
            if not await self.send_data(message, question_figure, 3):
                print("Failed to send figure for question")
                return False

        await asyncio.sleep(12)
        await self.send_data(message, "Anyone can answer — reply with A, B, C, or D!", 3)

        self._active_question = {
            "correct_letter": correct_letter.strip().lower(),
            "question_id": question.get("id"),
            "asked_at": time.time(),
        }
        return True

    async def _repeat_question(self, message: MeshMessage):
        question = None
        question_pool = self.get_question_pool()
        for q in list(question_pool):
            if q["id"] == self._active_question["question_id"]:
                question = q

        if not question:
            await self.send_response(
                message, "Failed to load question, check logs for more details...",
                skip_user_rate_limit=True,
            )
            return False

        question_text = question["question"]
        question_answers = question["answers"]
        question_figure = question["figure"]
        correct_letter = question["correct_letter"]

        if not await self.send_data(message, question_text, 3):
            return False
        await asyncio.sleep(12)

        labels = ["A", "B", "C", "D"]
        for index, answer in enumerate(question_answers):
            await asyncio.sleep(12)
            if index >= len(labels):
                print("Oops!! Too many options...")
                continue
            if not await self.send_data(message, f"{labels[index]}. {answer}", 3):
                print("Failed to send answers after 3 tries")
                return False

        if question_figure:
            qurl = self._get_figure_url(question_figure)
            if qurl:
                print(f"Question Figure: {qurl}")
                await asyncio.sleep(12)
                if not await self.send_data(message, qurl, 3):
                    print("Failed to send figure for question")
                    return False
            else:
                print(f"Failed to get figure URL, got {qurl} instead")
                return False

        await asyncio.sleep(12)
        await self.send_data(message, "Anyone can answer — reply with A, B, C, or D!", 3)

        return True

    async def execute(self, message: MeshMessage) -> bool:
        text = (message.content_lower or message.content).strip().lower()

        # --- Someone is answering the open question ---
        if self._active_question and text in ("a", "b", "c", "d"):
            active = self._active_question
            user_handle = self._user_handle(message)
            who = message.sender_id or "someone"

            if text == active["correct_letter"]:
                self._active_question = None  # close it out before awaiting anything else
                self._record_result(user_handle, correct=True)
                await self.send_response(
                    message, f"✅ Correct, {who}! Good job!  Next question will be shown in 60 seconds!", skip_user_rate_limit=True
                )
                await asyncio.sleep(60)
                await self._ask_new_question(message)
            else:
                self._record_result(user_handle, correct=False)
                await self.send_response(
                    message, f"❌ Not quite, {who}. Try again!", skip_user_rate_limit=True
                )
            return True

        if text in ("leaderboard", "lb"):
            await self.send_response(message, "Question Leaderboard:", skip_user_rate_limit=True)
            leaderboard_rows = self._get_leaderboard_data()
            for row in leaderboard_rows:
                leaderboard_string = (
                    f"{row['user_handle']}\n"
                    f"Total Questions: {row['total_questions']}\n"
                    f"Questions Correct: {row['questions_correct']}\n"
                    f"Questions Incorrect: {row['questions_incorrect']}\n"
                    f"Question Accuracy: {round(row['question_accuracy'], 1)}%\n"
                    f"Last practiced: {datetime.fromtimestamp(row['last_answer_ts'])}."
                )
                await self.send_response(message, leaderboard_string, skip_user_rate_limit=True)
                await asyncio.sleep(12)
            return True
        
        if text in ("manual", "man"):
            await self.send_response(message, self.help_string)
            return True

        # --- Bare a/b/c/d with nothing open — not ours ---
        if text in ("a", "b", "c", "d") and not self._active_question:
            return False

        # --- Trigger keyword ---
        if self._active_question:
            await self._repeat_question(message)
            return True

        return await self._ask_new_question(message)
