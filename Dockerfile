# Dockerfile
FROM python:3.12.9-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Ports — overridable at runtime
ENV PORT=8000
ENV RESTAURANT_API_PORT=6333

# Restaurant service URL — override to switch to hosted service:
#   docker run -e RESTAURANT_API_URL=https://restaurant.tomenu.sk ...
ENV RESTAURANT_API_URL=http://localhost:6333
ENV RESTAURANT_API_KEY=

ENV MAIN_DB=/app/data/main.db
ENV NAMENU_DB=/app/data/namenu.db

COPY requirements.txt /app
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py api.py restaurant_api.py restaurant_client.py scrapeAll.sh start.sh ./
COPY webUI    ./webUI
COPY scrapers ./scrapers
COPY ml       ./ml

RUN chmod +x start.sh scrapeAll.sh

VOLUME ["/app/data"]

# Main API port
EXPOSE ${PORT}
# Restaurant API port (internal — not exposed publicly in production)
EXPOSE ${RESTAURANT_API_PORT}

CMD ["./start.sh"]