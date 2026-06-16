# SimpliSafe LiveKit → HomeKit

Get your **newer SimpliSafe cameras into Apple HomeKit** — the ones that stream
over **LiveKit** (most current indoor cameras and some newer models), which the
existing community tools can't handle.

> **TL;DR:** SimpliSafe migrated its newer cameras from AWS Kinesis WebRTC to
> LiveKit. [`simplirtc`](https://github.com/gilliginsisland/simplirtc) + go2rtc
> already handle the *Kinesis* cameras. This project adds a small bridge that
> handles the *LiveKit* ones, so all of your SimpliSafe cameras can live in
> HomeKit through [go2rtc](https://github.com/AlexxIT/go2rtc).

---

## Why this exists

go2rtc can serve almost any camera to HomeKit, but it has **no LiveKit support**
([go2rtc#1680](https://github.com/AlexxIT/go2rtc/issues/1680)). The older
SimpliSafe cameras stream over AWS Kinesis WebRTC, which go2rtc *does* support
natively — that's what `simplirtc` produces. But the newer cameras return
`liveKitDetails` instead, and there's no native path for that.

This bridge fills the gap: it joins the camera's LiveKit room with a real LiveKit
client, decodes the H.264 video (+ Opus audio), re-encodes it, and hands it to
go2rtc as an ordinary stream.

## How it works

```
  SimpliSafe cloud (LiveKit room)
            │  wss://  (H.264 video + Opus audio)
            ▼
   ┌──────────────────┐   MPEG-TS over     ┌──────────┐   RTSP/SRTP   ┌──────────┐
   │  bridge (this)   │ ── localhost TCP ─▶ │  go2rtc  │ ───────────▶ │ HomeKit  │
   │ decode→re-encode │   (on demand)      │          │              │  (Home)  │
   └──────────────────┘                    └──────────┘              └──────────┘
```

Two things make it practical to run 24/7 on a small box (Raspberry Pi / Banana
Pi / any Docker host):

- **On-demand** — the bridge does nothing and joins no LiveKit room until go2rtc
  actually connects (i.e. until *you open the camera* in Home). When you close
  it, the LiveKit session is dropped and the box returns to idle (~0% CPU). One
  active stream is roughly ~1 CPU core.
- **Keep-warm (optional)** — a lightweight background poll keeps the camera
  "online" so the first frame shows up in a few seconds instead of ~20s. This
  costs no CPU — it never decodes until you actually view.

## Requirements

- A Linux host with **Docker** + **Docker Compose** (this is ideal to run on the
  same box as Homebridge/HOOBS/Home Assistant). Works on `arm64` and `amd64`.
- A SimpliSafe account with cameras and an active monitoring/recording plan that
  permits live view in the app.
- A few minutes. No coding required.

## Setup

```bash
git clone <your-fork-url> simplisafe-livekit-homekit
cd simplisafe-livekit-homekit

# 1) Build the bridge image
docker compose build

# 2) One-time SimpliSafe login (saves a refresh token to config/)
docker compose run --rm bridge-livingroom python authenticate.py
#    -> open the printed URL, log in, paste the redirect URL back

# 3) Discover your cameras (UUIDs, location id, and which are LiveKit)
docker compose run --rm bridge-livingroom python list_cameras.py
```

`list_cameras.py` prints something like:

```
Location: 123 Main St
  LOCATION id: 1234567

  - Living Room
      CAMERA id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      type:      LiveKit  -> use this bridge   (status=online)

  - Back Yard
      CAMERA id: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      type:      Kinesis  -> use simplirtc/go2rtc natively (no bridge needed)
```

Then:

4. **Edit `docker-compose.yml`** — set `CAMERA` and `LOCATION` for each LiveKit
   camera. Add one `bridge-*` service per camera with a unique `TCP_PORT`
   (8099, 8100, …). Template blocks are in the file.

5. **Create `config/go2rtc.yaml`** from the example:
   ```bash
   cp config/go2rtc.example.yaml config/go2rtc.yaml
   ```
   Add a `streams:` entry (pointing at the matching `TCP_PORT`) and a `homekit:`
   entry (with an 8-digit PIN you choose) for each camera.

6. **Start everything**
   ```bash
   docker compose up -d
   ```

7. **Pair in HomeKit.** Open `http://<host-ip>:1984` (the go2rtc web UI) → each
   camera has a HomeKit QR code. In the Home app: **+ → Add Accessory →
   More options…**, scan the QR (or enter the 8-digit PIN). Done.

(Optional: run on boot — see `go2rtc.service.example`.)

## What about the older (Kinesis) cameras?

Those don't need this bridge. Point go2rtc straight at them using
[`simplirtc`](https://github.com/gilliginsisland/simplirtc) (you'll need go2rtc
built with `simplirtc` installed):

```yaml
streams:
  back_yard:
    - echo:simplirtc --token /config/simplisafe.token stream --location <LOCATION> --camera <CAMERA_UUID>
```

## Tuning (env vars per bridge service)

| Variable        | Default | Notes |
|-----------------|---------|-------|
| `TARGET_FPS`    | `10`    | Output frame rate. Cost is mostly the LiveKit *decode*, so lowering this saves less than you'd think. |
| `THREADS`       | `2`     | libx264 thread cap. Keep low (1–2) so multiple cameras don't oversubscribe the CPU. |
| `WARM_INTERVAL` | `10`    | Seconds between keep-online polls. `0` disables keep-warm (idles cooler, but ~20s spinner on first view). |
| `ENABLE_AUDIO`  | `1`     | `0` for video only. |
| `AUDIO_SR`      | `16000` | Audio sample rate fed to HomeKit. |
| `PRESET`        | `ultrafast` | libx264 preset. |

## Troubleshooting

- **Black tile / no video:** the camera may be offline. Run `list_cameras.py`;
  if it shows `offline (404)` it's unplugged or a model without live view.
- **No audio:** make sure the live view is **unmuted** in the Home app (speaker
  icon). The go2rtc source *re-encodes* audio to AAC on purpose — copying AAC
  from MPEG-TS straight into RTSP fails with `AAC with no global headers`.
- **Slow first frame (~20s):** enable keep-warm (`WARM_INTERVAL=10`) and keep
  the small `-probesize/-analyzeduration` values in the go2rtc exec source.
- **High CPU / heat:** each *active* view ≈ ~1 core (LiveKit decode + re-encode).
  Keep `THREADS` low; don't run more simultaneous viewers than your box can take.
  At idle it should be ~0%.
- **See what's happening:** set `log: { level: debug }` in `go2rtc.yaml`; the
  bridge logs to `docker logs ss-bridge-<name>`.

## Limitations & honest caveats

- **It depends on SimpliSafe's cloud.** There is no local stream; if SimpliSafe
  changes their API, this can break (as has happened to similar projects).
- Re-encoding costs CPU while viewing (the LiveKit SDK only exposes *decoded*
  frames, so passthrough isn't possible).
- First-frame latency is a few seconds even warmed.

## Legal

This is an unofficial, community interoperability tool for viewing **cameras you
own and already pay for**. It is **not affiliated with, endorsed by, or
supported by SimpliSafe**. Use at your own risk; it may stop working at any time.
It includes no SimpliSafe code or assets and does not redistribute credentials.
Using a non-official client may be against SimpliSafe's Terms of Service — review
them and decide for yourself. Provided "as is" with no warranty (see `LICENSE`).

## Credits

- [go2rtc](https://github.com/AlexxIT/go2rtc) — the streaming engine + HomeKit server
- [simplisafe-python](https://github.com/bachya/simplisafe-python) — SimpliSafe auth/API
- [simplirtc](https://github.com/gilliginsisland/simplirtc) — prior art for the Kinesis cameras
- [LiveKit](https://github.com/livekit) — the Python client SDK used to pull the stream

## License

MIT — see [`LICENSE`](LICENSE).
