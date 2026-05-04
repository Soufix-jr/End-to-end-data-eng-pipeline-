"""
trade_producer.py
-----------------
Connects to Finnhub's WebSocket and streams real-time trades into Kafka.

DATA SHAPE from Finnhub WebSocket (per message):
{
  "data": [
    { "s": "AAPL", "p": 189.50, "t": 1709123456789, "v": 100.0, "c": ["1"] }
  ],
  "type": "trade"
}

Fields:
  s = symbol (e.g., "AAPL")
  p = price (last trade price)
  t = timestamp (UNIX milliseconds)
  v = volume (shares traded in this tick)
  c = trade conditions (list of codes)

DATA ENGINEER LESSON:
  We use symbol (s) as the Kafka partition key.
  This ensures all trades for the SAME stock go to the SAME partition,
  guaranteeing chronological ordering per symbol. If we used round-robin,
  AAPL Trade 1 and AAPL Trade 2 could land in different partitions and
  be processed out of order → corrupted OHLCV data.
"""

import json
import logging
import os
import signal
import sys
import time

import websocket
from confluent_kafka import Producer

# ── Logging setup (never use print() in production) ─────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("trade_producer")

# ── Load .env file ───────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()  # reads .env file in project root

# ── Config ───────────────────────────────────────────────────────────────────
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = "trades"

# Symbols from .env (comma-separated), e.g. SYMBOLS=AAPL,AMZN,MSFT
SYMBOLS = os.getenv("SYMBOLS", "AAPL").split(",")

# ── Kafka Producer config ────────────────────────────────────────────────────
# linger.ms: wait up to 5ms before sending a batch → reduces requests
# batch.size: max bytes per batch → improves throughput
producer = Producer({
    "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
    "linger.ms": 5,
    "batch.size": 65536,  # 64KB
})

# ── Delivery report callback ─────────────────────────────────────────────────
def delivery_report(err, msg):
    """Called for every message after Kafka acknowledges it."""
    if err is not None:
        log.error(f"Message delivery failed: {err}")
    else:
        log.debug(f"Delivered to {msg.topic()} partition [{msg.partition()}] offset {msg.offset()}")


# ── WebSocket handlers ───────────────────────────────────────────────────────
def on_message(ws, message):
    """Called every time a new message arrives from Finnhub."""
    data = json.loads(message)

    # Finnhub also sends "ping" type messages - ignore non-trade messages
    if data.get("type") != "trade":
        return

    for trade in data.get("data", []):
        # Enrich the trade with an ingestion timestamp
        trade["ingested_at"] = int(time.time() * 1000)

        symbol = trade.get("s", "UNKNOWN")
        payload = json.dumps(trade).encode("utf-8")

        # KEY CONCEPT: partition key = symbol
        # All AAPL trades → same partition → guaranteed ordering
        producer.produce(
            topic=KAFKA_TOPIC,
            key=symbol.encode("utf-8"),     # ← partition key
            value=payload,
            callback=delivery_report,
        )

    # poll() triggers delivery callbacks - call often to avoid buffer overflow
    producer.poll(0)


def on_error(ws, error):
    log.error(f"WebSocket error: {error}")


def on_close(ws, close_status_code, close_msg):
    log.warning(f"WebSocket closed: {close_status_code} / {close_msg}")
    producer.flush()  # make sure all pending messages are sent before exit


def on_open(ws):
    """Subscribe to all symbols once the connection is open."""
    log.info(f"WebSocket connected. Subscribing to: {SYMBOLS}")
    for symbol in SYMBOLS:
        ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))


# ── Graceful shutdown ────────────────────────────────────────────────────────
def shutdown(signum, frame):
    log.info("Shutdown signal received. Flushing Kafka producer...")
    producer.flush()
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ws_url = f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}"

    while True:
        try:
            log.info("Connecting to Finnhub WebSocket...")
            ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            # run_forever() blocks; reconnect=True automatically retries on disconnect
            ws.run_forever(reconnect=5)
        except Exception as e:
            log.error(f"Unexpected error: {e}. Reconnecting in 10s...")
            time.sleep(10)
