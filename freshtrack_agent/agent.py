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
- Only say a place needs no ID or no paperwork if that place's own tool result says so (the Loaves & Fishes programs and mobile pantries do). For anywhere else, say to call and ask what to bring. If the person says they have no ID, also search with need "mobile" and lead with places that say no ID is needed.
- If a site has eligibility, service_area, or visit_limit, say it in a few words (for example "WMU students only" or "once every 90 days"). Don't send someone to a place they can't use.
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

        reply, trace["safety_lines_added"] = add_safety_lines(message, reply)
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


# Safety lines are added in code, not left to the prompt: in testing, the model
# sometimes skipped "call 911" for someone about to faint.
EMERGENCY = re.compile(
    r"pass(ing)? out|faint|dizzy|chest pain|can'?t breathe|unconscious|overdose|seizure|"
    r"desmay|mareado|no puedo respirar|dolor (de|en el) pecho|"
    r"إغماء|أغمى|سأفقد الوعي|دوار شديد|لا أستطيع التنفس|ألم في الصدر",
    re.IGNORECASE,
)
CRISIS = re.compile(
    r"suicid|kill myself|\bend it\b|end my life|want to die|hurt myself|self[- ]harm|"
    r"quitarme la vida|matarme|no quiero vivir|"
    r"أريد الانتحار|أفكر في الانتحار|أريد أن أموت|سأقتل نفسي",
    re.IGNORECASE,
)


def add_safety_lines(message, reply):
    """Put 911 or 988 first when the message calls for it and the reply left it out."""
    added = []
    arabic = bool(re.search(r"[\u0600-\u06ff]", message))
    spanish = bool(re.search(r"desmay|mareado|suicid|matarme|no quiero vivir", message, re.IGNORECASE))
    if CRISIS.search(message) and "988" not in reply:
        line = ("يمكنك الاتصال أو إرسال رسالة نصية إلى 988 في أي وقت." if arabic else
                "Puedes llamar o enviar un mensaje al 988 a cualquier hora." if spanish else
                "You can call or text 988 any time, day or night, to talk with someone.")
        reply = line + "\n\n" + reply
        added.append("988")
    if EMERGENCY.search(message) and "911" not in reply:
        line = ("إذا كنت على وشك الإغماء أو في خطر طبي، اتصل بـ 911 الآن." if arabic else
                "Si estás por desmayarte o tienes una emergencia médica, llama al 911 ahora." if spanish else
                "If you feel faint or very sick, call 911 now.")
        reply = line + "\n\n" + reply
        added.append("911")
    return reply, added


# Narration of the agent's own steps, which means nothing to someone reading a text.
FILLER = re.compile(
    r"\b(let me (check|look|see|find|search|get)|i'?ll (check|look|search|find)|i will (check|look|search)|"
    r"one moment|déjame (ver|buscar|revisar)|voy a (buscar|revisar))\b",
    re.IGNORECASE,
)


def tidy_for_sms(text):
    """Drop filler sentences, repeated sentences, and markdown bold (SMS shows it as literal asterisks).

    Repeats happen when the agent answers briefly before a tool call and again after it.
    """
    lines, seen = [], set()
    for line in text.replace("**", "").split("\n"):
        sentences = re.split(r"(?<=[.!?؟])\s+", line.strip())
        kept_sentences = []
        for sentence in sentences:
            key = sentence.strip().lower()
            if FILLER.search(sentence) or (key and key in seen):
                continue
            seen.add(key)
            kept_sentences.append(sentence)
        kept = " ".join(kept_sentences)
        if kept or line.strip() == "":
            lines.append(kept)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
