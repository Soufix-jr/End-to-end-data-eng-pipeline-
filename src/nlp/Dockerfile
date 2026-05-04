FROM python:3.11-slim

WORKDIR /app

# Build deps for psycopg2 + cleanup in same layer
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt confluent-kafka==2.3.0

COPY streaming_nlp.py .
COPY nlp_processor.py .

# HF cache lives on a mounted volume so models persist across restarts
ENV HF_HOME=/root/.hf_cache TRANSFORMERS_CACHE=/root/.hf_cache

CMD ["python", "-u", "streaming_nlp.py"]
