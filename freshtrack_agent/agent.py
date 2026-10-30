"""FreshTrack: two Strands agents and a deterministic guard between them.

    person -> Navigator agent (tools) -> Hours guard -> reply
                                           |  fails
                                           v
                                   Fact-checker agent (no tools,
                                   sees only the tool results) -> guard again

The navigator answers with tools. A hook records every tool result. The
guard checks the reply's times and phone numbers against the dataset. If it
finds anything made up, the fact-checker rewrites the reply from the
recorded tool results alone. If that still fails, the person gets a safe
fallback with the phone numbers that are always right.
"""

import json
import os
import re

from strands import Agent
from strands.handlers.callback_handler import PrintingCallbackHandler
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel

from . import guard, tools

MODEL_ID = os.environ.get("FRESHTRACK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

NAVIGATOR_PROMPT = """You are FreshTrack, a text-message helper that finds free food in Kalamazoo County, Michigan.
The current local time is {now}.

People writing to you may be hungry, stressed, on a cheap phone, or reading in a second language.

Rules:
- Always call find_food_help before naming any site. Use route_to_site when they mention walking, a bus, or no car. When they ask about one site by name, call site_details with that name.
- Only state hours, days, and phone numbers that appear in a tool result. Never estimate or round a time. If the tool says the time is not listed, say that.
- Each phone number belongs to one site. If a site's phone is "not listed", say so; never give it another site's number.
- Lead with what they can do right now: the closest place open now, or the next one that opens today. Then one backup.
- If the best option is far, still name it with its distance, and also name any free meal in free_meals_open_today that is close.
- Do not write any text before or between tool calls. Only your final message reaches the person, so it must make sense on its own and name every place it talks about.
- If sources_disagree_call_first is true, or status is maybe_open or time_unknown, tell them to call first.
- If nothing is open today, say so plainly and give the fallback from the tool result (phone scheduling and home delivery).
- Reply in the same language the person wrote in.
- Write at a 6th-grade reading level. Short sentences. No more than about 90 words. No markdown headers or tables; it is a text message.
- Do not ask for their name, income, or immigration status. Never promise eligibility, and never say what a site does or does not ask about (immigration status, income, ID) unless a tool result says it. If unsure, tell them to call and ask.
- If someone mentions danger or a medical emergency, such as fainting, tell them to call 911 first.
- If someone mentions suicide or self-harm, first tell them they can call or text 988 (Suicide and Crisis Lifeline) any time, then help with food.
- A message may end with "(Map pin: <lat>, <lon>)". If the person names no other place, pass "<lat>, <lon>" as the location. Never mention the coordinates; call it "the pin on the map".
- If the location is outside Kalamazoo County, say FreshTrack only covers Kalamazoo County and tell them to dial 2-1-1."""

CHECKER_PROMPT = """You are the FreshTrack fact-checker. You get a draft text message and the exact tool results it was based on.
Rewrite the draft so that every time, day, and phone number comes from the tool results word for word. Remove anything you cannot find there.
Keep the same language, the same friendly tone, and keep it under about 90 words. Return only the rewritten message."""

SAFE_FALLBACK = (
    "I could not confirm hours just now. Call Kalamazoo Loaves & Fishes at 269-343-3663 "
    "(Mon-Fri 9am-4pm) to schedule groceries or free home delivery, or dial 2-1-1 any time."
)


class ToolResultRecorder(HookProvider):
    """Keeps every tool result from the current turn, for the fact-checker."""

    def __init__(self):
        self.results = []

    def register_hooks(self, registry: HookRegistry, **kwargs):
        registry.add_callback(AfterToolCallEvent, self._record)

    def _record(self, event: AfterToolCallEvent):
        texts = [block.get("text", "") for block in (event.result or {}).get("content", []) if isinstance(block, dict)]
        self.results.append({"tool": event.tool_use.get("name"), "input": event.tool_use.get("input"), "output": "\n".join(texts)})


class FreshTrack:
    def __init__(self, model_id=MODEL_ID, verbose=False, history=None):
        """history: earlier turns as [{"role": "user" | "assistant", "text": ...}], for stateless callers like Lambda."""
        model = BedrockModel(model_id=model_id, temperature=0.2)
        self.recorder = ToolResultRecorder()
        self.navigator = Agent(
            name="navigator",
            model=model,
            messages=[{"role": turn["role"], "content": [{"text": turn["text"]}]} for turn in (history or [])],
            tools=tools.ALL_TOOLS,
            system_prompt=NAVIGATOR_PROMPT.format(now=tools.now().strftime("%A %B %d, %I:%M %p")),
            hooks=[self.recorder],
            callback_handler=PrintingCallbackHandler() if verbose else None,
        )
        self.checker_model = model
        self.last_trace = {}

    def ask(self, message):
        self.recorder.results.clear()
        first_new = len(self.navigator.messages)
        self.navigator(message)
        # Join every bit of text the navigator wrote this turn, so a sentence
        # it wrote before a tool call is not lost from the final message.
        draft = tidy_for_sms("\n\n".join(
            block["text"].strip()
            for msg in self.navigator.messages[first_new:]
            if msg["role"] == "assistant"
            for block in msg["content"]
            if block.get("text", "").strip()
        ))
        problems = guard.check(draft, tools.now())
        trace = {"draft": draft, "guard_problems": problems, "tool_calls": [r["tool"] for r in self.recorder.results], "rewritten": False}
        reply = draft

        if problems:
            checker = Agent(name="fact_checker", model=self.checker_model, system_prompt=CHECKER_PROMPT, callback_handler=None)
            evidence = json.dumps(self.recorder.results, ensure_ascii=False)[:20000]
            reply = tidy_for_sms(str(checker(f"Draft:\n{draft}\n\nProblems found:\n{problems}\n\nTool results:\n{evidence}")))
            trace["rewritten"] = True
            trace["after_rewrite_problems"] = guard.check(reply, tools.now())
            if trace["after_rewrite_problems"]:
                reply = self._fallback_in_language_of(message)
                trace["fell_back"] = True

        self.last_trace = trace
        return reply

    def _fallback_in_language_of(self, message):
        """The safe fallback, translated to the person's language if the translation passes the guard."""
        translator = Agent(
            name="translator",
            model=self.checker_model,
            system_prompt="Translate the text into the language of the sample. Keep every number, time, and name exactly. Return only the translation.",
            callback_handler=None,
        )
        try:
            translated = tidy_for_sms(str(translator(f"Sample: {message}\n\nText: {SAFE_FALLBACK}")))
        except Exception:
            return SAFE_FALLBACK
        return translated if translated and not guard.check(translated, tools.now()) else SAFE_FALLBACK


# Narration of the agent's own steps, which means nothing to someone reading a text.
FILLER = re.compile(
    r"\b(let me (check|look|see|find|search|get)|i'?ll (check|look|search|find)|i will (check|look|search)|"
    r"one moment|déjame (ver|buscar|revisar)|voy a (buscar|revisar))\b",
    re.IGNORECASE,
)


def tidy_for_sms(text):
    """Drop filler sentences and markdown bold, which SMS shows as literal asterisks."""
    lines = []
    for line in text.replace("**", "").split("\n"):
        sentences = re.split(r"(?<=[.!?؟])\s+", line.strip())
        kept = " ".join(s for s in sentences if not FILLER.search(s))
        if kept or line.strip() == "":
            lines.append(kept)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
