-- One-time bootstrap for the wobot database. Run as the instance admin:
-- `postgres` in Cloud SQL Studio, or wobot_admin via compose locally.
-- Login users and their group memberships differ per environment and are
-- granted separately (infra/README.md, section 8).

CREATE EXTENSION IF NOT EXISTS vector;

-- Privileges go to these group roles, never to login users directly.
CREATE ROLE wobot_migrator NOLOGIN;  -- owns the schemas; runs migrations
CREATE ROLE wobot_api NOLOGIN;       -- the Cloud Run API
CREATE ROLE wobot_ingest NOLOGIN;    -- knowledge ingestion jobs

-- PG16+: creating a role leaves a non-superuser admin with ADMIN OPTION only,
-- but naming another role as a schema owner requires being able to SET ROLE to it.
GRANT wobot_migrator TO CURRENT_USER WITH SET TRUE, INHERIT FALSE;

CREATE SCHEMA app AUTHORIZATION wobot_migrator;        -- private per-account data
CREATE SCHEMA knowledge AUTHORIZATION wobot_migrator;  -- shared corpus
CREATE SCHEMA ops AUTHORIZATION wobot_migrator;        -- jobs, runs, deletion state

REVOKE ALL ON DATABASE wobot FROM PUBLIC;
GRANT CONNECT ON DATABASE wobot TO wobot_migrator, wobot_api, wobot_ingest;