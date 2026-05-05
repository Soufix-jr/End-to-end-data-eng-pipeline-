"""
stream_processor.py
-------------------
Spark Structured Streaming job that reads raw trades from Kafka
and computes OHLCV (Open, High, Low, Close, Volume) candles.

WHAT IS OHLCV?
  For every time window (1 minute, 5 minutes), we compute:
  - Open  = first price in the window
  - High  = highest price
  - Low   = lowest price
  - Close = last price
  - Volume = total shares traded
  This is how candlestick charts on TradingView/Yahoo Finance are built.

SPARK STRUCTURED STREAMING CONCEPTS:
  - Reads from Kafka as an unbounded table (new rows keep arriving)
  - Windowed aggregation: groups data into time windows
  - Watermark: tells Spark how late data can be before it's dropped
    (e.g., 30s means a trade arriving 30s late still gets counted)
  - Checkpoint: saves processing state to disk so Spark can recover
    from crashes without losing or duplicating data (exactly-once)

DATA ENGINEER LESSON - When to use Spark vs plain Python:
  100 articles/min → plain Python is fine
  10,000 trades/sec → you NEED Spark for windowed aggregations
  Spark adds overhead (JVM, cluster setup), so don't use it when
  a simple Python script would do the job.
"""

import os
from pyspark.sql import SparkSession
from pyspark.sql.functions import (
    col, from_json, window, first, last, max as spark_max,
    min as spark_min, sum as spark_sum, from_unixtime
)
from pyspark.sql.types import (
    StructType, StructField, StringType, DoubleType, LongType
)

# ── Config ───────────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092")
POSTGRES_URL = os.getenv("POSTGRES_URL", "jdbc:postgresql://postgres:5432/market_data")
POSTGRES_USER = os.getenv("POSTGRES_USER", "pipeline")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "pipeline_secret")
CHECKPOINT_DIR = "/tmp/spark-checkpoints"

# ── Schema for trade data coming from Kafka ──────────────────────────────────
trade_schema = StructType([
    StructField("s", StringType(), True),      # symbol
    StructField("p", DoubleType(), True),       # price
    StructField("t", LongType(), True),         # timestamp (UNIX ms)
    StructField("v", DoubleType(), True),       # volume
    StructField("ingested_at", LongType(), True),
])


def write_to_postgres(batch_df, batch_id, table_name):
    """Write a micro-batch to PostgreSQL via JDBC."""
    if batch_df.count() == 0:
        return

    batch_df.write \
        .format("jdbc") \
        .option("url", POSTGRES_URL) \
        .option("dbtable", table_name) \
        .option("user", POSTGRES_USER) \
        .option("password", POSTGRES_PASSWORD) \
        .option("driver", "org.postgresql.Driver") \
        .mode("append") \
        .save()


def main():
    # ── Create Spark session ─────────────────────────────────────────────────
    spark = SparkSession.builder \
        .appName("TradeStreamProcessor") \
        .config("spark.jars.packages",
                "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.3,"
                "org.postgresql:postgresql:42.7.1") \
        .getOrCreate()

    spark.sparkContext.setLogLevel("WARN")

    # ── Read from Kafka ──────────────────────────────────────────────────────
    raw_stream = spark.readStream \
        .format("kafka") \
        .option("kafka.bootstrap.servers", KAFKA_BOOTSTRAP_SERVERS) \
        .option("subscribe", "trades") \
        .option("startingOffsets", "latest") \
        .option("maxOffsetsPerTrigger", 10000) \
        .load()

    # ── Parse the JSON payload ───────────────────────────────────────────────
    trades = raw_stream \
        .select(from_json(col("value").cast("string"), trade_schema).alias("data")) \
        .select("data.*") \
        .withColumn("event_time", from_unixtime(col("t") / 1000).cast("timestamp")) \
        .withWatermark("event_time", "30 seconds")

    # ── 1-Minute OHLCV Candles ───────────────────────────────────────────────
    ohlcv_1m = trades \
        .groupBy(
            window(col("event_time"), "1 minute"),
            col("s").alias("symbol")
        ) \
        .agg(
            first("p").alias("open"),
            spark_max("p").alias("high"),
            spark_min("p").alias("low"),
            last("p").alias("close"),
            spark_sum("v").alias("volume"),
        ) \
        .select(
            col("window.start").alias("time"),
            col("symbol"),
            col("open"), col("high"), col("low"), col("close"), col("volume"),
        )

    # ── 5-Minute OHLCV Candles ───────────────────────────────────────────────
    ohlcv_5m = trades \
        .groupBy(
            window(col("event_time"), "5 minutes"),
            col("s").alias("symbol")
        ) \
        .agg(
            first("p").alias("open"),
            spark_max("p").alias("high"),
            spark_min("p").alias("low"),
            last("p").alias("close"),
            spark_sum("v").alias("volume"),
        ) \
        .select(
            col("window.start").alias("time"),
            col("symbol"),
            col("open"), col("high"), col("low"), col("close"), col("volume"),
        )

    # ── Write 1-min candles to PostgreSQL ────────────────────────────────────
    query_1m = ohlcv_1m.writeStream \
        .outputMode("append") \
        .foreachBatch(lambda df, id: write_to_postgres(df, id, "ohlcv_1m")) \
        .option("checkpointLocation", f"{CHECKPOINT_DIR}/ohlcv_1m") \
        .trigger(processingTime="30 seconds") \
        .start()

    # ── Write 5-min candles to PostgreSQL ────────────────────────────────────
    query_5m = ohlcv_5m.writeStream \
        .outputMode("append") \
        .foreachBatch(lambda df, id: write_to_postgres(df, id, "ohlcv_5m")) \
        .option("checkpointLocation", f"{CHECKPOINT_DIR}/ohlcv_5m") \
        .trigger(processingTime="60 seconds") \
        .start()

    print("Spark streaming started. Computing OHLCV candles from trades...")
    query_1m.awaitTermination()


if __name__ == "__main__":
    main()
