#!/usr/bin/env python3

"""
This is a command to send out random HAM radio practice questions.

The questions were put together by this repo: (https://github.com/russolsen/ham_radio_question_pool) thank you russolsen on github!
"""

import random
import json
import os
import asyncio

from ..models import MeshMessage
from .base_command import BaseCommand
from pathlib import Path

class HamHelperCommand(BaseCommand):
    # Plugin metadata
    name = "hamhelper"
    keywords = ['hamhelper', 'helpmyham', 'hamme', 'whathamisit']
    description = "Send a random question for the HAM radio license test."
    category = "education"
    cooldown_seconds = 3

    def __init__(self, bot):
        super().__init__(bot)

    def generate_question(self) -> dict or None:
        cwd = Path(__file__).resolve().parent
        data_path = Path(__file__).resolve().parent.parent.parent
        try:
            if data_path.exists():
                with open((data_path / "data/randomlines/ham_questions.json"), "r") as data:
                    json_data = json.load(data)
                    question = json_data[random.randrange(0, len(json_data))]
                    print(question)
                    return question
            else:
                raise BaseException(f"Couldn't find hamhelper question data at {data_path}")
        except BaseException as e:
            print(f"Failed to load question: {e}")

    async def execute(self, message: MeshMessage) -> bool:
        question = self.generate_question()
        question_id = question["id"]
        question_text = question["question"]
        question_answers = question["answers"]
        question_figure = question["figure"]
        correct_letter = question["correct_letter"]
        if question:
            print("Sending message text")
            await self.send_response(message, question_text)
            await asyncio.sleep(3)

            for index, answer in enumerate(question_answers):
                await asyncio.sleep(3)
                if index == 0:
                    # A
                    answer = f"A. {answer}"
                elif index == 1:
                    # B
                    answer = f"B. {answer}"
                elif index == 2:
                    # C
                    answer = f"C. {answer}"
                elif index == 3:
                    # D
                    answer = f"D. {answer}"
                else:
                    print("Something weird here?")
                print("Sending answer")
                await self.send_response(message, answer, skip_user_rate_limit=True)

            if question_figure:
                print("Sending figure")
                await asyncio.sleep(3)
                await self.send_response(message, question_figure, skip_user_rate_limit=True)
            
            print("Sending correct letter")
            await asyncio.sleep(20)
            await self.send_response(message, f"The correct answer is: {correct_letter}", skip_user_rate_limit=True)
            return True
        await self.send_response(message, "Failed to load question, check logs for more details...", skip_user_rate_limit=True)
        return False