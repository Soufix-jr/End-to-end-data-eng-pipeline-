# Real-time Market Intelligence Pipeline

End-to-end streaming stack that turns Finnhub ticks and headlines into live
BUY/SELL decisions with target, stop and P&L — all observable in Grafana.

```
            (Kafka topics in italics)

 Finnhub WS  ->  trade_producer  ->  trades  ----------------+
                                                             |
 Finnhub REST ->  news_producer  ->  news    ->  streaming_nlp
                                                  (FinBERT)
                                                      |
                                                      v
                                              news_enriched
                                                      |
                                                      v
                                              decision_engine
                                                  |       |
                                                  v       v
                                              signals    positions
                                                  |       |
                                                  v       v
                                              Postgres / Timescale  ->  Grafana
```

The decision engine keeps an in-memory sentiment cache per symbol. Every
trade tick is a dict lookup, never a model call, so trade-to-decision
latency stays in single-digit milliseconds. Spark runs on the side and
rolls trades into 1m / 5m OHLCV candles for charts.

## What's in the box

| layer       | component                              | role                                          |
|-------------|----------------------------------------|-----------------------------------------------|
| ingest      | `trade_producer`, `news_producer`      | Finnhub WS / REST -> Kafka                    |
| storage     | Postgres + TimescaleDB hypertables     | trades, news, OHLCV, signals, positions       |
| NLP         | `streaming_nlp` (FinBERT)              | per-article sentiment, ~150 ms / headline     |
| decisions   | `decision_engine`                      | sentiment cache + position lifecycle          |
| analytics   | Spark structured streaming             | 1m / 5m OHLCV candles                         |
| dashboards  | Grafana (provisioned)                  | KPIs, charts, signal feed, P&L tables         |

## Tables

| table             | what it holds                                 |
|-------------------|-----------------------------------------------|
| `raw_trades`      | every Finnhub tick (hypertable)               |
| `raw_news`        | every news article (deduped by Finnhub id)    |
| `nlp_results`     | FinBERT sentiment per article                 |
| `ohlcv_1m`, `_5m` | candles computed by Spark                     |
| `signals`         | log of every OPEN / CLOSE recommendation      |
| `positions`       | one row per trade (entry, target, stop, PnL)  |

## Running it

Requires Docker Desktop. The stack is split into compose profiles so you
can run a subset on an 8 GB laptop.

**Linux / macOS / WSL**
```bash
cp .env.example .env          # set FINNHUB_API_KEY
make up-nlp                   # infra + ingest + NLP + decisions  (~3 GB)
make up-dash                  # add Grafana on http://localhost:3000  (admin/admin)
make smoke
make logs S=decision_engine
```

**Windows cmd / PowerShell** (uses the bundled `make.bat`)
```cmd
copy .env.example .env
notepad .env
make up-nlp
make up-dash
make smoke
make logs S=decision_engine
```

**Or call docker compose directly**
```cmd
docker compose --profile nlp up -d --build
docker compose --profile dashboard up -d
docker compose ps
docker compose logs -f --tail=200 decision_engine
```

Profiles:

| profile     | services                                                      |
|-------------|---------------------------------------------------------------|
| `up-infra`  | Kafka + Postgres                                              |
| `up-ingest` | + producers + db_consumer                                     |
| `up-nlp`    | + streaming_nlp + decision_engine **(recommended for 8 GB)**  |
| `up-dash`   | + Grafana                                                     |
| `up-spark`  | + Spark master/worker (needs 16 GB)                           |
| `up-full`   | everything                                                    |

## How a decision gets made

1. A news article arrives. `streaming_nlp` runs FinBERT once, gets
   e.g. `positive 0.94`, publishes to `news_enriched`, writes `nlp_results`.
2. `decision_engine` updates its per-symbol sentiment cache. Old scores
   decay with a 30-minute half life.
3. A trade tick arrives for the same symbol. The engine:
   1. checks if any open position hit its target / stop / expiry, closes it
   2. if there is no open position and aggregated sentiment crosses
      `OPEN_THRESHOLD`, opens a new LONG (positive) or SHORT (negative)
      position with target = entry +/- `TARGET_PCT` and stop +/- `STOP_PCT`
4. Both events are written to `signals` and the position lifecycle is
   tracked in `positions`. P&L is computed at close.

## Configuration knobs

All in `.env`:

| variable                | default | meaning                                |
|-------------------------|---------|----------------------------------------|
| `OPEN_THRESHOLD`        | `0.4`   | sentiment magnitude required to open   |
| `TARGET_PCT`            | `1.0`   | take-profit distance from entry (%)    |
| `STOP_PCT`              | `0.5`   | stop-loss distance from entry (%)      |
| `HORIZON_MINUTES`       | `240`   | max position duration before auto-close|
| `SENTIMENT_HALF_LIFE_S` | `1800`  | how fast old news fades                |

## Grafana dashboard

`Market Intelligence Pipeline` is auto-provisioned at
http://localhost:3000 (admin / admin). It is laid out as a Z-pattern:

1. **Overview** — P&L 24h, win rate gauge, open positions, articles/hr
2. **Market & sentiment** — price by symbol, sentiment donut, aggregated
   sentiment over time (-1..+1, color-thresholded), trades per minute
3. **Decisions & positions** — signal feed, open and closed position tables
   with side, exit reason and P&L color-coded inline

A `symbol` template variable filters every panel, and signals appear as
annotations on the price/sentiment charts.

## Project layout

```
db/init.sql                          schema (hypertables)
docker-compose.yml                   profiles: infra, ingest, nlp, dashboard, spark, full
grafana/                             provisioned datasource and dashboard
src/producers/trade_producer.py      Finnhub WS -> Kafka 'trades'
src/producers/news_producer.py       Finnhub REST -> Kafka 'news'
src/consumers/db_consumer.py         Kafka -> Postgres (trades + news)
src/nlp/streaming_nlp.py             Kafka 'news' -> FinBERT -> 'news_enriched'
src/nlp/nlp_processor.py             offline batch NLP (clustering, NER)
src/decision_engine/                 state machine: sentiment cache, positions, signals
src/spark/stream_processor.py        OHLCV candles
```

## Resource budget on 8 GB

| service          | RAM cap |
|------------------|---------|
| kafka            | 1.0 G   |
| postgres         | 768 M   |
| streaming_nlp    | 700 M   |
| decision_engine  | 192 M   |
| db_consumer      | 256 M   |
| trade_producer   | 128 M   |
| news_producer    | 128 M   |
| grafana          | 256 M   |

Spark adds ~2 GB and is off by default. Turn it on only on a 16 GB box.

## Notes

- Topics are auto-created. Symbol is the partition key for trades, so
  ordering per symbol is preserved without locking.
- `decision_engine` restores open positions from Postgres on startup so a
  restart does not double-open.
- The batch NLP script (`nlp_processor.py`) is kept for offline backfill
  and clustering / NER. The streaming path uses sentiment only.
