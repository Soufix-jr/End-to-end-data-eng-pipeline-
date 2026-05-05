"""
streaming_nlp.py
----------------
Consumes news articles from Kafka 'news' topic, scores each headline with
FinBERT in real time, and publishes the enriched record to:
  - Kafka topic 'news_enriched'  (consumed by decision_engine)
  - PostgreSQL table 'nlp_results'

DESIGN NOTES - Latency:
  * Models are loaded ONCE at startup (~5s). Inference per headline ≈ 50-150ms
    on CPU. End-to-end news → enriched signal: well under 1 second.
  * We extract candidate stock tickers from Finnhub's 'related' field plus a
    simple uppercase-token heuristic on the headline. This lets the decision
    engine join sentiment to trades without an extra NER hop.
  * The batch nlp_processor.py is kept as-is for offline backfills / clustering.
"""

import json
import logging
import os
import re
import signal
import sys
import time

# ── Resource caps (set BEFORE importing torch/transformers) ──────────────────
# 8GB dev box: keep CPU usage modest so producers/consumers/Kafka don't starve.
_THREADS = os.getenv("NLP_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", _THREADS)
os.environ.setdefault("MKL_NUM_THREADS", _THREADS)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import psycopg2
from confluent_kafka import Consumer, Producer, KafkaError
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("streaming_nlp")

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
NEWS_TOPIC = "news"
ENRICHED_TOPIC = "news_enriched"

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "pipeline")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "pipeline_secret")
POSTGRES_DB = os.getenv("POSTGRES_DB", "market_data")

MODEL_NAME = os.getenv("FINBERT_MODEL", "ProsusAI/finbert")
SYMBOLS_HINT = {s.strip().upper() for s in os.getenv("SYMBOLS", "AAPL").split(",") if s.strip()}

TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")

# Company-name -> ticker. General news says "Apple" not "AAPL", so the bare
# ticker regex misses them. This map covers the symbols we care about.
NAME_TO_TICKER = {
    "apple": "AAPL",
    "microsoft": "MSFT",
    "tesla": "TSLA",
    "nvidia": "NVDA",
    "amazon": "AMZN",
    "meta": "META",
    "facebook": "META",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "netflix": "NFLX",
    "intel": "INTC",
    "amd": "AMD",
}


def load_finbert():
    import torch
    from transformers import pipeline

    torch.set_num_threads(int(_THREADS))
    log.info(f"Loading FinBERT model: {MODEL_NAME} (threads={_THREADS}, device=cpu)")
    return pipeline(
        "sentiment-analysis",
        model=MODEL_NAME,
        tokenizer=MODEL_NAME,
        device=-1,            # force CPU; on this box there's no GPU
        truncation=True,
        max_length=256,        # headlines are short; halves inference time
    )


def extract_symbols(article: dict) -> list:
    """
    Pull tickers from three sources:
      1. Finnhub 'related' field (when populated)
      2. Bare ticker mentions in headline / summary (e.g. "AAPL")
      3. Company-name mentions (e.g. "Apple" -> AAPL)
    """
    found = set()
    related = article.get("related") or ""
    for tok in related.split(","):
        tok = tok.strip().upper()
        if tok in SYMBOLS_HINT:
            found.add(tok)

    text = f"{article.get('headline','')} {article.get('summary','')}"
    for tok in TICKER_RE.findall(text):
        if tok in SYMBOLS_HINT:
            found.add(tok)

    text_lower = text.lower()
    for name, ticker in NAME_TO_TICKER.items():
        if ticker in SYMBOLS_HINT and name in text_lower:
            found.add(ticker)

    return sorted(found)


def get_db_connection():
    for attempt in range(10):
        try:
            conn = psycopg2.connect(
                host=POSTGRES_HOST, port=POSTGRES_PORT,
                user=POSTGRES_USER, password=POSTGRES_PASSWORD,
                dbname=POSTGRES_DB,
            )
            conn.autocommit = True
            log.info("Connected to PostgreSQL")
            return conn
        except psycopg2.OperationalError as e:
            log.warning(f"DB connection attempt {attempt+1}/10 failed: {e}")
            time.sleep(3)
    sys.exit(1)


INSERT_NLP = """
    INSERT INTO nlp_results (news_id, sentiment, sentiment_score, model_name)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (news_id) DO NOTHING
"""


def run():
    classifier = load_finbert()

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        "group.id": "streaming-nlp-group",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    })
    consumer.subscribe([NEWS_TOPIC])

    producer = Producer({
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        "linger.ms": 0,
    })

    conn = get_db_connection()

    def shutdown(signum, frame):
        log.info("Shutting down streaming NLP...")
        producer.flush(5)
        consumer.close()
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("Streaming NLP started")

    while True:
        msg = consumer.poll(timeout=1.0)
        if msg is None:
            continue
        if msg.error():
            if msg.error().code() != KafkaError._PARTITION_EOF:
                log.error(f"Kafka error: {msg.error()}")
            continue

        try:
            article = json.loads(msg.value().decode("utf-8"))
        except json.JSONDecodeError:
            continue

        headline = (article.get("headline") or "").strip()
        if not headline:
            continue

        t0 = time.time()
        try:
            result = classifier(headline)[0]
        except Exception as e:
            log.error(f"Inference failed: {e}")
            continue
        infer_ms = int((time.time() - t0) * 1000)

        symbols = extract_symbols(article)

        enriched = {
            "id": article.get("id"),
            "headline": headline,
            "source": article.get("source"),
            "url": article.get("url"),
            "datetime": article.get("datetime"),
            "symbols": symbols,
            "sentiment": result["label"].lower(),
            "sentiment_score": round(float(result["score"]), 4),
            "scored_at": int(time.time() * 1000),
            "infer_ms": infer_ms,
        }

        # Publish enriched event - partition by first symbol (or source) so
        # the decision engine sees per-symbol ordering.
        key = (symbols[0] if symbols else (article.get("source") or "unknown")).encode("utf-8")
        producer.produce(
            topic=ENRICHED_TOPIC,
            key=key,
            value=json.dumps(enriched).encode("utf-8"),
        )
        producer.poll(0)

        try:
            with conn.cursor() as cur:
                cur.execute(
                    INSERT_NLP,
                    (article.get("id"), enriched["sentiment"],
                     enriched["sentiment_score"], "finbert-stream"),
                )
        except Exception as e:
            log.error(f"DB insert failed: {e}")

        log.info(
            f"Scored id={article.get('id')} sym={symbols} "
            f"{enriched['sentiment']}({enriched['sentiment_score']}) {infer_ms}ms"
        )


if __name__ == "__main__":
    run()
