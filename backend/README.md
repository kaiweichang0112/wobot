# Wobot backend

The FastAPI service and its database migrations, packaged as one container image.
LangGraph agents will live here too. Google Cloud setup is in
[`infra/README.md`](../infra/README.md).

## Layout

| Path | Contents |
| --- | --- |
| `src/wobot/config.py` | Settings from environment variables, and from `.env` locally |
| `src/wobot/db.py` | Async engine: password login locally, Cloud SQL connector with IAM auth in the cloud |
| `src/wobot/models.py` | SQLAlchemy models; the migrations define the schema |
| `src/wobot/api/` | FastAPI app, Firebase ID token verification, error envelope |
| `src/wobot/knowledge/` | Knowledge ingestion: sources → records → chunks → embeddings → versions, and search |
| `migrations/` | Alembic migrations, run as the `wobot_migrator` group role |
| `tests/` | API and privilege tests against a migrated local database |

## Local development

Requires [uv](https://docs.astral.sh/uv/) and Docker. Start PostgreSQL with pgvector
from the repository root:

```bash
docker compose up -d
```

Then, in `backend/`:

```bash
cp .env.example .env    # then set GOOGLE_CLOUD_PROJECT
uv sync
DB_USER=wobot_migrator_user uv run alembic upgrade head
uv run uvicorn wobot.api.main:app --reload
```

`.env` holds the API's login user. Commands that change the schema name the migrator
explicitly, so a shell never keeps more privileges than the command needs.

- `GET /health` reports the running version and never touches the database.
- `GET /v1/me` needs a Firebase ID token whose verified email is on the allowlist
  (`app.allowed_emails`). Token verification loads Application Default Credentials:
  run `gcloud auth application-default login` once.

## Tests

```bash
uv run ruff format && uv run ruff check
uv run pytest
```

The tests need the compose database. They migrate it first, force local settings so
they can never reach Cloud SQL, and leave no rows behind: API tests delete theirs, and
knowledge tests run inside a transaction that is rolled back, so they never touch a
version you ingested locally. No test calls OpenAI. `DB_HOST` and `DB_PORT` can point
them at another local instance.

## Knowledge ingestion

`wobot-ingest` reads the product catalog, stores it as records, chunks and embeddings,
and publishes a new version only when the content changed and every check passed. Run
it as the ingest role, with `OPENAI_API_KEY` in `.env`:

```bash
DB_USER=wobot_ingest_user uv run wobot-ingest run --catalog-file ../references/smart-care-products.xlsx
DB_USER=wobot_ingest_user uv run wobot-ingest search "離床預警 不用穿戴"
DB_USER=wobot_ingest_user uv run wobot-ingest status
```

- One run: read the active version → parse and normalize → check → store records and
  chunks → embed the chunks that have no vector yet → build a version listing every
  record and chunk → check what was stored → publish with compare-and-swap.
- Records, chunks and embeddings are content-addressed and never updated. A version
  reuses every unchanged one, so an edit to one row embeds one chunk.
- A run whose content matches the active version ends as `no_change` and embeds
  nothing. `--policy dry-run` builds and validates a version without publishing it.
- Blocking problems (a missing name, an implausible year) stop the run with the row
  number; warnings are printed and kept in the version's validation report.
- The raw file is kept under `KNOWLEDGE_LOCAL_DIR`, named by its SHA-256.
- Exit code: 0 for `published`, `no_change` and `validated`; 1 for `failed`.

## Migrations

```bash
uv run alembic revision --rev-id 0002 -m "short description"
DB_USER=wobot_migrator_user uv run alembic upgrade head
DB_USER=wobot_migrator_user uv run alembic check
```

- `migrations/env.py` switches to `wobot_migrator` with `SET ROLE`, so the group role
  owns every new object and the default privileges from `0001` apply to it.
- Write grants by hand: autogenerate does not see privileges.
- Migrations run before a new API revision takes traffic, so the running revision
  must keep working on the new schema: expand first, remove old columns in a later
  release.
- Never downgrade Cloud SQL; fix forward with a new revision. `downgrade` is for
  checking locally that a revision reverses cleanly.
- `alembic check` fails when the models and the migrations disagree.

## Container image

```bash
docker build -t wobot-api:local .
```

- A uv builder stage installs the locked dependencies; the runtime stage runs as a
  non-root user and contains neither uv nor its download cache.
- One image serves both roles: its default command starts the API, and the migration
  job runs `alembic upgrade head`.
- `.dockerignore` is an allowlist. Add any new file the image needs to it.
- Cloud Run needs `linux/amd64` images; section 9 of `infra/README.md` cross-builds and
  pushes one.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_CLOUD_PROJECT` | required | Firebase project whose ID tokens are accepted |
| `APP_VERSION` | `dev` | Reported by `/health`; the git SHA in Cloud Run |
| `DB_MODE` | `local` | `local`: password login; `cloudsql`: connector with IAM auth |
| `DB_USER` | required | Login user; in `cloudsql` mode an IAM user such as `wobot-api@<project>.iam` |
| `DB_PASSWORD` | none | `local` mode only |
| `DB_HOST`, `DB_PORT` | `localhost`, `5432` | `local` mode only |
| `DB_NAME` | `wobot` | Database name |
| `INSTANCE_CONNECTION_NAME` | none | `cloudsql` mode: `<project>:<region>:<instance>` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `2`, `2` | Connections per instance, sized against the budget in `infra/README.md` |
| `DB_POOL_TIMEOUT_SECONDS` | `10` | Longest wait for a free pooled connection |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | Longest wait to open a connection |
| `OPENAI_API_KEY` | none | Ingestion and search only; from Secret Manager in the cloud |
| `OPENAI_TIMEOUT_SECONDS` | `60` | Longest wait for one OpenAI request |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Must match a row of `knowledge.embedding_configs` |
| `KNOWLEDGE_LOCAL_DIR` | `.data/knowledge` | Where local runs keep raw source files |
