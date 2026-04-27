"""
news_producer.py
----------------
Polls the Finnhub REST API for GENERAL market news every 60 seconds
and publishes new articles to the Kafka 'news' topic.

DATA SHAPE from Finnhub /news endpoint (one article):
{
  "category": "general",
  "datetime":  1709123456,         # UNIX seconds
  "headline":  "Fed signals rate cuts...",
  "id":        119440189,           # unique article ID
  "image":     "https://...",
  "related":   "",                  # may be empty for general news
  "source":    "Reuters",
  "summary":   "The Federal Reserve...",
  "url":       "https://reuters.com/..."
}

DATA ENGINEER LESSON — Deduplication:
  REST polling every 60s risks re-publishing articles we've already seen.
  We track seen article IDs in a local set. In production, you'd use Redis
  or a database for distributed deduplication across multiple workers.

DATA ENGINEER LESSON — Rate Limiting (429):
  Finnhub free tier: 60 API calls/minute.
  General news = 1 call per cycle, so we're well within limits.
  We still add exponential backoff in case we get a 429 response.
"""

import json
import logging
import os
import signal
import sys
import time

import requests
from confluent_kafka import Producer

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("news_producer")

# ── Load .env file ───────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = "news"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# ── Kafka Producer ────────────────────────────────────────────────────────────
producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS})

def delivery_report(err, msg):
    if err is not None:
        log.error(f"Message delivery failed: {err}")


# ── Fetch general market news ────────────────────────────────────────────────
def fetch_general_news() -> list:
    """
    Call Finnhub /news?category=general to get broad market news.
    No stock symbol required — returns articles about the whole market.
    """
    url = "https://finnhub.io/api/v1/news"
    params = {
        "category": "general",
        "token": FINNHUB_API_KEY,
    }

    backoff = 1
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=10)

            if response.status_code == 200:
                return response.json()

            elif response.status_code == 429:
                log.warning(f"Rate limited (429). Waiting {backoff}s before retry...")
                time.sleep(backoff)
                backoff *= 2

            else:
                log.error(f"Unexpected status {response.status_code}")
                return []

        except requests.RequestException as e:
            log.error(f"Request failed: {e}")
            time.sleep(backoff)
            backoff *= 2

    return []


# ── Main polling loop ─────────────────────────────────────────────────────────
def run():
    seen_ids: set = set()
    log.info(f"Starting news producer (general market news). Polling every {POLL_INTERVAL_SECONDS}s")

    while True:
        articles = fetch_general_news()
        new_count = 0

        for article in articles:
            article_id = article.get("id")

            if article_id in seen_ids:
                continue

            seen_ids.add(article_id)

            # Add ingestion metadata
            article["ingested_at"] = int(time.time() * 1000)

            payload = json.dumps(article).encode("utf-8")

            # Use source as partition key (e.g., "Reuters", "Yahoo")
            source = article.get("source", "unknown")
            producer.produce(
                topic=KAFKA_TOPIC,
                key=source.encode("utf-8"),
                value=payload,
                callback=delivery_report,
            )
            new_count += 1

        if new_count > 0:
            log.info(f"Published {new_count} new articles")

        producer.flush()
        log.info(f"Poll cycle complete. Sleeping {POLL_INTERVAL_SECONDS}s...")
        time.sleep(POLL_INTERVAL_SECONDS)


# ── Graceful shutdown ─────────────────────────────────────────────────────────
def shutdown(signum, frame):
    log.info("Shutting down news producer...")
    producer.flush()
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


if __name__ == "__main__":
    run()
