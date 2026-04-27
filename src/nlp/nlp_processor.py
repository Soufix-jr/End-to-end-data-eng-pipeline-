"""
nlp_processor.py
----------------
Reads news articles from PostgreSQL, runs 3 HuggingFace models,
and saves enriched results back to the nlp_results table.

MODELS USED:
  1. FinBERT (ProsusAI/finbert)
     → Sentiment: positive / negative / neutral + confidence score
     → Trained specifically on financial text (not generic BERT)

  2. Sentence-Transformers (all-MiniLM-L6-v2)
     → Converts each headline into a 384-dim vector (embedding)
     → Similar headlines get similar vectors
     → KMeans clusters these vectors into topic groups

  3. BERT NER (dslim/bert-base-NER)
     → Extracts named entities: people (PER), organizations (ORG),
       locations (LOC), miscellaneous (MISC)

DATA ENGINEER LESSON — Batch Processing vs Stream Processing:
  The news producer + Kafka = stream processing (real-time, continuous)
  This NLP script = batch processing (run once, process everything)
  Both are valid. Batch is simpler and fine when you don't need
  sub-second latency. You'd switch to stream if you needed instant
  sentiment scores for algorithmic trading.

DATA ENGINEER LESSON — Model Loading:
  Loading a model from disk takes 5-10 seconds. Running inference
  on 100 articles takes ~30 seconds. So we load models ONCE at
  startup and reuse them for all articles. Never load inside a loop.
"""

import json
import logging
import os
import sys
import time

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nlp_processor")

# ── Config ───────────────────────────────────────────────────────────────────
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "pipeline")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "pipeline_secret")
POSTGRES_DB = os.getenv("POSTGRES_DB", "market_data")


def get_db_connection():
    conn = psycopg2.connect(
        host=POSTGRES_HOST, port=POSTGRES_PORT,
        user=POSTGRES_USER, password=POSTGRES_PASSWORD,
        dbname=POSTGRES_DB,
    )
    conn.autocommit = False
    return conn


# ── Step 1: Load articles from PostgreSQL ────────────────────────────────────
def load_articles(conn):
    """Fetch all articles that haven't been processed by NLP yet."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT n.id, n.headline, n.summary
            FROM raw_news n
            LEFT JOIN nlp_results r ON n.id = r.news_id
            WHERE r.news_id IS NULL
            AND n.headline IS NOT NULL
        """)
        rows = cur.fetchall()
    log.info(f"Loaded {len(rows)} unprocessed articles from PostgreSQL")
    return rows


# ── Step 2: Sentiment Analysis with FinBERT ─────────────────────────────────
def run_sentiment(articles):
    """
    Uses ProsusAI/finbert to classify each headline as
    positive, negative, or neutral with a confidence score.
    """
    from transformers import pipeline

    log.info("Loading FinBERT model...")
    classifier = pipeline(
        "sentiment-analysis",
        model="ProsusAI/finbert",
        tokenizer="ProsusAI/finbert",
        top_k=1,
        truncation=True,
        max_length=512,
    )

    headlines = [a[1] for a in articles]  # index 1 = headline

    log.info(f"Running sentiment analysis on {len(headlines)} headlines...")
    # Process in batches of 16 to avoid memory issues
    results = []
    batch_size = 16
    for i in range(0, len(headlines), batch_size):
        batch = headlines[i:i + batch_size]
        batch_results = classifier(batch)
        results.extend(batch_results)
        log.info(f"  Sentiment: processed {min(i + batch_size, len(headlines))}/{len(headlines)}")

    sentiments = []
    for result in results:
        top = result[0]  # top_k=1, so one result per headline
        sentiments.append({
            "label": top["label"],
            "score": round(top["score"], 4),
        })

    log.info("Sentiment analysis complete")
    return sentiments


# ── Step 3: Topic Clustering with Sentence-Transformers ──────────────────────
def run_topic_clustering(articles, n_clusters=5):
    """
    Converts headlines into embeddings, then clusters them with KMeans.
    Similar articles end up in the same cluster.
    """
    from sentence_transformers import SentenceTransformer
    from sklearn.cluster import KMeans
    import numpy as np

    headlines = [a[1] for a in articles]

    log.info("Loading sentence-transformer model...")
    model = SentenceTransformer("all-MiniLM-L6-v2")

    log.info(f"Computing embeddings for {len(headlines)} headlines...")
    embeddings = model.encode(headlines, show_progress_bar=True, batch_size=32)

    # Adjust n_clusters if we have fewer articles
    actual_clusters = min(n_clusters, len(headlines))

    log.info(f"Clustering into {actual_clusters} topic groups...")
    kmeans = KMeans(n_clusters=actual_clusters, random_state=42, n_init=10)
    labels = kmeans.fit_predict(embeddings)

    # Generate cluster labels by finding the most representative headline
    cluster_labels = {}
    for cluster_id in range(actual_clusters):
        cluster_indices = np.where(labels == cluster_id)[0]
        # Pick the headline closest to the cluster center
        cluster_embeddings = embeddings[cluster_indices]
        center = kmeans.cluster_centers_[cluster_id]
        distances = np.linalg.norm(cluster_embeddings - center, axis=1)
        representative_idx = cluster_indices[np.argmin(distances)]
        # Use first 50 chars of the most representative headline as label
        cluster_labels[cluster_id] = headlines[representative_idx][:50]

    topics = []
    for i, label in enumerate(labels):
        topics.append({
            "cluster": int(label),
            "label": cluster_labels[int(label)],
        })

    log.info(f"Topic clustering complete. Clusters: {list(cluster_labels.values())}")
    return topics


# ── Step 4: Named Entity Recognition ────────────────────────────────────────
def run_ner(articles):
    """
    Extracts named entities (people, organizations, locations)
    from each headline using BERT NER.
    """
    from transformers import pipeline

    log.info("Loading NER model...")
    ner = pipeline(
        "ner",
        model="dslim/bert-base-NER",
        tokenizer="dslim/bert-base-NER",
        aggregation_strategy="simple",
    )

    headlines = [a[1] for a in articles]

    log.info(f"Running NER on {len(headlines)} headlines...")
    all_entities = []
    batch_size = 16
    for i in range(0, len(headlines), batch_size):
        batch = headlines[i:i + batch_size]
        batch_results = ner(batch)
        all_entities.extend(batch_results)
        log.info(f"  NER: processed {min(i + batch_size, len(headlines))}/{len(headlines)}")

    entities_list = []
    for entities in all_entities:
        # Deduplicate and format entities
        unique = {}
        for ent in entities:
            name = ent["word"].strip()
            etype = ent["entity_group"]  # PER, ORG, LOC, MISC
            if name not in unique:
                unique[name] = etype
        entities_list.append(
            [{"name": k, "type": v} for k, v in unique.items()]
        )

    log.info("NER complete")
    return entities_list


# ── Step 5: Save results to PostgreSQL ───────────────────────────────────────
def save_results(conn, articles, sentiments, topics, entities):
    """Insert all NLP results into the nlp_results table."""
    values = []
    for i, article in enumerate(articles):
        article_id = article[0]  # index 0 = id
        values.append((
            article_id,
            sentiments[i]["label"],
            sentiments[i]["score"],
            topics[i]["cluster"],
            topics[i]["label"],
            json.dumps(entities[i]),
            None,  # categories (zero-shot, not implemented yet)
            None,  # keywords (not implemented yet)
            "finbert+minilm+bert-ner",
        ))

    query = """
        INSERT INTO nlp_results
            (news_id, sentiment, sentiment_score, topic_cluster, topic_label,
             entities, categories, keywords, model_name)
        VALUES %s
        ON CONFLICT (news_id) DO NOTHING
    """

    with conn.cursor() as cur:
        execute_values(cur, query, values)
    conn.commit()
    log.info(f"Saved NLP results for {len(values)} articles to PostgreSQL")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    start_time = time.time()
    conn = get_db_connection()

    # Load unprocessed articles
    articles = load_articles(conn)
    if not articles:
        log.info("No unprocessed articles found. Exiting.")
        return

    # To save RAM on an 8GB machine, we initialize empty lists
    sentiments = []
    topics = []
    entities = []

    try:
        # Load and run Model 1 (FinBERT)
        sentiments = run_sentiment(articles)
    except Exception as e:
        log.error(f"Sentiment failed: {e}")

    # Force garbage collection to free up the ~400MB of RAM FinBERT used
    import gc
    gc.collect()
    log.info("Cleared FinBERT from memory to save RAM.")

    try:
        # Load and run Model 2 (MiniLM)
        topics = run_topic_clustering(articles)
    except Exception as e:
        log.error(f"Topic Clustering failed: {e}")

    gc.collect()
    log.info("Cleared MiniLM from memory.")

    try:
        # Load and run Model 3 (BERT-NER)
        entities = run_ner(articles)
    except Exception as e:
        log.error(f"NER failed: {e}")

    gc.collect()
    log.info("Cleared BERT-NER from memory.")

    # Only save if we got results (checking lengths match)
    valid_count = min(len(sentiments), len(topics), len(entities))
    if valid_count > 0:
        save_results(conn, articles[:valid_count], sentiments[:valid_count], topics[:valid_count], entities[:valid_count])
        elapsed = round(time.time() - start_time, 1)
        log.info(f"NLP pipeline complete. Processed {valid_count} articles in {elapsed}s")

        print("\n" + "=" * 70)
        print("SAMPLE RESULTS (first 5 articles)")
        print("=" * 70)
        for i in range(min(5, valid_count)):
            print(f"\n📰 {articles[i][1]}")
            print(f"   Sentiment:  {sentiments[i]['label']} ({sentiments[i]['score']})")
            print(f"   Topic:      Cluster {topics[i]['cluster']} — {topics[i]['label']}")
            print(f"   Entities:   {[e['name'] for e in entities[i]]}")
    else:
        log.error("Failed to process articles. Check logs above.")

    conn.close()


if __name__ == "__main__":
    main()
