# Makefile for Data Engineering Pipeline

# Variables
DC = docker compose

.PHONY: help up-all up-kafka up-db up-kafka-db up-kafka-spark down clean logs

help:
	@echo "Pipeline Commands for 8GB and 16GB Machines"
	@echo "------------------------------------------------"
	@echo "For 16GB Machine (Full Stack):"
	@echo "  make up-all         - Start all services (Kafka, DB, Spark, NLP, Airflow)"
	@echo ""
	@echo "For 8GB Machine (Start-Test-Kill method):"
	@echo "  make up-kafka       - Start only Kafka (~800MB RAM)"
	@echo "  make up-db          - Start only PostgreSQL (~300MB RAM)"
	@echo "  make up-kafka-db    - Start Kafka + PostgreSQL (~1.1GB RAM)"
	@echo "  make up-kafka-spark - Start Kafka + Spark Worker (~2.5GB RAM)"
	@echo ""
	@echo "Utility:"
	@echo "  make down           - Stop all services and free RAM"
	@echo "  make clean          - Stop services and remove volumes (WIPES DB!)"
	@echo "  make logs           - View logs for all running services"

# 16GB Machine - Run everything
up-all:
	$(DC) up -d

# 8GB Machine - Isolate components
up-kafka:
	$(DC) up -d kafka

up-db:
	$(DC) up -d postgres

up-kafka-db:
	$(DC) up -d kafka postgres

up-kafka-spark:
	$(DC) up -d kafka spark-master spark-worker

# Utilities
down:
	$(DC) down

clean:
	$(DC) down -v

logs:
	$(DC) logs -f
