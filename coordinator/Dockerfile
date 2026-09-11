FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# Shared package first (better layer caching).
COPY common /srv/common
RUN pip install --no-cache-dir /srv/common

COPY coordinator/requirements.txt /srv/coordinator/requirements.txt
RUN pip install --no-cache-dir -r /srv/coordinator/requirements.txt

COPY coordinator /srv/coordinator
WORKDIR /srv/coordinator
RUN chmod +x entrypoint.sh

EXPOSE 8000
CMD ["./entrypoint.sh"]
