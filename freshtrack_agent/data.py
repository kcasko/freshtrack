"""Loads the FreshTrack dataset and answers schedule questions without any AI.

Everything the agent says about hours comes from here, so it can be tested
directly and never depends on the model reading free text correctly.
"""

import datetime as dt
import json
import math
import os
from functools import lru_cache

DATA_DIR = os.environ.get(
    "FRESHTRACK_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}
UNKNOWN_END_MINUTES = 180


def _load(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def sites():
    """Every site, with its GeoJSON properties, coordinates, and structured schedule."""
    schedules = _load("schedules.json")["sites"]
    result = []
    for feature in _load("places.geojson")["features"]:
        props = dict(feature["properties"])
        props["position"] = feature["geometry"]["coordinates"]
        props["schedule"] = schedules.get(props["name"], {"windows": []})
        result.append(props)
    return result


@lru_cache(maxsize=1)
def zip_centroids():
    return _load("zip_centroids.json")["zips"]


def find_site(name):
    """Match a site by exact name, then by substring of its name or a short name."""
    lowered = name.lower().strip()
    for site in sites():
        if site["name"].lower() == lowered:
            return site
    # Prefer the longest matching name, so "Comstock Middle School" doesn't
    # match the short name "Comstock" of the community center.
    best, best_len = None, 0
    for site in sites():
        names = [site["name"].lower(), *(a.lower() for a in site["schedule"].get("aliases", []))]
        for n in names:
            if (lowered in n or n in lowered) and len(n) > best_len:
                best, best_len = site, len(n)
    return best


def haversine_miles(a, b):
    radius = 3958.8
    d_lat = math.radians(b[1] - a[1])
    d_lon = math.radians(b[0] - a[0])
    lat1 = math.radians(a[1])
    lat2 = math.radians(b[1])
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def week_of_month(day):
    return (day.day - 1) // 7 + 1


def _minutes(text):
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def format_time(text):
    """'17:30' -> '5:30pm', '09:00' -> '9am'."""
    if text is None:
        return None
    hours, minutes = (int(part) for part in text.split(":"))
    suffix = "am" if hours < 12 else "pm"
    hours = hours % 12 or 12
    return f"{hours}{suffix}" if minutes == 0 else f"{hours}:{minutes:02d}{suffix}"


def window_applies(window, day):
    # Mobile distributions run on listed dates rather than a weekly rule.
    if window.get("dates"):
        return day.isoformat() in window["dates"]
    if DAYS[day.weekday()] not in window["days"]:
        return False
    week = week_of_month(day)
    if window.get("weeks") and week not in window["weeks"]:
        return False
    if week in window.get("exclude_weeks", []):
        return False
    return True


def describe_window(window):
    if window.get("dates"):
        listed = [dt.date.fromisoformat(d) for d in window["dates"]]
        days = ", ".join(f"{d:%a %b} {d.day}" for d in listed)
    else:
        days = "/".join(window["days"]) if len(window["days"]) < 7 else "Every day"
    if window.get("weeks"):
        days = " and ".join(ORDINALS[w] for w in window["weeks"]) + " " + days
    if window.get("exclude_weeks"):
        days += " (not the " + " or ".join(ORDINALS[w] for w in window["exclude_weeks"]) + ")"
    start, end = format_time(window.get("start")), format_time(window.get("end"))
    if start and end:
        times = f"{start}-{end}"
    elif start:
        times = f"starts {start}, end time not listed"
    else:
        times = "time not listed"
    label = f" ({window['label']})" if window.get("label") else ""
    return f"{days} {times}{label}"


def status_at(site, now):
    """Return a plain status for one site at one moment.

    status is one of:
      open_now        a listed window covers now
      maybe_open      a window started within the last few hours and its end time is not listed
      time_unknown    the site runs today but the source gives no time
      later_today     a window opens later today
      closed_today    nothing today
    """
    minute = now.hour * 60 + now.minute
    today = [w for w in site["schedule"].get("windows", []) if window_applies(w, now.date())]
    timed = sorted((w for w in today if w.get("start")), key=lambda w: _minutes(w["start"]))

    def first(test):
        return next((w for w in timed if test(_minutes(w["start"]), w.get("end"))), None)

    # Checked in priority order: a definite answer beats a vague one.
    candidates = [
        ("open_now", first(lambda s, e: s <= minute and e and minute < _minutes(e))),
        # With no listed end, assume a window may still be running for a few
        # hours, not all day: a 9am produce stand is not "maybe open" at 5pm.
        ("maybe_open", first(lambda s, e: s <= minute < s + UNKNOWN_END_MINUTES and e is None)),
        ("later_today", first(lambda s, e: minute < s)),
        ("time_unknown", next((w for w in today if not w.get("start")), None)),
    ]
    status, detail = next(((s, w) for s, w in candidates if w), ("closed_today", None))

    return {
        "status": status,
        "window": describe_window(detail) if detail else None,
        "next_open": None if status == "open_now" else next_open(site, now),
    }


def next_open(site, now, days_ahead=35):
    """Describe the next listed window after now, looking up to five weeks out."""
    minute = now.hour * 60 + now.minute
    for offset in range(days_ahead + 1):
        day = now.date() + dt.timedelta(days=offset)
        windows = [w for w in site["schedule"].get("windows", []) if window_applies(w, day)]
        windows.sort(key=lambda w: _minutes(w["start"]) if w.get("start") else 24 * 60)
        for window in windows:
            if offset == 0 and (window.get("start") is None or _minutes(window["start"]) <= minute):
                continue
            when = "today" if offset == 0 else ("tomorrow" if offset == 1 else f"{day:%A %B} {day.day}")
            start = format_time(window.get("start"))
            return f"{when}, " + (f"from {start}" if start else "time not listed")
    return None
