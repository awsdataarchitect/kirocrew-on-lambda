"""Guest side of the KiroCrew MicroVM: lifecycle hooks, crew boot, and the dashboard forwarder.

Ports
  8080  Lambda lifecycle hooks (/ready at image build, /run once per MicroVM) plus the
        launcher's own routes: GET /crew/status, POST /crew/token, POST /crew/chat and
        GET /crew/chat/<id>.
  8081  HTTP and WebSocket reverse proxy to the KiroCrew gateway, which stays bound to
        loopback. The gateway validates Host and Origin, so the proxy presents the request
        the way an `ssh -L` tunnel on the same port number would: Host is the gateway's own
        address and the forwarding headers added on the way in are dropped.

Nothing secret exists before /run: the image snapshot holds only installed software.
At /run the NIM key is read from SSM Parameter Store through the execution role, stored
in KiroCrew's own vault, and the gateway is started. Two harnesses are installed: Strands (on the
LiteLLM provider, to NVIDIA NIM) and OpenCode (on its free Zen models, no key). The run payload's
`harness` picks the default; without it a choice saved from the dashboard survives restarts.
Reaching either port needs a token minted by lambda:CreateMicrovmAuthToken.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from statesync import StateSync

HOOK_PORT = 8080
FORWARD_PORT = 8081
# The laptop launcher listens on this same port number, so the browser's Origin
# (http://127.0.0.1:5490) is one the gateway already trusts.
GATEWAY_PORT = 5490
GATEWAY_HOST = f"127.0.0.1:{GATEWAY_PORT}"
MAX_CHAT_MESSAGE = 8 * 1024
CHAT_TIMEOUT_SECS = 300
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"
MAX_BODY = 16 * 1024  # runHookPayload is capped at 16 KB by the service
READY_MARK = "KIROCREW_READY:"
RESTART_LIMIT = 5
RESTART_WINDOW = 600

# Durable state: the crew home is mirrored to S3 over the internet (statesync.py). Local disk stays the working copy.
STATE_PREFIX = "crew/home/"
PUSH_EVERY_SECS = 60
HOOK_BUDGET_SECS = 55

# Harnesses baked into the image. KiroCrew's `agent.acp_backend` holds the one new sessions use.
HARNESSES = ("strands", "opencode")
DEFAULT_HARNESS = "strands"
# OpenCode Zen free model. Chosen because the Zen docs list it as zero-retention and not used for training; several other
# free models (Big Pickle, Iceman, Flint, MiMo...) may train on prompts. Prompts reach this model, and so does the first
# message of every session (OpenCode titles a session with `small_model`), which is why both are set to it.
DEFAULT_OPENCODE_MODEL = "opencode/deepseek-v4.1-flash-free"
_OPENCODE_MODEL_RE = re.compile(r"^opencode/[a-z0-9][a-z0-9.\-]{0,63}$")

_PARAM_RE = re.compile(r"^/[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*$")
_FILE_RE = re.compile(r"^/[A-Za-z0-9_.\-/]+$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]*(/[a-z0-9][a-z0-9._\-]*)+$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_URL_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._\-/]*)?$")
_WEBFN_RE = re.compile(r"^[a-z0-9]{8,32}\.kfn\.lambda-web\.[a-z]{2}(-[a-z]+)+-\d\.on\.aws$")
_SSM_ID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_SSM_CODE_RE = re.compile(r"^[A-Za-z0-9+/=]{10,64}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


@dataclass(frozen=True)
class RunConfig:
    model: str
    base_url: str
    region: str
    key_parameter: str  # SSM parameter name; "" when key_file is used
    key_file: str  # local-development source: a file inside the guest
    state_bucket: str = ""  # S3 bucket mirroring the crew home; "" runs without durable state (local development only)
    harness: str = ""  # "strands" | "opencode" to force the default harness at boot; "" keeps the one saved in the crew home
    opencode_model: str = DEFAULT_OPENCODE_MODEL  # OpenCode Zen model id, "opencode/<id>"
    web_function_domain: str = ""  # Lambda Web Function endpoint domain that runs OpenCode remotely; "" keeps OpenCode local
    ssm_activation_id: str = ""  # SSM hybrid activation; with the code, registers this VM as an mi- node so the laptop hub can reach it
    ssm_activation_code: str = ""  # single-use, short-lived; never logged, never stored


class PayloadError(ValueError):
    pass


def parse_run_payload(body: bytes) -> RunConfig:
    """The /run request body -> a validated RunConfig, or PayloadError.

    The service delivers the caller's runHookPayload in the body. It is accepted either as
    the body itself or under a "runHookPayload" key (object or JSON string), because the
    envelope also carries service fields such as microvmId. Every value that later reaches
    a command line or an AWS call is matched against a strict grammar here.
    """
    if len(body) > MAX_BODY:
        raise PayloadError("payload too large")
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PayloadError("payload is not JSON") from None
    if isinstance(doc, dict) and "runHookPayload" in doc:
        doc = doc["runHookPayload"]
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except ValueError:
                raise PayloadError("runHookPayload is not JSON") from None
    if not isinstance(doc, dict):
        raise PayloadError("payload is not an object")

    def text(name: str, default: str = "") -> str:
        value = doc.get(name, default)
        if not isinstance(value, str):
            raise PayloadError(f"{name} must be a string")
        return value

    model = text("model")
    base_url = text("baseUrl")
    region = text("region")
    key_parameter = text("nimKeyParameter")
    key_file = text("nimKeyFile")
    if not _MODEL_RE.fullmatch(model) or len(model) > 128:
        raise PayloadError("model is not a provider/model id")
    if not _URL_RE.fullmatch(base_url) or len(base_url) > 256:
        raise PayloadError("baseUrl is not an https URL")
    if bool(key_parameter) == bool(key_file):
        raise PayloadError("exactly one of nimKeyParameter and nimKeyFile is required")
    if key_parameter and (not _PARAM_RE.fullmatch(key_parameter) or len(key_parameter) > 512):
        raise PayloadError("nimKeyParameter is not an SSM parameter name")
    if key_parameter and not _REGION_RE.fullmatch(region):
        raise PayloadError("region is not an AWS region")
    if key_file and (not _FILE_RE.fullmatch(key_file) or ".." in key_file):
        raise PayloadError("nimKeyFile is not an absolute path")
    state_bucket = text("stateBucket")
    if state_bucket:
        if not _BUCKET_RE.fullmatch(state_bucket) or ".." in state_bucket:
            raise PayloadError("stateBucket is not an S3 bucket name")
        if not _REGION_RE.fullmatch(region):
            raise PayloadError("region is not an AWS region")
    harness = text("harness")
    if harness and harness not in HARNESSES:
        raise PayloadError(f"harness must be one of {', '.join(HARNESSES)}")
    opencode_model = text("opencodeModel", DEFAULT_OPENCODE_MODEL)
    if not _OPENCODE_MODEL_RE.fullmatch(opencode_model):
        raise PayloadError("opencodeModel is not an opencode/<model> id")
    web_function_domain = text("webFunctionDomain")
    if web_function_domain and not _WEBFN_RE.fullmatch(web_function_domain):
        raise PayloadError("webFunctionDomain is not a Lambda Web Function endpoint domain")
    if web_function_domain and not _REGION_RE.fullmatch(region):
        raise PayloadError("region is not an AWS region")
    ssm_id = text("ssmActivationId")
    ssm_code = text("ssmActivationCode")
    if bool(ssm_id) != bool(ssm_code):
        raise PayloadError("ssmActivationId and ssmActivationCode go together")
    if ssm_id and not (_SSM_ID_RE.fullmatch(ssm_id) and _SSM_CODE_RE.fullmatch(ssm_code)):
        raise PayloadError("ssm activation id or code is malformed")
    if ssm_id and not _REGION_RE.fullmatch(region):
        raise PayloadError("region is not an AWS region")
    return RunConfig(model, base_url, region, key_parameter, key_file, state_bucket, harness, opencode_model, web_function_domain,
                     ssm_id, ssm_code)


def redact_ready_line(line: str) -> str:
    """Gateway stdout for the log: the ready line carries a dashboard token, so only its mark is kept."""
    return READY_MARK + " <redacted>" if READY_MARK in line else line


# Hop-by-hop headers (RFC 9110 7.6.1) and the forwarding headers a proxy adds.
_HOP_HEADERS = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
     "transfer-encoding", "upgrade", "proxy-connection"}
)
_FORWARD_HEADERS = frozenset({"forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                              "x-forwarded-port", "x-real-ip"})


def gateway_request_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The headers the gateway receives for a proxied request.

    Host is always the gateway's own loopback address. Hop-by-hop headers, forwarding
    headers, the x-aws-proxy-* namespace, Content-Length (the client library sets it) and
    the WebSocket handshake headers (the proxy performs its own handshake) are dropped;
    everything else, Origin and Cookie included, passes through unchanged.
    """
    out: list[tuple[str, str]] = [("Host", GATEWAY_HOST)]
    for name, value in headers:
        low = name.lower()
        if (
            low == "host"
            or low == "content-length"
            or low in _HOP_HEADERS
            or low in _FORWARD_HEADERS
            or low.startswith("x-aws-proxy-")
            or low.startswith("sec-websocket-")
        ):
            continue
        out.append((name, value))
    return out


def client_response_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The gateway's response headers minus the ones the proxy's own framing replaces."""
    return [(n, v) for n, v in headers if n.lower() not in _HOP_HEADERS and n.lower() != "content-length"]


def parse_chat_request(body: bytes) -> str:
    """The POST /crew/chat body -> the message, or PayloadError."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PayloadError("body is not JSON") from None
    message = doc.get("message") if isinstance(doc, dict) else None
    if not isinstance(message, str) or not message.strip():
        raise PayloadError("message must be a non-empty string")
    if len(message.encode("utf-8")) > MAX_CHAT_MESSAGE:
        raise PayloadError("message too large")
    if "\x00" in message:
        raise PayloadError("message contains a NUL byte")
    return message


class Chat:
    """One `kirocrew chat -m` turn at a time, polled by id. Tool calls are denied: the
    CLI's permission prompt reads stdin, which is closed here."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, object]] = {}
        self._running = False

    def start(self, message: str) -> str | None:
        with self._lock:
            if self._running:
                return None
            self._running = True
            job_id = secrets.token_hex(8)
            self._jobs = {job_id: {"done": False}}  # only the latest turn is kept
        threading.Thread(target=self._run, args=(job_id, message), daemon=True).start()
        return job_id

    def _run(self, job_id: str, message: str) -> None:
        try:
            proc = popen_as_crew(["kirocrew", "chat", "-m", message],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                out, err = proc.communicate(timeout=CHAT_TIMEOUT_SECS)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise
            result: dict[str, object] = {"done": True, "exitCode": proc.returncode,
                                         "output": out[-20000:], "error": err[-4000:]}
        except subprocess.TimeoutExpired:
            result = {"done": True, "exitCode": -1, "output": "", "error": "timed out"}
        except Exception as exc:
            result = {"done": True, "exitCode": -1, "output": "", "error": f"{type(exc).__name__}: {exc}"[:500]}
        with self._lock:
            self._jobs[job_id] = result
            self._running = False

    def get(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            return self._jobs.get(job_id)


class Crew:
    """Boot state. `phase` is image -> booting -> ready | failed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "image"
        self.detail = ""
        self.sandbox = ""
        self.gateway: subprocess.Popen[str] | None = None
        self.relay: ThreadingHTTPServer | None = None
        self.version = ""  # the installed KiroCrew release, read at boot
        self.config: RunConfig | None = None
        self.state: StateSync | None = None
        self._push_lock = threading.Lock()
        # Cleared by /suspend and /terminate so the supervisor does not restart the gateway while state is flushed.
        self.may_run = threading.Event()
        self.may_run.set()

    def status(self) -> dict[str, object]:
        with self._lock:
            return {"phase": self.phase, "detail": self.detail, "sandbox": self.sandbox, "kirocrew": self.version,
                    "state": dict(self.state.last) if self.state else {}, "stateBucket": bool(self.state)}

    def _set(self, phase: str, detail: str = "") -> None:
        with self._lock:
            self.phase, self.detail = phase, detail
        print(f"crew: {phase} {detail}".rstrip(), flush=True)

    def start(self, config: RunConfig) -> bool:
        """Begin booting once; a repeated /run is answered without a second boot."""
        with self._lock:
            if self.phase != "image":
                return False
            self.phase = "booting"
            self.config = config
        threading.Thread(target=self._boot, args=(config,), daemon=True).start()
        return True

    def pause(self) -> None:
        """/suspend and /terminate: stop the gateway so nothing is writing, then push the crew home to S3."""
        self.may_run.clear()
        stop_process(self.gateway)
        if self.state is not None and self.state.restored:
            with self._push_lock:
                self.state.push()
            print(f"crew: state {json.dumps(self.state.last)}", flush=True)

    def resume(self) -> None:
        """/resume: local disk survived the snapshot, so just let the supervisor start the gateway again."""
        self.may_run.set()

    def _push_forever(self) -> None:
        while True:
            time.sleep(PUSH_EVERY_SECS)
            if not self.may_run.is_set() or self.state is None or not self.state.restored:
                continue
            try:
                with self._push_lock:
                    self.state.push()
            except Exception as exc:  # a failed push is retried next minute; the gateway keeps running
                print(f"crew: state push failed: {type(exc).__name__}: {exc}"[:300], flush=True)

    def _boot(self, config: RunConfig) -> None:
        try:
            if config.state_bucket:
                self._set("booting", "restoring state")
                import boto3

                self.state = StateSync(boto3.client("s3", region_name=config.region), config.state_bucket, STATE_PREFIX,
                                       crew_env()["KIROCREW_HOME"])
                self.state.restore()
                threading.Thread(target=self._push_forever, daemon=True).start()
            self.version = (run(["kirocrew", "--version"]).strip().splitlines() or [""])[-1][:60]
            self._set("booting", "reading key")
            key = read_key(config)
            self._set("booting", "configuring")
            configure(config, key)
            del key
            if config.web_function_domain:
                # KiroCrew resolves its OpenCode binary from OPENCODE_BIN in the gateway's own environment.
                self.relay = start_relay(config.web_function_domain, config.region)
                os.environ["OPENCODE_BIN"] = SHIM_PATH
            sandbox = run_python("from kiro_crew import sandbox; print(sandbox.detect_backend())")
            with self._lock:
                self.sandbox = sandbox.strip().splitlines()[-1] if sandbox.strip() else "unknown"
            if config.ssm_activation_id:
                self._set("booting", "registering with Systems Manager")
                start_ssm(config)
            self._set("booting", "starting gateway")
            self._start_gateway()
        except Exception as exc:  # the launcher reads this through /crew/status
            self._set("failed", f"{type(exc).__name__}: {exc}"[:500])

    def _start_gateway(self) -> None:
        """Run the gateway and restart it when it exits.

        A MicroVM resume restores memory from a snapshot and the gateway's own stall watchdog can end the
        process with SIGALRM right after (seen once: exit code -14 straight after a suspend and resume).
        The crew's home is on disk and survives, so the right answer is to start the gateway again, not to
        report the crew as dead. At most RESTART_LIMIT restarts per RESTART_WINDOW seconds, then it stays failed.
        """
        starts: list[float] = []
        while True:
            self.may_run.wait()  # held clear by /suspend and /terminate while state is flushed
            now = time.monotonic()
            starts = [t for t in starts if now - t < RESTART_WINDOW]
            if len(starts) > RESTART_LIMIT:
                self._set("failed", f"gateway restarted {RESTART_LIMIT} times in {RESTART_WINDOW}s; giving up")
                return
            starts.append(now)
            code = self._run_gateway_once()
            self._set("booting", f"gateway exited with code {code}; restarting")

    def _run_gateway_once(self) -> int:
        proc = popen_as_crew(
            ["kirocrew", "gateway", "--no-tunnel", "--no-open", "--port", str(GATEWAY_PORT), "--json-ready"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.gateway = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            if READY_MARK in line and self.status()["phase"] == "booting":
                self._set("ready")
            print(redact_ready_line(line.rstrip("\n")), flush=True)
        return proc.wait()


RELAY_PORT = 8099
RELAY_MAX_BODY = 64 * 1024
RELAY_TIMEOUT_SECS = 120
SHIM_PATH = "/opt/guest/opencode-remote"


def signed_headers(domain: str, region: str, body: bytes, credentials) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """The headers of a SigV4-signed POST /ask (service `lambda`). Pure: no network, credentials passed in."""
    import hashlib

    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    # The endpoint verifies the payload hash header: without a signed x-amz-content-sha256 it answers 403 (measured 2026-11-23).
    request = AWSRequest(method="POST", url=f"https://{domain}/ask", data=body,
                         headers={"content-type": "application/json", "x-amz-content-sha256": hashlib.sha256(body).hexdigest()})
    SigV4Auth(credentials, "lambda", region).add_auth(request)
    return dict(request.headers.items())


def sign_and_post(domain: str, region: str, body: bytes) -> tuple[int, bytes]:
    """POST /ask to the Web Function endpoint, signed with the VM's execution role."""
    import urllib.error
    import urllib.request

    import boto3

    headers = signed_headers(domain, region, body, boto3.Session().get_credentials().get_frozen_credentials())
    outgoing = urllib.request.Request(f"https://{domain}/ask", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(outgoing, timeout=RELAY_TIMEOUT_SECS) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def make_relay_handler(domain: str, region: str, poster=sign_and_post):  # type: ignore[no-untyped-def]
    """The loopback relay: one route, one fixed destination, so it is not an open proxy for whatever else runs in the VM."""

    class RelayHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            if self.path != "/ask":
                self.send_error(404)
                return
            length = int(self.headers.get("content-length") or 0)
            if length <= 0 or length > RELAY_MAX_BODY:
                self.send_error(413 if length > RELAY_MAX_BODY else 400)
                return
            try:
                status, payload = poster(domain, region, self.rfile.read(length))
            except Exception as exc:  # report the cause to the shim instead of dropping the connection
                status, payload = 502, json.dumps({"error": f"relay: {type(exc).__name__}: {str(exc)[:200]}"}).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return RelayHandler


def start_relay(domain: str, region: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", RELAY_PORT), make_relay_handler(domain, region))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


SSM_SOCKET = "/run/kirocrew/ssm.sock"


def start_ssm(config: RunConfig, socket_path: str = SSM_SOCKET) -> None:
    """Ask the root-side helper to register this VM as an SSM hybrid node. The code goes over the socket, never on a command line."""
    import socket

    request = json.dumps({"region": config.region, "id": config.ssm_activation_id, "code": config.ssm_activation_code}) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(75)
        # The helper starts beside the hook server and may not be listening yet: wait for it instead of failing the boot.
        for attempt in range(50):
            try:
                conn.connect(socket_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if attempt == 49:
                    raise
                time.sleep(0.2)
        conn.sendall(request.encode())
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = conn.recv(1024)
            if not chunk:
                break
            reply += chunk
    try:
        answer = json.loads(reply.decode("utf-8", "replace"))
    except ValueError:
        raise RuntimeError("ssm helper gave no answer") from None
    if not answer.get("ok"):
        raise RuntimeError(f"ssm registration failed: {str(answer.get('error', 'unknown'))[:120]}")


def crew_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("TMPDIR", os.path.join(env["HOME"], "tmp"))
    os.makedirs(env["TMPDIR"], mode=0o700, exist_ok=True)
    env.setdefault("KIROCREW_HOME", os.path.join(env["HOME"], ".kiro", "crew"))
    return env


def popen_as_crew(argv: list[str], **kwargs: object) -> subprocess.Popen[str]:
    """The hook server already runs as the crew user, so this is a plain Popen with the crew environment."""
    kwargs.setdefault("cwd", os.environ["HOME"])
    return subprocess.Popen(argv, env=crew_env(), text=True, **kwargs)  # type: ignore[call-overload]


def stop_process(proc: subprocess.Popen[str] | None, grace: float = 20.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run(argv: list[str], stdin: str | None = None) -> str:
    done = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=crew_env(), timeout=120)
    if done.returncode != 0:
        # stderr may echo arguments; none of the callers pass a secret as an argument.
        raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {done.stderr.strip()[-300:]}")
    return done.stdout


def run_python(code: str, stdin: str | None = None) -> str:
    """Run a snippet with KiroCrew's own interpreter (its venv holds kiro_crew; the hook server's python does not)."""
    return run([os.environ.get("KIROCREW_PYTHON", sys.executable), "-c", code], stdin)


def read_key(config: RunConfig) -> str:
    if config.key_file:
        with open(config.key_file, encoding="utf-8") as handle:
            key = handle.read().strip()
    else:
        import boto3  # imported here so the image-build /ready does not pay for it

        ssm = boto3.client("ssm", region_name=config.region)
        key = ssm.get_parameter(Name=config.key_parameter)["Parameter"]["Value"].strip()
    if not key or key == "REPLACE_ME":
        raise RuntimeError("the NIM key source holds no key")
    return key


_VAULT_CODE = """
import json, sys
from kiro_crew.config.paths import config_dir
from kiro_crew.secrets import SecretVault
vault = SecretVault(config_dir())
for name, value in json.load(sys.stdin).items():
    vault.set_sync(name, value)
"""


def choose_harness(config: RunConfig, stored: str) -> str:
    """The harness new sessions use after this boot.

    The payload wins when it names one (the launcher decided). Otherwise a choice already saved in the crew home survives, so a
    switch made on the dashboard ("Use OpenCode") is not undone by the next VM. Anything else, including a saved value this
    image does not carry, falls back to the default.
    """
    if config.harness in HARNESSES:
        return config.harness
    if config.web_function_domain:
        return "opencode"  # the remote harness is the reason this image exists
    return stored if stored in HARNESSES else DEFAULT_HARNESS


def crew_settings(config: RunConfig, stored: str) -> list[tuple[str, str]]:
    """The `kirocrew config set` pairs for this boot, in order.

    The Strands provider settings are always written so a switch back to Strands works whichever harness is default.
    """
    strands_env = {"LITELLM_BASE_URL": "secret://nim-base-url", "LITELLM_API_KEY": "secret://nim-api-key"}
    return [
        ("agent.acp_backend", choose_harness(config, stored)),
        ("agent.strands_provider", "litellm"),
        ("agent.strands_model", config.model),
        ("agent.strands_env", json.dumps(strands_env)),
        # Widest stall budget (300 s max): a snapshot resume must not look like a wedged event loop.
        ("dashboard.loop_stall_exit_after_secs", "300"),
    ]


def opencode_config(model: str) -> dict[str, object]:
    """OpenCode's global config for this VM (written to ~/.config/opencode/opencode.json).

    Every key is a decision, not a default:
    - model, small_model: the same model, so the first message of a session (sent to small_model for the session title) goes
      to the model chosen for its data policy and not to whatever OpenCode picks as "small".
    - enabled_providers: only Zen. The execution role's AWS credentials would otherwise let OpenCode offer other providers.
    - autoupdate false: the binary is part of the image; a self-update at runtime would change it unseen.
    - share disabled: no public share links for sessions.
    - lsp false: language servers are downloaded from the network on first use; this VM's network egress is the allowance
      of the gateway, not of the agent.
    The permission posture ("ask before every tool") is not here: KiroCrew seeds it per session and reads it back.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "small_model": model,
        "enabled_providers": ["opencode"],
        "autoupdate": False,
        "share": "disabled",
        "lsp": False,
    }


def write_opencode_config(model: str, home: str | None = None) -> str:
    """Write opencode_config(model) atomically; returns the path. The file holds no secret."""
    base = home if home is not None else os.environ["HOME"]
    directory = os.path.join(base, ".config", "opencode")
    os.makedirs(directory, mode=0o755, exist_ok=True)
    path = os.path.join(directory, "opencode.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(opencode_config(model), handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def stored_harness() -> str:
    """`agent.acp_backend` as saved in the (restored) crew home, "" when unset or unreadable."""
    try:
        out = run(["kirocrew", "config", "get", "agent.acp_backend"])
    except RuntimeError:
        return ""
    # Stdout carries the value alone; warnings go to stderr.
    return out.strip().strip('"')


def configure(config: RunConfig, key: str) -> None:
    """Point KiroCrew at its harnesses. The NIM key travels on stdin only; OpenCode's free models need no key."""
    run_python(_VAULT_CODE, json.dumps({"nim-base-url": config.base_url, "nim-api-key": key}))
    write_opencode_config(config.opencode_model)
    for name, value in crew_settings(config, stored_harness()):
        run(["kirocrew", "config", "set", name, value])


def mint_dashboard_token() -> str:
    """A dashboard access token from the gateway's own CLI (`kirocrew token`)."""
    out = run(["kirocrew", "token", "--port", str(GATEWAY_PORT), "--ttl", "1h"])
    match = re.search(r"[?&]token=([A-Za-z0-9._\-~+/=%]+)", out)
    if not match:
        raise RuntimeError("kirocrew token printed no token")
    return match.group(1)



CREW = Crew()
CHAT = Chat()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:  # request lines are not logged
        return

    def _send(self, code: int, doc: dict[str, object] | None = None) -> None:
        body = json.dumps(doc).encode() if doc is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        return self.rfile.read(min(max(length, 0), MAX_BODY + 1))

    def do_GET(self) -> None:
        if self.path == "/crew/status":
            self._send(200, CREW.status())
        elif self.path.startswith("/crew/chat/"):
            job = CHAT.get(self.path[len("/crew/chat/"):])
            self._send(200, job) if job is not None else self._send(404, {"error": "no such chat"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        body = self._body()
        if self.path == f"{HOOK_PREFIX}/ready":
            self._send(200)
        elif self.path == f"{HOOK_PREFIX}/run":
            try:
                config = parse_run_payload(body)
            except PayloadError as exc:
                print(f"crew: run payload refused: {exc}", flush=True)
                self._send(400, {"error": str(exc)})
                return
            CREW.start(config)
            self._send(200)
        elif self.path == f"{HOOK_PREFIX}/resume":
            try:
                CREW.resume()
            except Exception as exc:
                print(f"crew: resume failed: {type(exc).__name__}: {exc}", flush=True)
            self._send(200)
        elif self.path in (f"{HOOK_PREFIX}/suspend", f"{HOOK_PREFIX}/terminate"):
            try:
                CREW.pause()
            except Exception as exc:
                print(f"crew: pause failed: {type(exc).__name__}: {exc}", flush=True)
            self._send(200)
        elif self.path == "/crew/token":
            if CREW.status()["phase"] != "ready":
                self._send(409, {"error": "crew is not ready"})
                return
            try:
                self._send(200, {"token": mint_dashboard_token()})
            except Exception as exc:
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"[:300]})
        elif self.path == "/crew/chat":
            if CREW.status()["phase"] != "ready":
                self._send(409, {"error": "crew is not ready"})
                return
            try:
                message = parse_chat_request(body)
            except PayloadError as exc:
                self._send(400, {"error": str(exc)})
                return
            job_id = CHAT.start(message)
            self._send(202, {"id": job_id}) if job_id else self._send(429, {"error": "a chat turn is already running"})
        else:
            self._send(404, {"error": "not found"})


def serve_proxy() -> None:
    """Reverse proxy 0.0.0.0:8081 -> the gateway on loopback, HTTP and WebSocket."""
    import aiohttp
    from aiohttp import web

    upstream = f"http://{GATEWAY_HOST}"

    async def relay(source: object, sink: object) -> None:
        async for msg in source:  # type: ignore[attr-defined]
            if msg.type == aiohttp.WSMsgType.TEXT:
                await sink.send_str(msg.data)  # type: ignore[attr-defined]
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await sink.send_bytes(msg.data)  # type: ignore[attr-defined]
            else:
                break
        await sink.close()  # type: ignore[attr-defined]

    async def handle(request: web.Request) -> web.StreamResponse:
        session: aiohttp.ClientSession = request.app["session"]
        headers = gateway_request_headers(list(request.headers.items()))
        url = upstream + request.rel_url.raw_path_qs
        if request.headers.get("Upgrade", "").lower() == "websocket":
            protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
            try:
                up = await session.ws_connect(url, headers=headers, protocols=protocols, max_msg_size=0, autoping=True)
            except aiohttp.WSServerHandshakeError as exc:
                return web.Response(status=exc.status, text=exc.message)
            down = web.WebSocketResponse(protocols=[up.protocol] if up.protocol else [], max_msg_size=0)
            await down.prepare(request)
            await asyncio.wait([asyncio.ensure_future(relay(down, up)), asyncio.ensure_future(relay(up, down))],
                               return_when=asyncio.FIRST_COMPLETED)
            await up.close()
            await down.close()
            return down
        body = request.content if request.can_read_body else None
        async with session.request(request.method, url, headers=headers, data=body, allow_redirects=False) as resp:
            out = web.StreamResponse(status=resp.status, reason=resp.reason)
            for name, value in client_response_headers(list(resp.headers.items())):
                out.headers.add(name, value)
            await out.prepare(request)
            async for chunk in resp.content.iter_any():
                await out.write(chunk)
            await out.write_eof()
            return out

    async def guarded(request: web.Request) -> web.StreamResponse:
        try:
            return await handle(request)
        except aiohttp.ClientConnectorError:
            return web.Response(status=503, text="gateway is not listening yet")

    async def main() -> None:
        app = web.Application(client_max_size=1024**3)
        # auto_decompress off: bytes are relayed exactly as the gateway encoded them.
        app["session"] = aiohttp.ClientSession(auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None),
                                               cookie_jar=aiohttp.DummyCookieJar())
        app.router.add_route("*", "/{tail:.*}", guarded)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", FORWARD_PORT).start()
        await asyncio.Event().wait()

    asyncio.run(main())


if __name__ == "__main__":
    threading.Thread(target=serve_proxy, daemon=True).start()
    print(f"crew: hook server on {HOOK_PORT}, gateway proxy on {FORWARD_PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), Handler).serve_forever()
