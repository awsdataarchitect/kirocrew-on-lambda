#!/usr/bin/env bash
# One command from a clean checkout to a deployed stack pair. Safe to re-run.
#   ./deploy.sh            deploy both stacks
#   ./deploy.sh check      run the live check against the deployed Web Function
set -euo pipefail
cd "$(dirname "$0")"
REGION=us-east-1                      # the only region this has been tested in
KEY_PARAM=${NIM_KEY_PARAM:-/nim-free/api-key}

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)" >&2; exit 1; }; }
need aws "AWS CLI v2"; need node "Node 22 or newer"; need npm "comes with Node"; need uv "https://docs.astral.sh/uv/"
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' || { echo "Node 22 or newer is required" >&2; exit 1; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text) \
  || { echo "no AWS credentials: run 'aws login' or set AWS_PROFILE, then retry" >&2; exit 1; }
export CDK_DEFAULT_ACCOUNT=$ACCOUNT CDK_DEFAULT_REGION=$REGION AWS_REGION=$REGION

if [ "${1:-}" = "check" ]; then exec uv run --with boto3 python webfn/check.py; fi

# The crew reads one NVIDIA NIM API key from SSM Parameter Store (Standard tier String, free) for its Strands harness.
if ! aws ssm get-parameter --region "$REGION" --name "$KEY_PARAM" >/dev/null 2>&1; then
  echo "create the key parameter first (free NVIDIA NIM key from build.nvidia.com):" >&2
  echo "  aws ssm put-parameter --region $REGION --name $KEY_PARAM --type String --value <your-key>" >&2
  exit 1
fi

[ -d node_modules ] || npm install --no-fund --no-audit
echo "account $ACCOUNT, region $REGION"

# CDK needs a one-time bootstrap per account and region.
if ! aws cloudformation describe-stacks --region "$REGION" --stack-name CDKToolkit >/dev/null 2>&1; then
  npx cdk bootstrap "aws://$ACCOUNT/$REGION"
fi

echo "== 1/2 Web Function (OpenCode) =="
webfn/build.sh
(cd webfn && ../node_modules/.bin/cdk deploy --require-approval never)

echo "== 2/2 MicroVM image and crew stack (the image build takes about 5 minutes) =="
(cd crew && ../node_modules/.bin/cdk deploy --require-approval never)

cat <<EOF

Deployed. Next:
  ./deploy.sh check                       # live check of the Web Function
  crew/scripts/crew.sh up                 # launch a crew (bills about \$0.13 per hour while it runs)
  crew/scripts/crew.sh remote-config      # values for Settings -> Remote Crew (SSM)
  crew/scripts/crew.sh connect <id>       # or: loopback tunnel to the dashboard
  crew/scripts/crew.sh down <id>          # stop billing
EOF
