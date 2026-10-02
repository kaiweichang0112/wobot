# Wobot

Monorepo: Flutter app (`app/`), FastAPI backend and migrations (`backend/`),
Google Cloud runbook (`infra/`), robot firmware (`firmware/`, with its own
`CLAUDE.md`). Each directory's README is the reference; this file lists what is
easy to get wrong.

## Commands

Backend, from `backend/`, with `docker compose up -d` running at the repository
root:

```bash
uv sync
uv run ruff format && uv run ruff check
uv run pytest
uv run alembic revision --rev-id 0002 -m "short description"
DB_USER=wobot_migrator_user uv run alembic upgrade head
DB_USER=wobot_migrator_user uv run alembic check
DB_USER=wobot_ingest_user uv run wobot-ingest run --catalog-file <path.xlsx> [--sources grc_website]
DB_USER=wobot_ingest_user uv run wobot-ingest search "<query>"
DB_USER=wobot_ingest_user uv run wobot-ingest report|accept|rollback <version>
uv run wobot-eval check-gold [--index-version <version>] [--show <case IDs>]
uv run wobot-eval run [--dataset seed-v1] [--index-version <version>]
DB_USER=wobot_ingest_user uv run wobot-eval vision --model <model> --model <model>
```

`docker compose down -v` resets the local database: its init scripts run only
on an empty volume.

App, from `app/`, once the Firebase config is generated (`app/README.md`):

```bash
flutter analyze
flutter test
flutter run -d <device> --dart-define=API_BASE_URL=<API URL>
```

## Constraints

- **Everything that enters git is in English**, and commits follow Conventional
  Commits.
- **No identifiers or secrets in the repository.** Firebase config is generated
  and gitignored, docs use placeholders such as `$PROJECT_ID` and `<REPO_ID>`,
  allowlist emails live only in the database, and provider keys only in Secret
  Manager.
- **Deploys happen by merging to `main`.** CI tests, migrates, deploys and checks
  `/health` against the commit. Deploy by hand only as the runbook describes.
- **Schema changes are hand-written Alembic migrations**, run as
  `wobot_migrator` (`migrations/env.py` switches with `SET ROLE`). Autogenerate
  misses privileges, so grants are written by hand. Migrations run before the
  new API revision serves, so expand first and contract in a later release.
  Never downgrade Cloud SQL; fix forward.
- **The API's database role is limited on purpose.** `wobot_api` has no DDL,
  only reads `knowledge`, and cannot write `app.allowed_emails`. Do not widen it
  to make something work.
- **Ingestion publishes by itself only what loses nothing.** A version that empties a
  source or drops more than `PUBLISH_MAX_DROP` of its records is held for
  `wobot-ingest accept`. The schedule lives in two places: Cloud Scheduler starts
  `run --scheduled` every Sunday, and `knowledge/schedule.py` lets it go ahead only on
  the month's first Sunday; change both together.
- **Ingestion is append-only.** `wobot_ingest` may insert into `knowledge` but not
  update or delete, except moving `active_knowledge` and version status. Write
  with `ON CONFLICT DO NOTHING` and read IDs back; never `DO UPDATE`. Tests that
  write run inside a rolled-back transaction (`tests/knowledge/conftest.py`).
- **Ingestion reads only listed pages and documents** (`knowledge/profiles.py`),
  politely, and never follows links; the WhizToys docs alone are found in their
  sitemap, under one path, and images are read where a listed page shows them.
  Parsers decide how many records a page holds; what a page cannot supply is
  reported, never guessed.
- **A model may label, never count or invent.** Fields a model reads are kept only
  as verbatim spans of their source (`knowledge/extraction.py`, enforced by CHECK
  constraints). A vision model's reading of a picture has no source text to check, so
  it stays marked as the model's in records and chunks (`knowledge/vision.py`) and is
  measured against a person's transcriptions. Answers are cached by prompt version:
  changing the instructions, schema or request settings means bumping that module's
  `PROMPT_VERSION`, which a test pins.
- **Gold labels come from a person reading the sources** (`backend/eval/gold`), never
  from parser or model output, which would only measure the system against itself.
- **RAGAS scores evaluation and is a dev dependency**, kept installable by two pins in
  `[tool.uv]` of `backend/pyproject.toml`; lifting either breaks it. Its usage reporting
  stays off: `wobot/eval/__init__.py` sets `RAGAS_DO_NOT_TRACK`.
- **Vectors go through the SQLAlchemy `Vector` type.** Do not register the
  pgvector asyncpg codec: it rejects the text the type sends.
- **Connections are budgeted.** `db-f1-micro` allows 25. Pool sizes and the
  API's cap of 3 instances follow the budget in `infra/README.md`; update it
  before changing either.
- **Every API request is checked**: Firebase ID token, verified email,
  allowlist. Errors use the `{"error": {"code", "message"}}` envelope.
- **Cloud resources are created in the console first**, then recorded in
  `infra/README.md` with the equivalent `gcloud` command and a read-only check.
  There is no Terraform.
- **CI runs with least privilege.** Actions are pinned to commit SHAs, workflows
  default to `contents: read`, and only the deploy job may request an OIDC token.
  Pins do not update themselves: to move one, take the release tag's commit
  (`gh api repos/<owner>/<action>/commits/<tag> --jq .sha`) and update the
  version comment beside it.
- **`backend/.dockerignore` is an allowlist.** Add any file the image needs.
