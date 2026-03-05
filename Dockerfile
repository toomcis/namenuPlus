FROM python:3.12.9-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8000

WORKDIR /app

COPY requirements.txt /app
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py api.py start.sh ./

CMD ["./start.sh"]