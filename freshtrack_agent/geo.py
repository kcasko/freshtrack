"""Amazon Location Service calls: geocoding with Places and routing with Routes.

Both use the v2 Amazon Location APIs (geo-places, geo-routes), which need no
place index or route calculator resource, only IAM permission for
geo-places:Geocode and geo-routes:CalculateRoutes.

Set FRESHTRACK_OFFLINE=1 to skip AWS entirely. Then only ZIP codes can be
located, and routes fall back to straight-line distance, labeled as such.
"""

import os
import re

from . import data

REGION = os.environ.get("AWS_REGION", "us-east-1")
KALAMAZOO_CENTER = [-85.5872, 42.2917]
MAX_MILES_FROM_CENTER = 35
ZIP_PATTERN = re.compile(r"\b(49\d{3})\b")

_clients = {}


def offline():
    return os.environ.get("FRESHTRACK_OFFLINE") == "1"


def _client(name):
    if name not in _clients:
        import boto3

        _clients[name] = boto3.client(name, region_name=REGION)
    return _clients[name]


def locate(text):
    """Turn what the person typed into a point.

    Returns {"position": [lon, lat], "label": ..., "precision": ...} or None.
    A bare ZIP code uses the ZIP center from data/zip_centroids.json, which
    came from Amazon Location. Anything else is geocoded live, biased toward
    Kalamazoo, and rejected if it lands far outside the county.
    """
    text = (text or "").strip()
    zip_match = ZIP_PATTERN.search(text)
    zip_info = data.zip_centroids().get(zip_match.group(1)) if zip_match else None

    if text and not offline() and not re.fullmatch(r"\d{5}", text):
        try:
            response = _client("geo-places").geocode(
                QueryText=text,
                BiasPosition=KALAMAZOO_CENTER,
                Filter={"IncludeCountries": ["USA"]},
                MaxResults=1,
            )
            items = response.get("ResultItems", [])
            if items:
                position = items[0]["Position"]
                if data.haversine_miles(KALAMAZOO_CENTER, position) <= MAX_MILES_FROM_CENTER:
                    return {
                        "position": position,
                        "label": items[0].get("Title", text),
                        "precision": items[0].get("PlaceType", "address"),
                    }
        except Exception:
            pass  # fall through to the ZIP center, if there is one

    if zip_info:
        return {"position": zip_info["position"], "label": f"ZIP {zip_match.group(1)} ({zip_info['label']})", "precision": "zip_center"}
    return None


def route(start, end, mode):
    """Road or walking route with Amazon Location Routes.

    Returns {"miles", "minutes", "method"}. If routing is unavailable, gives
    straight-line miles and no minutes, because a guessed travel time is worse
    than none.
    """
    if not offline():
        try:
            response = _client("geo-routes").calculate_routes(
                Origin=start,
                Destination=end,
                TravelMode="Pedestrian" if mode == "walk" else "Car",
            )
            meters = seconds = 0
            for leg in response["Routes"][0]["Legs"]:
                details = leg.get("PedestrianLegDetails") or leg.get("VehicleLegDetails") or {}
                overview = details.get("Summary", {}).get("Overview", {})
                meters += overview.get("Distance", 0)
                seconds += overview.get("Duration", 0)
            if meters:
                return {
                    "miles": round(meters / 1609.34, 1),
                    "minutes": round(seconds / 60),
                    "method": "Amazon Location route, " + ("walking" if mode == "walk" else "driving"),
                }
        except Exception:
            pass
    return {
        "miles": round(data.haversine_miles(start, end), 1),
        "minutes": None,
        "method": "straight line only, no travel time available",
    }
