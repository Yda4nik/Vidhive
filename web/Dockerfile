FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY web/requirements.txt /srv/web/requirements.txt
RUN pip install --no-cache-dir -r /srv/web/requirements.txt

COPY web /srv/web
WORKDIR /srv/web

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
