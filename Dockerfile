# Use official Python 3.12 slim base image
FROM python:3.12-slim

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    GOOGLE_APPLICATION_CREDENTIALS="/app/credentials.json"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements separately for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary folders and assign permissions
RUN mkdir -p \
    static/temp \
    static/bat_species \
    reports \
    downloads \
    documents \
    recordings \
    && chmod -R a+rwX /app

# Expose port (used by Cloud Run)
EXPOSE $PORT

# Optional health check endpoint
# (Make sure you implement /health endpoint in app.py if using this)
# HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
#     CMD curl -f http://localhost:$PORT/health || exit 1

# Start the FastAPI app using Uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
