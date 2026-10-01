# Image officielle Playwright : Chromium + dépendances déjà installés,
# version alignée sur le `playwright==1.55.0` de requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# ffmpeg : transcodage/assemblage · fonts-noto-color-emoji : les scènes
# utilisent des emojis (🥖 🍎 📝 …) rendus par Chromium.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py renderer.py ./

# La plateforme exécute l'image telle quelle : CMD démarre le worker.
CMD ["python", "worker.py"]
