#!/usr/bin/env bash
# Laptop launcher for the KiroCrew MicroVM whose OpenCode harness runs in a Lambda Web Function.
# Usage: crew.sh up | list | status [ID] | connect [ID] | remote-config | chat ID "msg" | suspend [ID] | resume [ID] | down [ID]
#   ID is optional where shown: it defaults to the one live crew.
#   connect ID: tunnel this laptop's 127.0.0.1:5490 to the crew's dashboard and print the sign-in URL (no SSH or SSM needed).
# Billing starts at `up` (~$0.126/h at 2 GB / 1 vCPU) and stops at `down`, the idle policy, or the lifetime cap.
# The crew home is mirrored to the state bucket (stack output StateBucket): /run restores it, the guest pushes it every
# minute, and /suspend and /terminate push it once more. A new VM therefore starts with the old sessions and config.
set -euo pipefail
R=us-east-1
STACK=KiroCrewRemoteCrewStack
WEBFN_STACK=OpenCodeWebFnStack  # the Web Function that runs OpenCode (webfn/)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
IMG=arn:aws:lambda:$R:$ACCOUNT:microvm-image:kirocrew-remote
out() { aws cloudformation describe-stacks --region $R --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
CON=arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector
LIFETIME=${CREW_LIFETIME_SECS:-3600}
# A suspended VM freezes its SSM agent and a Session Manager connection does not wake it (measured 2026-11-23: the Remote Crew port
# forward timed out on a suspended VM). Idle is judged on the VM's own traffic, not on SSM, so by default it never suspends within
# its lifetime; set CREW_IDLE_SECS lower to trade the SSM connection for cost.
IDLE=${CREW_IDLE_SECS:-$LIFETIME}
token() { aws lambda-microvms create-microvm-auth-token --region $R --microvm-identifier "$1" \
  --expiration-in-minutes 15 --allowed-ports '[{"port":8080},{"port":8081}]' --query 'authToken."X-aws-proxy-auth"' --output text; }
host() { aws lambda-microvms get-microvm --region $R --microvm-identifier "$1" --query endpoint --output text; }
live() { aws lambda-microvms list-microvms --region $R \
  --query "items[?state=='RUNNING'||state=='SUSPENDED'||state=='PENDING'].[microvmId,state]" --output text; }
# The crew id: the one given, else the single live crew (only one is allowed at a time).
vm() {
  if [ -n "${1:-}" ]; then printf '%s' "$1"; return; fi
  local n; n=$(live | awk '{print $1}')
  if [ -n "$n" ] && [ "$(printf '%s\n' "$n" | wc -l | tr -d ' ')" -eq 1 ]; then printf '%s' "$n"; return; fi
  echo "no single live crew: pass an id (crew.sh list shows them)" >&2; return 1
}
case "${1:-}" in
  up)
    # Optional: CREW_HARNESS=strands|opencode forces the default harness for this boot (otherwise the one saved in the crew home
    # is kept, so a "Use OpenCode" click on the dashboard survives restarts); CREW_OPENCODE_MODEL=opencode/<zen model id>.
    EXTRA=""
    case "${CREW_HARNESS:-}" in
      ""|strands|opencode) [ -z "${CREW_HARNESS:-}" ] || EXTRA="$EXTRA,\"harness\":\"$CREW_HARNESS\"" ;;
      *) echo "CREW_HARNESS must be strands or opencode" >&2; exit 2 ;;
    esac
    if [ -n "${CREW_OPENCODE_MODEL:-}" ]; then
      [[ "$CREW_OPENCODE_MODEL" =~ ^opencode/[a-z0-9][a-z0-9.-]{0,63}$ ]] || { echo "CREW_OPENCODE_MODEL must look like opencode/deepseek-v4.1-flash-free" >&2; exit 2; }
      EXTRA="$EXTRA,\"opencodeModel\":\"$CREW_OPENCODE_MODEL\""
    fi
    # Two VMs mirroring to one prefix would overwrite each other's files. One at a time.
    if [ -n "$(live)" ]; then echo "a crew VM is already live:" >&2; live >&2; echo "use it, or crew.sh down <id> first" >&2; exit 1; fi
    BUCKET=$(out StateBucket); ROLE=$(out ExecutionRoleArn)
    WEBFN=$(aws cloudformation describe-stacks --region $R --stack-name $WEBFN_STACK --query "Stacks[0].Outputs[?OutputKey=='Domain'].OutputValue" --output text)
    [[ "$WEBFN" =~ ^[a-z0-9]{8,32}\.kfn\.lambda-web\.[a-z0-9-]+\.on\.aws$ ]] || { echo "no Web Function domain from stack $WEBFN_STACK" >&2; exit 1; }
    # Remote Crew over SSM: a single-use activation that expires in an hour. The code goes into the payload only, never to the terminal.
    HYBRID=$(out SsmHybridRoleName)
    ACT=$(aws ssm create-activation --region $R --iam-role "$HYBRID" --registration-limit 1 --default-instance-name kirocrew-remote \
      --expiration-date "$(date -u -v+1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)" \
      --tags Key=kirocrew-remote,Value=1 --output json)
    ACT_ID=$(printf '%s' "$ACT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ActivationId"])')
    ACT_CODE=$(printf '%s' "$ACT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ActivationCode"])')
    EXTRA="$EXTRA,\"ssmActivationId\":\"$ACT_ID\",\"ssmActivationCode\":\"$ACT_CODE\""
    PAYLOAD=$(printf '{"model":"nvidia/nemotron-3-ultra-550b-a55b","baseUrl":"https://integrate.api.nvidia.com/v1","region":"%s","nimKeyParameter":"%s","stateBucket":"%s","webFunctionDomain":"%s"%s}' "$R" "${NIM_KEY_PARAM:-/nim-free/api-key}" "$BUCKET" "$WEBFN" "$EXTRA")
    aws lambda-microvms run-microvm --region $R --image-identifier "$IMG" --execution-role-arn "$ROLE" \
      --ingress-network-connectors "$CON:HTTP_INGRESS" --egress-network-connectors "$CON:INTERNET_EGRESS" \
      --idle-policy "{\"autoResumeEnabled\":true,\"maxIdleDurationSeconds\":$IDLE,\"suspendedDurationSeconds\":600}" \
      --maximum-duration-in-seconds "$LIFETIME" --run-hook-payload "$PAYLOAD" \
      --query '{id:microvmId,state:state,endpoint:endpoint}' --output json ;;
  list) live ;;
  connect)
    # Laptop attach: a loopback tunnel to the crew's dashboard. Settings -> Remote Crew only knows SSH and SSM transports and a
    # MicroVM has neither, so this is the way in. The sign-in URL is also written to a file (a chat renderer may redact it).
    ID=$(vm "${2:-}")
    echo "sign in (fresh token each time): http://127.0.0.1:5490/_signin"
    exec uv run "$(dirname "$0")/tunnel.py" "$ID" ;;
  status) ID=$(vm "${2:-}"); curl -sS -m 30 "https://$(host "$ID")/crew/status" -H "X-aws-proxy-auth: $(token "$ID")"; echo ;;
  chat)
    [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo "usage: crew.sh chat ID \"message\"" >&2; exit 2; }
    T=$(token "$2"); H=$(host "$2")
    JOB=$(curl -sS -m 30 -X POST "https://$H/crew/chat" -H "X-aws-proxy-auth: $T" -H 'content-type: application/json' \
      -d "$(printf '{"message":%s}' "$(printf '%s' "$3" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")" \
      | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])') || { echo "chat was not accepted" >&2; exit 1; }
    # The VM runs the turn in the background; poll until it finishes (about 3 minutes at most) and print the answer.
    for _ in $(seq 1 60); do
      sleep 3
      R_=$(curl -sS -m 30 "https://$H/crew/chat/$JOB" -H "X-aws-proxy-auth: $T")
      if [ "$(printf '%s' "$R_" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("done"))')" = "True" ]; then
        printf '%s' "$R_" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("output","").strip());sys.exit(0 if d.get("exitCode")==0 else 1)'
        exit $?
      fi
    done
    echo "chat did not finish in 3 minutes (job $JOB)" >&2; exit 1 ;;
  suspend) aws lambda-microvms suspend-microvm --region $R --microvm-identifier "$(vm "${2:-}")" ;;
  resume) aws lambda-microvms resume-microvm --region $R --microvm-identifier "$(vm "${2:-}")" ;;
  down)
    aws lambda-microvms terminate-microvm --region $R --microvm-identifier "$(vm "${2:-}")"
    # Deregister the crew's SSM node so a dead VM does not linger in the Remote Crew list.
    for N in $(aws ssm describe-instance-information --region $R --filters "Key=tag:kirocrew-remote,Values=1" --query 'InstanceInformationList[].InstanceId' --output text); do
      aws ssm deregister-managed-instance --region $R --instance-id "$N" && echo "deregistered $N"
    done ;;
  remote-config)
    # What to put in Settings -> Remote Crew -> Add remote crew (connection method: AWS SSM).
    N=$(aws ssm describe-instance-information --region $R --filters "Key=tag:kirocrew-remote,Values=1" \
      --query "InstanceInformationList[?PingStatus=='Online'].InstanceId | [0]" --output text)
    [ -n "$N" ] && [ "$N" != "None" ] || { echo "no online crew node yet; wait for crew.sh status to show ready, then retry" >&2; exit 1; }
    cat <<EOF
Settings -> Remote Crew -> Add remote crew
  Name:                 kirocrew-remote
  Connection method:    AWS SSM
  SSM target:           $N
  AWS profile / region: default / $R
  Remote POSIX user:    crew
  Remote port:          5490
  Remote kirocrew path: /usr/local/bin/kirocrew
EOF
    ;;
  *) echo "usage: crew.sh up | list | status [ID] | connect [ID] | remote-config | chat ID MSG | suspend [ID] | resume [ID] | down [ID]" >&2; exit 2 ;;
esac
