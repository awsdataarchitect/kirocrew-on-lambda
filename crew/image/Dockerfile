# (OpenCode runs remotely in a Lambda Web Function through an ACP shim; the image still carries both harnesses.)
# KiroCrew gateway on a Lambda MicroVM with two harnesses: Strands (LiteLLM provider -> NVIDIA NIM) and OpenCode (free Zen models).
# Built by Lambda (CreateMicrovmImage) from this directory zipped.
# No secret is baked in: the NIM key is read from SSM at /run through the execution role; OpenCode's free models need no key.
# The crew home is mirrored to an S3 bucket over the internet by guest/statesync.py (an S3 Files mount cannot be combined
# with internet egress; see README).

# Node (>= 22) for the Strands CLI. The CLI itself is installed further down, at the version the installed KiroCrew has verified.
FROM public.ecr.aws/docker/library/node:22-bookworm-slim AS node

# OpenCode is one self-contained executable; the npm package only fetches it. Install it here and keep that single file,
# so the image does not carry the package twice. KiroCrew launches OpenCode on major 1 only and refuses any other
# (acp/harness/opencode.py, _OPENCODE_TESTED_MAJOR), so the pin stays inside 1.x.
FROM public.ecr.aws/docker/library/node:22-bookworm-slim AS opencode
RUN npm install -g --no-fund --no-audit opencode-ai@1.19.5 \
    && cp -L "$(npm root -g)/opencode-ai/bin/opencode.exe" /opencode \
    && /opencode --version | grep -E '^1\.19\.5$'

FROM public.ecr.aws/amazonlinux/amazonlinux:2023

# openssl: the KiroCrew installer uses it to verify the signed release manifest.
RUN dnf -y install git procps-ng shadow-utils tar gzip findutils which python3-pip openssl \
    && dnf clean all && rm -rf /var/cache/dnf

# tini is PID 1 so the gateway's orphaned children are reaped (static binary, checksum from the upstream release).
RUN curl -fsSL -o /usr/local/bin/tini https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-arm64 \
    && echo "eae1d3aa50c48fb23b8cbdf4e369d0910dfc538566bfd09df89a774aa84a48b9  /usr/local/bin/tini" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/tini

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

# The second harness. Runs on AL2023 here, not only in the build stage above: a missing shared library shows up now.
COPY --from=opencode /opencode /usr/local/bin/opencode
RUN chmod 0755 /usr/local/bin/opencode && opencode --version | grep -E '^1\.19\.5$'

# Python 3.12 for KiroCrew comes from uv's managed builds, installed once for every user.
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_CACHE_DIR=/opt/uv-cache UV_LINK_MODE=copy
RUN pip3 install --no-cache-dir uv \
    && uv python install 3.12 \
    && uv venv -q --python 3.12 /opt/venv \
    && chmod -R a+rX /opt/python /opt/venv

# Python for the hook server and the ACP shim: boto3 for AWS calls, aiohttp for the dashboard forwarder. KiroCrew itself is NOT
# installed here: it comes from the official installer below, in its own venv.
RUN uv pip install -q --no-cache --python /opt/venv/bin/python 'boto3>=1.34,<2' aiohttp \
    && /opt/venv/bin/python -c 'import boto3, aiohttp'

# Remote Crew over SSM. The VM runs with no-new-privileges (real sudo refuses to start), so nothing here escalates: the SSM agent
# runs as root (it must), started by a tiny root-side helper that takes one kind of request on a unix socket; everything else,
# including the hook server and the gateway, runs as crew. `sudo` is a stand-in that only runs the hub's `sudo -u crew -i bash`.
RUN dnf -y install amazon-ssm-agent util-linux && dnf clean all && rm -rf /var/cache/dnf \
    && mkdir -p /var/lib/amazon/ssm /var/log/amazon/ssm /etc/amazon/ssm \
    && printf 'export PATH=/opt/venv/bin:/usr/local/bin:$PATH\nexport KIROCREW_HOME=/home/crew/.kiro/crew\n' > /etc/profile.d/kirocrew.sh
# /usr/bin, not /usr/local/bin: SSM Run Command's PATH has no /usr/local/bin (measured 2026-11-23: "sudo: command not found").
COPY guest/sudo-compat /usr/bin/sudo
RUN chmod 0755 /usr/bin/sudo
RUN useradd --uid 1000 --create-home --home-dir /home/crew --shell /bin/bash crew

# Governance ceiling, owned by root so the crew user (and the agent) cannot edit it. It denies the network
# tools that the gateway would otherwise auto-allow, which the laptop-side approver never sees.
RUN mkdir -m 0755 /etc/kirocrew-policy
COPY --chown=root:root guest/security_policy.json /etc/kirocrew-policy/security_policy.json
RUN chmod 0644 /etc/kirocrew-policy/security_policy.json

# KiroCrew: the official installer (https://download.crew.kiro.dev/cli.sh). It verifies the channel feed's RSA-SHA256 signature and
# the wheel's SHA-256 before installing. Installed OUTSIDE the crew home (KIROCREW_HOME / KIROCREW_VENV below) so the state sync
# never mirrors it, then made read-only. The channel is a build argument: `stable` is the latest stable release at build time;
# pin an exact release by adding `--version X.Y.Z` to the installer line. --no-kas skips the Kiro Agent engine: the harnesses used
# here (Strands, OpenCode) do not need it, and the image stays small.
ARG KIROCREW_CHANNEL=stable
RUN install -d -o crew -g crew /opt/kc
USER crew
RUN set -eux; \
    curl -fsSL --proto '=https' https://download.crew.kiro.dev/cli.sh -o /tmp/cli.sh; \
    test -s /tmp/cli.sh; \
    UV_CACHE_DIR=/tmp/uv-cache-kc KIROCREW_HOME=/opt/kc/home KIROCREW_VENV=/opt/kc/venv sh /tmp/cli.sh --channel "$KIROCREW_CHANNEL" --no-kas; \
    rm -rf /tmp/cli.sh /tmp/uv-cache-kc; \
    /opt/kc/venv/bin/kirocrew --version
USER root
RUN ln -sf /opt/kc/venv/bin/kirocrew /usr/local/bin/kirocrew && chown -R root:root /opt/kc && chmod -R a+rX /opt/kc \
    && rm -rf /home/crew/.local/bin/kirocrew

# The Strands CLI version this KiroCrew has verified (read from the installed release, so a newer KiroCrew brings its own).
RUN set -eux; \
    CLI=$(/opt/kc/venv/bin/python -c 'from kiro_crew.agent_sdk.backends_launch import STRANDS_VERIFIED_VERSION as v; print(v)'); \
    echo "Strands CLI $CLI"; \
    npm install -g --no-fund --no-audit "@strands-agents/cli@$CLI"; \
    ln -sf ../lib/node_modules/@strands-agents/cli/bin/strands.js /usr/local/bin/strands; \
    strands --version

USER crew
ENV HOME=/home/crew \
    KIROCREW_HOME=/home/crew/.kiro/crew \
    KIROCREW_PYTHON=/opt/kc/venv/bin/python

# The Strands project venv (<crew home>/pi-gate/strands/.venv, strands-harness at the version the installed KiroCrew pins). It is image
# content, not state, and guest/statesync.py never mirrors a .venv. The [litellm] extra is required: without it the
# project fails to load and KiroCrew's gate probe times out after 90s.
# Created by path only: no `kirocrew` command runs at build time, so the snapshot carries no per-install state
# (vault key, dashboard secret) that VMs would share.
RUN set -eux; \
    D="$KIROCREW_HOME/pi-gate/strands"; \
    export UV_CACHE_DIR=/tmp/uv-cache-crew; \
    install -d -m 700 "$HOME/.kiro" "$KIROCREW_HOME" "$KIROCREW_HOME/pi-gate" "$D"; \
    uv venv -q --python 3.12 "$D/.venv"; \
    REQ=$(/opt/kc/venv/bin/python -c 'from kiro_crew.agent_sdk.backends_launch import STRANDS_PYTHON_REQUIREMENT as r; print(r)'); \
    echo "Strands harness requirement: $REQ"; \
    uv pip install -q --no-cache --python "$D/.venv/bin/python" "$(printf '%s' "$REQ" | sed 's/^strands-harness/strands-harness[litellm]/')"; \
    "$D/.venv/bin/python" -c 'import strands_harness, litellm'; \
    rm -rf /tmp/uv-cache-crew

WORKDIR /home/crew

COPY --chown=crew:crew guest/hooks.py guest/statesync.py guest/acp_shim.py guest/opencode-remote /opt/guest/
RUN chmod 0755 /opt/guest/opencode-remote
# Root-owned, not writable by crew: the helper and the entrypoint that starts it.
USER root
COPY guest/ssm-helper.py guest/entrypoint.sh /opt/guest-root/
RUN chmod 0755 /opt/guest-root/ssm-helper.py /opt/guest-root/entrypoint.sh && chown -R root:root /opt/guest-root

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin \
    KIROCREW_SECURITY_POLICY=/etc/kirocrew-policy/security_policy.json \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 8080: lifecycle hooks + crew status. 8081: forwarder to the gateway dashboard on loopback.
EXPOSE 8080 8081
USER root
ENTRYPOINT ["tini", "--", "/opt/guest-root/entrypoint.sh"]
