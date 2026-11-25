"""Tests for the Web Function variant of the guest. Real code, real sockets, a real shim subprocess; nothing is mocked.

Run: uv run --with pytest --with hypothesis --with boto3 pytest crew/test -q
"""

import http.client
import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

GUEST = pathlib.Path(__file__).resolve().parents[1] / "image" / "guest"
sys.path.insert(0, str(GUEST))
import hooks  # noqa: E402

GOOD_DOMAIN = "abcd1234efgh5678.kfn.lambda-web.us-east-1.on.aws"


def payload(**extra):
    base = {"model": "nvidia/nemotron-3-ultra-550b-a55b", "baseUrl": "https://integrate.api.nvidia.com/v1", "region": "us-east-1",
            "nimKeyParameter": "/nim-free/api-key"}
    base.update(extra)
    return json.dumps(base).encode()


# ---- payload ----------------------------------------------------------------------------------------------------------

def test_domain_is_optional_and_empty_by_default():
    assert hooks.parse_run_payload(payload()).web_function_domain == ""


def test_valid_domain_is_accepted():
    assert hooks.parse_run_payload(payload(webFunctionDomain=GOOD_DOMAIN)).web_function_domain == GOOD_DOMAIN


@pytest.mark.parametrize("bad", [
    "example.com", "evil.com/x", GOOD_DOMAIN + "/ask", "http://" + GOOD_DOMAIN, GOOD_DOMAIN + ".evil.com",
    "a.kfn.lambda-web.us-east-1.on.aws", "x" * 40 + ".kfn.lambda-web.us-east-1.on.aws", GOOD_DOMAIN.upper(),
    "abcd1234efgh5678.kfn.lambda-web.us-east-1.on.aws:8443", "abcd1234efgh5678@evil.com",
])
def test_bad_domains_are_refused(bad):
    with pytest.raises(hooks.PayloadError):
        hooks.parse_run_payload(payload(webFunctionDomain=bad))


def test_domain_requires_a_region():
    with pytest.raises(hooks.PayloadError):
        hooks.parse_run_payload(payload(webFunctionDomain=GOOD_DOMAIN, region="nowhere"))


def test_opencode_is_the_default_harness_when_a_domain_is_set():
    cfg = hooks.parse_run_payload(payload(webFunctionDomain=GOOD_DOMAIN))
    assert hooks.choose_harness(cfg, "strands") == "opencode"
    forced = hooks.parse_run_payload(payload(webFunctionDomain=GOOD_DOMAIN, harness="strands"))
    assert hooks.choose_harness(forced, "opencode") == "strands"


def test_without_a_domain_the_stored_harness_still_wins():
    cfg = hooks.parse_run_payload(payload())
    assert hooks.choose_harness(cfg, "opencode") == "opencode"
    assert hooks.choose_harness(cfg, "") == hooks.DEFAULT_HARNESS


def test_default_opencode_model_is_in_the_current_catalog():
    assert hooks.DEFAULT_OPENCODE_MODEL == "opencode/deepseek-v4.1-flash-free"


# ---- relay: one route, one destination ------------------------------------------------------------------------------

def run_relay(poster):
    server = ThreadingHTTPServer(("127.0.0.1", 0), hooks.make_relay_handler(GOOD_DOMAIN, "us-east-1", poster))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def post(server, path, body):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        conn.request("POST", path, body=body, headers={"content-type": "application/json"})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def test_relay_forwards_ask_to_the_fixed_domain_and_returns_status_and_body():
    seen = []

    def poster(domain, region, body):
        seen.append((domain, region, body))
        return 429, b'{"error":"free_usage_limit_reached"}'

    server = run_relay(poster)
    try:
        status, body = post(server, "/ask", b'{"prompt":"hi"}')
    finally:
        server.shutdown()
    assert (status, json.loads(body)) == (429, {"error": "free_usage_limit_reached"})
    assert seen == [(GOOD_DOMAIN, "us-east-1", b'{"prompt":"hi"}')]


@pytest.mark.parametrize("path", ["/", "/health", "/ask/../x", "/log", "/ask/", "/ask?x=1", "/ask/extra"])
def test_relay_refuses_every_other_path(path):
    called = []
    server = run_relay(lambda *a: called.append(a) or (200, b"{}"))
    try:
        status, _ = post(server, path, b"{}")
    finally:
        server.shutdown()
    assert status == 404 and not called


def test_relay_rejects_oversized_and_empty_bodies():
    server = run_relay(lambda *a: (200, b"{}"))
    try:
        assert post(server, "/ask", b"")[0] == 400
        assert post(server, "/ask", b"x" * (hooks.RELAY_MAX_BODY + 1))[0] == 413
    finally:
        server.shutdown()


def test_relay_reports_an_upstream_failure_as_502_with_the_cause():
    def poster(*_):
        raise TimeoutError("timed out")

    server = run_relay(poster)
    try:
        status, body = post(server, "/ask", b"{}")
    finally:
        server.shutdown()
    assert status == 502 and "TimeoutError" in json.loads(body)["error"]


# ---- shim: a real subprocess speaking ACP, against a real fake Web Function --------------------------------------------

class FakeRelay(BaseHTTPRequestHandler):
    log = []
    mode = "ok"

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeRelay.log.append(body)
        if FakeRelay.mode == "cap":
            code, out = 429, {"error": "free_usage_limit_reached"}
        else:
            code, out = 200, {"text": f"echo:{body['prompt']}", "session_state": "new"}
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def shim():
    FakeRelay.log, FakeRelay.mode = [], "ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRelay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {"PATH": os.environ["PATH"], "WEBFN_RELAY": f"http://127.0.0.1:{server.server_address[1]}"}
    proc = subprocess.Popen([sys.executable, str(GUEST / "acp_shim.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)

    def call(obj):
        proc.stdin.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")
        proc.stdin.flush()

    def read():
        return json.loads(proc.stdout.readline())

    yield call, read
    proc.stdin.close()
    proc.wait(timeout=10)
    server.shutdown()


def test_shim_initialize_new_prompt_and_stable_state_key(shim):
    call, read = shim
    call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
    init = read()["result"]
    assert init["protocolVersion"] == 1 and init["agentCapabilities"]["loadSession"] is True
    call({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/tmp", "mcpServers": []}})
    sid = read()["result"]["sessionId"]
    for n, text in enumerate(["first", "second"], start=3):
        call({"jsonrpc": "2.0", "id": n, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}})
        update = read()
        assert update["method"] == "session/update"
        assert update["params"]["update"]["content"]["text"] == f"echo:{text}"
        assert read() == {"jsonrpc": "2.0", "id": n, "result": {"stopReason": "end_turn"}}
    keys = {entry["session"] for entry in FakeRelay.log}
    assert len(keys) == 1, "one ACP session must map to one state key"
    key = keys.pop()
    assert 1 <= len(key) <= 64 and key.replace("-", "").replace("_", "").isalnum()


def test_session_load_keeps_the_same_state_key(shim):
    call, read = shim
    call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    read()
    call({"jsonrpc": "2.0", "id": 2, "method": "session/load", "params": {"sessionId": "ses_old", "cwd": "/tmp", "mcpServers": []}})
    assert read()["result"] == {}
    call({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"sessionId": "ses_old", "prompt": [{"type": "text", "text": "again"}]}})
    read(), read()
    call({"jsonrpc": "2.0", "id": 4, "method": "session/prompt", "params": {"sessionId": "ses_old", "prompt": [{"type": "text", "text": "and again"}]}})
    read(), read()
    assert len({e["session"] for e in FakeRelay.log}) == 1


def test_shim_reports_the_free_cap_in_plain_words(shim):
    call, read = shim
    FakeRelay.mode = "cap"
    call({"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": {"sessionId": "ses_x", "prompt": [{"type": "text", "text": "hi"}]}})
    text = read()["params"]["update"]["content"]["text"]
    assert "free usage limit" in text
    assert read()["result"]["stopReason"] == "end_turn"


def test_shim_answers_unknown_requests_instead_of_hanging(shim):
    call, read = shim
    call({"jsonrpc": "2.0", "id": 9, "method": "session/set_mode", "params": {}})
    reply = read()
    assert reply["id"] == 9 and reply["error"]["code"] == -32601


def test_shim_survives_garbage_lines(shim):
    call, read = shim
    call("this is not json")
    call("")
    call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert read()["id"] == 1  # the first frame out is the answer to the valid request: the garbage produced none and did not kill it


# ---- signing ---------------------------------------------------------------------------------------------------------------

def test_signed_request_carries_the_payload_hash_the_endpoint_demands():
    import hashlib

    from botocore.credentials import Credentials

    body = b'{"prompt":"hi"}'
    headers = {k.lower(): v for k, v in hooks.signed_headers(GOOD_DOMAIN, "us-east-1", body, Credentials("AKIDEXAMPLE", "secret", "token")).items()}
    assert headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
    assert "x-amz-content-sha256" in headers["authorization"].split("SignedHeaders=")[1].split(",")[0], "the hash header must be signed"
    assert "/us-east-1/lambda/aws4_request" in headers["authorization"]
    assert headers["x-amz-security-token"] == "token"


# ---- SSM registration -------------------------------------------------------------------------------------------------------

import importlib.util
import socket
import stat

SSM_ID = "11111111-2222-3333-4444-555555555555"
SSM_CODE = "AbCdEfGhIjKlMnOpQrSt"


def test_ssm_fields_are_optional_and_travel_together():
    assert hooks.parse_run_payload(payload()).ssm_activation_id == ""
    cfg = hooks.parse_run_payload(payload(ssmActivationId=SSM_ID, ssmActivationCode=SSM_CODE))
    assert (cfg.ssm_activation_id, cfg.ssm_activation_code) == (SSM_ID, SSM_CODE)
    for half in ({"ssmActivationId": SSM_ID}, {"ssmActivationCode": SSM_CODE}):
        with pytest.raises(hooks.PayloadError):
            hooks.parse_run_payload(payload(**half))


@pytest.mark.parametrize("bad_id,bad_code", [
    ("not-a-uuid", SSM_CODE), (SSM_ID, "short"), (SSM_ID, "has space in code!!"), (SSM_ID, "$(touch /tmp/x)aaaaaaaaaaaaa"),
    ("ABCDEF12-2222-3333-4444-555555555555", SSM_CODE), (SSM_ID + "x", SSM_CODE), (SSM_ID, "a" * 65),
])
def test_malformed_activation_is_refused(bad_id, bad_code):
    with pytest.raises(hooks.PayloadError):
        hooks.parse_run_payload(payload(ssmActivationId=bad_id, ssmActivationCode=bad_code))


@pytest.fixture()
def helper_server(tmp_path):
    """The real root-side helper, started in-process on a real unix socket with a fake agent binary standing in for amazon-ssm-agent."""
    agent = tmp_path / "agent"
    # The helper starts the agent with a scrubbed environment on purpose, so the fake agent gets its paths baked in, not from env.
    agent.write_text(f'#!/bin/bash\necho "$@" >> {tmp_path}/agent.log\n'
                     f'[ "$1" = "-register" ] && [ -e {tmp_path}/fail ] && {{ echo "boom $3" >&2; exit 1; }}\nexit 0\n')
    agent.chmod(0o755)
    os.environ.update({"SSM_AGENT_BIN": str(agent), "SSM_AGENT_LOG": str(tmp_path / "helper.log"), "SSM_HELPER_GROUP": "nonexistent-group"})
    spec = importlib.util.spec_from_file_location("ssm_helper_under_test", GUEST / "ssm-helper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import shutil
    import tempfile

    short = tempfile.mkdtemp(prefix="s", dir="/tmp")  # a macOS AF_UNIX path is limited to ~104 bytes; pytest's tmp_path is longer
    sock = os.path.join(short, "h.sock")
    threading.Thread(target=module.serve, args=(sock,), daemon=True).start()
    import time

    for _ in range(200):  # wait until it is LISTENING, not merely bound: a connect between bind and listen is refused
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(sock)
            probe.close()
            break
        except OSError:
            probe.close()
            time.sleep(0.02)
    yield sock, tmp_path
    shutil.rmtree(short, ignore_errors=True)
    for key in ("SSM_AGENT_BIN", "SSM_AGENT_LOG", "SSM_HELPER_GROUP"):
        os.environ.pop(key, None)


def raw(sock, text):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(20)
        conn.connect(sock)
        conn.sendall(text.encode())
        return json.loads(conn.recv(4096).decode())


def req(**over):
    doc = {"region": "us-east-1", "id": SSM_ID, "code": SSM_CODE}
    doc.update(over)
    return json.dumps(doc) + "\n"


def test_helper_registers_then_starts_the_agent_and_replies_ok(helper_server):
    sock, tmp = helper_server
    assert raw(sock, req()) == {"ok": True}
    lines = (tmp / "agent.log").read_text().splitlines()
    assert lines[0] == f"-register -code {SSM_CODE} -id {SSM_ID} -region us-east-1 -y"


def test_helper_refuses_a_second_registration(helper_server):
    sock, _ = helper_server
    assert raw(sock, req())["ok"] is True
    assert raw(sock, req()) == {"ok": False, "error": "already registered"}


@pytest.mark.parametrize("text", [
    req(id="not-a-uuid"), req(code="short"), req(code="$(id)aaaaaaaaaaaa"), req(region="us-east-1; id"), req(region="nowhere"),
    json.dumps({"region": "us-east-1", "id": SSM_ID}) + "\n", json.dumps({"region": "us-east-1", "id": SSM_ID, "code": SSM_CODE, "x": 1}) + "\n",
    "not json\n", "[]\n",
])
def test_helper_refuses_bad_requests_without_running_the_agent(helper_server, text):
    sock, tmp = helper_server
    answer = raw(sock, text)
    assert answer["ok"] is False
    assert not (tmp / "agent.log").exists()


def test_failed_registration_never_leaks_the_code_in_reply_or_log(helper_server):
    sock, tmp = helper_server
    (tmp / "fail").write_text("1")
    answer = raw(sock, req())
    assert answer["ok"] is False and SSM_CODE not in json.dumps(answer)
    log = (tmp / "helper.log").read_text()
    assert "register failed" in log and SSM_CODE not in log


def test_start_ssm_talks_to_the_helper_over_the_socket(helper_server):
    sock, _ = helper_server
    cfg = hooks.parse_run_payload(payload(ssmActivationId=SSM_ID, ssmActivationCode=SSM_CODE))
    hooks.start_ssm(cfg, sock)  # returns None on success
    with pytest.raises(RuntimeError, match="already registered"):
        hooks.start_ssm(cfg, sock)


def test_the_helper_socket_is_not_world_accessible(helper_server):
    sock, _ = helper_server
    assert stat.S_IMODE(os.stat(sock).st_mode) & 0o007 == 0


# ---- sudo stand-in for the hub's wrapper ----------------------------------------------------------------------------------

def compat(args, stdin=""):
    return subprocess.run(["bash", str(GUEST / "sudo-compat"), *args], input=stdin, capture_output=True, text=True, timeout=30,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")})


def test_compat_runs_the_hubs_wrapper_as_the_current_user():
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    done = compat(["-u", me, "-i", "bash"], "echo ran-as-$(id -un)\n")
    assert done.returncode == 0 and done.stdout.strip() == f"ran-as-{me}"


def test_compat_passes_the_script_exit_code_through():
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    assert compat(["-u", me, "-i", "bash"], "exit 7\n").returncode == 7


@pytest.mark.parametrize("args", [["-u", "root", "-i", "bash"], ["-u", "nobody", "-i", "id"], ["-s"], ["--preserve-env=X", "id"]])
def test_compat_refuses_to_become_anyone_else_or_take_unknown_options(args):
    done = compat(args, "id\n")
    assert done.returncode == 1 and "uid=" not in done.stdout


def test_sudo_stand_in_is_installed_where_ssm_run_command_can_find_it():
    """SSM Run Command's PATH has /usr/bin but not /usr/local/bin; the hub's wrapper failed with 'sudo: command not found' there."""
    dockerfile = (GUEST.parent / "Dockerfile").read_text()
    assert "COPY guest/sudo-compat /usr/bin/sudo" in dockerfile
    assert "/usr/local/bin/sudo" not in dockerfile


def test_start_ssm_waits_for_a_helper_that_is_not_listening_yet(tmp_path):
    import shutil
    import tempfile
    import time

    short = tempfile.mkdtemp(prefix="s", dir="/tmp")
    sock = os.path.join(short, "h.sock")
    agent = tmp_path / "agent"
    agent.write_text("#!/bin/bash\nexit 0\n")
    agent.chmod(0o755)
    os.environ.update({"SSM_AGENT_BIN": str(agent), "SSM_AGENT_LOG": str(tmp_path / "h.log"), "SSM_HELPER_GROUP": "nonexistent-group"})
    try:
        spec = importlib.util.spec_from_file_location("ssm_helper_late", GUEST / "ssm-helper.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        threading.Timer(1.0, lambda: threading.Thread(target=module.serve, args=(sock,), daemon=True).start()).start()
        cfg = hooks.parse_run_payload(payload(ssmActivationId=SSM_ID, ssmActivationCode=SSM_CODE))
        started = time.monotonic()
        hooks.start_ssm(cfg, sock)  # the socket does not exist for the first second
        assert time.monotonic() - started >= 0.9
    finally:
        for key in ("SSM_AGENT_BIN", "SSM_AGENT_LOG", "SSM_HELPER_GROUP"):
            os.environ.pop(key, None)
        shutil.rmtree(short, ignore_errors=True)


def test_the_helper_survives_a_client_that_disconnects_without_sending(helper_server):
    sock, _ = helper_server
    for _ in range(3):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(sock)
        c.close()
    assert raw(sock, req())["ok"] is True


# ---- KiroCrew comes from the official installer, not a vendored wheel ------------------------------------------------------

def test_the_image_installs_kirocrew_with_the_official_installer_and_keeps_it_out_of_the_crew_home():
    dockerfile = (GUEST.parent / "Dockerfile").read_text()
    assert "https://download.crew.kiro.dev/cli.sh" in dockerfile and "--channel \"$KIROCREW_CHANNEL\"" in dockerfile
    assert "ARG KIROCREW_CHANNEL=stable" in dockerfile
    assert "KIROCREW_HOME=/opt/kc/home KIROCREW_VENV=/opt/kc/venv" in dockerfile, "install outside the mirrored crew home"
    assert "vendor" not in dockerfile and not (GUEST.parent / "vendor").exists()
    assert "KIROCREW_PYTHON=/opt/kc/venv/bin/python" in dockerfile


def test_strands_versions_come_from_the_installed_release_not_from_this_repo():
    dockerfile = (GUEST.parent / "Dockerfile").read_text()
    assert "STRANDS_VERIFIED_VERSION" in dockerfile and "STRANDS_PYTHON_REQUIREMENT" in dockerfile
    import re

    assert not re.search(r"strands-harness(\[litellm\])?==\d", dockerfile) and not re.search(r"@strands-agents/cli@\d", dockerfile)


def test_run_python_uses_kirocrews_own_interpreter(monkeypatch):
    seen = {}
    monkeypatch.setattr(hooks, "run", lambda argv, stdin=None: seen.setdefault("argv", argv) and "")
    monkeypatch.setenv("KIROCREW_PYTHON", "/opt/kc/venv/bin/python")
    hooks.run_python("print(1)")
    assert seen["argv"][0] == "/opt/kc/venv/bin/python"


def test_the_image_has_the_tools_the_official_installer_needs():
    """The installer stops at once without openssl (it verifies the signed manifest with it); seen in a build on 2026-11-23."""
    dockerfile = (GUEST.parent / "Dockerfile").read_text()
    first_install = dockerfile[dockerfile.index("RUN dnf -y install"):].split("&&")[0]
    assert "openssl" in first_install and "tar" in first_install


def test_the_installer_gets_its_own_uv_cache_because_the_image_wide_one_belongs_to_root():
    """The image sets UV_CACHE_DIR=/opt/uv-cache (root-owned); the installer runs as crew and failed on it (build log, 2026-11-23)."""
    dockerfile = (GUEST.parent / "Dockerfile").read_text()
    install_step = dockerfile[dockerfile.index("sh /tmp/cli.sh") - 200: dockerfile.index("sh /tmp/cli.sh") + 80]
    assert "UV_CACHE_DIR=/tmp/uv-cache-kc" in install_step


def test_the_strands_cli_link_is_idempotent():
    """`npm install -g` already creates /usr/local/bin/strands; a plain `ln -s` after it failed the build (2026-11-23)."""
    dockerfile = (GUEST.parent / "Dockerfile").read_text()
    for line in dockerfile.splitlines():
        if "bin/strands.js" in line and "ln " in line:
            assert "ln -sf" in line, line


def test_strands_extra_goes_before_the_version_specifier():
    """`pkg==1.2[extra]` is not valid; the build failed on it. The extra must sit between the name and the specifier."""
    import re
    import subprocess

    dockerfile = (GUEST.parent / "Dockerfile").read_text()
    assert '${REQ}[litellm]' not in dockerfile
    line = next(l for l in dockerfile.splitlines() if "sed 's/^strands-harness/" in l)
    expr = re.search(r"sed '([^']+)'", line).group(1)
    out = subprocess.run(["sed", expr], input="strands-harness==0.3.3", capture_output=True, text=True).stdout.strip()
    assert out == "strands-harness[litellm]==0.3.3"
