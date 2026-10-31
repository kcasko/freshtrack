"""Deploy the FreshTrack map to Amazon S3 + CloudFront, and manage its Amazon Location API key.

    python deploy/deploy_site.py            # create or update the bucket, distribution, key, and files
    python deploy/deploy_site.py --files    # only upload files and invalidate the cache

Creates or updates:
- S3 bucket freshtrack-site-<account>: private, never public; CloudFront reads it through Origin Access Control
- CloudFront distribution for the bucket, HTTPS only, compressed, with index.html at the root
- Amazon Location API key freshtrack-web: only map tiles, geocoding, and routes, and only from the
  site's own addresses (CloudFront and localhost). The key is never stored in git:
  this script fills it into the copy of config.js it uploads.

Prints the site URL. Run deploy/deploy.py afterwards so the chat Lambda allows the CloudFront origin.
"""

import argparse
import json
import mimetypes
import os
import time
import uuid

import boto3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGION = "us-east-1"
SITE_FILES = ["index.html", "config.js", "data/places.geojson", "docs/architecture.svg", "docs/map-architecture.svg"]
KEY_NAME = "freshtrack-web"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"  # AWS managed cache policy
COMMENT = "FreshTrack map"


def ensure_bucket(s3, name):
    try:
        s3.head_bucket(Bucket=name)
    except s3.exceptions.ClientError:
        s3.create_bucket(Bucket=name)
        print(f"created bucket {name}")
    s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})


def ensure_distribution(cf, bucket):
    for dist in cf.list_distributions().get("DistributionList", {}).get("Items", []):
        if dist.get("Comment") == COMMENT:
            return dist["Id"], dist["DomainName"], dist["ARN"]
    oac_id = None
    for oac in cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", []):
        if oac["Name"] == "freshtrack-site":
            oac_id = oac["Id"]
    if not oac_id:
        oac_id = cf.create_origin_access_control(OriginAccessControlConfig={
            "Name": "freshtrack-site", "SigningProtocol": "sigv4", "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3"})["OriginAccessControl"]["Id"]
    origin = f"{bucket}.s3.{REGION}.amazonaws.com"
    dist = cf.create_distribution(DistributionConfig={
        "CallerReference": str(uuid.uuid4()),
        "Comment": COMMENT,
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "PriceClass": "PriceClass_100",
        "HttpVersion": "http2and3",
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "s3-site", "DomainName": origin, "OriginAccessControlId": oac_id,
            "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
        "DefaultCacheBehavior": {
            "TargetOriginId": "s3-site", "ViewerProtocolPolicy": "redirect-to-https",
            "CachePolicyId": CACHING_OPTIMIZED, "Compress": True,
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"], "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}}},
    })["Distribution"]
    print(f"created distribution {dist['Id']}")
    return dist["Id"], dist["DomainName"], dist["ARN"]


def allow_cloudfront(s3, bucket, dist_arn):
    policy = {"Version": "2012-10-17", "Statement": [{
        "Sid": "CloudFrontReadOnly", "Effect": "Allow",
        "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*",
        "Condition": {"StringEquals": {"AWS:SourceArn": dist_arn}}}]}
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))


def ensure_key(location, referers):
    restrictions = {
        "AllowActions": ["geo-maps:GetTile", "geo-maps:GetStaticMap", "geo-places:Geocode", "geo-routes:CalculateRoutes"],
        "AllowResources": [f"arn:aws:geo-maps:{REGION}::provider/default", f"arn:aws:geo-places:{REGION}::provider/default",
                           f"arn:aws:geo-routes:{REGION}::provider/default"],
        "AllowReferers": referers,
    }
    try:
        location.describe_key(KeyName=KEY_NAME)
        location.update_key(KeyName=KEY_NAME, Restrictions=restrictions, NoExpiry=True, ForceUpdate=True)
        print(f"updated key {KEY_NAME}")
    except location.exceptions.ResourceNotFoundException:
        location.create_key(KeyName=KEY_NAME, Restrictions=restrictions, NoExpiry=True,
                            Description="FreshTrack public map key: tiles, geocode, routes; referer-restricted")
        print(f"created key {KEY_NAME}")
    return location.describe_key(KeyName=KEY_NAME)["Key"]


def upload(s3, bucket, api_key):
    for rel in SITE_FILES:
        kind = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        if rel.endswith(".geojson"):
            kind = "application/geo+json"
        content_type = kind + ("; charset=utf-8" if kind.startswith(("text/", "application/javascript")) else "")
        # Pages and config change often; keep them fresh. Other files can cache for an hour.
        cache = "no-cache" if rel in ("index.html", "config.js") else "max-age=3600"
        with open(os.path.join(ROOT, rel), "rb") as handle:
            body = handle.read()
        if rel == "config.js":
            # The key never lives in git; it goes only into the deployed copy.
            text = body.decode("utf-8")
            if 'apiKey: ""' not in text:
                raise SystemExit("config.js in git should have an empty apiKey")
            body = text.replace('apiKey: ""', f'apiKey: "{api_key}"', 1).encode("utf-8")
        s3.put_object(Bucket=bucket, Key=rel, Body=body, ContentType=content_type, CacheControl=cache)
        print(f"uploaded {rel}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", action="store_true", help="only upload files and invalidate")
    args = parser.parse_args()

    session = boto3.Session(region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = f"freshtrack-site-{account}"
    s3, cf = session.client("s3"), session.client("cloudfront")

    ensure_bucket(s3, bucket)
    dist_id, domain, dist_arn = ensure_distribution(cf, bucket)
    location = session.client("location")
    if not args.files:
        allow_cloudfront(s3, bucket, dist_arn)
        ensure_key(location, [
            f"https://{domain}/*",
            "http://localhost:8000/*", "http://localhost:8001/*", "http://127.0.0.1:8000/*"])
    upload(s3, bucket, location.describe_key(KeyName=KEY_NAME)["Key"])
    cf.create_invalidation(DistributionId=dist_id, InvalidationBatch={
        "Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": str(time.time())})
    print(f"\nsite: https://{domain}/")


if __name__ == "__main__":
    main()
