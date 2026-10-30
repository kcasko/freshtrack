"""Strands tools for the FreshTrack navigator agent.

The tools do all the factual work: where the person is, what is near, and
what is open at this moment. The model only explains the results.
"""

import datetime as dt
import os
from zoneinfo import ZoneInfo

from strands import tool

from . import data, geo

TIMEZONE = ZoneInfo("America/Detroit")
NEEDS = {
    "groceries": "emergency_pantry",
    "meal": "soup_kitchen",
    "senior": "senior_program",
    "mobile": "mobile_program",
}
AVAILABLE = ("open_now", "maybe_open", "later_today")
FAR_MILES = 10

_fixed_now = None


def set_now(value):
    """Pin the clock, for demos and the evaluation. None means real time."""
    global _fixed_now
    _fixed_now = value


def now():
    return _fixed_now or dt.datetime.now(TIMEZONE).replace(tzinfo=None)


def _site_summary(site, start, moment):
    schedule = site["schedule"]
    status = data.status_at(site, moment)
    summary = {
        "name": site["name"],
        "address": site.get("address"),
        "phone": site.get("phone") or "not listed",
        "services": site.get("service_types", []),
        "straight_line_miles": round(data.haversine_miles(start, site["position"]), 1),
        "status": status["status"],
        "todays_window": status["window"],
        "next_open": status["next_open"],
        "hours_as_published": site.get("hours_note"),
        "sources_disagree_call_first": bool(schedule.get("conflict")),
    }
    for key in ("appointment_only", "delivery", "service_area", "eligibility", "visit_limit", "bring", "kind", "notes"):
        if schedule.get(key):
            summary[key] = schedule[key]
    return summary


@tool
def find_food_help(location: str, need: str = "any", limit: int = 4) -> dict:
    """Find food help near a person, with what is open right now.

    Args:
        location: Street address, intersection, landmark, or ZIP code in or near Kalamazoo County, Michigan.
        need: One of "groceries" (food pantry), "meal" (free hot meal / soup kitchen), "senior" (programs for people 60+), "mobile" (mobile food distributions), or "any".
        limit: How many sites to return in each list, 1 to 6.
    """
    place = geo.locate(location)
    if not place:
        return {"error": "Could not find that location in Kalamazoo County. Ask for a ZIP code or a nearby street corner."}

    moment = now()
    service = NEEDS.get(need)
    limit = max(1, min(int(limit), 6))
    matches = [s for s in data.sites() if service is None or service in s.get("service_types", [])]
    ranked = sorted((_site_summary(s, place["position"], moment) for s in matches), key=lambda s: s["straight_line_miles"])

    available = [s for s in ranked if s["status"] in AVAILABLE]
    available.sort(key=lambda s: (AVAILABLE.index(s["status"]), s["straight_line_miles"]))
    result = {
        "now": moment.strftime("%A %B %d, %I:%M %p"),
        "located_as": place["label"],
        "location_precision": place["precision"],
        "open_now_or_later_today": available[:limit],
        "nearest_any_day": ranked[:limit],
        "fallback": "Kalamazoo Loaves & Fishes, 269-343-3663, schedules groceries by phone and offers free home delivery in Kalamazoo County. For anything else, dial 2-1-1.",
    }
    if service != "soup_kitchen":
        meals = (_site_summary(s, place["position"], moment) for s in data.sites() if "soup_kitchen" in s.get("service_types", []))
        result["free_meals_open_today"] = sorted(
            (m for m in meals if m["status"] in AVAILABLE), key=lambda m: m["straight_line_miles"]
        )[:2]
    if ranked and ranked[0]["straight_line_miles"] > FAR_MILES:
        result["far_from_sites"] = True
    return result


@tool
def route_to_site(location: str, site_name: str, travel_mode: str = "walk") -> dict:
    """Get distance and travel time from the person to one site, using Amazon Location Routes.

    Args:
        location: Where the person is starting from.
        site_name: Site name exactly as returned by find_food_help.
        travel_mode: "walk" or "drive".
    """
    place = geo.locate(location)
    site = data.find_site(site_name)
    if not place or not site:
        return {"error": "Unknown location or site name."}
    route = geo.route(place["position"], site["position"], "walk" if travel_mode == "walk" else "drive")
    return {"site": site["name"], "address": site.get("address"), "from": place["label"], **route}


@tool
def site_details(site_name: str) -> dict:
    """Full details for one site: every listed window, notes, eligibility, and source.

    Args:
        site_name: Site name exactly as returned by find_food_help.
    """
    site = data.find_site(site_name)
    if not site:
        return {"error": "No site by that name."}
    return {
        "name": site["name"],
        "address": site.get("address"),
        "phone": site.get("phone") or "not listed",
        "all_windows": [data.describe_window(w) for w in site["schedule"].get("windows", [])],
        "hours_as_published": site.get("hours_note"),
        "notes": site.get("note"),
        "source": site.get("source"),
        "status_now": data.status_at(site, now()),
    }


ALL_TOOLS = [find_food_help, route_to_site, site_details]
