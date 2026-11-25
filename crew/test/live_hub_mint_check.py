"""Live check of the Remote Crew hub's own SSM token mint against a crew VM. Prints only the shape of the token, never the token.

Run with KiroCrew's bundled interpreter so the hub's real code runs:
  <KiroCrew app>/Contents/Resources/backend-dist/kirocrew-backend-arm64/bin/python3 -I crew/test/live_hub_mint_check.py mi-xxxxxxxx
"""
import asyncio
import sys

from kiro_crew.instances.ssm_token_mint import mint_remote_token_ssm

target = sys.argv[1]
token = asyncio.run(mint_remote_token_ssm(target, aws_region="us-east-1", ssm_run_as="crew", remote_bin="/opt/venv/bin/kirocrew", remote_port=5490))
parts = token.split(".")
print(f"minted a token: {len(token)} chars, {len(parts)} dot-separated parts, starts with {token[:3]}... (value not shown)")
