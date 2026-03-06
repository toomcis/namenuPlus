FROM python:3.12.9-slim

# Install cron
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Default port — overridable at runtime via environment
ENV PORT=8000
ENV MAIN_DB=/app/data/main.db
ENV NAMENU_DB=/app/data/namenu.db

# Dependencies
COPY requirements.txt /app
RUN pip install --no-cache-dir -r requirements.txt

# App files
COPY main.py api.py scrapeAll.sh start.sh ./
COPY webUI    ./webUI
COPY scrapers ./scrapers
COPY static   ./static

RUN chmod +x start.sh scrapeAll.sh

# /app/data is where both DBs live — mount a volume here
VOLUME ["/app/data"]

EXPOSE ${PORT}

CMD ["./start.sh"]