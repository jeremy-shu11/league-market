FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LEAGUE_MARKET_ENV=production \
    LEAGUE_MARKET_HOST=0.0.0.0 \
    LEAGUE_MARKET_PORT=5065 \
    LEAGUE_MARKET_DB=/data/market.sqlite \
    LEAGUE_MARKET_RAW_DATA=/data/raw \
    LEAGUE_MARKET_BACKUP_DIR=/data/backups \
    LEAGUE_MARKET_SIMULATIONS=20000

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend

RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 5065

CMD ["python3", "backend/app.py"]
