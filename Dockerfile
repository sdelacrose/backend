# Base Python image
ARG PYTHON_VERSION=3.10.19
ARG TARGETPLATFORM=linux/amd64
FROM --platform=$TARGETPLATFORM python:${PYTHON_VERSION}-slim-bookworm AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Copy application files
COPY . /app

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

# The shared `models` package comes from the sdelacrose account only, pinned to an explicit commit
# (PR #2, RH leave/entitlement models -- descendant of the previously-pinned doc-mail models commit)
# so every build installs the exact same migrations. Re-pin when the models branch is published
# under a different commit.
ARG MODELS_REF=0841991f496e0f4805dd080cfbbf25e55179fa2c
# The repository is public, so no credentials are needed. Only for a private fork, pass
# --build-arg MODELS_GIT_AUTH="<user>:<token>@" (the value stays in the image history: use a throw-away
# token). The GitHub workflows that used to replace <username>/<token> here are disabled.
ARG MODELS_GIT_AUTH=""
RUN pip install "git+https://${MODELS_GIT_AUTH}github.com/sdelacrose/admin-server-models.git@${MODELS_REF}"

# Make the entry point script executable
RUN chmod +x /app/scripts/entry_point.sh

# Expose the application port
EXPOSE 8000

# Set the container's entry point
ENTRYPOINT ["/app/scripts/entry_point.sh"]
