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
import errno
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
FD_WARN_PCT = int(os.environ.get("FD_WARN_PCT", "60"))       # log when fds exceed this % of the limit
FD_CHECK_INTERVAL = int(os.environ.get("FD_CHECK_INTERVAL", "300"))  # seconds; 0 disables
TOKEN_FILE = os.environ.get("TOKEN_FILE", "/config/simplisafe.token")
URL_BASE = "https://app-hub.prd.aser.simplisafe.com/v2"


def log(*a):
    print("[bridge]", *a, flush=True)


async def teardown_step(what, coro):
    """Await one teardown step so that nothing can skip the steps after it.

    Two traps this avoids:

    * `contextlib.suppress(Exception)` does NOT catch `CancelledError` — it
      derives from BaseException. So on the cancellation path (go2rtc
      reconnecting while a session is live, which cancels the old session), the
      first `await` in a cleanup block re-raises and every later step is
      skipped. That used to leave the LiveKit room connected, leaking its
      sockets for the lifetime of the process.
    * A teardown that raises for any other reason shouldn't strand the rest.

    Cancellation is swallowed here on purpose: `handle()` already treats a
    cancelled session as normal, and finishing the teardown matters more than
    propagating promptly.
    """
    try:
        await coro
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        # A viewer that hangs up mid-stream is the normal case, not an incident.
        # Logging it per session is how a chatty bridge fills a disk.
        pass
    except BaseException as e:  # noqa: BLE001 — deliberately includes CancelledError
        log(f"teardown: {what} failed: {e!r}")


def close_fd(fd, what):
    """Close a raw fd exactly once; `None` means already closed."""
    if fd is None:
        return None
    try:
        os.close(fd)
    except OSError as e:
        log(f"teardown: closing {what} failed: {e!r}")
    return None


def fd_count():
    """Open file descriptors for this process, or None if unavailable."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def fd_limit():
    import resource

    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


def die_on_fd_exhaustion(loop):
    """Exit on EMFILE instead of spinning in a traceback loop.

    asyncio's default response to `accept()` returning EMFILE is to log the
    failure and keep retrying. When the descriptors are gone for good that turns
    into an unbounded traceback loop: on 2026-07-29 this process wrote ~17 GB/hour
    into its container log and filled the *host's* root disk, all while the camera
    it was supposed to be serving stayed dark. Nobody noticed for days.

    Exiting is strictly better. `restart: unless-stopped` brings the bridge back
    with a clean descriptor table in about a second, and a container that restarts
    is something a health check can actually see.

    os._exit is deliberate — an orderly shutdown wants file descriptors, and by
    definition there are none left.
    """
    previous = loop.get_exception_handler()

    def handler(loop_, context):
        exc = context.get("exception")
        if isinstance(exc, OSError) and exc.errno in (errno.EMFILE, errno.ENFILE):
            log(
                f"FATAL: out of file descriptors ({fd_count()}/{fd_limit()}) — "
                f"exiting so the container restarts clean: {exc!r}"
            )
            os._exit(1)
        if previous is not None:
            previous(loop_, context)
        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(handler)


async def watch_fds():
    """Make descriptor growth visible long before it becomes fatal."""
    limit = fd_limit()
    warn_at = max(1, limit * FD_WARN_PCT // 100)
    peak = 0
    while True:
        await asyncio.sleep(FD_CHECK_INTERVAL)
        n = fd_count()
        if n is None:
            return
        # Only speak up past the threshold, and only on a new high, so a healthy
        # bridge stays quiet and a leaking one leaves a readable trail.
        if n >= warn_at and n > peak:
            peak = n
            log(f"WARNING: {n}/{limit} file descriptors in use ({n * 100 // limit}%)")


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
    ar_fd = None
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
                # Parent keeps only the write end. Note this is *after*
                # create_subprocess_exec; if that raises, the finally below
                # closes both ends.
                ar_fd = close_fd(ar_fd, "audio pipe read end")

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
                # NB: aw_fd is deliberately NOT closed here. If this task is
                # cancelled before its body ever runs, this finally never
                # executes and the fd leaks. run_session's finally owns it.

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

        # 1. Stop the feeder tasks and WAIT for them. Without the wait they are
        #    merely scheduled for cancellation, so their own finally blocks had
        #    not run by the time we closed the things they were using.
        for t in tasks:
            t.cancel()
        if tasks:
            await teardown_step(
                "feeder tasks", asyncio.gather(*tasks, return_exceptions=True)
            )

        # 2. Close the audio pipe. This gives ffmpeg EOF before we signal it,
        #    and it happens here rather than in feed_audio() so that a task
        #    cancelled before it started cannot strand the fd.
        aw_fd = close_fd(aw_fd, "audio pipe write end")
        ar_fd = close_fd(ar_fd, "audio pipe read end")

        # 3. ffmpeg.
        if ff:
            with contextlib.suppress(Exception):
                ff.terminate()
            await teardown_step("ffmpeg exit", asyncio.wait_for(ff.wait(), timeout=5))
            if ff.returncode is None:
                with contextlib.suppress(Exception):
                    ff.kill()
                await teardown_step("ffmpeg kill", ff.wait())

        # 4. Media streams, then the room. The room is last and is the
        #    expensive one — a LiveKit session holds a signalling websocket
        #    plus a spread of ICE/UDP sockets, so skipping this is what turned
        #    reconnect churn into a file-descriptor leak.
        if vs:
            await teardown_step("video stream", vs.aclose())
        if aus:
            await teardown_step("audio stream", aus.aclose())
        if room:
            await teardown_step("livekit room", room.disconnect())


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
        # close() only *schedules* the transport shutdown; without wait_closed()
        # the accepted socket's fd can outlive this handler, which is one leaked
        # fd per viewer connection.
        with contextlib.suppress(Exception):
            writer.close()
        await teardown_step("viewer socket", writer.wait_closed())
        if active.get("task") is cur:
            active["task"] = None  # don't pin the finished session's objects


async def main():
    die_on_fd_exhaustion(asyncio.get_running_loop())
    if WARM_INTERVAL > 0:
        asyncio.create_task(keep_warm())
    if FD_CHECK_INTERVAL > 0:
        asyncio.create_task(watch_fds())
    server = await asyncio.start_server(handle, "127.0.0.1", TCP_PORT)
    log(
        f"on-demand bridge audio={'on' if ENABLE_AUDIO else 'off'} "
        f"keepwarm={WARM_INTERVAL}s port={TCP_PORT} cam={CAM} "
        f"fds={fd_count()}/{fd_limit()}"
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
