#!/usr/bin/python3
"""Root-side helper for Remote Crew over SSM.

The SSM agent must run as root (it keeps its credentials under /root and starts sessions as other users), while everything else in
this VM runs as the crew user. This small daemon is the only root process the guest adds. It listens on a unix socket that the
crew user can write to and accepts one thing: a request to register this VM with a Systems Manager hybrid activation.

Request (one JSON line): {"region": "...", "id": "<activation id>", "code": "<activation code>"}
Reply   (one JSON line): {"ok": true} or {"ok": false, "error": "<fixed message>"}  (never the id or the code)

Every field is checked against a strict grammar before anything runs, the code reaches the agent only as an argument of the
registration child, and a second registration request is refused. stdlib only (runs on the image's system python).
"""
import json
import os
import re
import socket
import subprocess
import sys

SOCKET_PATH = os.environ.get("SSM_HELPER_SOCKET", "/run/kirocrew/ssm.sock")
AGENT = os.environ.get("SSM_AGENT_BIN", "/usr/bin/amazon-ssm-agent")
AGENT_LOG = os.environ.get("SSM_AGENT_LOG", "/var/log/amazon/ssm/ssm-start.out")
SOCKET_GROUP = os.environ.get("SSM_HELPER_GROUP", "crew")

REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
ID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
CODE_RE = re.compile(r"^[A-Za-z0-9+/=]{10,64}$")
MAX_REQUEST = 1024


def validate(doc):
    """The (region, id, code) of a well-formed request, or an error string."""
    if not isinstance(doc, dict) or set(doc) != {"region", "id", "code"}:
        return "request must be an object with region, id and code"
    region, ident, code = doc["region"], doc["id"], doc["code"]
    if not all(isinstance(v, str) for v in (region, ident, code)):
        return "fields must be strings"
    if not REGION_RE.fullmatch(region):
        return "bad region"
    if not ID_RE.fullmatch(ident):
        return "bad activation id"
    if not CODE_RE.fullmatch(code):
        return "bad activation code"
    return (region, ident, code)


class Helper:
    def __init__(self):
        self.registered = False

    def register(self, region, ident, code):
        env = {"PATH": "/usr/bin:/bin", "HOME": "/root"}
        done = subprocess.run([AGENT, "-register", "-code", code, "-id", ident, "-region", region, "-y"], env=env,
                              capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            # The agent's own words help find the cause; the code is replaced in case it is echoed.
            tail = (done.stdout + done.stderr)[-300:].replace(code, "***")
            os.makedirs(os.path.dirname(AGENT_LOG), exist_ok=True)
            with open(AGENT_LOG, "a") as handle:
                handle.write("register failed: " + tail + "\n")
            return "registration failed (see the helper log)"
        log = open(AGENT_LOG, "a")
        subprocess.Popen([AGENT], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.registered = True
        return None

    def handle(self, line):
        try:
            doc = json.loads(line)
        except ValueError:
            return {"ok": False, "error": "request is not JSON"}
        checked = validate(doc)
        if isinstance(checked, str):
            return {"ok": False, "error": checked}
        if self.registered:
            return {"ok": False, "error": "already registered"}
        error = self.register(*checked)
        return {"ok": True} if error is None else {"ok": False, "error": error}


def serve(path=SOCKET_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    try:
        import grp
        os.chown(path, 0, grp.getgrnam(SOCKET_GROUP).gr_gid)
    except (KeyError, PermissionError):
        pass  # a test running as an ordinary user keeps its own group
    os.chmod(path, 0o660)
    server.listen(4)
    helper = Helper()
    while True:
        conn, _ = server.accept()
        with conn:
            conn.settimeout(65)
            data = b""
            try:
                while not data.endswith(b"\n") and len(data) <= MAX_REQUEST:
                    chunk = conn.recv(256)
                    if not chunk:
                        break
                    data += chunk
                reply = helper.handle(data.decode("utf-8", "replace").strip()) if len(data) <= MAX_REQUEST else {"ok": False, "error": "request too large"}
            except OSError:
                continue
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                continue  # the client went away; the next one is served


if __name__ == "__main__":
    serve(*sys.argv[1:2])
