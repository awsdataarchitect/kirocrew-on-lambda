"""Live check (spends one free-model request): does hooks.sign_and_post produce a request the Web Function accepts?

Run: WEBFN_DOMAIN=<endpoint domain> uv run --no-project --with boto3 python crew/test/live_sign_check.py
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "image" / "guest"))
import hooks  # noqa: E402

status, body = hooks.sign_and_post(os.environ["WEBFN_DOMAIN"], "us-east-1", json.dumps({"prompt": "Reply with exactly: ok"}).encode())
print(status, body[:300])
sys.exit(0 if status == 200 else 1)
