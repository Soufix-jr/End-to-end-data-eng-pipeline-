COMPOSE := docker compose

.PHONY: help up-infra up-ingest up-nlp up-dash up-spark up-full down logs ps psql topics smoke clean

help:
	@echo "make up-infra      Kafka + Postgres only"
	@echo "make up-ingest     + producers + db_consumer            (8GB OK)"
	@echo "make up-nlp        + streaming_nlp + decision_engine    (8GB OK)"
	@echo "make up-dash       + Grafana on http://localhost:3000   (admin/admin)"
	@echo "make up-spark      + Spark master/worker                (16GB only)"
	@echo "make up-full       everything"
	@echo "make down          stop everything"
	@echo "make logs S=name   tail logs for a service"
	@echo "make psql          open psql shell"
	@echo "make topics        list Kafka topics"
	@echo "make smoke         quick row counts in each table"

up-infra:
	$(COMPOSE) --profile infra up -d
up-ingest:
	$(COMPOSE) --profile ingest up -d --build
up-nlp:
	$(COMPOSE) --profile nlp up -d --build
up-dash:
	$(COMPOSE) --profile dashboard up -d
up-spark:
	$(COMPOSE) --profile spark up -d --build
up-full:
	$(COMPOSE) --profile full up -d --build

down:
	$(COMPOSE) --profile full down

logs:
	$(COMPOSE) logs -f --tail=200 $(S)

ps:
	$(COMPOSE) ps

psql:
	docker exec -it postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB

topics:
	docker exec -it kafka kafka-topics --bootstrap-server localhost:9092 --list

smoke:
	@echo "[topics]";       docker exec kafka kafka-topics --bootstrap-server localhost:9092 --list
	@echo "[raw_trades]";   docker exec postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB -c "SELECT count(*) FROM raw_trades;"
	@echo "[raw_news]";     docker exec postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB -c "SELECT count(*) FROM raw_news;"
	@echo "[nlp_results]";  docker exec postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB -c "SELECT sentiment, count(*) FROM nlp_results GROUP BY 1;"
	@echo "[signals]";      docker exec postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB -c "SELECT action, count(*) FROM signals GROUP BY 1;"
	@echo "[positions]";    docker exec postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB -c "SELECT status, count(*), ROUND(AVG(pnl_pct)::numeric, 3) AS avg_pnl FROM positions GROUP BY 1;"

clean:
	$(COMPOSE) --profile full down -v
