#!/bin/sh
# Local stand-in for Cloud SQL, run once when the data volume is created.
# Cloud SQL's `postgres` is a CREATEROLE admin but not a superuser, so the
# bootstrap runs as such an admin here too: privilege mistakes fail locally first.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE wobot_admin LOGIN CREATEROLE;
ALTER DATABASE wobot OWNER TO wobot_admin;
-- Only a superuser can create pgvector here; Cloud SQL allowlists it for its admin.
CREATE EXTENSION vector;
SQL

psql -v ON_ERROR_STOP=1 --username wobot_admin --dbname "$POSTGRES_DB" \
  -f /bootstrap/bootstrap.sql

# Local login users. In Cloud SQL these are IAM users (infra/README.md, section 8).
psql -v ON_ERROR_STOP=1 --username wobot_admin --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE wobot_migrator_user LOGIN PASSWORD 'wobot' IN ROLE wobot_migrator;
CREATE ROLE wobot_api_user LOGIN PASSWORD 'wobot' IN ROLE wobot_api;
SQL
