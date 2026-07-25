#!/usr/bin/env python3
"""On-demand legacy-FLV relay for `simplisafe`-provider cameras (Video Doorbell
Pro). Those cameras 404 on the v2 WebRTC live-view endpoint, but still serve
h264+AAC over the legacy media.simplisafe.com FLV endpoint (what
homebridge-simplisafe3 used). Mints a fresh access token from the shared
refresh token, then execs ffmpeg relaying FLV -> go2rtc's {output} RTSP.

Usage (go2rtc.yaml):  exec:python3 /config/flv_stream.py <camera-uuid> {output}
"""
import asyncio
import os
import sys

from aiohttp import ClientSession
from simplirtc import SimpliRTC

CAMERA, OUTPUT = sys.argv[1], sys.argv[2]


async def get_access_token() -> str:
    async with ClientSession() as session:
        api = await SimpliRTC.async_from_token_file(
            "/config/simplisafe.token", session=session
        )
        return api.access_token


token = asyncio.run(get_access_token())
url = f"https://media.simplisafe.com/v1/{CAMERA}/flv?x=1920&audioEncoding=AAC"
# -c:v copy      : already h264
# -c:a aac 16k/1 : re-encode audio for RTSP global headers (same gotcha as the
#                  LiveKit bridges - copying AAC into RTSP breaks HomeKit audio)
os.execvp(
    "ffmpeg",
    [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
        "-rw_timeout", "30000000",
        "-headers", f"Authorization: Bearer {token}\r\n",
        "-i", url,
        "-c:v", "copy",
        "-c:a", "aac", "-ar", "16000", "-ac", "1",
        "-f", "rtsp", OUTPUT,
    ],
)
