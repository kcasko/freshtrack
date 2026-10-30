"""Hours and phone guard: a deterministic check that the reply invents nothing.

A wrong pantry time sends someone on a bus ride to a locked door. So every
clock time and phone number in a reply must appear in the dataset, or be
the current time. This runs on every reply, with no model involved.
"""

import re

from . import data

TIME_12H = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?", re.IGNORECASE)
TIME_24H = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[ap]\.?\s?m)", re.IGNORECASE)
PHONE = re.compile(r"\(?\b(\d{3})\)?[-.\s]?(\d{3})[-.\s](\d{4})\b")


def _to_minutes(hour, minute, meridiem):
    hour, minute = int(hour), int(minute or 0)
    if meridiem:
        if hour > 12:
            return None
        hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
    return hour * 60 + minute


def time_mentions(text):
    """Each clock time in the text, as the set of minute values it could mean.

    '5:30pm' can only mean 17:30. A bare '5:30' could be 5:30 or 17:30,
    because many languages put the am/pm word after it ('6:30 مساءً',
    '5:30 de la tarde'), so both readings are kept.
    """
    mentions = []
    for match in TIME_12H.finditer(text):
        value = _to_minutes(match.group(1), match.group(2), match.group(3))
        if value is not None:
            mentions.append({value})
    for match in TIME_24H.finditer(text):
        value = _to_minutes(match.group(1), match.group(2), None)
        mentions.append({value, value + 12 * 60} if value < 12 * 60 else {value})
    return mentions


def times_in(text):
    return set().union(*time_mentions(text))


def _range_ends(text):
    """Times written as '9-11am' or '10:30am-12:30pm' put the meridiem on one end only."""
    found = set()
    pattern = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", re.IGNORECASE)
    for match in pattern.finditer(text):
        for meridiem in ("a", "p"):
            value = _to_minutes(match.group(1), match.group(2), meridiem)
            if value is not None:
                found.add(value)
    return found


def allowed_times():
    allowed = set()
    for site in data.sites():
        note = site.get("hours_note", "")
        allowed |= times_in(note) | _range_ends(note)
        for window in site["schedule"].get("windows", []):
            for key in ("start", "end"):
                if window.get(key):
                    hours, minutes = window[key].split(":")
                    allowed.add(int(hours) * 60 + int(minutes))
    return allowed


def allowed_phones():
    phones = {"2-1-1"}
    for site in data.sites():
        text = " ".join(str(site.get(k, "")) for k in ("phone", "hours_note", "note"))
        phones |= {"".join(m.groups()) for m in PHONE.finditer(text)}
    return phones


def check(reply, now=None):
    """Return a list of problems. An empty list means the reply passed."""
    problems = []
    allowed = allowed_times()
    if now is not None:
        allowed.add(now.hour * 60 + now.minute)
    for readings in time_mentions(reply):
        if not readings & allowed:
            minutes = max(readings)
            problems.append(f"time {data.format_time(f'{minutes // 60:02d}:{minutes % 60:02d}')} is not in the dataset")
    phones = allowed_phones()
    for match in PHONE.finditer(reply):
        digits = "".join(match.groups())
        if digits not in phones:
            problems.append(f"phone {match.group(0)} is not in the dataset")
    return problems
