-- ============================================================================
-- PostgreSQL + TimescaleDB Schema for Market Intelligence Pipeline
-- ============================================================================
-- This file runs automatically when the PostgreSQL container starts for the
-- first time (mounted to /docker-entrypoint-initdb.d/).
--
-- DATA ENGINEER LESSON — Hypertables:
--   TimescaleDB is a PostgreSQL extension that auto-partitions tables by time.
--   Regular PostgreSQL stores everything in one big heap. TimescaleDB splits
--   data into "chunks" (e.g., one chunk per day). This makes time-range
--   queries (last 24h, last week) dramatically faster because it only scans
--   relevant chunks instead of the entire table.

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── Raw Trade Data ──────────────────────────────────────────────────────────
-- Every trade tick from Finnhub lands here.
-- Later, Spark aggregates these into OHLCV candles.
CREATE TABLE IF NOT EXISTS raw_trades (
    time        TIMESTAMPTZ NOT NULL,   -- when the trade happened
    symbol      TEXT NOT NULL,           -- e.g., "AAPL"
    price       DOUBLE PRECISION,       -- trade price
    volume      DOUBLE PRECISION,       -- shares traded
    conditions  TEXT,                    -- trade condition codes
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

-- Convert to hypertable (auto-partition by time)
SELECT create_hypertable('raw_trades', 'time', if_not_exists => TRUE);

-- ── Raw News Articles ───────────────────────────────────────────────────────
-- Every article from Finnhub's general news endpoint.
CREATE TABLE IF NOT EXISTS raw_news (
    id          BIGINT PRIMARY KEY,     -- Finnhub article ID (for dedup)
    category    TEXT,                   -- "general", "company", etc.
    headline    TEXT NOT NULL,
    summary     TEXT,
    source      TEXT,                   -- "Reuters", "CNBC", etc.
    url         TEXT,
    image_url   TEXT,
    related     TEXT,                   -- related stock symbols
    published_at TIMESTAMPTZ,           -- article publish time
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── OHLCV Candles (1-minute) ────────────────────────────────────────────────
-- Spark computes these from raw_trades using windowed aggregation.
CREATE TABLE IF NOT EXISTS ohlcv_1m (
    time    TIMESTAMPTZ NOT NULL,
    symbol  TEXT NOT NULL,
    open    DOUBLE PRECISION,
    high    DOUBLE PRECISION,
    low     DOUBLE PRECISION,
    close   DOUBLE PRECISION,
    volume  DOUBLE PRECISION
);
SELECT create_hypertable('ohlcv_1m', 'time', if_not_exists => TRUE);

-- ── OHLCV Candles (5-minute) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ohlcv_5m (
    time    TIMESTAMPTZ NOT NULL,
    symbol  TEXT NOT NULL,
    open    DOUBLE PRECISION,
    high    DOUBLE PRECISION,
    low     DOUBLE PRECISION,
    close   DOUBLE PRECISION,
    volume  DOUBLE PRECISION
);
SELECT create_hypertable('ohlcv_5m', 'time', if_not_exists => TRUE);

-- ── NLP Enrichment Results ──────────────────────────────────────────────────
-- One row per article after HuggingFace models process it.
CREATE TABLE IF NOT EXISTS nlp_results (
    news_id         BIGINT UNIQUE REFERENCES raw_news(id),
    sentiment       TEXT,               -- "positive", "negative", "neutral"
    sentiment_score DOUBLE PRECISION,   -- confidence 0.0-1.0
    topic_cluster   INTEGER,            -- cluster ID from KMeans
    topic_label     TEXT,               -- human-readable cluster name
    entities        JSONB,              -- extracted entities as JSON array
    categories      JSONB,              -- zero-shot classification results
    keywords        JSONB,              -- extracted keywords
    model_name      TEXT,               -- which model produced this
    processed_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Trading Signals ─────────────────────────────────────────────────────────
-- Correlation between sentiment and price movement.
CREATE TABLE IF NOT EXISTS trading_signals (
    time            TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    signal_type     TEXT,               -- "sentiment_divergence", "volume_spike"
    sentiment_score DOUBLE PRECISION,
    price_change    DOUBLE PRECISION,
    description     TEXT
);
SELECT create_hypertable('trading_signals', 'time', if_not_exists => TRUE);

-- ── Indexes for common queries ──────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON raw_trades (symbol, time DESC);
CREATE INDEX IF NOT EXISTS idx_news_source ON raw_news (source, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_published ON raw_news (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_nlp_sentiment ON nlp_results (sentiment, sentiment_score DESC);
