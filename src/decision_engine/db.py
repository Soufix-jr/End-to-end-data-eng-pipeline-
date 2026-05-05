"""Thin DB layer for the decision engine. All SQL lives here."""

import logging
import os
import sys
import time
from datetime import datetime

import psycopg2

log = logging.getLogger("decision_engine.db")


def connect():
    cfg = dict(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        user=os.getenv("POSTGRES_USER", "pipeline"),
        password=os.getenv("POSTGRES_PASSWORD", "pipeline_secret"),
        dbname=os.getenv("POSTGRES_DB", "market_data"),
    )
    for attempt in range(10):
        try:
            conn = psycopg2.connect(**cfg)
            conn.autocommit = True
            log.info("Connected to PostgreSQL")
            return conn
        except psycopg2.OperationalError as e:
            log.warning(f"DB connect attempt {attempt+1}/10 failed: {e}")
            time.sleep(3)
    log.error("Could not connect to PostgreSQL")
    sys.exit(1)


class DB:
    def __init__(self, conn):
        self._conn = conn

    def insert_signal(
        self, time_: datetime, symbol: str, action: str, price: float,
        confidence: float, horizon: str, reason: str, position_id
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals
                    (time, symbol, action, price, confidence, horizon, reason, position_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (time_, symbol, action, price, confidence, horizon, reason, position_id),
            )

    def insert_position(
        self, symbol: str, side: str, entry_time: datetime, entry_price: float,
        target: float, stop: float, valid_until: datetime, confidence: float
    ) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO positions
                    (symbol, side, status, entry_time, entry_price,
                     target_price, stop_price, valid_until, confidence)
                VALUES (%s, %s, 'OPEN', %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (symbol, side, entry_time, entry_price, target, stop, valid_until, confidence),
            )
            return cur.fetchone()[0]

    def close_position(
        self, position_id: int, exit_time: datetime, exit_price: float,
        reason: str, pnl: float
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE positions
                SET status='CLOSED', exit_time=%s, exit_price=%s,
                    exit_reason=%s, pnl_pct=%s
                WHERE id=%s
                """,
                (exit_time, exit_price, reason, pnl, position_id),
            )

    def fetch_open_positions(self):
        """Reload open positions on engine restart so we don't double-open."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, side, entry_time, entry_price,
                       target_price, stop_price, valid_until, confidence
                FROM positions
                WHERE status='OPEN'
                """
            )
            return cur.fetchall()
