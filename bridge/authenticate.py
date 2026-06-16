"""
One-time SimpliSafe login -> saves a refresh token for the bridge to use.

Run this once (interactively):

    docker compose run --rm bridge-livingroom python authenticate.py

It prints a SimpliSafe login URL. Open it in a browser, log in, and you'll be
redirected to a "com.simplisafe.mobile://..." URL (your browser will likely show
it as a failed/blank page -- that's expected). Copy the FULL redirect URL from
the address bar and paste it back here. The refresh token is written to the
shared token file so every bridge container can use it.

Auth flow mirrors gilliginsisland/simplirtc and bachya/simplisafe-python.
"""
import asyncio
import json
import os
from urllib.parse import urlparse, parse_qs

from aiohttp import ClientSession
from simplipy import API
from simplipy.util.auth import (
    get_auth0_code_challenge,
    get_auth0_code_verifier,
    get_auth_url,
)

TOKEN_FILE = os.environ.get("TOKEN_FILE", "/config/simplisafe.token")


async def main():
    verifier = get_auth0_code_verifier()
    challenge = get_auth0_code_challenge(verifier)
    auth_url = get_auth_url(challenge)

    print("\n1) Open this URL in a browser and log in to SimpliSafe:\n")
    print(f"   {auth_url}\n")
    print("2) After approving, you'll be redirected to a 'com.simplisafe.mobile://...'")
    print("   URL. The page may look broken -- that's fine. Copy the FULL URL from")
    print("   the address bar.\n")
    raw = input("Paste the full redirect URL (or just the code): ").strip()

    code = raw
    if raw.startswith("com.simplisafe.mobile://"):
        code = parse_qs(urlparse(raw).query).get("code", [""])[0]
    if code.startswith("="):
        code = code[1:]
    if len(code) != 45:
        raise SystemExit(f"That doesn't look like a valid SimpliSafe code (len={len(code)}).")

    async with ClientSession() as session:
        api = await API.async_from_auth(code, verifier, session=session)
        token = api.refresh_token

    with open(TOKEN_FILE, "w") as f:
        json.dump(token, f)
    print(f"\nSaved refresh token to {TOKEN_FILE}. You're ready to run the bridge.")


if __name__ == "__main__":
    asyncio.run(main())
