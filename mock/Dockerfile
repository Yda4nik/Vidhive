FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ffmpeg is used once, at build time, to bake a real playable sample video so
# the pipeline can be verified all the way to playback in a browser.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

RUN ffmpeg -loglevel error -f lavfi -i testsrc=duration=5:size=320x240:rate=15 \
    -pix_fmt yuv420p -movflags +faststart -y /srv/sample.mp4

COPY mock/requirements.txt /srv/mock/requirements.txt
RUN pip install --no-cache-dir -r /srv/mock/requirements.txt

COPY mock /srv/mock
WORKDIR /srv/mock

EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
