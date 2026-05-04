"""
db_consumer.py
--------------
Reads from Kafka 'trades' AND 'news' topics and writes to PostgreSQL.

DATA ENGINEER LESSON - Batch Inserts:
  Inserting rows one-at-a-time is slow (network round-trip per row).
  We batch messages and insert them together using execute_values().
  Each topic gets its own batch; we flush whichever fills first.
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

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

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "pipeline")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "pipeline_secret")
POSTGRES_DB = os.getenv("POSTGRES_DB", "market_data")

BATCH_SIZE = int(os.getenv("DB_BATCH_SIZE", "200"))
FLUSH_INTERVAL_SECONDS = float(os.getenv("DB_FLUSH_INTERVAL", "2"))

consumer = Consumer({
    "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
    "group.id": "db-consumer-group",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": True,
})
consumer.subscribe(["news", "trades"])


def get_db_connection():
    for attempt in range(10):
        try:
            conn = psycopg2.connect(
                host=POSTGRES_HOST, port=POSTGRES_PORT,
                user=POSTGRES_USER, password=POSTGRES_PASSWORD,
                dbname=POSTGRES_DB,
            )
            conn.autocommit = False
            log.info("Connected to PostgreSQL")
            return conn
        except psycopg2.OperationalError as e:
            log.warning(f"DB connection attempt {attempt+1}/10 failed: {e}")
            time.sleep(3)
    log.error("Could not connect to PostgreSQL")
    sys.exit(1)


NEWS_QUERY = """
    INSERT INTO raw_news (id, category, headline, summary, source, url, image_url, related, published_at)
    VALUES %s
    ON CONFLICT (id) DO NOTHING
"""

TRADES_QUERY = """
    INSERT INTO raw_trades (time, symbol, price, volume, conditions, ingested_at)
    VALUES %s
    ON CONFLICT (symbol, time) DO NOTHING
"""


def insert_news(conn, batch):
    if not batch:
        return
    values = [(
        a.get("id"), a.get("category"), a.get("headline"), a.get("summary"),
        a.get("source"), a.get("url"), a.get("image"), a.get("related"),
        datetime.fromtimestamp(a["datetime"], tz=timezone.utc) if a.get("datetime") else None,
    ) for a in batch]
    try:
        with conn.cursor() as cur:
            execute_values(cur, NEWS_QUERY, values)
        conn.commit()
        log.info(f"Inserted {len(values)} news rows")
    except Exception as e:
        conn.rollback()
        log.error(f"News insert failed: {e}")


def insert_trades(conn, batch):
    if not batch:
        return
    values = []
    for t in batch:
        ts = t.get("t")
        if ts is None or t.get("s") is None:
            continue
        values.append((
            datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc),
            t.get("s"), t.get("p"), t.get("v"),
            ",".join(t.get("c") or []) if isinstance(t.get("c"), list) else t.get("c"),
            datetime.fromtimestamp(t.get("ingested_at", ts) / 1000.0, tz=timezone.utc),
        ))
    if not values:
        return
    try:
        with conn.cursor() as cur:
            execute_values(cur, TRADES_QUERY, values)
        conn.commit()
        log.info(f"Inserted {len(values)} trade rows")
    except Exception as e:
        conn.rollback()
        log.error(f"Trade insert failed: {e}")


def run():
    conn = get_db_connection()
    news_batch: list = []
    trades_batch: list = []
    last_flush = time.time()

    log.info("DB consumer started - Kafka {news,trades} → PostgreSQL")

    while True:
        msg = consumer.poll(timeout=0.5)

        if msg is not None and not msg.error():
            try:
                payload = json.loads(msg.value().decode("utf-8"))
            except json.JSONDecodeError:
                log.warning("Skipped non-JSON message")
            else:
                if msg.topic() == "news":
                    news_batch.append(payload)
                elif msg.topic() == "trades":
                    trades_batch.append(payload)
        elif msg is not None and msg.error():
            if msg.error().code() != KafkaError._PARTITION_EOF:
                log.error(f"Kafka error: {msg.error()}")

        time_to_flush = time.time() - last_flush > FLUSH_INTERVAL_SECONDS
        if len(news_batch) >= BATCH_SIZE or (time_to_flush and news_batch):
            insert_news(conn, news_batch)
            news_batch = []
        if len(trades_batch) >= BATCH_SIZE or (time_to_flush and trades_batch):
            insert_trades(conn, trades_batch)
            trades_batch = []
        if time_to_flush:
            last_flush = time.time()


def shutdown(signum, frame):
    log.info("Shutting down db consumer...")
    consumer.close()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

if __name__ == "__main__":
    run()
