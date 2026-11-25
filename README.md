# kirocrew-on-lambda

A cheap remote [KiroCrew](https://download.crew.kiro.dev) crew on AWS Lambda.

- The **gateway** runs on a **Lambda MicroVM**. It installs the latest stable KiroCrew with the official installer.
- The **OpenCode harness** runs in a **Lambda Web Function** on free models (`opencode serve`), with conversations kept in S3.
- Your laptop connects over **AWS Systems Manager** (Settings, Remote Crew). No inbound port, no SSH key, no Secrets Manager, no KMS.

KiroCrew is not patched. The only seams used are its Agent Harness setting (`agent.acp_backend=opencode`) and the `OPENCODE_BIN` variable.

```
laptop KiroCrew --(SSM Run Command + Session Manager, your IAM)--> MicroVM: KiroCrew gateway (crew user)
                                                                     | harness = opencode, OPENCODE_BIN = acp_shim
                                                                     v
                       acp_shim (ACP on stdio) --> loopback relay (SigV4 with the VM's role)
                                                                     v
                       Lambda Web Function: opencode serve, free model, transcripts in S3
```

## Status

Tested in **us-east-1** only, in one account. Treat it as a working prototype.

| Verified | Not verified |
|---|---|
| A chat turn through the real gateway reaches the Web Function and answers | Remote Crew "Connect" from the laptop after the last fix (the VM registers and shows Online) |
| A second turn in the same session remembers the first | A deploy in a second AWS account |
| A new VM continues the same session (state restored from S3) | An 8-hour launch, cost per turn at scale |
| Turns that move between Web Function environments keep their history | Tool use on the remote harness (chat only) |

## Prerequisites

- AWS CLI v2 with credentials for an account where Lambda MicroVMs and Lambda Web Functions are available (tested in us-east-1)
- Node 22 or newer, `npm`, and [uv](https://docs.astral.sh/uv/)
- A free NVIDIA NIM API key for the Strands harness, stored as a free Standard-tier SSM String parameter:

```bash
aws ssm put-parameter --region us-east-1 --name /nim-free/api-key --type String --value <your-key>
```

Use another name with `NIM_KEY_PARAM=/your/name`. A Standard String is not encrypted with KMS, so limit `ssm:GetParameter` on it.

## Deploy

```bash
./deploy.sh          # checks prerequisites, bootstraps CDK if needed, deploys both stacks (about 8 minutes)
./deploy.sh check    # live check of the Web Function (spends a few free-model requests)
```

`deploy.sh` builds `webfn/dist` (arm64 OpenCode binary and boto3 for Python 3.15), deploys `OpenCodeWebFnStack`, then deploys `KiroCrewRemoteCrewStack`, whose image build runs the KiroCrew installer.

## Use

```bash
crew/scripts/crew.sh up                  # launch one crew; prints its id (about a minute to ready)
crew/scripts/crew.sh status [id]         # phase, installed KiroCrew version
crew/scripts/crew.sh remote-config       # the values for Settings -> Remote Crew
crew/scripts/crew.sh connect [id]        # alternative: loopback tunnel, then open http://127.0.0.1:5490/_signin
crew/scripts/crew.sh down <id>           # stop billing, save state, deregister the SSM node
```

**Connect from KiroCrew over SSM.** After `up`, run `crew.sh remote-config` and enter the values in Settings, Remote Crew, Add remote crew (connection method AWS SSM, remote user `crew`, remote port `5490`, remote kirocrew path `/usr/local/bin/kirocrew`). KiroCrew mints a token with Run Command and forwards the port with Session Manager, using your own AWS credentials. You need `ssm:SendCommand`, `ssm:GetCommandInvocation` and `ssm:StartSession` on that node, and the Session Manager plugin installed locally.

Settings (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `CREW_LIFETIME_SECS` | 3600 | VM lifetime cap; the platform maximum is 28800 (8 h) |
| `CREW_IDLE_SECS` | the lifetime | Suspend after this many idle seconds. A suspended VM freezes the SSM agent and SSM does not wake it |
| `CREW_HARNESS` | the saved one | `opencode` or `strands` for this boot |
| `NIM_KEY_PARAM` | `/nim-free/api-key` | SSM parameter that holds the NIM key |
| `KIROCREW_CHANNEL` | `stable` | Docker build argument for the installer channel. Add `--version X.Y.Z` to the installer line in `crew/image/Dockerfile` to pin a release |

One crew at a time: two VMs would write the same state prefix. So `[id]` is optional and defaults to the one live crew.

## Cost

us-east-1, Arm, list prices (Lambda pricing page, Price List API, AWS blog for SSM). Estimates, not bills.

| Item | Rate |
|---|---|
| MicroVM, 2 GB and 1 vCPU | about $0.126 per hour while running |
| Web Function | $0.30 per million requests plus active CPU ($0.0000276944 per vCPU-second) and memory ($0.0000036667 per GB-second) |
| MicroVM snapshots | $0.00155 per GB read, $0.0038 per GB written, $0.08 per GB-month image storage |
| Session Manager, Run Command | $0.05 per connection, $0.002 per command |
| Free models | $0 |

Example: 2 hours a day for 20 days is about $5.04 for the MicroVM and $1.04 for SSM, plus pennies for the Web Function.

## Security

What protects it:

- **No secrets in the repo or the image.** The NIM key is read from SSM at boot with the VM's role. The gateway's vault and signing keys are never copied to S3 (the state sync skips them by name and location).
- **Web Function endpoint uses IAM auth.** The crew's role may invoke that one endpoint only. Its own role may read and write `sessions/*` in one private bucket.
- **MicroVM access** needs a token that lasts 15 minutes and covers only ports 8080 and 8081.
- **SSM** access is an IAM decision on the node. The activation is single-use and expires in an hour; its code is never printed or logged.
- **One root process.** The SSM agent must run as root, and the VM forbids `sudo` (no-new-privileges). A small helper (`crew/image/guest/ssm-helper.py`) listens on a unix socket, accepts one request, validates every field against a strict pattern, registers once, and starts the agent. The hook server and the gateway run as the `crew` user.
- **Buckets** block public access, require TLS and expire or retain by purpose. The Web Function's transcripts expire after 14 days.
- **Model allowlist.** The Web Function accepts only two free models whose provider states zero retention and no training.
- No dependency vulnerabilities reported by `npm audit` (dev tooling only).

What to know:

- Conversation text (prompts and answers) is stored in S3 and sent to the free model provider. Do not use it for confidential work.
- The hook server and the dashboard forwarder bind all interfaces inside the VM. They are reachable only through the platform proxy, which requires the token above.
- The loopback relay can be called by any process in the VM, including the agent's own tools. It can only reach the one Web Function.
- The installer is downloaded at build time and verifies a signed release feed. Pin a version for reproducible builds.
- `python3.15` on Web Functions is a public preview.


## Layout

```
deploy.sh                one command: prerequisites, bootstrap, both stacks
webfn/                   Web Function: app/main.py, build.sh, app.ts (CDK), check.py (live check)
crew/image/              MicroVM image: Dockerfile and guest/ (hooks, state sync, ACP shim, SSM helper)
crew/infra.ts            MicroVM stack: image, roles, state bucket
crew/scripts/            crew.sh (launcher), tunnel.py (loopback tunnel)
crew/test/               tests and the mutation check
```

## Tests

```bash
cd crew && uv run --no-project --with pytest --with hypothesis --with boto3 python -m pytest test -q
uv run --no-project --with pytest --with hypothesis --with boto3 python test/mutation_check.py   # each mutant must be killed
```

The tests use real sockets and subprocesses, no mocks. `crew/test/live_sign_check.py` and `live_hub_mint_check.py` are live checks that spend a free-model request.

## Limits

- Chat only: the shim offers no tools, so KiroCrew's approval gate has nothing to gate on this path.
- Strands stays on the MicroVM. Its harness package needs a dependency without a Python 3.15 wheel, and Web Functions offer only 3.15 for Python.
- OpenCode's free tier has a usage cap and its model catalog changes. Redeploying the MicroVM might reset the free-tier daily limit, but this is not tested. The Web Function fails fast with HTTP 429 when the cap is reached.
- Web Function limits: 512 MB package, 512 MB `/tmp`, 2 vCPU, 2 GB, 900 s per request, arm64 only, zip packages only.

## Remove

```bash
(cd crew && ../node_modules/.bin/cdk destroy) && (cd webfn && ../node_modules/.bin/cdk destroy)
```

The crew's state bucket is retained on purpose; empty and delete it by hand. Image storage has a one-week minimum.
