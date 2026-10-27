# EDITH · il contenitore del bot e della pagina web.
# Il codice arriva dalla cartella del progetto (docker-compose la monta in /app): qui si installano solo le dipendenze.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non serve essere root
RUN useradd --create-home --uid 1000 edith
USER edith

CMD ["python", "bot/main.py"]
