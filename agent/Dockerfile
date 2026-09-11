FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ffmpeg is needed by yt-dlp to merge best video+audio streams.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY common /srv/common
RUN pip install --no-cache-dir /srv/common

COPY agent/requirements.txt /srv/agent/requirements.txt
RUN pip install --no-cache-dir -r /srv/agent/requirements.txt

COPY agent /srv/agent
WORKDIR /srv/agent

EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
