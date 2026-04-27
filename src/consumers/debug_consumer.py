"""
debug_consumer.py
-----------------
A simple Kafka consumer that prints messages from 'trades' and 'news' topics.

PURPOSE:
  This is ONLY for verifying Phase 1 works end-to-end.
  You should see real-time trade data and news articles printed to the console.
  Replace with db_consumer.py in Phase 2 once you're confident data flows correctly.

HOW TO RUN:
  # Make sure your .env is populated and Kafka is running
  # Terminal 1:  make up-kafka
  # Terminal 2:  python trade_producer.py
  # Terminal 3:  python news_producer.py
  # Terminal 4:  python debug_consumer.py

WHAT TO OBSERVE:
  - Every few seconds, trade messages appear (price, volume, symbol)
  - After 60s, news articles appear (headline, source, symbol)
  - Each message shows which partition it came from → verify same symbol = same partition

DATA ENGINEER LESSON — Consumer Groups:
  A consumer group allows horizontal scaling. If you start 2 instances of
  this consumer with the same group_id, Kafka splits the partitions between them.
  E.g., AAPL trades → Consumer A, TSLA trades → Consumer B.
  No message is delivered twice. This is how services scale.
"""

import json
import logging
import os
import signal
import sys

from confluent_kafka import Consumer, KafkaError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("debug_consumer")

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

consumer = Consumer({
    "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
    "group.id": "debug-consumer-v2",
    # earliest = read ALL messages from the beginning of the topic
    # latest   = only messages that arrive AFTER this consumer starts
    "auto.offset.reset": "earliest",
})

# Subscribe to both topics
consumer.subscribe(["news"])


def shutdown(signum, frame):
    log.info("Shutting down consumer...")
    consumer.close()
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


if __name__ == "__main__":
    log.info("Debug consumer started. Waiting for messages on 'trades' and 'news'...")
    log.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            msg = consumer.poll(timeout=1.0)

            if msg is None:
                continue  # no message within timeout window

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    # Reached end of partition — normal in low-volume scenarios
                    continue
                else:
                    log.error(f"Kafka error: {msg.error()}")
                    break

            # ── Successful message ──────────────────────────────────────────
            topic = msg.topic()
            partition = msg.partition()
            offset = msg.offset()
            key = msg.key().decode("utf-8") if msg.key() else "N/A"

            try:
                value = json.loads(msg.value().decode("utf-8"))
            except json.JSONDecodeError:
                value = msg.value().decode("utf-8")

            if topic == "trades":
                log.info(
                    f"[TRADE] partition={partition} | key={key} | "
                    f"price={value.get('p')} | volume={value.get('v')} | "
                    f"time_ms={value.get('t')}"
                )
            elif topic == "news":
                log.info(
                    f"[NEWS] partition={partition} | key={key} | "
                    f"headline={value.get('headline', '')[:80]}..."
                )

    except KeyboardInterrupt:
        pass
    finally:
        consumer.close()
