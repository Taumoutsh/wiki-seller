# Python seul : le script parle à l'API du site en HTTP, sans navigateur.
FROM python:3.12-slim

# Fuseau horaire (heure de Paris dans le journal et pour les durées de nuit).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Paris \
    STATE_DIR=/app/state \
    SAFE_CARDS_FILE=/app/safed_cards.json \
    WANTED_CARDS_FILE=/app/wanted_cards.json

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wiki_seller ./wiki_seller

# Session, listes de cartes, caches et journal.
VOLUME ["/app/state"]

CMD ["python", "-m", "wiki_seller", "--loop"]
