# ---- Stage 1: build environment ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm AS build

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY ingestion.py ./
COPY models.py ./
# (Add any other local modules you import)

# Install dependencies using uv
RUN uv sync --frozen --no-dev

# ---- Stage 2: minimal runtime image ----
FROM python:3.12-slim-bookworm

WORKDIR /app

# Copy virtual environment from uv
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy source code
COPY ingestion.py ./
COPY models.py ./

# Set env to production
ENV PYTHONUNBUFFERED=1

# Default command
CMD ["python", "ingestion.py"]
