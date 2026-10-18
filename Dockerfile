FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install system utilities: ffmpeg, ffprobe, 7zip, p7zip, curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    p7zip-full \
    unrar-free \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency specifications
COPY pyproject.toml README.md /app/

# Install uv package manager and install dependencies
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

RUN uv pip install --system -e ".[postgres]"

# Copy application source code
COPY . /app/

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
