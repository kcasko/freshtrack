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
    owners = phone_owners()
    for match in PHONE.finditer(reply):
        digits = "".join(match.groups())
        if digits not in phones:
            problems.append(f"phone {match.group(0)} is not in the dataset")
            continue
        named = sites_for_phone(reply, match.start(), match.end())
        if named and digits in owners and not named & owners[digits]:
            problems.append(f"phone {match.group(0)} belongs to {' / '.join(sorted(owners[digits]))}, not {' / '.join(sorted(named))}")
    return problems + unsupported_promises(reply)


# Promises about what pantries ask. Nothing in the dataset supports them, and a
# wrong one can put someone at risk, so they always go to the fact-checker.
UNSUPPORTED_PROMISE = re.compile(
    r"(do(es)?n'?t|do(es)? not|won'?t|will not|never)\s+(ask|check|require|need)\w*\s+(about\s+|for\s+)?(your\s+)?"
    r"(immigration|citizenship|papers|documents|id\b|income)"
    r"|no\s+(preguntan|piden|revisan)\w*\s+(por\s+|sobre\s+)?(el\s+|tu\s+|su\s+)?(estatus|estado|situación)\s+migratori"
    r"|(?<!if )(?<!whether )\b(you|everyone)\s+(qualify|are eligible|will be served)",
    re.IGNORECASE,
)


def unsupported_promises(reply):
    return [f"unsupported promise: '{m.group(0)}'" for m in UNSUPPORTED_PROMISE.finditer(reply)]


def phone_owners():
    """Map each listed phone number to the sites that publish it."""
    owners = {}
    for site in data.sites():
        text = " ".join(str(site.get(k, "")) for k in ("phone", "hours_note"))
        for match in PHONE.finditer(text):
            owners.setdefault("".join(match.groups()), set()).add(site["name"])
    return owners


def _mentions(text):
    """Every (start, end, site name) where a site's name or short name appears."""
    found = []
    for site in data.sites():
        for alias in [site["name"], *site["schedule"].get("aliases", [])]:
            for match in re.finditer(re.escape(alias), text, re.IGNORECASE):
                found.append((match.start(), match.end(), site["name"]))
    # Prefer the longest alias at a position, so "Kalamazoo Loaves & Fishes"
    # wins over the "Loaves and Fishes" inside a church pantry's name.
    found.sort(key=lambda m: (m[0], -(m[1] - m[0])))
    kept, covered_to = [], -1
    for start, end, name in found:
        if start >= covered_to:
            kept.append((start, end, name))
            covered_to = end
    return kept


def sites_for_phone(text, start, end):
    """Which sites a phone number could be being given for.

    Every site named in the same sentence, since a sentence like "Ministry
    with Community and Gospel Mission serve lunch, call 269-343-5880" names
    two. If the sentence names none, every site in the last earlier sentence
    of the paragraph that named one. An empty set means no site is named.
    """
    paragraph_start = text.rfind("\n\n", 0, start) + 1
    sentence_start = max(paragraph_start, *(m.end() for m in re.finditer(r"[.!?؟]\s|\n", text[:start])), 0)
    next_break = re.search(r"[.!?؟](\s|$)|\n", text[end:])
    sentence_end = end + next_break.start() if next_break else len(text)

    mentions = _mentions(text)
    in_sentence = [m for m in mentions if m[0] >= sentence_start and m[1] <= sentence_end]
    if in_sentence:
        return {m[2] for m in in_sentence}
    earlier = [m for m in mentions if paragraph_start <= m[0] < start]
    if not earlier:
        return set()
    # Every site in the last sentence that named one.
    last = earlier[-1][0]
    last_sentence_start = max(paragraph_start, *(m.end() for m in re.finditer(r"[.!?؟]\s|\n", text[:last])), 0)
    return {m[2] for m in earlier if m[0] >= last_sentence_start}
