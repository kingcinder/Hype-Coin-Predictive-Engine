FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system dependencies (curl for healthchecks)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer cache)
COPY pyproject.toml README.md /app/
RUN pip install --upgrade pip \
    && pip install -e ".[dev]"

# Copy application code
COPY . /app

# Create non-root user for runtime
RUN groupadd -r serpent && useradd -r -g serpent -d /app -s /sbin/nologin serpent \
    && mkdir -p /app/data /app/data/archive \
    && chown -R serpent:serpent /app

# ── All-in-one mode (default): runs API + GUI + worker in one process ──
# Exposed ports: 8000 (API), 8501 (GUI)
EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

USER serpent

# Default: full stack in one process
CMD ["python", "-m", "engine"]
