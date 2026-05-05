-- =============================================================================
-- PostgreSQL + TimescaleDB schema for the market intelligence pipeline.
-- Runs automatically on first Postgres startup via /docker-entrypoint-initdb.d/.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Raw trade ticks from Finnhub (one row per executed trade).
CREATE TABLE IF NOT EXISTS raw_trades (
    time        TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    price       DOUBLE PRECISION,
    volume      DOUBLE PRECISION,
    conditions  TEXT,
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);
SELECT create_hypertable('raw_trades', 'time', if_not_exists => TRUE);

-- Dedup key. Time must be in the unique index for hypertables.
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_trades_symbol_time
    ON raw_trades (symbol, time);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON raw_trades (symbol, time DESC);


-- Raw news articles from Finnhub /news.
CREATE TABLE IF NOT EXISTS raw_news (
    id           BIGINT PRIMARY KEY,
    category     TEXT,
    headline     TEXT NOT NULL,
    summary      TEXT,
    source       TEXT,
    url          TEXT,
    image_url    TEXT,
    related      TEXT,
    published_at TIMESTAMPTZ,
    ingested_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_news_published ON raw_news (published_at DESC);


-- 1-minute and 5-minute OHLCV candles, written by Spark.
CREATE TABLE IF NOT EXISTS ohlcv_1m (
    time   TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    open   DOUBLE PRECISION,
    high   DOUBLE PRECISION,
    low    DOUBLE PRECISION,
    close  DOUBLE PRECISION,
    volume DOUBLE PRECISION
);
SELECT create_hypertable('ohlcv_1m', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ohlcv1m_symbol ON ohlcv_1m (symbol, time DESC);

CREATE TABLE IF NOT EXISTS ohlcv_5m (
    time   TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    open   DOUBLE PRECISION,
    high   DOUBLE PRECISION,
    low    DOUBLE PRECISION,
    close  DOUBLE PRECISION,
    volume DOUBLE PRECISION
);
SELECT create_hypertable('ohlcv_5m', 'time', if_not_exists => TRUE);


-- FinBERT scores written by streaming_nlp, one row per article.
CREATE TABLE IF NOT EXISTS nlp_results (
    news_id         BIGINT UNIQUE REFERENCES raw_news(id),
    sentiment       TEXT,
    sentiment_score DOUBLE PRECISION,
    topic_cluster   INTEGER,
    topic_label     TEXT,
    entities        JSONB,
    categories      JSONB,
    keywords        JSONB,
    model_name      TEXT,
    processed_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_nlp_sentiment
    ON nlp_results (sentiment, sentiment_score DESC);


-- Signals: every entry or exit recommendation emitted by the decision engine.
-- This is an audit log of what the model said and when.
CREATE TABLE IF NOT EXISTS signals (
    time        TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    action      TEXT NOT NULL,           -- OPEN_LONG, OPEN_SHORT, CLOSE
    price       DOUBLE PRECISION,
    confidence  DOUBLE PRECISION,
    horizon     TEXT,                    -- intraday, 1d, 5d
    reason      TEXT,
    position_id BIGINT
);
SELECT create_hypertable('signals', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, time DESC);


-- Positions: a position lives from entry to exit. One row per trade.
-- This is the table that powers the P&L scoreboard.
CREATE TABLE IF NOT EXISTS positions (
    id            BIGSERIAL PRIMARY KEY,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,         -- LONG or SHORT
    status        TEXT NOT NULL,         -- OPEN or CLOSED
    entry_time    TIMESTAMPTZ NOT NULL,
    entry_price   DOUBLE PRECISION NOT NULL,
    target_price  DOUBLE PRECISION,
    stop_price    DOUBLE PRECISION,
    valid_until   TIMESTAMPTZ,
    confidence    DOUBLE PRECISION,
    exit_time     TIMESTAMPTZ,
    exit_price    DOUBLE PRECISION,
    exit_reason   TEXT,                  -- target_hit, stop_hit, expired, flip
    pnl_pct       DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status, symbol);
CREATE INDEX IF NOT EXISTS idx_positions_entry  ON positions (entry_time DESC);
