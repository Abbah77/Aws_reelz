# Docker image for the resolver — works on Render (or any container
# host) as-is. Render's build/run infrastructure is standard x86_64, so
# no architecture-specific changes are needed for that path.
#
# ARM NOTE (kept for reference, not the primary path anymore): this
# Dockerfile also works unmodified on ARM hosts like Oracle Cloud's
# Always Free Ampere VMs, because `playwright install --with-deps
# chromium` below auto-detects the host CPU architecture and pulls the
# matching Chromium build — nothing here is hardcoded to one architecture.

FROM python:3.12-slim

# Playwright + Chromium need these system libraries regardless of
# architecture. --with-deps below handles most of this automatically,
# but a few base packages are worth having explicitly for a smaller,
# more predictable image layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# This is the ARM-safe part: `playwright install --with-deps chromium`
# detects the host CPU architecture (arm64 vs amd64) and pulls the
# correct Chromium build + correct OS-level dependencies for THIS
# machine, rather than a hardcoded x86 assumption. This is what makes
# the same Dockerfile work on Oracle's Ampere ARM VM without edits.
RUN playwright install --with-deps chromium

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# NOTE ON PORT: Render (and several other PaaS hosts) inject a $PORT
# environment variable and expect the app to bind to it, not a fixed
# port. Using shell form here (not exec-array form) so $PORT actually
# gets substituted by the shell at container start. Defaults to 8000
# via ${PORT:-8000} for local `docker run` / docker-compose use where
# no PORT is set.
CMD uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
