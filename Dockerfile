FROM python:3.11-slim AS builder

WORKDIR /app

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim

WORKDIR /app
 
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

COPY bot/static /app/bot/static

COPY . .

RUN rm -rf /root/.cache

CMD ["python", "main.py"]
