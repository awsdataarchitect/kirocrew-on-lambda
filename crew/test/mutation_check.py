"""Mutation check for the Web Function variant: each mutant weakens one rule and must make a test fail.

Run: uv run --no-project --with pytest --with hypothesis --with boto3 python crew/test/mutation_check.py
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOKS = ROOT / "image" / "guest" / "hooks.py"
SHIM = ROOT / "image" / "guest" / "acp_shim.py"
HELPER = ROOT / "image" / "guest" / "ssm-helper.py"
CREW = ROOT / "scripts" / "crew.sh"

MUTANTS = [
    (HOOKS, 'if self.path != "/ask":', "if False:", "relay accepts any path"),
    (HOOKS, 'if length <= 0 or length > RELAY_MAX_BODY:', "if False:", "relay accepts empty/oversized bodies"),
    (HOOKS, r'\.kfn\.lambda-web\.', r'.*', "domain grammar loosened"),
    (HOOKS, 'if config.web_function_domain:\n        return "opencode"', 'if False:\n        return "opencode"', "remote harness not the default"),
    (HOOKS, 'status, payload = 502, json.dumps', 'status, payload = 200, json.dumps', "upstream failure reported as success"),
    (HOOKS, ', "x-amz-content-sha256": hashlib.sha256(body).hexdigest()}', '}', "payload hash header dropped"),
    (HOOKS, '_SSM_CODE_RE = re.compile(r"^[A-Za-z0-9+/=]{10,64}$")', '_SSM_CODE_RE = re.compile(r".*")', "ssm code grammar loosened"),
        (HOOKS, 'if bool(ssm_id) != bool(ssm_code):', 'if False:', "half an activation accepted"),
    (HELPER, 'CODE_RE = re.compile(r"^[A-Za-z0-9+/=]{10,64}$")', 'CODE_RE = re.compile(r".*")', "helper skips the code check"),
    (HELPER, 'REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")', 'REGION_RE = re.compile(r".*")', "helper skips the region check"),
    (HELPER, 'if self.registered:\n            return {"ok": False, "error": "already registered"}', 'if False:\n            return {"ok": False, "error": "already registered"}', "second registration accepted"),
    (HELPER, 'tail = (done.stdout + done.stderr)[-300:].replace(code, "***")', 'tail = (done.stdout + done.stderr)[-300:]', "code may reach the helper log"),
    (CREW, 'IDLE=${CREW_IDLE_SECS:-$LIFETIME}', 'IDLE=${CREW_IDLE_SECS:-300}', "crew suspends after 5 idle minutes again"),
    (HOOKS, 'except (FileNotFoundError, ConnectionRefusedError):', 'except ZeroDivisionError:', "no wait for the helper to start"),
    (HELPER, '            except OSError:\n                continue  # the client went away; the next one is served', '            except ZeroDivisionError:\n                continue', "helper dies when a client leaves early"),
    (SHIM, '"loadSession": True', '"loadSession": False', "session/load not advertised"),
    (SHIM, 'hashlib.sha256(session_id.encode()).hexdigest()[:40]', 'uuid.uuid4().hex[:40]', "state key not stable per session"),
    (SHIM, 'if status == 429:', 'if False:', "free cap not reported"),
]

failed = 0
for path, old, new, name in MUTANTS:
    original = path.read_text()
    if original.count(old) != 1:
        print(f"SETUP ERROR  {name}: target found {original.count(old)} times")
        failed += 1
        continue
    backup = pathlib.Path(tempfile.mkdtemp()) / path.name
    shutil.copy(path, backup)
    try:
        path.write_text(original.replace(old, new))
        assert path.read_text() != original, "mutation did not change the file"
        r = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "test"), "-x", "-q", "-p", "no:cacheprovider"],
                           capture_output=True, text=True, timeout=300)
        killed = r.returncode != 0
        print(("KILLED   " if killed else "SURVIVED ") + name)
        failed += 0 if killed else 1
    finally:
        shutil.copy(backup, path)
        shutil.rmtree(backup.parent)
        assert path.read_text() == original, "restore failed"
sys.exit(1 if failed else 0)
