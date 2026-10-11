-- Initialization script for the InfraGuardian demo Postgres container.
-- Mounted as /docker-entrypoint-initdb.d/init.sql so it runs once on first container start.

-- pg_stat_statements is preloaded via the `shared_preload_libraries` flag in docker-compose.yml;
-- this CREATE EXTENSION makes the view queryable so PostgresCollector can read query latencies.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
