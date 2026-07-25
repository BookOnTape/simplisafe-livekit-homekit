# go2rtc with simplirtc baked in, for Kinesis-backed SimpliSafe cameras
# (e.g. the Video Doorbell Pro) via `echo:simplirtc` stream sources.
# Same recipe as the original Pi deployment.
FROM alexxit/go2rtc:latest
RUN apk add --no-cache git && \
    python3 -m pip install --no-cache-dir --break-system-packages \
    "git+https://github.com/bachya/simplisafe-python.git@2024.01.0" \
    git+https://github.com/gilliginsisland/simplirtc.git
