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

# Chrome + Xvfb dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libpangocairo-1.0-0 libasound2 \
    libx11-xcb1 libxcb-dri3-0 libxshmfence1 \
    fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv .venv/
COPY . .

# instala Chromium do patchright
RUN .venv/bin/python -m patchright install chromium

CMD ["xvfb-run", "--server-args=-screen 0 1280x720x24", \
     "/app/.venv/bin/gunicorn", \
     "--workers", "1", "--threads", "4", "--timeout", "120", \
     "--bind", "0.0.0.0:8080", \
     "--access-logfile", "/dev/null", "--error-logfile", "-", "--log-level", "warning", \
     "app:app"]
