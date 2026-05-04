"""
Low-latency trade decision engine.

Inputs (Kafka):
    trades          one row per Finnhub tick, keyed by symbol
    news_enriched   FinBERT-scored articles with extracted symbols

Outputs:
    Kafka topic 'signals'
    Postgres tables 'signals' and 'positions'

Hot-path design:
    News scoring is async and only updates an in-memory sentiment cache.
    Each trade tick does:
        1. lookup decayed sentiment for the symbol (O(history))
        2. ask the position manager to evaluate stop/target/expiry
        3. open a new position if FLAT and |sentiment| > threshold
    No model inference, no cross-topic join, no DB read on the hot path.
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from confluent_kafka import Consumer, Producer, KafkaError
from dotenv import load_dotenv

from db import DB, connect
from position_manager import PositionManager
from sentiment_cache import SentimentCache, signed_score

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("decision_engine")

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
SIGNALS_TOPIC = "signals"

OPEN_THRESHOLD = float(os.getenv("OPEN_THRESHOLD", "0.4"))
TARGET_PCT = float(os.getenv("TARGET_PCT", "0.01"))            # +1.0%
STOP_PCT = float(os.getenv("STOP_PCT", "0.005"))               # -0.5%
HORIZON_MINUTES = int(os.getenv("HORIZON_MINUTES", "240"))     # 4 hours intraday
SENTIMENT_HALF_LIFE_S = float(os.getenv("SENTIMENT_HALF_LIFE_S", "1800"))
SENTIMENT_HISTORY = int(os.getenv("SENTIMENT_HISTORY", "50"))
HORIZON_LABEL = os.getenv("HORIZON_LABEL", "intraday")


def to_utc(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


def make_consumer() -> Consumer:
    c = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        "group.id": "decision-engine-group",
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
        "fetch.wait.max.ms": 10,
    })
    c.subscribe(["trades", "news_enriched"])
    return c


def make_producer() -> Producer:
    return Producer({
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        "linger.ms": 0,
        "acks": "1",
    })


def emit_signal(producer: Producer, db: DB, *, time_, symbol, action, price,
                confidence, reason, position_id):
    payload = {
        "time": time_.isoformat(),
        "symbol": symbol,
        "action": action,
        "price": price,
        "confidence": round(confidence, 4),
        "horizon": HORIZON_LABEL,
        "reason": reason,
        "position_id": position_id,
    }
    producer.produce(
        topic=SIGNALS_TOPIC,
        key=symbol.encode("utf-8"),
        value=json.dumps(payload).encode("utf-8"),
    )
    producer.poll(0)
    db.insert_signal(time_, symbol, action, price, confidence, HORIZON_LABEL, reason, position_id)
    log.info(f"{action:11s} {symbol} @ {price:.2f} conf={confidence:.3f} {reason}")


def handle_news(payload: dict, cache: SentimentCache) -> None:
    symbols = payload.get("symbols") or []
    if not symbols:
        return
    score = signed_score(
        payload.get("sentiment", "neutral"),
        float(payload.get("sentiment_score", 0.0)),
    )
    ts = int(payload.get("scored_at") or time.time() * 1000)
    for sym in symbols:
        cache.add(sym, score, ts)


def handle_trade(payload: dict, *, cache, positions, db, producer):
    sym = payload.get("s")
    price = payload.get("p")
    ts = payload.get("t")
    if not sym or price is None or ts is None:
        return
    price = float(price)
    ts = int(ts)

    # 1. Close an open position if target/stop/expiry reached.
    closed = positions.evaluate_tick(sym, price, ts)
    if closed is not None:
        pos, reason, pnl = closed
        emit_signal(
            producer, db,
            time_=to_utc(ts), symbol=sym, action="CLOSE", price=price,
            confidence=pos.confidence,
            reason=f"{reason} pnl={pnl:+.2f}%",
            position_id=pos.id,
        )

    # 2. Maybe open a new position based on aggregated sentiment.
    if positions.has_open(sym):
        return
    score = cache.score(sym, ts)
    if abs(score) < OPEN_THRESHOLD:
        return
    side = "LONG" if score > 0 else "SHORT"
    pos = positions.open_position(sym, side, price, ts, abs(score))
    if pos is None:
        return
    action = "OPEN_LONG" if side == "LONG" else "OPEN_SHORT"
    emit_signal(
        producer, db,
        time_=to_utc(ts), symbol=sym, action=action, price=price,
        confidence=abs(score),
        reason=f"agg_sentiment={score:+.3f} threshold={OPEN_THRESHOLD}",
        position_id=pos.id,
    )


def run() -> None:
    consumer = make_consumer()
    producer = make_producer()
    conn = connect()
    db = DB(conn)
    cache = SentimentCache(SENTIMENT_HALF_LIFE_S, SENTIMENT_HISTORY)
    positions = PositionManager(db, TARGET_PCT, STOP_PCT, HORIZON_MINUTES)

    def shutdown(signum, frame):
        log.info("Shutting down decision engine")
        producer.flush(5)
        consumer.close()
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info(
        f"Decision engine started: open_threshold={OPEN_THRESHOLD} "
        f"target=+{TARGET_PCT*100:.2f}% stop=-{STOP_PCT*100:.2f}% "
        f"horizon={HORIZON_MINUTES}min half_life={SENTIMENT_HALF_LIFE_S}s"
    )

    while True:
        msg = consumer.poll(timeout=0.2)
        if msg is None:
            continue
        if msg.error():
            if msg.error().code() != KafkaError._PARTITION_EOF:
                log.error(f"Kafka error: {msg.error()}")
            continue
        try:
            payload = json.loads(msg.value().decode("utf-8"))
        except json.JSONDecodeError:
            continue

        if msg.topic() == "news_enriched":
            handle_news(payload, cache)
        elif msg.topic() == "trades":
            handle_trade(payload, cache=cache, positions=positions, db=db, producer=producer)


if __name__ == "__main__":
    run()
