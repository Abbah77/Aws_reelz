# ARM64-targeted image for Oracle Cloud's Always Free Ampere (ARM) VMs.
#
# WHY THIS MATTERS: Oracle's Always Free compute is ARM (Ampere A1), not
# x86_64. Playwright's default Chromium download and most generic Docker
# base images assume x86_64 — on ARM they either fail to pull, or pull
# and then fail to execute. This Dockerfile is written specifically to
# avoid that, using Playwright's own ARM-aware install path rather than
# assuming an architecture.
#
# If you deploy this SAME resolver on an x86_64 box later (a different
# provider, or an x86 Oracle shape), this Dockerfile still works —
# Playwright's `--with-deps` install detects the host architecture
# automatically. This file does not need to change per-architecture,
# only the base image tag below might, per the note next to it.

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

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
