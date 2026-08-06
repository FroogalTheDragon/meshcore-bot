"""
Sends random HAM radio practice questions and lets anyone in the channel/DM
answer them. First correct answer wins, gets logged, and a new question is
generated. Wrong answers are logged too but leave the question open.

Questions and Figures from https://github.com/russolsen/ham_radio_question_pool — thanks russolsen!
"""
import asyncio
import json
import random
import time
from pathlib import Path

from ..models import MeshMessage
from ..transmission_tracker import TransmissionTracker
from .base_command import BaseCommand

"""
DO NOT REMOVE
NOTE: For reference the HAM question data structure:
{
    "id": "T1A01",
    "correct": 2,
    "refs": "[97.1]",
    "question": "Which of the following is part of the Basis and Purpose of the Amateur Radio Service?",
    "answers": [
      "Providing personal radio communications for as many citizens as possible",
      "Providing communications for international contesting",
      "Advancing skills in the technical and communication phases of the radio art",
      "All these choices are correct"
    ],
    "figure": "",
    "correct_letter": "C"
}
"""

class HamhelperCommand(BaseCommand):
    name = "hamhelper"
    keywords = ['hamhelper', 'a', 'b', 'c', 'd', 'leaderboard', 'lb', 'status']
    description = "'hamhelper' - HAM radio practice Q&A, answer A/B/C/D\n'leaderboard'/'lb' - show leaderboard\n'help hamhelper' - show this help message"
    category = "education"
    cooldown_seconds = 3
    mesh_char_limit = 136

    # User must answer this many questions before showing up in the leaderboard
    min_leaderboard_questions = 10

    class HamhelperException(BaseException):
        def __init__(self, msg: str):
            super().__init__(msg)

    def __init__(self, bot):
        super().__init__(bot)
        self.transmission_tracker = TransmissionTracker(bot)
        self.hamhelper_enabled = self.get_config_value('Hamhelper_Command', 'enabled', fallback=True, value_type='bool')
        self.question_pool_path = self.get_config_value(
            'Hamhelper_Command',
            'question_pool_path',
            fallback=str(Path(__file__).resolve().parent.parent.parent / 'data/randomlines/ham_questions.json'),
            value_type='str',
        )
        self.cooldown_seconds = self.get_config_value(
            'Hamhelper_Command',
            'cooldown_seconds',
            fallback=self.cooldown_seconds,
            value_type='int',
        )
        self.min_leaderboard_questions = self.get_config_value(
            'Hamhelper_Command',
            'min_leaderboard_questions',
            fallback=self.min_leaderboard_questions,
            value_type='int',
        )
        self.mesh_char_limit = self.get_config_value(
            'Hamhelper_Command',
            'mesh_char_limit',
            fallback=self.mesh_char_limit,
            value_type='int',
        )
        self.schedule_delay_seconds = self.get_config_value(
            'Hamhelper_Command',
            'schedule_delay_seconds',
            fallback=60,
            value_type='int',
        )
        self.figure_urls = {
            'fig_1': self.get_config_value(
                'Hamhelper_Command',
                'figure_1_url',
                fallback="https://github.com/russolsen/ham_radio_question_pool/blob/main/technician-2026-2030/t-1.png",
                value_type='str',
            ),
            'fig_2': self.get_config_value(
                'Hamhelper_Command',
                'figure_2_url',
                fallback="https://github.com/russolsen/ham_radio_question_pool/blob/main/technician-2026-2030/t-2.png",
                value_type='str',
            ),
            'fig_3': self.get_config_value(
                'Hamhelper_Command',
                'figure_3_url',
                fallback="https://github.com/russolsen/ham_radio_question_pool/blob/main/technician-2026-2030/t-3.png",
                value_type='str',
            ),
        }
        # Single shared open question — anyone in the channel/DM can answer it.
        # {"correct_letter": "b", "question_id": ..., "asked_at": ts}
        self._active_question = None
        # If a trigger is used when no question is active we schedule an
        # ask task so the command doesn't block the caller. Track that
        # task here so we can cancel it if needed (tests, shutdown, etc.).
        self._scheduled_ask_task = None
        # Metadata about the scheduled ask (when it was scheduled and any delay)
        self._scheduled_ask_meta = None

    def can_execute_now(self, message: MeshMessage) -> bool:
        """Answering an open question is never subject to the trigger cooldown.

        command_manager calls this before calling execute() at all — and it
        re-stamps this user's cooldown right before every execute() call,
        including wrong-answer responses. Without this override, a user who
        answers wrong and immediately guesses again gets blocked here before
        execute() ever runs. Everything else (leaderboard, manual, status,
        triggering a new question) still goes through the normal
        cooldown/DM/channel checks.
        """
        text = (message.content_lower or message.content or "").strip().lower()
        if self._active_question and text in ("a", "b", "c", "d"):
            if not self.is_channel_allowed(message):
                return False
            if self.requires_dm and not message.is_dm:
                return False
            return not (self.requires_admin_access() and not self._check_admin_access(message))
        return super().can_execute_now(message)

    def _schedule_ask(self, message: MeshMessage) -> None:
        """Schedule `_ask_question` as a background task and track it.

        Cancels any previously scheduled ask task that hasn't completed yet.
        The task is stored on `self._scheduled_ask_task` and cleared when
        it finishes or is cancelled.
        """
        # Cancel an already-scheduled task if it's still pending
        if self._scheduled_ask_task is not None and not self._scheduled_ask_task.done():
            try:
                self._scheduled_ask_task.cancel()
            except self.HamhelperException("Failed to stop task"):
                pass

        # Record scheduled metadata and create the tracked task
        self._scheduled_ask_meta = {'scheduled_at': time.time(), 'delay': 0}
        task = asyncio.create_task(self._ask_question(message))
        self._scheduled_ask_task = task

        # Ensure we clear the reference when the task completes
        def _clear_task(_):
            try:
                # avoid keeping dead references
                self._scheduled_ask_task = None
            except Exception:
                pass

        task.add_done_callback(_clear_task)

    def _schedule_delayed_ask(self, message: MeshMessage, delay_seconds: int) -> None:
        """Schedule `_ask_question` to run after `delay_seconds` and track the task.

        Cancels any previously scheduled ask task that hasn't completed yet.
        """
        # Cancel any existing scheduled task
        if self._scheduled_ask_task is not None and not self._scheduled_ask_task.done():
            try:
                self._scheduled_ask_task.cancel()
            except Exception:
                pass

        async def _delayed():
            try:
                await asyncio.sleep(delay_seconds)
                await self._ask_question(message)
            except asyncio.CancelledError:
                # allow cancellation to propagate silently
                raise

        # Record scheduled metadata and create the tracked task
        self._scheduled_ask_meta = {'scheduled_at': time.time(), 'delay': delay_seconds}
        task = asyncio.create_task(_delayed())
        self._scheduled_ask_task = task

        def _clear_task_inner(_):
            try:
                self._scheduled_ask_task = None
                self._scheduled_ask_meta = None
            except Exception:
                pass

        task.add_done_callback(_clear_task_inner)

    def _cancel_scheduled_ask(self) -> None:
        """Cancel the currently scheduled ask task, if any."""
        if self._scheduled_ask_task is not None and not self._scheduled_ask_task.done():
            try:
                self._scheduled_ask_task.cancel()
            except Exception:
                pass
        self._scheduled_ask_task = None
        self._scheduled_ask_meta = None

    def _user_handle(self, message: MeshMessage) -> str:
        return message.sender_pubkey or message.sender_id or "unknown"

    def get_question_pool(self) -> dict:
        question_file = self.get_config_value(
            'Hamhelper_Command',
            'question_pool_path',
            fallback=str(Path(__file__).resolve().parent.parent.parent / 'data/randomlines/ham_questions.json'),
            value_type='str',
        )
        try:
            with open(Path(question_file), encoding='utf-8') as data:
                json_data = json.load(data)
                return json_data
        except Exception as e:
            self.logger.error(f"Failed to load question pool from {question_file}: {e}")
            return None


    def generate_question(self) -> dict or None:
        question_pool = self.get_question_pool()
        if not question_pool:
            self.logger.error("Couldn't load hamhelper question pool")
            return None

        try:
            question = question_pool[random.randrange(0, len(question_pool))]
            # Build a normalized active question and prepend the id and refs
            qid = question.get("id")
            raw_q_text = question.get("question", "")
            refs = question.get("refs") or question.get("ref") or ""
            # Normalize refs: if list, join; otherwise use as-is
            if isinstance(refs, (list, tuple)):
                refs_str = ", ".join(str(r) for r in refs)
            else:
                refs_str = str(refs).strip()

            if refs_str:
                composed_question = f"{qid}, {refs_str}\n{raw_q_text}"
            else:
                composed_question = f"{qid}\n{raw_q_text}"

            self._active_question = {
                "question_id": qid,
                "question": composed_question,
                "answers": question.get("answers", []),
                "figure": question.get("figure", ""),
                "correct_letter": (question.get("correct_letter") or "").lower(),
                "asked_at": int(time.time()),
            }
            self.logger.log(f"Successuflly set new question: {self._active_question}")
            return question
        except self.HamhelperException as e:
            self.logger.error(f"Failed to select hamhelper question: {e}")
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
            raise self.HamhelperException(f"hamhelper: failed to record result for {user_handle}: {e}")

    def _get_leaderboard_data(self):
        try:
            rows = None
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT * FROM hamhelper_leaderboard
                    WHERE total_questions >= ?
                    ORDER BY question_accuracy DESC, total_questions DESC
                    LIMIT 5;
                    """,
                    (self.min_leaderboard_questions,),
                )
                rows = cursor.fetchall()
            return rows
        except Exception as e:
            self.logger.error(f"Failed to get leaderboard data: {e}")
            raise self.HamhelperException(f"Failed to get leaderboard data: {e}")

    def _get_figure_url(self, figure_file_name: str) -> str or None:
        if figure_file_name == "t-1.png":
            return self.figure_urls["fig_1"]
        elif figure_file_name == "t-2.png":
            return self.figure_urls["fig_2"]
        elif figure_file_name == "t-3.png":
            return self.figure_urls["fig_3"]
        else:
            return None

    async def _ask_question(self, message: MeshMessage) -> bool:
        # Ensure there's an active question; generate one if needed and
        # normalize its structure for downstream use.
        if not self._active_question:
            self.generate_question()
            if not self._active_question:
                await self.send_response(
                    message, "Failed to load question, check logs for more details...",
                    skip_user_rate_limit=True,
                )
                return False

        try:
            # Check the question length max length and only chunk if a positive limit is configured
            if self.mesh_char_limit > 0 and len(self._active_question["question"]) > self.mesh_char_limit:
                q: str = self._active_question["question"]
                q_words: [str] = q.split(" ")
                midpoint = len(q_words) // 2
                first_half = " ".join(q_words[:midpoint])
                second_half = " ".join(q_words[midpoint:])
                chunks = [first_half, second_half] if second_half else [first_half]

                if not await self.send_response_chunked(message, chunks):
                    await self.send_response(message, "Failed to send multi-part question")
                    raise BaseException
            else:
                if not await self.send_response(message, self._active_question["question"], skip_user_rate_limit=True):
                    return False
            await asyncio.sleep(self.cooldown_seconds)
            
            # Lets scramble the answers here
            labels = ["A", "B", "C", "D"]
            for index, answer in enumerate(self._active_question["answers"]):
                if index >= len(labels):
                    self.logger.warn("Oops!! Too many options...")
                    break
                if not await self.send_response(message, f"{labels[index]}. {answer}", skip_user_rate_limit=True):
                    self.logger.error("Failed to send answers")
                    return False
                await asyncio.sleep(self.cooldown_seconds)

            if self._active_question["figure"]:
                if not await self.send_response(message, self._get_figure_url(self._active_question["figure"]), skip_user_rate_limit=True):
                    self.logger.error("Failed to send figure for question")
                    return False
                asyncio.sleep(self.cooldown_seconds)
            return True
        except asyncio.CancelledError:
            # Scheduled ask was cancelled; stop cleanly without an error
            self.logger.info("hamhelper: scheduled ask task was cancelled")
            return False
        except Exception as e:
            self.logger.error(f"Ran into an error asking the question: {e}")
            raise self.HamhelperException(f"Ran into an error asking the question: {e}")

    async def execute(self, message: MeshMessage) -> bool:
        if not getattr(self, 'hamhelper_enabled', True):
            return False

        text = (message.content_lower or message.content).strip().lower()

        # --- Someone is answering the open question (never cooldown-gated) ---
        if self._active_question and text in ("a", "b", "c", "d"):
            active = self._active_question
            user_handle = self._user_handle(message)
            who = message.sender_id or "someone"

            if text == active["correct_letter"]:
                self._active_question = None
                try:
                    self._record_result(user_handle, correct=True)
                except Exception:
                    self.logger.error("hamhelper: leaderboard write failed, continuing anyway")

                # If schedule delay seconds is set to zero, it will be turned off, users will have to invoke new questions with 'Hamhelper'
                if self.schedule_delay_seconds > 0:
                    await self.send_response(message, f"✅ Correct, {who}! Good job!  Next question will be shown in {self.schedule_delay_seconds} seconds!", skip_user_rate_limit=True)
                    self._schedule_delayed_ask(message, self.schedule_delay_seconds)
                    return True
                await self.send_response(message, f"✅ Correct, {who}! Good job!  Type Hamhelper to test your knowledge again!", skip_user_rate_limit=True)
            else:
                try:
                    self._record_result(user_handle, correct=False)
                except Exception:
                    self.logger.error("hamhelper: leaderboard write failed, continuing anyway")
                self.logger.log(f"The current answer is {self._active_question["correct_letter"]}")
                await self.send_response(message, f"❌ Not quite, {who}. Try again!", skip_user_rate_limit=True)
            return True

        text_tokens = text.split()

        if text in ("leaderboard", "lb"):
            leaderboard_rows = self._get_leaderboard_data()
            if not leaderboard_rows:
                await self.send_response(
                    message,
                    f"No one has answered {self.min_leaderboard_questions} questions yet!",
                    skip_user_rate_limit=True,
                )
                return True

            lines = ["Top 5 Leaderboard:"]
            for i, row in enumerate(leaderboard_rows, start=1):
                lines.append(
                    f"{i}. {row['user_handle']} — {round(row['question_accuracy'], 1)}%"
                )
            leaderboard_string = "\n".join(lines)

            await self.send_response(message, leaderboard_string, skip_user_rate_limit=True)
            return True

        # --- Status: report scheduled ask or active question age ---
        if "status" in text_tokens or "stat" in text_tokens:
            # If there's a scheduled task pending, report time until it runs
            if self._scheduled_ask_task is not None and not self._scheduled_ask_task.done():
                meta = self._scheduled_ask_meta or {}
                scheduled_at = meta.get('scheduled_at', 0)
                delay = meta.get('delay', 0)
                if delay and scheduled_at:
                    remaining = max(0, int(scheduled_at + delay - time.time()))
                    await self.send_response(message, f"A question is scheduled to run in {remaining} seconds.", skip_user_rate_limit=True)
                elif scheduled_at:
                    age = int(time.time() - scheduled_at)
                    await self.send_response(message, f"A question was scheduled {age} seconds ago and will run shortly.", skip_user_rate_limit=True)
                else:
                    await self.send_response(message, "A question is scheduled to run shortly.", skip_user_rate_limit=True)
                return True

            # If there's an active question, report its age
            if self._active_question:
                asked_at = self._active_question.get('asked_at', 0)
                age = int(time.time() - asked_at) if asked_at else 0
                await self.send_response(message, f"Active question asked {age} seconds ago.", skip_user_rate_limit=True)
                return True

            # No scheduled or active question
            await self.send_response(message, "No question is scheduled or active.", skip_user_rate_limit=True)
            return True

        # --- Bare a/b/c/d with nothing open — not ours ---
        if text in ("a", "b", "c", "d") and not self._active_question:
            return False

        # --- Trigger keyword ---
        if self._active_question:
            await self._ask_question(message)
            self.logger.log(f"Active Question: {self._active_question}")
            return True

        self._schedule_ask(message)
        return True
