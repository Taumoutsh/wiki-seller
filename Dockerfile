# Image officielle Playwright : Chromium et ses dépendances système déjà installés.
# La version doit correspondre à celle de playwright dans requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Paris \
    STATE_DIR=/app/state \
    SAFE_CARDS_FILE=/app/safed_cards.json \
    WANTED_CARDS_FILE=/app/wanted_cards.json

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wiki_seller ./wiki_seller

# Session navigateur, journal et captures de débogage.
VOLUME ["/app/state"]

CMD ["python", "-m", "wiki_seller", "--loop"]
