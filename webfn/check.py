"""Live check of the OpenCode Web Function (spends a few free-model requests): health, one answer, and a session that survives
moving between processes through the S3 transcript.

Run: uv run --with boto3 python webfn/check.py            (reads the endpoint domain from the OpenCodeWebFnStack output)
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = "us-east-1"


def domain():
    out = subprocess.run(["aws", "cloudformation", "describe-stacks", "--region", REGION, "--stack-name", "OpenCodeWebFnStack",
                          "--query", "Stacks[0].Outputs[?OutputKey=='Domain'].OutputValue", "--output", "text"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def call(host, method, path, body=None):
    import hashlib

    data = json.dumps(body).encode() if body is not None else b""
    headers = {"x-amz-content-sha256": hashlib.sha256(data).hexdigest()}  # the endpoint answers 403 without this signed header
    if body is not None:
        headers["content-type"] = "application/json"
    req = AWSRequest(method=method, url=f"https://{host}{path}", data=data or None, headers=headers)
    SigV4Auth(boto3.Session().get_credentials().get_frozen_credentials(), "lambda", REGION).add_auth(req)
    r = urllib.request.Request(req.url, data=data or None, headers=dict(req.headers.items()), method=method)
    try:
        with urllib.request.urlopen(r, timeout=130) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw[:200]}


host = domain()
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


status, health = call(host, "GET", "/health")
check("health answers", status == 200 and health.get("arch") == "aarch64", f"python {health.get('python')} on {health.get('arch')}")

key = "check-" + uuid.uuid4().hex[:12]
status, out = call(host, "POST", "/ask", {"session": key, "prompt": "Remember this code word: kumquat-42. Reply with exactly: stored"})
check("first turn answers", status == 200 and "stored" in out.get("text", "").lower(), f"{status} {out.get('error', '')}")
status, out = call(host, "POST", "/ask", {"session": key, "prompt": "What was the code word? Reply with only the word."})
check("second turn remembers", status == 200 and "kumquat-42" in out.get("text", ""), f"{status} state={out.get('session_state')}")
status, out = call(host, "POST", "/ask", {"session": "check-" + uuid.uuid4().hex[:12], "prompt": "What was the code word? Reply with only the word, or NONE."})
check("a new session does not know it", status == 200 and "kumquat" not in out.get("text", "").lower(), out.get("text", "")[:40])
status, out = call(host, "POST", "/ask", {"prompt": "hi", "model": "not-allowed"})
check("a model outside the allowlist is refused", status == 400)
sys.exit(1 if failures else 0)
