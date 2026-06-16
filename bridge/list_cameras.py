"""
List your SimpliSafe cameras with their UUIDs, location id, and stream type.

    docker compose run --rm bridge-livingroom python list_cameras.py

For each camera it tells you whether it's:
  * LiveKit   -> use THIS bridge (needs a per-camera bridge service + go2rtc exec source)
  * Kinesis   -> handled natively by go2rtc via gilliginsisland/simplirtc (no bridge needed)
  * offline / unavailable (404) -> unplugged, or a model with no live-view endpoint

Use the printed UUID + location id in docker-compose.yml (CAMERA / LOCATION).
"""
import asyncio
import json
import os

from aiohttp import ClientSession
from simplipy import API
from simplipy.system.v3 import SystemV3

TOKEN_FILE = os.environ.get("TOKEN_FILE", "/config/simplisafe.token")
URL_BASE = "https://app-hub.prd.aser.simplisafe.com/v2"


async def classify(api, cam, loc):
    try:
        resp = await api.async_request(
            "get", f"cameras/{cam}/{loc}/live-view", url_base=URL_BASE
        )
    except Exception as e:  # noqa: BLE001
        if "404" in str(e):
            return "offline / unavailable (404) -- unplugged or unsupported"
        return f"error: {e}"
    if (resp.get("liveKitDetails") or {}).get("userToken"):
        return f"LiveKit  -> use this bridge   (status={resp.get('cameraStatus')})"
    return "Kinesis  -> use simplirtc/go2rtc natively (no bridge needed)"


async def main():
    with open(TOKEN_FILE) as f:
        refresh = json.load(f)
    async with ClientSession() as session:
        api = await API.async_from_refresh_token(refresh, session=session)
        systems = await api.async_get_systems()
        for sid, system in systems.items():
            if not isinstance(system, SystemV3):
                continue
            print(f"\nLocation: {system.address}")
            print(f"  LOCATION id: {sid}")
            for cid, cam in system.cameras.items():
                kind = await classify(api, cid, sid)
                print(f"\n  - {cam.name}")
                print(f"      CAMERA id: {cid}")
                print(f"      type:      {kind}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
