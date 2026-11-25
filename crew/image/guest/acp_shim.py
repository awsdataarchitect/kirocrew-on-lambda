"""ACP agent over stdio that answers each prompt through the OpenCode Web Function (chat only, no tools).

KiroCrew starts this in place of `opencode acp` (OPENCODE_BIN). It speaks Agent Client Protocol on stdin/stdout and sends
each prompt to the loopback relay in hooks.py, which signs it with the VM's execution role. Session continuity lives in
the Web Function's S3 transcript, keyed by the ACP session id, so any environment can serve any turn and a new VM that
KiroCrew reconnects to the same ACP session id (session/load) continues the same conversation.
Stdlib only: the harness child runs with a scrubbed environment and holds no credentials.
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

RELAY = os.environ.get("WEBFN_RELAY", "http://127.0.0.1:8099")
MODEL = os.environ.get("WEBFN_MODEL", "deepseek-v4.1-flash-free")  # the Web Function accepts only its zero-retention allowlist
LOG = os.environ.get("SHIM_LOG")


def log(direction, obj):
    if LOG:
        with open(LOG, "a") as handle:
            handle.write(json.dumps({"d": direction, "frame": obj}) + "\n")


def send(obj):
    log("out", obj)
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def state_key(session_id):
    """1-64 chars of letters, digits, - and _ (the Web Function's grammar); stable for one ACP session id."""
    return "k" + hashlib.sha256(session_id.encode()).hexdigest()[:40]


def prompt_text(blocks):
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def ask(session_id, text):
    body = json.dumps({"session": state_key(session_id), "model": MODEL, "prompt": text}).encode()
    request = urllib.request.Request(RELAY + "/ask", data=body, headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=130) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw[:200]}
    except OSError as exc:  # relay not up, or the call timed out
        return 0, {"error": f"{type(exc).__name__}: {exc}"[:200]}


def reply_text(status, out):
    if status == 200 and "text" in out:
        return out["text"]
    if status == 429:
        return "[remote OpenCode: the free usage limit was reached; try again later]"
    return f"[remote OpenCode error {status}: {str(out.get('error', out))[:200]}]"


def handle(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        return send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False}},
            "authMethods": [],
            "agentInfo": {"name": "opencode-webfn-shim", "version": "0.1.0"}}})
    if method == "session/new":
        return send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "ses_" + uuid.uuid4().hex[:24]}})
    if method == "session/load":
        return send({"jsonrpc": "2.0", "id": mid, "result": {}})  # the transcript is in S3 under this session id; nothing to replay
    if method == "session/prompt":
        sid = params["sessionId"]
        status, out = ask(sid, prompt_text(params.get("prompt", [])))
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply_text(status, out)}}}})
        return send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    if mid is not None:  # an unimplemented request gets an answer rather than a hang
        return send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            log("in-bad", line)
            continue
        log("in", msg)
        try:
            handle(msg)
        except Exception as exc:
            if msg.get("id") is not None:
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": f"{type(exc).__name__}: {str(exc)[:200]}"}})


if __name__ == "__main__":
    main()
