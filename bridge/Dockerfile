# glibc-based image -- the LiveKit native lib needs glibc (go2rtc's Alpine image
# would not work for the bridge), and ffmpeg does the re-encode.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir livekit aiohttp aiofiles simplisafe-python

COPY bridge.py authenticate.py list_cameras.py /app/
WORKDIR /app

ENTRYPOINT ["python", "bridge.py"]
