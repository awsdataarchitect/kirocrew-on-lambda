# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp>=3.10", "boto3>=1.40"]
# ///
"""Laptop side of the KiroCrew MicroVM: 127.0.0.1:5490 -> the VM's gateway forwarder (port 8081).

Usage: uv run microvm/scripts/tunnel.py <microvm-id>

Why this exists: a MicroVM has no sshd or SSM agent, so KiroCrew's SSH/SSM Remote Crew transport cannot
reach it. A browser also cannot send the X-aws-proxy-auth header the endpoint requires. This process mints
short-lived tokens with your own AWS credentials (lambda:CreateMicrovmAuthToken), adds them, and forwards
HTTP and WebSocket traffic. It listens on loopback only, on 5490 because the gateway trusts that Origin.
"""
import asyncio
import os
import random
import sys
import time

import aiohttp
import boto3
from aiohttp import web

REGION = "us-east-1"
PORT = int(os.environ.get("TUNNEL_PORT", "5490"))  # 5490 is the Origin the gateway trusts; override only for tests
GUEST_PORT = "8081"
TOKEN_MINUTES = 15
MAX_PROXY_RETRIES = 8
UPSTREAM_CONCURRENCY = 6  # in-flight upstream requests; SSE streams hold a slot, so keep headroom
GATE = asyncio.Semaphore(UPSTREAM_CONCURRENCY)
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "proxy-connection", "host", "content-length"}
# content-encoding is NOT hop-by-hop: the body is forwarded undecoded (auto_decompress=False), so the header must stay.

lm = boto3.client("lambda-microvms", region_name=REGION)
vm_id = sys.argv[1]
endpoint = lm.get_microvm(microvmIdentifier=vm_id)["endpoint"]
_tok = ["", 0.0]


def token() -> str:
    if time.monotonic() > _tok[1]:
        r = lm.create_microvm_auth_token(microvmIdentifier=vm_id, expirationInMinutes=TOKEN_MINUTES,
                                         allowedPorts=[{"port": 8080}, {"port": 8081}])
        _tok[0] = r["authToken"]["X-aws-proxy-auth"]
        _tok[1] = time.monotonic() + (TOKEN_MINUTES - 3) * 60
    return _tok[0]


def upstream_headers(req: web.Request, port: str) -> dict[str, str]:
    h = {k: v for k, v in req.headers.items() if k.lower() not in HOP and not k.lower().startswith("sec-websocket-")}
    h["X-aws-proxy-auth"] = token()
    h["X-aws-proxy-port"] = port
    return h


async def ws_proxy(req: web.Request) -> web.WebSocketResponse:
    ours = web.WebSocketResponse()
    await ours.prepare(req)
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"wss://{endpoint}{req.path_qs}", headers=upstream_headers(req, GUEST_PORT)) as theirs:
            async def up():
                async for m in ours:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        await theirs.send_str(m.data)
                    elif m.type == aiohttp.WSMsgType.BINARY:
                        await theirs.send_bytes(m.data)
                    else:
                        break
            async def down():
                async for m in theirs:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        await ours.send_str(m.data)
                    elif m.type == aiohttp.WSMsgType.BINARY:
                        await ours.send_bytes(m.data)
            done, pending = await asyncio.wait({asyncio.create_task(up()), asyncio.create_task(down())},
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    return ours


async def http_proxy(req: web.Request) -> web.StreamResponse:
    if req.headers.get("Upgrade", "").lower() == "websocket":
        return await ws_proxy(req)
    body = await req.read()
    async with aiohttp.ClientSession(auto_decompress=False) as s:
        for attempt in range(MAX_PROXY_RETRIES + 1):
            # The MicroVM proxy caps requests per second (a browser loading ~80 modules trips it) and answers
            # 429 + x-aws-proxy-error before the request reaches the app, so a retry is safe for any method.
            async with GATE:
                async with s.request(req.method, f"https://{endpoint}{req.path_qs}", data=body or None,
                                     headers=upstream_headers(req, GUEST_PORT), allow_redirects=False) as r:
                    throttled = r.status == 429 and r.headers.get("x-aws-proxy-error", "").startswith("LIMIT_EXCEEDED")
                    if not (throttled and attempt < MAX_PROXY_RETRIES):
                        resp = web.StreamResponse(status=r.status)
                        for k, v in r.headers.items():
                            if k.lower() not in HOP:
                                resp.headers.add(k, v)
                        await resp.prepare(req)
                        async for chunk in r.content.iter_any():  # streams SSE as it arrives
                            await resp.write(chunk)
                        await resp.write_eof()
                        return resp
                    delay = float(r.headers.get("Retry-After", "1") or 1)
            await asyncio.sleep(min(max(delay, 0.5), 5.0) + random.random() * 0.5)
    raise AssertionError("unreachable")


READY_TIMEOUT = 240


async def guest_json(s: aiohttp.ClientSession, method: str, path: str) -> tuple[int, dict]:
    """Call one of the guest's own routes (port 8080). Returns (status, body); (0, {}) when it cannot be reached yet."""
    try:
        async with s.request(method, f"https://{endpoint}{path}", headers={"X-aws-proxy-auth": token()},
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
            try:
                return r.status, await r.json(content_type=None)
            except Exception:
                return r.status, {}
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return 0, {}


async def wait_ready() -> bool:
    """Poll /crew/status until the guest reports phase 'ready' (a fresh VM needs about a minute)."""
    deadline = time.monotonic() + READY_TIMEOUT
    last = ""
    async with aiohttp.ClientSession() as s:
        while time.monotonic() < deadline:
            code, doc = await guest_json(s, "GET", "/crew/status")
            phase = doc.get("phase", "") if code == 200 else f"HTTP {code}"
            if phase != last:
                print(f"waiting for VM: {phase}", flush=True)
                last = phase
            if code == 200 and phase == "ready":
                return True
            await asyncio.sleep(3)
    return False


async def signin(_req: web.Request) -> web.Response:
    """Mint a fresh dashboard sign-in token and redirect to the dashboard with it. Open http://127.0.0.1:5490/_signin."""
    async with aiohttp.ClientSession() as s:
        code, doc = await guest_json(s, "POST", "/crew/token")
    if code == 200 and doc.get("token"):
        raise web.HTTPFound(f"/?token={doc['token']}")
    return web.Response(status=503, text=f"VM not ready for sign-in (guest answered HTTP {code}); retry in a few seconds\n")


async def main() -> None:
    app = web.Application()
    app.router.add_get("/_signin", signin)  # not /signin: the dashboard may own that path
    app.router.add_route("*", "/{tail:.*}", http_proxy)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    print(f"tunnel up on http://127.0.0.1:{PORT}/ for VM {vm_id}", flush=True)
    if not await wait_ready():
        print(f"VM did not report ready within {READY_TIMEOUT}s; the tunnel stays up. Open http://127.0.0.1:{PORT}/_signin "
              "once `crew.sh status` shows ready.", flush=True)
    else:
        print(f"ready. Sign in (fresh token each time): http://127.0.0.1:{PORT}/_signin", flush=True)
    await asyncio.Event().wait()


asyncio.run(main())
