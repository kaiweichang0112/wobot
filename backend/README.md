# Wobot backend

The FastAPI service and its database migrations, packaged as one container image.
LangGraph agents and ingestion jobs will live here too. Google Cloud setup is in
[`infra/README.md`](../infra/README.md).

## Layout

| Path | Contents |
| --- | --- |
| `src/wobot/config.py` | Settings from environment variables, and from `.env` locally |
| `src/wobot/db.py` | Async engine: password login locally, Cloud SQL connector with IAM auth in the cloud |
| `src/wobot/models.py` | SQLAlchemy models; the migrations define the schema |
| `src/wobot/api/` | FastAPI app, Firebase ID token verification, error envelope |
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
they can never reach Cloud SQL, and delete the rows they create. `DB_HOST` and
`DB_PORT` can point them at another local instance.

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
