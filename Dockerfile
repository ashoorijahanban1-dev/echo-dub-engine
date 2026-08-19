# ==============================================================================
# EchoDub AI Engine - Production Dockerfile for Coolify / Ubuntu 24.04
# ==============================================================================

FROM python:3.11-slim

# Prevent interactive prompts during apt-get
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install essential system tools: FFmpeg, aria2c, git
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    git \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create storage directory for temporary file processing
RUN mkdir -p /app/storage/temp /app/storage/output

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Start FastAPI server with Uvicorn
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
