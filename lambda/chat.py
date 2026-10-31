"""FreshTrack chat Lambda: the Strands agent behind a Lambda Function URL.

The map page POSTs:
    {"message": "...", "history": [{"role": "user"|"assistant", "text": "..."}],
     "pin": [lon, lat] (optional), "now": "YYYY-MM-DD HH:MM" (optional demo clock)}
and gets back:
    {"reply": "...", "places": [{"name", "address", "phone", "position"}],
     "guard": {"tool_calls": [...], "problems": [...], "rewritten": bool}}

Abuse limits, because the URL is public and every call spends Bedrock tokens:
- message, history, and pin are size- and range-checked
- a DynamoDB counter caps requests per day (DAILY_LIMIT), checked atomically
- reserved concurrency on the function caps how many run at once
CORS is set on the Function URL itself, not here.

No message text, location, or IP address is stored. The usage table holds
only a date and a count.
"""

import datetime as dt
import json
import os
import re

import boto3

from freshtrack_agent import data, guard, tools
from freshtrack_agent.agent import FreshTrack

DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "500"))
USAGE_TABLE = os.environ.get("USAGE_TABLE", "freshtrack-chat-usage")
MAX_MESSAGE = 500
MAX_HISTORY = 8
NOW_PATTERN = re.compile(r"2\d{3}-\d{2}-\d{2} \d{2}:\d{2}")

_dynamodb = None


def _respond(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, ensure_ascii=False)}


def _under_daily_limit():
    """Count this request against today's cap. False once the cap is reached."""
    global _dynamodb
    if not USAGE_TABLE:
        return True
    if _dynamodb is None:
        _dynamodb = boto3.client("dynamodb")
    today = dt.date.today().isoformat()
    try:
        _dynamodb.update_item(
            TableName=USAGE_TABLE,
            Key={"day": {"S": today}},
            UpdateExpression="ADD requests :one",
            ConditionExpression="attribute_not_exists(requests) OR requests < :limit",
            ExpressionAttributeValues={":one": {"N": "1"}, ":limit": {"N": str(DAILY_LIMIT)}},
        )
        return True
    except _dynamodb.exceptions.ConditionalCheckFailedException:
        return False


def _clean_history(raw):
    history = []
    for turn in (raw if isinstance(raw, list) else [])[-MAX_HISTORY:]:
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant") and isinstance(turn.get("text"), str):
            history.append({"role": turn["role"], "text": turn["text"][:1500]})
    # Bedrock needs turns to alternate, starting with the user.
    while history and history[0]["role"] != "user":
        history.pop(0)
    cleaned = []
    for turn in history:
        if cleaned and cleaned[-1]["role"] == turn["role"]:
            cleaned[-1] = turn
        else:
            cleaned.append(turn)
    if cleaned and cleaned[-1]["role"] == "user":
        cleaned.pop()
    return cleaned


def _places_in(reply):
    """Sites the reply names, so the map can highlight them."""
    seen, places = set(), []
    for _, _, name in guard._mentions(reply):
        if name in seen:
            continue
        seen.add(name)
        site = data.find_site(name)
        places.append({"name": name, "address": site.get("address"), "phone": site.get("phone"), "position": site["position"]})
    return places


def handler(event, context=None):
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _respond(204, {})
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        request = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return _respond(400, {"error": "Send JSON with a message."})

    if not isinstance(request, dict):
        return _respond(400, {"error": "Send a JSON object with a message."})
    if not isinstance(request.get("message"), str):
        return _respond(400, {"error": "Message must be text."})
    message = request["message"].strip()
    if not message or len(message) > MAX_MESSAGE:
        return _respond(400, {"error": f"Write a message of 1 to {MAX_MESSAGE} characters."})

    pin = request.get("pin")
    if isinstance(pin, list) and len(pin) == 2 and all(isinstance(v, (int, float)) for v in pin):
        if data.haversine_miles([-85.5872, 42.2917], pin) <= 35:
            message += f"\n\n(Map pin: {pin[1]:.5f}, {pin[0]:.5f})"

    now = request.get("now")
    try:
        requested_now = dt.datetime.strptime(now, "%Y-%m-%d %H:%M") if isinstance(now, str) and NOW_PATTERN.fullmatch(now) else None
    except ValueError:
        return _respond(400, {"error": "Invalid demo date or time."})
    tools.set_now(requested_now)

    if not _under_daily_limit():
        return _respond(429, {"error": "FreshTrack has reached today's demo limit. For food help now, call Kalamazoo Loaves & Fishes at 269-343-3663 or dial 2-1-1."})

    try:
        bot = FreshTrack(history=_clean_history(request.get("history")))
        reply = bot.ask(message)
        trace = bot.last_trace
    except Exception as exc:  # keep details out of the public response
        print(f"agent error type: {type(exc).__name__}")
        return _respond(502, {"error": "FreshTrack could not answer just now. Call Kalamazoo Loaves & Fishes at 269-343-3663 or dial 2-1-1."})
    finally:
        now_used = tools.now()
        tools.set_now(None)

    return _respond(200, {
        "reply": reply,
        "places": _places_in(reply),
        "now": now_used.strftime("%a %b %d, %I:%M %p"),
        "guard": {"tool_calls": trace["tool_calls"], "problems": trace["guard_problems"], "rewritten": trace["rewritten"]},
    })
