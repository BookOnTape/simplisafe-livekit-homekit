# Draft announcement posts

Edit the `<repo-url>` and tone to taste before posting. Keep it factual and
low-key (don't over-promise; these projects break when vendors change things).

---

## r/homebridge / r/homeassistant / r/smarthome

**Title:** Get newer SimpliSafe (LiveKit) cameras into HomeKit via go2rtc

I put together a small bridge for the SimpliSafe cameras that the existing tools
*don't* cover. Background: SimpliSafe moved its newer cameras from AWS Kinesis
WebRTC to **LiveKit**. The great existing project
[`simplirtc`](https://github.com/gilliginsisland/simplirtc) + go2rtc handles the
Kinesis cameras, but go2rtc has no LiveKit support, so the newer indoor cameras
were stuck.

This bridge joins the camera's LiveKit room, decodes the H.264/Opus track,
re-encodes it, and feeds go2rtc — which then serves it to HomeKit like any other
camera. It's **on-demand** (idles at ~0% CPU until you open the camera) with an
optional **keep-warm** so first frame is a few seconds, not ~20s. Audio works too.

Runs in Docker on anything (Pi/Banana Pi/etc.), ideally alongside your existing
Homebridge/HOOBS/HA box.

Repo + setup: `<repo-url>`

Caveats: unofficial, cloud-dependent, can break if SimpliSafe changes their API.
Not affiliated with SimpliSafe.

---

## Comment on simplirtc (gilliginsisland/simplirtc)

Heads-up for anyone hitting cameras that error with a `LiveViewResponse`
validation failure (missing `signedChannelEndpoint` / `clientId` / `iceServers`,
returning `liveKitDetails` instead): those cameras are on SimpliSafe's newer
**LiveKit** backend, which `simplirtc`'s Kinesis URL approach can't express
(go2rtc has no LiveKit source).

I built a separate transcoding bridge for the LiveKit ones that pairs with
go2rtc the same way: `<repo-url>`. Sharing in case it helps others, and happy to
collaborate if LiveKit support ever makes sense to fold in here.
