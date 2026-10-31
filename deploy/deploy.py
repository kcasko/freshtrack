"""Deploy the FreshTrack chat agent as a public Lambda Function URL.

    python deploy/deploy.py                      # build, create or update everything
    python deploy/deploy.py --daily-limit 200    # change the daily request cap

Creates or updates, all named freshtrack-chat*:
- DynamoDB table freshtrack-chat-usage: one counter per day, nothing else
- IAM role freshtrack-chat-role: logs, the one Bedrock model, Amazon Location
  geocode and routes, and that one table
- Lambda function freshtrack-chat (Python 3.12) with reserved concurrency
- A Function URL with no auth and CORS limited to the allowed origins

Prints the URL to put in config.js as chatEndpoint. Run it again after code
changes; it only updates what exists. To remove everything, delete those four
resources in the console.
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "freshtrack-chat"
ROLE = "freshtrack-chat-role"
TABLE = "freshtrack-chat-usage"
MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
FOUNDATION_MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"
# Windows-only, local-login-only, or already in the Lambda runtime.
SKIP_PACKAGES = {"pywin32", "pywin32-ctypes", "awscrt", "pip", "setuptools", "wheel"}
DEFAULT_ORIGINS = ["https://d7ua6xibk1726.cloudfront.net", "https://kcasko.github.io", "http://localhost:8000", "http://127.0.0.1:8000"]


def build_zip():
    """Linux wheels for Python 3.12, plus the agent package, data, and handler."""
    # Build only from the application's declared dependencies. Freezing the
    # developer's entire environment pulls unrelated tools into Lambda and
    # can fail when those tools have no Linux/Python 3.12 wheels.
    with tempfile.TemporaryDirectory() as build:
        requirements = os.path.join(ROOT, "requirements.txt")
        if os.name == "nt":
            # Resolve environment markers inside Linux. Cross-platform pip on
            # Windows still pulls Windows-only pywin32 from Strands' MCP deps.
            # Requires WSL with Python 3.12 installed.
            def wsl_path(path):
                absolute = os.path.abspath(path)
                drive, rest = os.path.splitdrive(absolute)
                if not drive:
                    raise ValueError("WSL build requires a Windows drive path")
                return "/mnt/" + drive[0].lower() + rest.replace("\\", "/")

            command = ["wsl", "python3", "-m", "pip", "install", "--quiet",
                       "--break-system-packages", "--target", wsl_path(build),
                       "-r", wsl_path(requirements)]
        else:
            command = [sys.executable, "-m", "pip", "install", "--quiet",
                       "--target", build, "-r", requirements]
        subprocess.run(command, check=True)
        shutil.copytree(os.path.join(ROOT, "freshtrack_agent"), os.path.join(build, "freshtrack_agent"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("places.geojson", "schedules.json", "zip_centroids.json"):
            os.makedirs(os.path.join(build, "data"), exist_ok=True)
            shutil.copy(os.path.join(ROOT, "data", name), os.path.join(build, "data", name))
        shutil.copy(os.path.join(ROOT, "lambda", "chat.py"), os.path.join(build, "chat.py"))

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for folder, dirs, files in os.walk(build):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for file in files:
                    path = os.path.join(folder, file)
                    archive.write(path, os.path.relpath(path, build))
        return buffer.getvalue()


def ensure_table(dynamodb):
    try:
        dynamodb.describe_table(TableName=TABLE)
    except dynamodb.exceptions.ResourceNotFoundException:
        dynamodb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "day", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "day", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=TABLE)
        print(f"created table {TABLE}")
    return dynamodb.describe_table(TableName=TABLE)["Table"]["TableArn"]


def ensure_role(iam, account, region, table_arn):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{NAME}*"},
            # The us. inference profile routes to the model in several US regions.
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
             "Resource": [f"arn:aws:bedrock:{region}:{account}:inference-profile/{MODEL}",
                          f"arn:aws:bedrock:*::foundation-model/{FOUNDATION_MODEL}"]},
            {"Effect": "Allow", "Action": ["geo-places:Geocode", "geo-routes:CalculateRoutes"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["dynamodb:UpdateItem"], "Resource": table_arn},
        ],
    }
    created = False
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="FreshTrack chat Lambda")["Role"]["Arn"]
        created = True
        print(f"created role {ROLE}")
    iam.put_role_policy(RoleName=ROLE, PolicyName="freshtrack-chat", PolicyDocument=json.dumps(policy))
    if created:
        time.sleep(12)  # a new role takes a few seconds before Lambda can assume it
    return arn


def ensure_function(lam, role_arn, code, daily_limit):
    env = {"Variables": {"FRESHTRACK_MODEL": MODEL, "DAILY_LIMIT": str(daily_limit), "USAGE_TABLE": TABLE}}
    try:
        lam.get_function(FunctionName=NAME)
        lam.update_function_code(FunctionName=NAME, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
        lam.update_function_configuration(FunctionName=NAME, Role=role_arn, Timeout=60, MemorySize=1024, Environment=env)
        print(f"updated function {NAME}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(5):
            try:
                lam.create_function(
                    FunctionName=NAME, Runtime="python3.12", Handler="chat.handler", Role=role_arn,
                    Code={"ZipFile": code}, Timeout=60, MemorySize=1024, Architectures=["x86_64"],
                    Environment=env, Description="FreshTrack Strands agent chat",
                )
                break
            except lam.exceptions.InvalidParameterValueException:
                if attempt == 4:
                    raise
                time.sleep(8)  # role not assumable yet
        print(f"created function {NAME}")
    lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)


def ensure_concurrency(lam, limit):
    try:
        lam.put_function_concurrency(FunctionName=NAME, ReservedConcurrentExecutions=limit)
        print(f"reserved concurrency set to {limit}")
    except ClientError as err:
        # Accounts with a low concurrency quota can't reserve any; the daily cap still applies.
        print(f"could not reserve concurrency ({err.response['Error']['Code']}); relying on the daily cap")


def ensure_url(lam, origins):
    cors = {"AllowOrigins": origins, "AllowMethods": ["POST"], "AllowHeaders": ["content-type"], "MaxAge": 3600}
    try:
        url = lam.update_function_url_config(FunctionName=NAME, AuthType="NONE", Cors=cors)["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=NAME, AuthType="NONE", Cors=cors)["FunctionUrl"]
    # A public Function URL needs both permissions.
    for sid, kwargs in (
        ("public-url", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
        ("public-url-invoke", {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True}),
    ):
        try:
            lam.add_permission(FunctionName=NAME, StatementId=sid, Principal="*", **kwargs)
        except lam.exceptions.ResourceConflictException:
            pass  # already there
    return url


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--daily-limit", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--origin", action="append", help="allowed browser origin; repeat for more")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    print(f"account {account}, region {args.region}")

    print("building package...")
    code = build_zip()
    print(f"package is {len(code) / 1e6:.1f} MB")

    table_arn = ensure_table(session.client("dynamodb"))
    role_arn = ensure_role(session.client("iam"), account, args.region, table_arn)
    lam = session.client("lambda")
    ensure_function(lam, role_arn, code, args.daily_limit)
    ensure_concurrency(lam, args.concurrency)
    url = ensure_url(lam, args.origin or DEFAULT_ORIGINS)
    print(f"\nchatEndpoint: {url}")


if __name__ == "__main__":
    main()
