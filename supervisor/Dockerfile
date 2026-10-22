FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv
# Standard library only: nothing to install.
COPY supervisor/app.py /srv/app.py

EXPOSE 8200
CMD ["python", "/srv/app.py"]
