"""crew.sh up builds the run-hook payload. The real script runs against a stub `aws` that never touches AWS, and the
payload it produced is fed to the guest's real parser, so the shell and the guest cannot drift apart."""
import os
import pathlib
import stat
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "image" / "guest"))
import hooks  # noqa: E402

GOOD = "abcd1234efgh5678.kfn.lambda-web.us-east-1.on.aws"

STUB = """#!/usr/bin/env bash
case "$*" in
  *"describe-stacks"*"KiroCrewRemoteCrewStack"*"StateBucket"*) echo "test-state-bucket" ;;
  *"describe-stacks"*"KiroCrewRemoteCrewStack"*"ExecutionRoleArn"*) echo "arn:aws:iam::000000000000:role/test" ;;
  *"describe-stacks"*"OpenCodeWebFnStack"*"Domain"*) echo "$STUB_DOMAIN" ;;
  *"describe-stacks"*"KiroCrewRemoteCrewStack"*"SsmHybridRoleName"*) echo "hybrid-role" ;;
  *"ssm create-activation"*) echo '{"ActivationId":"11111111-2222-3333-4444-555555555555","ActivationCode":"AbCdEfGhIjKlMnOpQrSt"}' ;;
  *"sts get-caller-identity"*) echo "000000000000" ;;
  *"lambda-microvms list-microvms"*) ;;
  *"lambda-microvms run-microvm"*)
    while [ $# -gt 0 ]; do
      if [ "$1" = "--run-hook-payload" ]; then printf '%s' "$2" > "$STUB_OUT"; fi
      if [ "$1" = "--idle-policy" ]; then printf '%s' "$2" > "$STUB_OUT.idle"; fi
      if [ "$1" = "--maximum-duration-in-seconds" ]; then printf '%s' "$2" > "$STUB_OUT.life"; fi
      shift
    done
    echo '{"id":"microvm-stub","state":"PENDING","endpoint":"none"}' ;;
  *) echo "stub aws: unexpected call: $*" >&2; exit 99 ;;
esac
"""


@pytest.fixture()
def up(tmp_path):
    stub = tmp_path / "aws"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    out = tmp_path / "payload.json"

    def run(domain=GOOD, **env: str):
        out.unlink(missing_ok=True)
        full = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "STUB_OUT": str(out), "STUB_DOMAIN": domain, **env}
        done = subprocess.run(["bash", str(ROOT / "scripts" / "crew.sh"), "up"], capture_output=True, text=True, env=full, timeout=60)
        return done.returncode, done.stderr, (out.read_bytes() if out.exists() else None)

    return run


def test_payload_carries_the_web_function_domain_and_the_guest_accepts_it(up):
    code, _, payload = up()
    assert code == 0 and payload is not None
    config = hooks.parse_run_payload(payload)
    assert config.web_function_domain == GOOD
    assert config.state_bucket == "test-state-bucket"
    assert hooks.choose_harness(config, "strands") == "opencode"


@pytest.mark.parametrize("bad", ["None", "", "evil.example.com", GOOD + "/x", "x.kfn.lambda-web.us-east-1.on.aws"])
def test_an_unexpected_domain_stops_the_launch_before_a_vm_starts(up, bad):
    code, err, payload = up(domain=bad)
    assert code != 0 and payload is None
    assert "Web Function domain" in err


def test_the_image_is_the_wf_image_not_the_first_design():
    text = (ROOT / "scripts" / "crew.sh").read_text()
    assert "microvm-image:kirocrew-remote" in text and "kirocrew-nim" not in text
    assert "KiroCrewRemoteCrewStack" in text and "STACK=KiroCrewMicrovmStack\n" not in text


def test_launch_registers_an_ssm_activation_and_never_prints_its_code(up, tmp_path):
    code, err, payload = up()
    assert code == 0 and payload is not None
    config = hooks.parse_run_payload(payload)
    assert (config.ssm_activation_id, config.ssm_activation_code) == ("11111111-2222-3333-4444-555555555555", "AbCdEfGhIjKlMnOpQrSt")
    assert "AbCdEfGhIjKlMnOpQrSt" not in err


def test_describe_instance_information_never_mixes_a_tag_filter_with_another_filter():
    """SSM refuses it ("Cannot mix tag or inventory filter with any other filter"), seen live on 2026-11-23."""
    import re

    text = (ROOT / "scripts" / "crew.sh").read_text().replace("\\\n", " ")
    calls = [line for line in text.splitlines() if "describe-instance-information" in line]
    assert calls
    for call in calls:
        filters = re.findall(r'"Key=([^"]+)"', call)
        assert not (any(f.startswith("tag:") for f in filters) and len(filters) > 1), call


def test_a_crew_does_not_suspend_within_its_lifetime_by_default(up, tmp_path):
    """A suspended VM freezes its SSM agent and Session Manager does not wake it: the hub's port forward timed out (2026-11-23)."""
    import json as _json

    code, _, _ = up()
    assert code == 0
    idle = _json.loads((tmp_path / "payload.json.idle").read_text())
    life = int((tmp_path / "payload.json.life").read_text())
    assert idle["maxIdleDurationSeconds"] >= life


def test_idle_can_be_lowered_on_purpose(up, tmp_path):
    import json as _json

    code, _, _ = up(CREW_IDLE_SECS="300")
    assert code == 0
    assert _json.loads((tmp_path / "payload.json.idle").read_text())["maxIdleDurationSeconds"] == 300


def test_nothing_is_tied_to_one_aws_account():
    """The project must run unchanged in another account: no 12-digit account id in any source file (tests use 000000000000)."""
    import re

    project = ROOT.parent
    offenders = []
    for path in project.rglob("*"):
        if not path.is_file() or any(part in {"node_modules", "cdk.out", "dist", "__pycache__"} for part in path.parts):
            continue
        if path.suffix not in {".ts", ".py", ".sh", ".json", ".md", ".js"} and path.name != "Dockerfile":
            continue
        if path.name in {"package-lock.json", "test_wf_launcher.py", "test_wf_guest.py"}:
            continue
        for found in re.findall(r"(?<![0-9])[0-9]{12}(?![0-9])", path.read_text(errors="ignore")):
            if found != "000000000000":
                offenders.append((str(path.relative_to(project)), found))
    assert not offenders, offenders


def test_auth_tokens_cover_only_the_two_guest_ports():
    """App-sec review: a token for all ports would also open anything else that listens in the VM."""
    for name in ("crew.sh", "tunnel.py"):
        text = (ROOT / "scripts" / name).read_text()
        assert "allPorts" not in text, name
        assert "8080" in text and "8081" in text, name


def test_the_web_function_does_not_leak_traces_or_logs_to_callers():
    main = (ROOT.parent / "webfn" / "app" / "main.py").read_text()
    assert '"trace"' not in main, "tracebacks go to CloudWatch Logs, not into the response"
    assert 'path == "/log" and DEBUG' in main, "the log route exists only with OC_DEBUG=1"
    assert 'DEBUG = os.environ.get("OC_DEBUG") == "1"' in main


def test_no_personal_names_or_real_endpoints_in_the_project():
    import re

    bad = re.compile(r"vivek|velso|waltsoft|dtl38v8x99r2r327|/Users/", re.IGNORECASE)
    hits = []
    for path in ROOT.parent.rglob("*"):
        if not path.is_file() or any(p in {"node_modules", "cdk.out", "dist", "__pycache__"} for p in path.parts):
            continue
        if path.suffix not in {".ts", ".py", ".sh", ".json", ".md"} and path.name != "Dockerfile" or path.name == "package-lock.json":
            continue
        if path.name == "test_wf_launcher.py":
            continue  # holds the pattern itself
        for line in path.read_text(errors="ignore").splitlines():
            if bad.search(line):
                hits.append((str(path.relative_to(ROOT.parent)), line.strip()[:80]))
    assert not hits, hits


def test_crew_sh_commands_take_the_id_from_the_one_live_crew_and_never_hit_an_unbound_variable():
    """`crew.sh status` with no id used to die with '$2: unbound variable' (set -u). Only chat needs an explicit id."""
    import re
    import subprocess

    script = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "crew.sh").read_text()
    assert subprocess.run(["bash", "-n", str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "crew.sh")]).returncode == 0
    bare = [l for l in script.splitlines() if re.search(r'"\$2"', l) and not l.lstrip().startswith("#")]
    assert len(bare) == 1 and "token" in bare[0], bare  # the chat line, guarded by an explicit usage check just above it
    assert 'usage: crew.sh chat ID' in script
