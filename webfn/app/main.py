"""Spike: OpenCode (`opencode serve`) behind a Lambda Web Function, Python ASGI, stdlib only.

Question under test: does the Linux arm64 OpenCode binary start inside a Web Function, answer one prompt on a free Zen model,
and how long do cold start and a turn take. Nothing here is production code: no auth beyond the endpoint's IAM auth.
"""
import asyncio, http.client, json, os, platform, shutil, subprocess, sys, time

T0 = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 4096
MODEL = os.environ.get("OC_MODEL", "deepseek-v4.1-flash-free")
ALLOWED_MODELS = ("deepseek-v4.1-flash-free", "hy4-flash-free")  # provider-stated zero retention, no training (OpenCode docs, 2026-11-23)
HOME = "/tmp/oc"
LOG = "/tmp/oc-serve.log"
_state = {"proc": None, "ready_at": None, "started_at": None, "binary": None, "error": None}


def _binary():
    src = os.path.join(HERE, "bin", "opencode")
    if os.access(src, os.X_OK):
        return src
    dst = "/tmp/opencode"  # package files can lose the exec bit; /tmp is 512 MB, the binary ~211 MB
    if not os.path.exists(dst):
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o755)
    return dst


_lock_fd = None


def _start():
    global _lock_fd
    try:
        # The managed server may import this module in more than one worker process. Only the process that wins this lock starts
        # OpenCode; the others reach the same server on 127.0.0.1:PORT. Two OpenCodes on one HOME fail with "database is locked".
        import fcntl
        os.makedirs(HOME, exist_ok=True)
        _lock_fd = open(f"{HOME}/start.lock", "w")
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _state["started_at"] = time.time()
            _state["role"] = "follower"
            return
        _state["role"] = "leader"
        for d in ("cfg", "data", "cache", "home"):
            os.makedirs(f"{HOME}/{d}", exist_ok=True)
        cfg = f"{HOME}/cfg/opencode.json"
        with open(cfg, "w") as f:
            json.dump({"$schema": "https://opencode.ai/config.json", "model": f"opencode/{MODEL}", "small_model": f"opencode/{MODEL}",
                       "enabled_providers": ["opencode"], "autoupdate": False, "share": "disabled", "lsp": False}, f)
        env = {"PATH": "/usr/bin:/bin", "HOME": f"{HOME}/home", "XDG_CONFIG_HOME": f"{HOME}/cfg", "XDG_DATA_HOME": f"{HOME}/data",
               "XDG_CACHE_HOME": f"{HOME}/cache", "OPENCODE_CONFIG": cfg}
        _state["binary"] = _binary()
        _state["started_at"] = time.time()
        _state["proc"] = subprocess.Popen([_state["binary"], "serve", "--print-logs", "--hostname", "127.0.0.1", "--port", str(PORT)],
                                          env=env, cwd=HOME, stdout=open(LOG, "ab"), stderr=subprocess.STDOUT)
    except Exception as e:  # recorded, shown by /health
        _state["error"] = f"{type(e).__name__}: {e}"


def _get(path, timeout=3):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def _post(path, body, timeout=120):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        c.request("POST", path, body=json.dumps(body), headers={"content-type": "application/json"})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def _delete(path, timeout=5):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        c.request("DELETE", path)
        r = c.getresponse()
        return r.status, r.read()
    except Exception:
        return 0, b""
    finally:
        c.close()


def _ready():
    if _state["ready_at"]:
        return True
    try:
        st, _ = _get("/global/health")
        if st == 200:
            _state["ready_at"] = time.time()
            return True
    except Exception:
        pass
    return False


ASK_TIMEOUT = int(os.environ.get("OC_ASK_TIMEOUT", "40"))
DEBUG = os.environ.get("OC_DEBUG") == "1"  # exposes /log and tracebacks; off by default
MAX_PROMPT_CHARS = 200_000  # a KiroCrew turn carries its whole agent prompt before the user message; a 2,000 cap cut the message off (2026-11-23)
STATE_BUCKET = os.environ.get("STATE_BUCKET", "")
_s3 = None
_sessions = {}  # state key -> OpenCode session id, valid only in this environment
_transcripts = {}  # state key -> turns, mirrored to S3
_key_locks = {}


def _s3c():
    global _s3
    if _s3 is None:
        import boto3  # bundled in the package; credentials come from the execution role
        _s3 = boto3.client("s3", region_name="us-east-1")
    return _s3


def _oc_env():
    return {"PATH": "/usr/bin:/bin", "HOME": f"{HOME}/home", "XDG_CONFIG_HOME": f"{HOME}/cfg", "XDG_DATA_HOME": f"{HOME}/data",
            "XDG_CACHE_HOME": f"{HOME}/cache", "OPENCODE_CONFIG": f"{HOME}/cfg/opencode.json"}


def _load_transcript(key):
    """The turns stored in S3 for this session key, or None."""
    from botocore.exceptions import ClientError
    try:
        body = _s3c().get_object(Bucket=STATE_BUCKET, Key=f"sessions/{key}.json")["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(body)["turns"]


def _save_transcript(key, turns):
    _s3c().put_object(Bucket=STATE_BUCKET, Key=f"sessions/{key}.json", Body=json.dumps({"turns": turns}).encode(), ContentType="application/json")


def _restore(key, model):
    """Rebuild a session in this environment from S3: a fresh OpenCode session primed with the earlier turns (no model call)."""
    turns = _load_transcript(key)
    if not turns:
        return None
    st, b = _post("/session", {}, timeout=10)
    if st != 200:
        raise RuntimeError(f"session {st}")
    sid = json.loads(b)["id"]
    history = "\n\n".join(f"User: {t['user']}\nAssistant: {t['assistant']}" for t in turns)
    note = "Earlier turns of this conversation, restored from storage. Treat them as the conversation so far; do not reply to this note.\n\n" + history
    st, b = _post(f"/session/{sid}/message", {"noReply": True, "model": {"providerID": "opencode", "modelID": model}, "parts": [{"type": "text", "text": note}]}, timeout=15)
    if st != 200:
        raise RuntimeError(f"restore message {st}: {b[:200]!r}")
    _transcripts[key] = turns
    return sid


def _ask(prompt, key=None, model=None):
    model = model or MODEL
    t = time.time()
    where = "new"
    if key:
        sid = _sessions.get(key)
        if sid:
            where = "warm"
        else:
            sid = _restore(key, model)
            where = "restored" if sid else "new"
            if sid:
                _sessions[key] = sid
    else:
        sid = None
    if not sid:
        st, b = _post("/session", {}, timeout=10)
        if st != 200:
            return {"error": f"session {st}", "body": b[:300].decode("utf-8", "replace")}
        sid = json.loads(b)["id"]
        if key:
            _sessions[key] = sid
    try:
        st, b = _post(f"/session/{sid}/message", {"model": {"providerID": "opencode", "modelID": model}, "parts": [{"type": "text", "text": prompt}]}, timeout=ASK_TIMEOUT)
        d = json.loads(b)
        if "parts" not in d:
            return {"error": f"message {st}", "body": json.dumps(d)[:300]}
        text = "".join(p.get("text", "") for p in d["parts"] if p.get("type") == "text")
        if key:
            turns = _transcripts.setdefault(key, [])
            turns.append({"user": prompt, "assistant": text})
            _save_transcript(key, turns)
        return {"text": "".join(p.get("text", "") for p in d["parts"] if p.get("type") == "text"), "model": d["info"].get("modelID"), "turn_seconds": round(time.time() - t, 2),
                "session_state": where, "environment_pid": os.getpid(), "env_id": _state.get("env_id")}
    except (TimeoutError, OSError):
        # OpenCode retries a refused call instead of failing. Name the free-tier cap, the one cause we have seen, rather than hanging.
        try:
            _post(f"/session/{sid}/abort", {}, timeout=5)
        except Exception:
            pass
        cap = "Free usage limit reached" in _tail(6000)
        return {"error": "free_usage_limit_reached" if cap else "model_timeout", "waited_seconds": round(time.time() - t, 1)}
    finally:
        if not key:
            _delete(f"/session/{sid}")  # one-shot calls: keep /tmp (512 MB, not adjustable) from filling with sessions


def _tail(n=1800):
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _poll():  # records when OpenCode first answers, independent of any request
    import threading
    def run():
        t = time.time()
        while time.time() - t < 120 and not _ready():
            time.sleep(0.1)
    threading.Thread(target=run, daemon=True).start()


def _env_id():
    path = f"{HOME}/env-id"
    try:
        os.makedirs(HOME, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        import uuid
        os.write(fd, uuid.uuid4().hex[:8].encode())
        os.close(fd)
    except FileExistsError:
        pass
    for _ in range(50):
        try:
            v = open(path).read().strip()
            if v:
                return v
        except OSError:
            pass
        time.sleep(0.02)
    return "unknown"


_start()
_state["env_id"] = _env_id()
_poll()  # non-blocking: Lambda gives the server 10 s to listen, OpenCode is awaited lazily
_state["imported_in"] = round(time.time() - T0, 3)


async def _send(send, status, obj):
    body = json.dumps(obj).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            m = await receive()
            if m["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif m["type"] == "lifespan.shutdown":
                p = _state["proc"]
                if p and p.poll() is None:
                    p.terminate()
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    path, method = scope["path"], scope["method"]
    if path == "/health":
        ready = await asyncio.to_thread(_ready)
        p = _state["proc"]
        du = shutil.disk_usage("/tmp")
        return await _send(send, 200, {
            "opencode_ready": ready, "opencode_exited": (p.poll() if p else None), "error": _state["error"],
            "seconds_since_import": round(time.time() - T0, 2),
            "seconds_to_ready": round(_state["ready_at"] - _state["started_at"], 2) if ready and _state["started_at"] else None,
            "imported_in": _state["imported_in"], "role": _state.get("role"), "pid": os.getpid(), "binary": _state["binary"], "arch": platform.machine(), "python": sys.version.split()[0],
            "tmp_free_mb": du.free // 2**20, "model": MODEL, "log_tail": _tail() if (DEBUG and not ready) else None})
    if path == "/ask" and method == "POST":
        chunks = b""
        while True:
            m = await receive()
            chunks += m.get("body", b"")
            if not m.get("more_body"):
                break
        prompt = json.loads(chunks or b"{}").get("prompt", "Reply with exactly: spike-ok")[:MAX_PROMPT_CHARS]
        t = time.time()
        while not await asyncio.to_thread(_ready):
            if time.time() - t > 60:
                return await _send(send, 503, {"error": "opencode not ready after 60 s", "log_tail": _tail() if DEBUG else None})
            await asyncio.sleep(0.5)
        waited = round(time.time() - t, 2)
        key = json.loads(chunks or b"{}").get("session")
        model = json.loads(chunks or b"{}").get("model")
        if model is not None and model not in ALLOWED_MODELS:
            return await _send(send, 400, {"error": "model must be one of " + ", ".join(ALLOWED_MODELS)})
        if key is not None and not (isinstance(key, str) and key.replace("-", "").replace("_", "").isalnum() and len(key) <= 64):
            return await _send(send, 400, {"error": "session must be 1-64 chars of letters, digits, - or _"})
        lock = _key_locks.setdefault(key, asyncio.Lock()) if key else None
        try:
            if lock:
                async with lock:
                    out = await asyncio.to_thread(_ask, prompt, key, model)
            else:
                out = await asyncio.to_thread(_ask, prompt, None, model)
        except Exception as e:  # surface the cause to the caller; the spike has no secrets in messages
            import traceback
            print(traceback.format_exc(), flush=True)  # the full trace goes to CloudWatch Logs, not to the caller
            return await _send(send, 500, {"error": type(e).__name__})
        out["waited_for_ready_seconds"] = waited
        err = out.get("error")
        return await _send(send, 429 if err == "free_usage_limit_reached" else 504 if err == "model_timeout" else 200, out)
    if path == "/log" and DEBUG:
        return await _send(send, 200, {"log_tail": _tail(4000)})
    await _send(send, 404, {"error": "not found"})
