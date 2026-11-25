# Base Python image
ARG PYTHON_VERSION=3.10.19
ARG TARGETPLATFORM=linux/amd64
FROM --platform=$TARGETPLATFORM python:${PYTHON_VERSION}-slim-bookworm AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Install system dependencies required for WeasyPrint
RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    for attempt in 1 2 3; do \
      apt-get update -o Acquire::Retries=5 -o Acquire::http::Timeout=30 && \
      apt-get install -y --no-install-recommends --fix-missing \
        libgdk-pixbuf2.0-0 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libcairo2 \
        libffi-dev \
        libglib2.0-0 \
        pkg-config \
        git && break; \
      if [ "$attempt" -eq 3 ]; then \
        exit 1; \
      fi; \
      rm -rf /var/lib/apt/lists/*; \
      sleep 5; \
    done; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    python -m pip install -r requirements.txt

# Public shared models, pinned to the revision validated with this source tree.
ARG MODELS_REF=a168001dc9d10e577ddedd2e2a9eb305ba5659b0
RUN pip install --no-deps "git+https://github.com/sdelacrose/admin-server-models.git@${MODELS_REF}"

# Keep source changes from reinstalling the locked dependency layer.
COPY . /app
ARG REVISION=unknown
LABEL org.opencontainers.image.revision=$REVISION org.opencontainers.image.version="1.4.69"

# Make the entry point script executable (and force LF: a CRLF shebang makes exec fail with "no such file")
RUN sed -i 's/\r$//' /app/scripts/entry_point.sh && chmod +x /app/scripts/entry_point.sh

# Expose the application port
EXPOSE 8000

# Set the container's entry point
ENTRYPOINT ["/app/scripts/entry_point.sh"]
