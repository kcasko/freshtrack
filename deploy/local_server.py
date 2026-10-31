"""Serve the map and the chat agent on one local port, for development and demos.

    python deploy/local_server.py          # then open http://localhost:8000

Static files come from the repo root. POST /chat runs the same handler as the
deployed Lambda (lambda/chat.py), using your local AWS credentials. The
daily-limit counter is skipped locally. config.js is served with the Amazon
Location API key filled in, read from your account, so the key never needs to
be written into the file.
"""

import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lambda"))
os.environ["USAGE_TABLE"] = ""

import chat  # noqa: E402

# The agent's demo clock is process-wide, so chat requests run one at a time.
CHAT_LOCK = threading.Lock()


_map_key = None


def map_key():
    """The freshtrack-web key from Amazon Location, or "" if it can't be read."""
    global _map_key
    if _map_key is None:
        try:
            import boto3

            _map_key = boto3.client("location", region_name="us-east-1").describe_key(KeyName="freshtrack-web")["Key"]
        except Exception:
            _map_key = ""
    return _map_key


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        if self.path.split("?")[0] != "/config.js":
            return super().do_GET()
        text = open(os.path.join(ROOT, "config.js"), encoding="utf-8").read()
        payload = text.replace('apiKey: ""', f'apiKey: "{map_key()}"', 1).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != "/chat":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        with CHAT_LOCK:
            result = chat.handler({"body": body, "requestContext": {"http": {"method": "POST"}}})
        payload = result["body"].encode("utf-8")
        self.send_response(result["statusCode"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"FreshTrack on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
