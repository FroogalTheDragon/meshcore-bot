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
from datetime import datetime
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

class HamHelperCommand(BaseCommand):
    name = "hamhelper"
    keywords = ['hamhelper', 'a', 'b', 'c', 'd', 'leaderboard', 'lb', 'manual', 'man']
    description = "Hamhelper: HAM radio practice Q&A, answer A/B/C/D. 'leaderboard'/'lb' for scores, 'manual'/'man' for this help."
    category = "education"
    cooldown_seconds = 3
    figure_base_url = "https://github.com/FroogalTheDragon/ham_radio_question_pool/blob/main/technician-2026-2030"
    figure_urls = {
        "fig_1": f"{figure_base_url}/t-1.png",
        "fig_2": f"{figure_base_url}/t-2.png",
        "fig_3": f"{figure_base_url}/t-3.png"
    }
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
        self.figure_base_url = self.get_config_value(
            'Hamhelper_Command',
            'figure_base_url',
            fallback=self.figure_base_url,
            value_type='str',
        )
        self.figure_urls = {
            'fig_1': self.get_config_value(
                'Hamhelper_Command',
                'figure_1_url',
                fallback=self.figure_urls['fig_1'],
                value_type='str',
            ),
            'fig_2': self.get_config_value(
                'Hamhelper_Command',
                'figure_2_url',
                fallback=self.figure_urls['fig_2'],
                value_type='str',
            ),
            'fig_3': self.get_config_value(
                'Hamhelper_Command',
                'figure_3_url',
                fallback=self.figure_urls['fig_3'],
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
            except Exception:
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
        # Same identity convention used elsewhere for per-user rate limiting: pubkey when
        # available, else display name.
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
        question_pool = self.get_question_pool()
        if not question_pool:
            self.logger.error("Couldn't load hamhelper question pool")
            return None

        try:
            question = question_pool[random.randrange(0, len(question_pool))]
            return question
        except Exception as e:
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
            question = self.generate_question()
            if not question:
                await self.send_response(
                    message, "Failed to load question, check logs for more details...",
                    skip_user_rate_limit=True,
                )
                return False

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
                if not await self.send_data(message, self._active_question["question"], 3):
                    return False
            # Prefer the bot's rate limiter if available so sends respect configured timeouts
            if hasattr(self.bot, 'bot_tx_rate_limiter') and self.bot.bot_tx_rate_limiter:
                try:
                    waiter = self.bot.bot_tx_rate_limiter.wait_for_tx()
                    if asyncio.iscoroutine(waiter):
                        await waiter
                except Exception:
                    # Testing environment may provide a non-awaitable MagicMock; yield briefly
                    await asyncio.sleep(0)
            else:
                await asyncio.sleep(12)
            labels = ["A", "B", "C", "D"]
            for index, answer in enumerate(self._active_question["answers"]):
                if hasattr(self.bot, 'bot_tx_rate_limiter') and self.bot.bot_tx_rate_limiter:
                    try:
                        waiter = self.bot.bot_tx_rate_limiter.wait_for_tx()
                        if asyncio.iscoroutine(waiter):
                            await waiter
                    except Exception:
                        await asyncio.sleep(0)
                else:
                    await asyncio.sleep(12)
                if index >= len(labels):
                    print("Oops!! Too many options...")
                    continue
                if not await self.send_data(message, f"{labels[index]}. {answer}", 3):
                    print("Failed to send answers after 3 tries")
                    return False

            if self._active_question["figure"]:
                if hasattr(self.bot, 'bot_tx_rate_limiter') and self.bot.bot_tx_rate_limiter:
                    try:
                        waiter = self.bot.bot_tx_rate_limiter.wait_for_tx()
                        if asyncio.iscoroutine(waiter):
                            await waiter
                    except Exception:
                        await asyncio.sleep(0)
                else:
                    await asyncio.sleep(12)
                if not await self.send_data(message, self._active_question["figure"], 3):
                    self.logger.error("Failed to send figure for question")
                    return False
            return True
        except asyncio.CancelledError:
            # Scheduled ask was cancelled; stop cleanly without an error
            self.logger.info("hamhelper: scheduled ask task was cancelled")
            return False

    async def execute(self, message: MeshMessage) -> bool:
        if not getattr(self, 'hamhelper_enabled', True):
            return False

        if self.can_execute_now(message):
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
                        message, f"✅ Correct, {who}! Good job!  Next question will be shown in {self.schedule_delay_seconds} seconds!", skip_user_rate_limit=True
                    )
                    # Schedule the next question after a delay without blocking
                    self._schedule_delayed_ask(message, self.schedule_delay_seconds)
                else:
                    self._record_result(user_handle, correct=False)
                    await self.send_response(
                        message, f"❌ Not quite, {who}. Try again!", skip_user_rate_limit=True
                    )
                return True

            if text in ("leaderboard", "lb"):
                leaderboard_rows = self._get_leaderboard_data()
                if not leaderboard_rows:
                    await self.send_response(
                        message,
                        f"No one has answered {self.min_leaderboard_questions}+ questions yet!",
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
            
            if text in ("manual", "man"):
                await self.get_help_text()
                return True

            # --- Status: report scheduled ask or active question age ---
            tokens = text.split()
            if "status" in tokens or "stat" in tokens:
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
                # Record per-user execution cooldown for triggering hamhelper
                user_id = message.sender_id if message.sender_id else None
                self.record_execution(user_id)
                await self._ask_question(message)
                return True
            
            # Schedule the coroutine so it's not left as an un-awaited coroutine.
            # Use the scheduler so the task is tracked and can be cancelled.
            user_id = message.sender_id if message.sender_id else None
            self.record_execution(user_id)
            self._schedule_ask(message)
