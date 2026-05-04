@echo off
REM Tiny wrapper so the Makefile commands also work from Windows cmd.exe.
REM Usage: make up-nlp / make up-dash / make smoke / make logs S=decision_engine

setlocal enabledelayedexpansion
set CMD=%~1
set ARG=%~2

if "%CMD%"=="" goto :help
if /I "%CMD%"=="help"      goto :help
if /I "%CMD%"=="up-infra"  goto :up_infra
if /I "%CMD%"=="up-ingest" goto :up_ingest
if /I "%CMD%"=="up-nlp"    goto :up_nlp
if /I "%CMD%"=="up-dash"   goto :up_dash
if /I "%CMD%"=="up-spark"  goto :up_spark
if /I "%CMD%"=="up-full"   goto :up_full
if /I "%CMD%"=="down"      goto :down
if /I "%CMD%"=="ps"        goto :ps
if /I "%CMD%"=="psql"      goto :psql
if /I "%CMD%"=="topics"    goto :topics
if /I "%CMD%"=="logs"      goto :logs
if /I "%CMD%"=="smoke"     goto :smoke
if /I "%CMD%"=="clean"     goto :clean

echo Unknown command: %CMD%
goto :help

:help
echo make up-infra      Kafka + Postgres only
echo make up-ingest     + producers + db_consumer
echo make up-nlp        + streaming_nlp + decision_engine    (recommended for 8GB)
echo make up-dash       + Grafana on http://localhost:3000   (admin/admin)
echo make up-spark      + Spark master/worker                (16GB only)
echo make up-full       everything
echo make down          stop everything
echo make logs S=name   tail logs for a service
echo make psql          open psql shell
echo make topics        list Kafka topics
echo make smoke         quick row counts in each table
goto :eof

:up_infra
docker compose --profile infra up -d
goto :eof
:up_ingest
docker compose --profile ingest up -d --build
goto :eof
:up_nlp
docker compose --profile nlp up -d --build
goto :eof
:up_dash
docker compose --profile dashboard up -d
goto :eof
:up_spark
docker compose --profile spark up -d --build
goto :eof
:up_full
docker compose --profile full up -d --build
goto :eof
:down
docker compose --profile full down
goto :eof
:ps
docker compose ps
goto :eof
:psql
for /f "usebackq tokens=1,2 delims==" %%a in (".env") do (
  if /I "%%a"=="POSTGRES_USER" set PGUSER=%%b
  if /I "%%a"=="POSTGRES_DB"   set PGDB=%%b
)
docker exec -it postgres psql -U %PGUSER% -d %PGDB%
goto :eof
:topics
docker exec -it kafka kafka-topics --bootstrap-server localhost:9092 --list
goto :eof
:logs
REM Accepts "make logs S=service_name"
set SVC=%ARG:S==%
if "%SVC%"=="" (
  echo Usage: make logs S=service_name
  goto :eof
)
docker compose logs -f --tail=200 %SVC%
goto :eof
:smoke
for /f "usebackq tokens=1,2 delims==" %%a in (".env") do (
  if /I "%%a"=="POSTGRES_USER" set PGUSER=%%b
  if /I "%%a"=="POSTGRES_DB"   set PGDB=%%b
)
echo [topics]
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --list
echo [raw_trades]
docker exec postgres psql -U %PGUSER% -d %PGDB% -c "SELECT count(*) FROM raw_trades;"
echo [raw_news]
docker exec postgres psql -U %PGUSER% -d %PGDB% -c "SELECT count(*) FROM raw_news;"
echo [nlp_results]
docker exec postgres psql -U %PGUSER% -d %PGDB% -c "SELECT sentiment, count(*) FROM nlp_results GROUP BY 1;"
echo [signals]
docker exec postgres psql -U %PGUSER% -d %PGDB% -c "SELECT action, count(*) FROM signals GROUP BY 1;"
echo [positions]
docker exec postgres psql -U %PGUSER% -d %PGDB% -c "SELECT status, count(*), ROUND(AVG(pnl_pct)::numeric, 3) AS avg_pnl FROM positions GROUP BY 1;"
goto :eof
:clean
docker compose --profile full down -v
goto :eof
