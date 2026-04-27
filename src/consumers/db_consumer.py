"""
db_consumer.py
--------------
Reads news articles from Kafka and writes them to PostgreSQL.
This replaces the debug_consumer for production use.

DATA ENGINEER LESSON — Batch Inserts:
  Inserting rows one-at-a-time is slow (network round-trip per row).
  We batch messages and insert them together using executemany().
  Batch size = 50 or every 5 seconds, whichever comes first.
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime

import psycopg2
from psycopg2.extras import execute_values
from confluent_kafka import Consumer, KafkaError
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("db_consumer")

# ── Config ───────────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "pipeline")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "pipeline_secret")
POSTGRES_DB = os.getenv("POSTGRES_DB", "market_data")

BATCH_SIZE = 50
FLUSH_INTERVAL_SECONDS = 5

# ── Kafka Consumer ───────────────────────────────────────────────────────────
consumer = Consumer({
    "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
    "group.id": "db-consumer-group",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": True,
})
consumer.subscribe(["news"])

# ── PostgreSQL Connection ────────────────────────────────────────────────────
def get_db_connection():
    """Create and return a PostgreSQL connection with retry logic."""
    for attempt in range(5):
        try:
            conn = psycopg2.connect(
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                user=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
                dbname=POSTGRES_DB,
            )
            conn.autocommit = False
            log.info("Connected to PostgreSQL")
            return conn
        except psycopg2.OperationalError as e:
            log.warning(f"DB connection attempt {attempt+1}/5 failed: {e}")
            time.sleep(3)
    log.error("Could not connect to PostgreSQL after 5 attempts")
    sys.exit(1)


def insert_news_batch(conn, batch):
    """Insert a batch of news articles into raw_news table."""
    if not batch:
        return

    query = """
        INSERT INTO raw_news (id, category, headline, summary, source, url, image_url, related, published_at)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    """

    values = []
    for article in batch:
        values.append((
            article.get("id"),
            article.get("category"),
            article.get("headline"),
            article.get("summary"),
            article.get("source"),
            article.get("url"),
            article.get("image"),
            article.get("related"),
            datetime.utcfromtimestamp(article["datetime"]) if article.get("datetime") else None,
        ))

    try:
        with conn.cursor() as cur:
            execute_values(cur, query, values)
        conn.commit()
        log.info(f"Inserted {len(values)} articles into raw_news")
    except Exception as e:
        conn.rollback()
        log.error(f"Failed to insert batch: {e}")


# ── Main loop ────────────────────────────────────────────────────────────────
def run():
    conn = get_db_connection()
    batch = []
    last_flush = time.time()

    log.info("DB consumer started. Reading from Kafka 'news' → PostgreSQL...")

    while True:
        msg = consumer.poll(timeout=1.0)

        if msg is not None and not msg.error():
            try:
                article = json.loads(msg.value().decode("utf-8"))
                batch.append(article)
            except json.JSONDecodeError:
                log.warning("Skipped non-JSON message")

        elif msg is not None and msg.error():
            if msg.error().code() != KafkaError._PARTITION_EOF:
                log.error(f"Kafka error: {msg.error()}")

        # Flush when batch is full OR time interval is reached
        if len(batch) >= BATCH_SIZE or (time.time() - last_flush > FLUSH_INTERVAL_SECONDS and batch):
            insert_news_batch(conn, batch)
            batch = []
            last_flush = time.time()


def shutdown(signum, frame):
    log.info("Shutting down db consumer...")
    consumer.close()
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

if __name__ == "__main__":
    run()
