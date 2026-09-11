FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY mock/requirements.txt /srv/mock/requirements.txt
RUN pip install --no-cache-dir -r /srv/mock/requirements.txt

COPY mock /srv/mock
WORKDIR /srv/mock

EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
