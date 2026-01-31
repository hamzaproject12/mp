# On part d'une image Python officielle
FROM python:3.9-slim

# Installation des dépendances système pour Playwright
RUN apt-get update && apt-get install -y \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Dossier de travail
WORKDIR /app

# Copie des requirements et installation
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# INSTALLATION DES NAVIGATEURS PLAYWRIGHT (L'étape magique)
RUN playwright install --with-deps chromium

# Copie du reste du code
COPY . .

# Commande de lancement (on lance le script en mode non-stop)
CMD ["python", "main.py"]