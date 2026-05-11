FROM python:3.12.13 AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN python -m venv .venv
COPY requirements.txt ./
RUN .venv/bin/pip install -r requirements.txt

FROM python:3.12.13-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# dependências do Firefox (necessárias para camoufox)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 libx11-xcb1 libdbus-glib-1-2 libxt6 \
    libasound2 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libxkbcommon0 libpangocairo-1.0-0 \
    fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv .venv/
COPY . .

# baixa o Firefox patcheado do camoufox
RUN .venv/bin/python -m camoufox fetch

CMD ["/app/.venv/bin/gunicorn", "--workers", "1", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:8080", "--access-logfile", "/dev/null", "--error-logfile", "-", "--log-level", "warning", "app:app"]
