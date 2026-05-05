# Pipeline temps reel d'intelligence de marche

Pipeline complet qui ingere les ticks et la presse Finnhub, score chaque
depeche avec FinBERT, emet des decisions BUY / SELL avec cible, stop et
P&L, le tout stocke dans Postgres + TimescaleDB et observe dans Grafana.

```
            (Topics Kafka en italique)

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

Le moteur de decision tient un cache de sentiment en memoire par symbole.
Chaque tick se traduit par une simple recherche dans un dictionnaire,
jamais par un appel au modele, ce qui maintient la latence trade vers
decision sous les 10 ms. Spark calcule en parallele des chandelles
OHLCV 1 minute et 5 minutes.

## Composants

| couche       | composant                              | role                                    |
|--------------|----------------------------------------|-----------------------------------------|
| ingestion    | `trade_producer`, `news_producer`      | Finnhub WS / REST -> Kafka              |
| stockage     | Postgres + TimescaleDB                 | trades, news, OHLCV, signals, positions |
| NLP          | `streaming_nlp` (FinBERT)              | sentiment par article, ~150 ms / titre  |
| decisions    | `decision_engine`                      | cache de sentiment + cycle de position  |
| analyses     | Spark Structured Streaming             | chandelles OHLCV 1m / 5m                |
| visualisation| Grafana (approvisionne)                | KPIs, courbes, journal des signaux      |

## Tables

| table             | contenu                                            |
|-------------------|----------------------------------------------------|
| `raw_trades`      | chaque tick Finnhub (hypertable)                   |
| `raw_news`        | chaque article (deduplique par identifiant)        |
| `nlp_results`     | score FinBERT par article                          |
| `ohlcv_1m`, `_5m` | chandelles calculees par Spark                     |
| `signals`         | journal des decisions OPEN et CLOSE                |
| `positions`       | une ligne par trade (entree, cible, stop, PnL)     |

---

# Guide de demarrage de A a Z

Ce guide vous emmene de zero (ordinateur sans rien) a un tableau de bord
Grafana qui affiche les decisions en direct.

## 1. Prerequis

Avant tout, installez :

| outil               | version minimale | verification                      |
|---------------------|------------------|-----------------------------------|
| Git                 | 2.30             | `git --version`                   |
| Docker Desktop      | 4.20             | `docker --version`                |
| Docker Compose v2   | inclus           | `docker compose version`          |

Sur Windows, pensez a **demarrer Docker Desktop** avant tout
(icone baleine dans la barre des taches, etat *Engine running*).

Memoire RAM recommandee : **8 Go libres** pour le profil par defaut,
**16 Go** si vous activez Spark.

## 2. Cle API Finnhub (gratuite)

1. Creez un compte sur https://finnhub.io.
2. Recuperez votre cle dans *Dashboard > API keys*.
3. Gardez-la sous la main, vous la collerez a l'etape 4.

## 3. Recuperer le code

Clonez le depot et basculez sur la branche `8GB` (la plus testee, optimisee
pour 8 Go de RAM) :

```bash
git clone https://github.com/Soufix-jr/End-to-end-data-eng-pipeline-.git
cd End-to-end-data-eng-pipeline-
git checkout 8GB
git pull origin 8GB
```

Si vous avez deja le depot, mettez-le a jour :

```bash
cd End-to-end-data-eng-pipeline-
git fetch --all
git checkout 8GB
git pull origin 8GB
```

## 4. Configurer la cle Finnhub

Copiez le modele d'environnement et editez-le pour y coller votre cle :

**Linux / macOS / WSL**
```bash
cp .env.example .env
nano .env          # ou vim, code, etc.
```

**Windows cmd ou PowerShell**
```cmd
copy .env.example .env
notepad .env
```

Dans `.env`, remplissez au minimum :

```
FINNHUB_API_KEY=votre_cle_ici
```

Les autres variables (`OPEN_THRESHOLD`, `TARGET_PCT`, `STOP_PCT`,
`HORIZON_MINUTES`, `SENTIMENT_HALF_LIFE_S`) ont des valeurs par defaut
raisonnables, vous les laisserez telles quelles pour le premier
demarrage.

## 5. Premier demarrage

La pile est decoupee en **profils Compose**, du plus minimal au plus
complet. Pour une demonstration tableau de bord complete, lancez :

**Linux / macOS / WSL**
```bash
make up-nlp              # infra + producteurs + NLP + decisions  (~3 Go)
make up-dash             # ajoute Grafana sur http://localhost:3000
```

**Windows cmd ou PowerShell** (le projet fournit `make.bat`)
```cmd
make up-nlp
make up-dash
```

**Sans Make, en docker compose direct**
```bash
docker compose --profile nlp up -d --build
docker compose --profile dashboard up -d
```

Le premier `up` prend une a deux minutes (telechargement et construction
des images, telechargement de FinBERT depuis HuggingFace, ~440 Mo).
Les fois suivantes c'est moins de 30 secondes.

## 6. Verifier que tout est sain

Listez les conteneurs :

```bash
docker compose ps
```

Vous devez voir 8 lignes au statut `Up`, dont `postgres (healthy)` et
`kafka (healthy)`. Sinon, voyez la section **Depannage** plus bas.

Test de fumee, qui compte les lignes par table :

```bash
make smoke
```

Sortie attendue (exemple) :

```
[topics]      news, news_enriched, trades
[raw_trades]  count : milliers (en heures de marche)
[raw_news]    count : 100+
[nlp_results] sentiment reparti positive / negative / neutral
[signals]     OPEN_LONG, OPEN_SHORT, CLOSE
[positions]   OPEN et CLOSED, avg_pnl mesurable
```

Suivre les journaux d'un service :

```bash
make logs S=streaming_nlp
make logs S=decision_engine
```

## 7. Ouvrir Grafana

1. Ouvrez http://localhost:3000 dans votre navigateur.
2. Identifiants par defaut : **admin** / **admin**. Vous pouvez cliquer
   *Skip* lors du changement de mot de passe pour un usage local.
3. Menu de gauche > *Dashboards* > **Market Intelligence Pipeline**.

Vous y trouverez trois sections :

- **Overview** : P&L 24h, taux de reussite, positions ouvertes, articles
  scores la derniere heure.
- **Market & sentiment** : prix par symbole, donut de distribution des
  sentiments, score de sentiment agrege, trades par minute.
- **Decisions & positions** : journal des signaux et tables des positions
  ouvertes et fermees.

Le tableau de bord se rafraichit toutes les 5 secondes.

## 8. Comportement hors heures de marche

Finnhub diffuse les ticks **uniquement pendant les heures de marche
americaines (lundi au vendredi, 14:30 a 21:00 UTC)**. En dehors :

- Les depeches presse continuent d'arriver via REST. Le compteur
  *Articles scored (1h)* monte normalement.
- Les ticks de transaction sont vides. Les panneaux *Price by symbol*,
  *Trades per minute*, le journal des signaux et les tables de positions
  affichent *No data*. **C'est attendu**, le moteur n'a aucune donnee
  pour ouvrir une position.

Pour faire une demonstration en dehors des heures de marche, vous pouvez
injecter des ticks artificiels :

```bash
docker compose exec postgres psql -U postgres -d marketdata -c "INSERT INTO raw_trades(time, symbol, price, volume) SELECT now() - (s || ' seconds')::interval, sym, base + (random()-0.5)*2, 100+random()*900 FROM generate_series(1,600) s, (VALUES ('AAPL',180.0),('MSFT',420.0),('TSLA',240.0),('NVDA',900.0),('AMZN',180.0)) AS v(sym,base);"
docker compose restart decision_engine
```

3000 ticks vont apparaitre dans Grafana et le moteur emettra des signaux
sur le rechargement.

## 9. Arret et nettoyage

Arret simple (les volumes restent, les donnees aussi) :

```bash
docker compose down
```

Arret avec suppression des volumes (efface trades, news, NLP, positions) :

```bash
docker compose down -v
```

## 10. Profils disponibles

| profil       | services inclus                                                  |
|--------------|------------------------------------------------------------------|
| `up-infra`   | Kafka et Postgres                                                |
| `up-ingest`  | + producteurs et `db_consumer`                                   |
| `up-nlp`     | + `streaming_nlp` et `decision_engine` **(recommande pour 8 Go)**|
| `up-dash`    | + Grafana                                                        |
| `up-spark`   | + Spark master et worker (16 Go necessaires)                     |
| `up-full`    | tout                                                             |

---

# Comment une decision est prise

1. Une depeche arrive. `streaming_nlp` execute FinBERT une seule fois,
   obtient par exemple `positive 0.94`, publie sur `news_enriched` et
   ecrit dans `nlp_results`.
2. `decision_engine` met a jour son cache de sentiment par symbole. Les
   anciens scores decroissent avec une demi-vie de 30 minutes.
3. Un tick arrive pour le meme symbole. Le moteur :
   1. verifie si une position ouverte a touche sa cible, son stop ou
      son expiration, et la ferme le cas echeant ;
   2. sinon, si aucune position n'est ouverte et que le sentiment agrege
      depasse `OPEN_THRESHOLD`, ouvre une nouvelle position LONG (positif)
      ou SHORT (negatif) avec cible = entree +/- `TARGET_PCT` et
      stop +/- `STOP_PCT`.
4. Les deux evenements sont ecrits dans `signals` et la position vit sa
   vie dans `positions`. Le P&L est calcule a la cloture.

# Reglages

Tout dans `.env` :

| variable                | defaut | role                                      |
|-------------------------|--------|-------------------------------------------|
| `OPEN_THRESHOLD`        | `0.4`  | magnitude minimale pour ouvrir            |
| `TARGET_PCT`            | `1.0`  | distance prise de profit (%)              |
| `STOP_PCT`              | `0.5`  | distance stop-loss (%)                    |
| `HORIZON_MINUTES`       | `240`  | duree maximale d'une position avant cloture auto |
| `SENTIMENT_HALF_LIFE_S` | `1800` | vitesse de fading des anciennes news      |

# Depannage

**Docker Compose dit `Cannot start a paused container`**
Un conteneur a ete mis en pause manuellement dans Docker Desktop.
Reactivez-le :
```bash
docker unpause postgres kafka
```

**Le port 3000 ou 5432 est deja occupe**
Une autre instance tourne. Stoppez-la ou modifiez le mappage dans
`docker-compose.yml`.

**`localhost:3000` refuse la connexion alors que les conteneurs sont up**
Essayez `http://127.0.0.1:3000`. Sur Windows, `localhost` resout parfois
en IPv6, non lie par le port forwarding Docker.

**`raw_trades` reste vide en heures de marche**
Verifiez les journaux :
```bash
docker compose logs --tail=50 trade_producer
```
Si vous voyez `WebSocket connected. Subscribing to: [...]` et rien
d'autre, c'est que la cle API est invalide ou hors quota. Generez une
nouvelle cle sur Finnhub et relancez :
```bash
docker compose restart trade_producer
```

**Reconstruire une image apres modification du code**
```bash
docker compose --profile nlp up -d --build streaming_nlp
```

**Tout reinitialiser de zero**
```bash
docker compose down -v
docker system prune -f
make up-nlp
make up-dash
```

# Empreinte memoire sur 8 Go

| service          | RAM   |
|------------------|-------|
| kafka            | 1.0 G |
| postgres         | 768 M |
| streaming_nlp    | 700 M |
| decision_engine  | 192 M |
| db_consumer      | 256 M |
| trade_producer   | 128 M |
| news_producer    | 128 M |
| grafana          | 256 M |

Spark ajoute environ 2 Go et reste desactive par defaut.

# Arborescence

```
db/init.sql                          schema (hypertables)
docker-compose.yml                   profils : infra, ingest, nlp, dashboard, spark, full
grafana/                             datasource et dashboard approvisionnes
src/producers/trade_producer.py      Finnhub WS -> Kafka 'trades'
src/producers/news_producer.py       Finnhub REST -> Kafka 'news'
src/consumers/db_consumer.py         Kafka -> Postgres
src/nlp/streaming_nlp.py             Kafka 'news' -> FinBERT -> 'news_enriched'
src/nlp/nlp_processor.py             NLP batch hors-ligne (clustering, NER)
src/decision_engine/                 cache de sentiment, positions, signaux
src/spark/stream_processor.py        chandelles OHLCV
```

# Notes

- Les topics Kafka sont auto-crees. Le symbole sert de cle de partition
  pour `trades`, ce qui preserve l'ordre par titre sans verrou.
- Le moteur de decision restaure les positions ouvertes depuis Postgres
  au demarrage, donc un redemarrage ne provoque pas de double ouverture.
- Le script NLP batch (`nlp_processor.py`) est conserve pour les
  retraitements hors-ligne (clustering, NER). Le chemin temps reel
  n'utilise que le sentiment.
