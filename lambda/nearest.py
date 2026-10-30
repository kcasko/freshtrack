"""FreshTrack nearest-3 Lambda, for Amazon Location Service.

Deploy notes, not a live endpoint yet:
- Create a Place Index and a Route Calculator in Amazon Location Service.
- Give this Lambda IAM permission for geo:SearchPlaceIndexForText and
  geo:CalculateRoute on those resources only.
- Put data/places.geojson in the same S3 bucket that hosts the site, or
  bundle a copy with the function. This sample reads it from S3 when
  PLACES_BUCKET and PLACES_KEY are set, and otherwise uses the bundled copy.
- Expose it as a Lambda Function URL, and put that URL in config.js as
  lambdaEndpoint. The frontend sends {"address": "..."} and expects
  {"longitude": ..., "latitude": ..., "nearest": [...]} back.

No address, phone, or other personal data is stored by this function.
It only geocodes the address the user typed, in that request.
"""

import json
import math
import os

try:
    import boto3
except ImportError:  # local testing without boto3
    boto3 = None

REGION = os.environ.get("AWS_REGION", "us-east-1")
PLACE_INDEX = os.environ.get("PLACE_INDEX_NAME", "freshtrack-place-index")
ROUTE_CALCULATOR = os.environ.get("ROUTE_CALCULATOR_NAME", "freshtrack-route-calculator")


def haversine_miles(a, b):
    radius = 3958.8
    d_lat = math.radians(b[1] - a[1])
    d_lon = math.radians(b[0] - a[0])
    lat1 = math.radians(a[1])
    lat2 = math.radians(b[1])
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def load_places():
    bucket = os.environ.get("PLACES_BUCKET")
    key = os.environ.get("PLACES_KEY", "data/places.geojson")
    if bucket and boto3:
        s3 = boto3.client("s3", region_name=REGION)
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body)["features"]
    local = os.path.join(os.path.dirname(__file__), "places.geojson")
    with open(local, encoding="utf-8") as handle:
        return json.load(handle)["features"]


def handler(event, context=None):
    try:
        body = event.get("body") or "{}"
        if isinstance(body, str):
            body = json.loads(body)
        address = (body.get("address") or "").strip()
        if not address or not boto3:
            return {"statusCode": 400, "body": json.dumps({"error": "An address is required."})}

        location = boto3.client("location", region_name=REGION)
        search = location.search_place_index_for_text(IndexName=PLACE_INDEX, Text=address, MaxResults=1)
        if not search.get("Results"):
            return {"statusCode": 404, "body": json.dumps({"error": "Address not found."})}
        point = search["Results"][0]["Place"]["Geometry"]["Point"]
        start = [point[0], point[1]]

        places = []
        for feature in load_places():
            coords = feature["geometry"]["coordinates"]
            places.append({
                "name": feature["properties"]["name"],
                "address": feature["properties"].get("address", ""),
                "service_types": feature["properties"].get("service_types", []),
                "straight_line_miles": round(haversine_miles(start, coords), 2),
                "longitude": coords[0],
                "latitude": coords[1],
            })
        places.sort(key=lambda item: item["straight_line_miles"])
        nearest = places[:3]

        # Add driving and walking routes for the nearest 3, when a calculator exists.
        for item in nearest:
            try:
                route = location.calculate_route(
                    CalculatorName=ROUTE_CALCULATOR,
                    DeparturePosition=start,
                    DestinationPosition=[item["longitude"], item["latitude"]],
                    TravelMode="Car",
                )
                summary = route["Legs"][0]["Summary"]
                item["driving_miles"] = round(summary["Distance"] / 1609.34, 2)
                item["driving_minutes"] = round(summary["DurationSeconds"] / 60)
            except Exception:
                # Routing is an upgrade, not a blocker for the nearest list.
                pass

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"longitude": start[0], "latitude": start[1], "nearest": nearest}),
        }
    except Exception as exc:  # keep the error generic in the response
        return {"statusCode": 500, "body": json.dumps({"error": "Nearest search failed."})}
