# Announcement posts

Repo: https://github.com/BookOnTape/simplisafe-livekit-homekit

Status:
- [x] simplirtc issue — posted: https://github.com/gilliginsisland/simplirtc/issues/2
- [ ] r/homebridge — paste-ready below
- [ ] r/homeassistant — paste-ready below
- [ ] r/SimpliSafe (optional) — paste-ready below

Keep it factual and low-key; don't over-promise (these break when vendors change things).

---

## r/homebridge

**Title:** Newer SimpliSafe (LiveKit) cameras → HomeKit via go2rtc — open-source bridge

SimpliSafe quietly moved its newer cameras (current indoor SimpliCam models and some others) from AWS Kinesis WebRTC to **LiveKit**. The existing community path — [simplirtc](https://github.com/gilliginsisland/simplirtc) + go2rtc — still covers the older Kinesis cameras, but go2rtc has no LiveKit support, so the newer ones were stuck with no way into HomeKit.

I made a small bridge for them: it joins the camera's LiveKit room, decodes the H.264/Opus track, re-encodes, and feeds [go2rtc](https://github.com/AlexxIT/go2rtc) as an ordinary stream — which then serves it to HomeKit. It's **on-demand** (idles at ~0% CPU until you open the camera), with an optional **keep-warm** so first frame is a few seconds instead of ~20s, and audio works.

Runs in Docker on anything (Pi/Banana Pi/etc.), ideally on the same box as Homebridge/HOOBS. Free, MIT.

Repo + setup: https://github.com/BookOnTape/simplisafe-livekit-homekit

Caveats: unofficial, cloud-dependent, can break if SimpliSafe changes their API. Not affiliated with SimpliSafe.

---

## r/homeassistant

**Title:** Bridging newer SimpliSafe (LiveKit) cameras into go2rtc / HomeKit

For anyone with newer SimpliSafe cameras: they've migrated from Kinesis WebRTC to **LiveKit**, which go2rtc can't ingest natively ([go2rtc#1680](https://github.com/AlexxIT/go2rtc/issues/1680)). [simplirtc](https://github.com/gilliginsisland/simplirtc) handles the older Kinesis cameras; this fills the gap for the LiveKit ones.

It's a small sidecar container that joins the LiveKit room with a real client, decodes + re-encodes, and exposes the stream to go2rtc on demand (idle ~0% CPU until viewed; keep-warm option for fast start; audio included). From go2rtc it's available to HomeKit, or anything else go2rtc feeds.

Free/MIT, Docker, arm64+amd64: https://github.com/BookOnTape/simplisafe-livekit-homekit

Unofficial + cloud-dependent (may break on SimpliSafe API changes). Feedback/PRs welcome.

---

## r/SimpliSafe (optional)

**Title:** Got my newer SimpliSafe cameras showing up in Apple Home (open-source)

If you've tried to view your newer SimpliSafe cameras outside the app and hit a wall: the newer models stream over LiveKit, which the existing community tools don't handle. I put together a free, open-source bridge that gets them into Apple Home (HomeKit) via go2rtc, with live video + audio.

It runs on a small always-on box (Raspberry Pi etc.) in Docker. Setup is a README walkthrough — no coding.

https://github.com/BookOnTape/simplisafe-livekit-homekit

Heads-up: it's unofficial and not affiliated with SimpliSafe, it relies on their cloud, and it could break if they change things. Sharing in case it helps.

---

## Comment posted on simplirtc (for reference)

Posted at https://github.com/gilliginsisland/simplirtc/issues/2 — documents the
`liveKitDetails` validation error, explains the LiveKit migration, and links the bridge.
