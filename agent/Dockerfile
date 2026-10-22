FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ffmpeg merges best video+audio (yt-dlp); iproute2 lets the entrypoint route internet
# traffic through a VPN gateway container when asked to.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY common /srv/common
RUN pip install --no-cache-dir /srv/common

COPY agent/requirements.txt /srv/agent/requirements.txt
RUN pip install --no-cache-dir -r /srv/agent/requirements.txt

COPY agent /srv/agent
WORKDIR /srv/agent

EXPOSE 8100
ENTRYPOINT ["sh", "/srv/agent/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
