"""
SimpliSafe LiveKit -> go2rtc bridge.

SimpliSafe's newer indoor cameras (and some others) stream over LiveKit, which
go2rtc cannot ingest natively. This service joins the camera's LiveKit room,
decodes the H.264 video (+ Opus audio) track, re-encodes it, and serves it as
an MPEG-TS stream that go2rtc pulls on demand.

Design goals:
  * On-demand: it does NO work (and joins NO room) until go2rtc actually connects
    to its TCP port (which only happens when something views the camera). On
    teardown it drops the LiveKit session so the box returns to idle.
  * Keep-warm (optional): a lightweight background poll keeps the camera "online"
    so the first frame arrives in a few seconds instead of ~20s. This costs no
    CPU (it's just an API call) -- it never decodes/transcodes until a viewer
    connects.

Everything is configured via environment variables -- see README.md.
"""
import asyncio
import contextlib
import json
import os
import time

import aiofiles
from aiohttp import ClientSession
from livekit import rtc
from simplipy import API

CAM = os.environ["CAMERA"]               # camera UUID (see list_cameras.py)
LOC = os.environ["LOCATION"]             # location / system id (see list_cameras.py)
TCP_PORT = int(os.environ.get("TCP_PORT", "8099"))
TARGET_FPS = int(os.environ.get("TARGET_FPS", "10"))
THREADS = os.environ.get("THREADS", "2")
PRESET = os.environ.get("PRESET", "ultrafast")
ENABLE_AUDIO = os.environ.get("ENABLE_AUDIO", "1") not in ("0", "false", "False")
AUDIO_SR = int(os.environ.get("AUDIO_SR", "16000"))
WARM_INTERVAL = int(os.environ.get("WARM_INTERVAL", "10"))   # seconds; 0 disables keep-warm
TOKEN_FILE = os.environ.get("TOKEN_FILE", "/config/simplisafe.token")
URL_BASE = "https://app-hub.prd.aser.simplisafe.com/v2"


def log(*a):
    print("[bridge]", *a, flush=True)


async def load_api(session):
    async with aiofiles.open(TOKEN_FILE) as f:
        refresh = json.loads(await f.read())
    api = await API.async_from_refresh_token(refresh, session=session)

    async def _save(rt):
        async with aiofiles.open(TOKEN_FILE, "w") as f:
            await f.write(json.dumps(rt))

    api.add_refresh_token_callback(lambda rt: asyncio.ensure_future(_save(rt)))
    # Only write back if the token actually rotated -- avoids races between the
    # keep-warm loop and an active session both touching the file.
    if api.refresh_token != refresh:
        await _save(api.refresh_token)
    return api


async def live_view(api):
    resp = await api.async_request(
        "get", f"cameras/{CAM}/{LOC}/live-view", url_base=URL_BASE
    )
    return resp, (resp.get("liveKitDetails") or {})


async def get_creds(api):
    for i in range(30):
        resp, lk = await live_view(api)
        if resp.get("cameraStatus") == "online" and lk.get("userToken"):
            return lk["liveKitURL"], lk["userToken"]
        log(f"wake poll {i}: status={resp.get('cameraStatus')}")
        await asyncio.sleep(2)
    raise RuntimeError("camera never came online")


async def keep_warm():
    """Periodically request live-view so the camera stays 'online'. No transcode."""
    last = None
    while True:
        try:
            async with ClientSession() as session:
                api = await load_api(session)
                while True:
                    resp, _ = await live_view(api)
                    st = resp.get("cameraStatus")
                    if st != last:
                        log(f"keep-warm: camera {st}")
                        last = st
                    await asyncio.sleep(WARM_INTERVAL)
        except Exception as e:  # noqa: BLE001
            log(f"keep-warm error (retry 10s): {e!r}")
            last = None
            await asyncio.sleep(10)


async def run_session(reader, writer):
    peer = writer.get_extra_info("peername")
    log(f"viewer connected {peer}")
    loop = asyncio.get_running_loop()
    room = None
    vs = None
    aus = None
    ff = None
    aw_fd = None
    tasks = []
    try:
        async with ClientSession() as session:
            api = await load_api(session)
            url, token = await get_creds(api)
            room = rtc.Room()
            tr = {}
            ready = asyncio.Event()

            @room.on("track_subscribed")
            def _ts(track, pub, p):
                if track.kind == rtc.TrackKind.KIND_VIDEO and "v" not in tr:
                    tr["v"] = track
                    ready.set()
                elif track.kind == rtc.TrackKind.KIND_AUDIO and "a" not in tr:
                    tr["a"] = track

            @room.on("disconnected")
            def _dc(*a):
                ready.set()

            await room.connect(url, token)
            try:
                await asyncio.wait_for(ready.wait(), timeout=60)
            except asyncio.TimeoutError:
                raise RuntimeError("no video track in 60s")
            if "v" not in tr:
                raise RuntimeError("room disconnected before video")
            await asyncio.sleep(0.5)  # give the audio track a moment to subscribe too
            have_audio = ENABLE_AUDIO and "a" in tr

            vs = rtc.VideoStream(tr["v"])
            first = await vs.__anext__()
            fr = first.frame.convert(rtc.VideoBufferType.I420)
            w, h = fr.width, fr.height

            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-thread_queue_size", "512",
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}",
                "-r", str(TARGET_FPS), "-i", "pipe:0",
            ]
            pass_fds = ()
            if have_audio:
                ar_fd, aw_fd = os.pipe()
                pass_fds = (ar_fd,)
                cmd += [
                    "-thread_queue_size", "512",
                    "-f", "s16le", "-ar", str(AUDIO_SR), "-ac", "1", "-i", f"pipe:{ar_fd}",
                ]
            cmd += ["-map", "0:v"]
            if have_audio:
                cmd += ["-map", "1:a"]
            cmd += [
                "-c:v", "libx264", "-preset", PRESET, "-tune", "zerolatency",
                "-threads", str(THREADS), "-g", str(TARGET_FPS * 2), "-bf", "0",
                "-pix_fmt", "yuv420p",
            ]
            if have_audio:
                cmd += ["-c:a", "aac", "-b:a", "48k", "-ar", str(AUDIO_SR), "-ac", "1"]
            cmd += ["-f", "mpegts", "pipe:1"]

            log(f"streaming {w}x{h}@{TARGET_FPS}fps audio={'on' if have_audio else 'off'} to viewer")
            ff = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                pass_fds=pass_fds,
            )
            if have_audio:
                os.close(ar_fd)  # parent keeps only the write end

            stop = asyncio.Event()
            interval = 1.0 / TARGET_FPS

            async def feed_video():
                last = 0.0
                try:
                    ff.stdin.write(fr.data.tobytes())
                    await ff.stdin.drain()
                    last = time.monotonic()
                    async for fev in vs:
                        if stop.is_set():
                            break
                        now = time.monotonic()
                        if now - last < interval:
                            continue  # drop frame to hit TARGET_FPS
                        last = now
                        f2 = fev.frame.convert(rtc.VideoBufferType.I420)
                        ff.stdin.write(f2.data.tobytes())
                        await ff.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    stop.set()
                    with contextlib.suppress(Exception):
                        ff.stdin.close()

            async def feed_audio():
                nonlocal aus
                try:
                    aus = rtc.AudioStream(tr["a"], sample_rate=AUDIO_SR, num_channels=1)
                    async for aev in aus:
                        if stop.is_set():
                            break
                        await loop.run_in_executor(None, os.write, aw_fd, bytes(aev.frame.data))
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        os.close(aw_fd)

            async def relay():
                try:
                    while True:
                        chunk = await ff.stdout.read(65536)
                        if not chunk:
                            break
                        writer.write(chunk)
                        await writer.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    stop.set()

            async def watch_disconnect():
                try:
                    while True:
                        d = await reader.read(4096)
                        if not d:
                            break
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    stop.set()

            tasks = [
                asyncio.create_task(feed_video()),
                asyncio.create_task(relay()),
                asyncio.create_task(watch_disconnect()),
            ]
            if have_audio:
                tasks.append(asyncio.create_task(feed_audio()))
            await stop.wait()
    finally:
        log("viewer gone; tearing down")
        for t in tasks:
            t.cancel()
        if ff:
            with contextlib.suppress(Exception):
                ff.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ff.wait(), timeout=5)
        if vs:
            with contextlib.suppress(Exception):
                await vs.aclose()
        if aus:
            with contextlib.suppress(Exception):
                await aus.aclose()
        if room:
            with contextlib.suppress(Exception):
                await room.disconnect()


# Only one active viewing session per camera. If go2rtc reconnects, cancel the old.
active = {"task": None}


async def handle(reader, writer):
    old = active.get("task")
    if old and not old.done():
        old.cancel()
        with contextlib.suppress(Exception):
            await old
    cur = asyncio.create_task(run_session(reader, writer))
    active["task"] = cur
    try:
        await cur
    except asyncio.CancelledError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"session error: {e!r}")
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def main():
    if WARM_INTERVAL > 0:
        asyncio.create_task(keep_warm())
    server = await asyncio.start_server(handle, "127.0.0.1", TCP_PORT)
    log(
        f"on-demand bridge audio={'on' if ENABLE_AUDIO else 'off'} "
        f"keepwarm={WARM_INTERVAL}s port={TCP_PORT} cam={CAM}"
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
