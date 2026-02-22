# ─────────────────────────────────────────────
# Stage 1: Base Python image
# ─────────────────────────────────────────────
FROM python:3.12-slim AS base

# Prevent Python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# ─────────────────────────────────────────────
# Stage 2: Dependencies
# ─────────────────────────────────────────────
FROM base AS dependencies

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium --with-deps

# ─────────────────────────────────────────────
# Stage 3: Development
# ─────────────────────────────────────────────
FROM dependencies AS development

COPY . .

EXPOSE 8000

# ─────────────────────────────────────────────
# Stage 4: Production
# ─────────────────────────────────────────────
FROM dependencies AS production

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

COPY . .

# Set permissions
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Production server: multiple workers, no reload
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--no-access-log"]
