# Real-time Market Intelligence Pipeline

Streams stock trades and financial news from Finnhub, scores headlines with
FinBERT, and emits live BUY / SELL signals with target, stop and P&L tracked
in Postgres + TimescaleDB. Dashboards in Grafana.

## What it does

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
                                              Postgres / Timescale
                                                      |
                                                      v
                                                   Grafana
```

The decision engine keeps an in-memory sentiment cache per symbol. Every
trade tick does a dict lookup, never a model call, so trade-to-decision
latency stays in single-digit milliseconds. Spark runs on the side and
rolls trades into 1m / 5m OHLCV candles for charts.

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
can run a subset on an 8GB laptop.

Linux / macOS / WSL:
```bash
cp .env.example .env          # set FINNHUB_API_KEY
make up-nlp                   # infra + ingest + NLP + decisions  (~3 GB)
make up-dash                  # add Grafana on http://localhost:3000  (admin/admin)
make smoke
make logs S=decision_engine
```

Windows cmd / PowerShell (use the bundled `make.bat`):
```cmd
copy .env.example .env
notepad .env
make up-nlp
make up-dash
make smoke
make logs S=decision_engine
```

Or just call docker compose directly:
```cmd
docker compose --profile nlp up -d --build
docker compose --profile dashboard up -d
docker compose ps
docker compose logs -f --tail=200 decision_engine
```

Profiles:

- `up-infra` Kafka + Postgres only
- `up-ingest` add producers + db_consumer
- `up-nlp` add streaming_nlp + decision_engine (recommended for 8GB)
- `up-dash` add Grafana
- `up-spark` add Spark master/worker (needs 16GB)
- `up-full` everything

## How a decision gets made

1. A news article arrives. `streaming_nlp` runs FinBERT once, gets
   `positive 0.94`, publishes to `news_enriched`, writes `nlp_results`.
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

- `OPEN_THRESHOLD` (default 0.4): how strong the sentiment must be
- `TARGET_PCT` (1.0%) and `STOP_PCT` (0.5%): exit rules
- `HORIZON_MINUTES` (240): max position duration before auto-close
- `SENTIMENT_HALF_LIFE_S` (1800): how fast old news fades

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

## Resource budget on 8GB

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

Spark adds ~2 GB and is off by default. Turn it on only on a 16GB box.

## Notes

- Topics are auto-created. Symbol is the partition key for trades, so
  ordering per symbol is preserved without locking.
- Decision engine restores open positions from Postgres on startup so a
  restart does not double-open.
- The batch NLP script (`nlp_processor.py`) is kept for offline backfill
  and clustering / NER. The streaming path uses sentiment only.
