# Python slim = smaller image (no build tools pre-installed)
# Data Engineer habit: keep images as small as possible
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (Docker layer caching efficiency)
# If requirements.txt doesn't change, this layer is reused → faster builds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source files after deps — changes here won't invalidate the pip layer
COPY trade_producer.py .
COPY news_producer.py .

# Command is set in docker-compose.yml using 'command:' key
# so this Dockerfile can serve both producers
CMD ["python", "trade_producer.py"]
