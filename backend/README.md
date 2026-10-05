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
| `src/wobot/agent/` | The chat agent: its read-only tools, prompt, answer guard, rendering and checkpoint tables |
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

`wobot-ingest` reads the knowledge sources, stores them as records, chunks and
embeddings, and publishes a new version only when the content changed and every check
passed. In the cloud it runs as the `wobot-ingest` Cloud Run job, which downloads the
catalog from Google Drive and keeps raw files in a bucket (`infra/README.md`, section
9). Locally, run it as the ingest role, with `OPENAI_API_KEY` in `.env`:

```bash
DB_USER=wobot_ingest_user uv run wobot-ingest run --catalog-file ../references/smart-care-products.xlsx
DB_USER=wobot_ingest_user uv run wobot-ingest run --sources grc_website --policy dry-run
DB_USER=wobot_ingest_user uv run wobot-ingest run --sources gtech_documents \
  --document-file whizpad_catalog=<path.pdf> --policy dry-run
DB_USER=wobot_ingest_user uv run wobot-ingest search "離床預警 不用穿戴"
DB_USER=wobot_ingest_user uv run wobot-ingest status
DB_USER=wobot_ingest_user uv run wobot-ingest report <version>
DB_USER=wobot_ingest_user uv run wobot-ingest accept <version>
DB_USER=wobot_ingest_user uv run wobot-ingest rollback <version>
```

| Source | Reads | Records |
| --- | --- | --- |
| `product_catalog` | The catalog workbook: a local file, or Drive in the cloud | One product per row |
| `grc_website` | Seven pages of the GRC site, listed in `knowledge/profiles.py` | Students, projects, publications, speeches, the profile page; the home page by heading |
| `gtech_website` | Four pages of the G-Tech site, listed in `knowledge/profiles.py`, and the images they show | One section per heading, with the catalog links; one image per file a page shows |
| `gtech_docs` | The WhizToys documentation, every page its sitemap lists under `/docs/whiztoys/`, and its images | One section per heading, tables row by row; one image per file |
| `gtech_documents` | The three PDFs the G-Tech site links to, listed in `knowledge/profiles.py`; the one on Drive through its API, or a local copy given with `--document-file` | One page per page |

- One run: read the active version → fetch and parse every source → check → store
  records and chunks → embed the chunks that have no vector yet → build a version
  listing every record and chunk → check what was stored → publish with
  compare-and-swap.
- A version holds every source. `--sources` reads only some; the others are carried
  over from the active version unchanged.
- Only listed pages are fetched, from allowed hosts, after robots.txt, one request a
  second; the WhizToys documentation alone is found in its sitemap. Links on those
  pages (Drive, DOI, theses, catalogs) are stored, never followed. Each run reports
  sitemap pages that no profile covers.
- Prose pages become one section per heading. Short sections under one heading share a
  chunk, long ones split between paragraphs near 500 tokens (never past 800), and each
  table row repeats its header. What every page of a site shows, such as an address,
  is read once, from its home page.
- Records, chunks and embeddings are content-addressed and never updated. A version
  reuses every unchanged one, so an edit to one row embeds one chunk.
- Images a listed page shows are fetched as their original files, except what is shown
  under 100 pixels (logos, icons) and SVG drawings. Each PDF page keeps its text layer as
  written and is also drawn as an image. A vision model (`VISION_MODEL`) reads every
  image and drawn page through structured output: the text it sees, numbers with their
  units, what the layout relates, a short description. Its reading cannot be checked
  against a source text, so chunks label every part of it as the model's, and a PDF
  page's chunk leaves out the lines and values its text layer already holds. Answers
  are kept by the file's hash, how it was drawn, model and prompt version.
- Code decides how many records a page holds; a model only labels parts of them.
  Each speech's title, event and location come from `EXTRACTION_MODEL` through
  structured output, and a value is kept only when the entry contains it verbatim,
  which the database checks too. Answers are kept in `knowledge.llm_extractions` by
  input, model and prompt version, so a talk is paid for once. Chunks are built from
  the entry text, never from what the model read.
- A run whose content matches the active version ends as `no_change` and embeds
  nothing. `--policy dry-run` builds and validates a version without publishing it.
- A version that would empty a source, or lose more than `PUBLISH_MAX_DROP` of a
  source's records, is held: built and checked, not published. `report <version>`
  lists the records each source added and lost; `accept <version>` publishes it as
  built, unless another version was published since; `rollback <version>` points
  back at a version published before. All three move the pointer by compare-and-swap.
- One run at a time: a run first takes a PostgreSQL advisory lock, and one that cannot
  is recorded as `skipped_concurrent`.
- The job runs once a month by itself: Cloud Scheduler starts `run --scheduled` every
  Sunday, and it goes ahead only on the month's first Sunday, Taipei time, before
  reading any setting otherwise (`infra/README.md`, "Schedule").
- Blocking problems stop the run: a missing name or an implausible year in the
  catalog, a page that yields nothing, a project without an amount on the site or a
  listed document that is no PDF. An image that cannot be fetched or gets no reading
  is a warning, as is a document link that no profile lists.
  Warnings are printed and kept in the version's validation report; a source's own
  typos, such as an impossible date, are kept as written and reported, never corrected.
- The raw file is kept under `KNOWLEDGE_LOCAL_DIR`, or in `KNOWLEDGE_BUCKET` when that
  is set, named by its SHA-256.
- A source that cannot be read (Drive, a changed column) fails the run, and the failure
  is recorded in `ops.ingestion_runs` like any other.
- Each stage logs one line; on Cloud Run the lines are JSON with `run_id` and `stage`.
- Exit code: 0 for `published`, `no_change`, `validated`, `held` and
  `skipped_concurrent`, which a retry would not change; 1 for `failed`.

## Evaluation

`wobot-eval` scores an index version against labelled datasets with RAGAS: complete lists,
exact fields, retrieval, and vision readings against a person's transcriptions, all from
item identities. RAGAS is a dev dependency, so this runs from a checkout. Labels are
written by a person from the sources, never from system output. See `eval/README.md`.

```bash
uv run wobot-eval check-gold
uv run wobot-eval run
DB_USER=wobot_ingest_user uv run wobot-eval vision --model <model> --model <model>
```

`vision` compares vision models on the dev transcriptions (paid calls); see
`eval/gold/README.md`.

## Chat

`wobot-chat` talks with the chat agent in a terminal (paid calls). Conversations are
kept in the local database as the API's role, and `--thread` takes one up again; each
turn prints the tools called and any answer the guard asked again.

```bash
DB_USER=wobot_api_user uv run --env-file .env wobot-chat [--thread <id>] [--name <name>]
```

Only `DB_MODE=local` keeps conversations for now: async psycopg through the Cloud SQL
connector is still to be settled.

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
- The agent's checkpoint tables (`0005`) are written in the state the pinned
  `langgraph-checkpoint-postgres` reaches, and its `setup()` is never run: the API's
  role has no DDL. A test builds the library's own tables and compares them with ours,
  so upgrading the package means a new revision for whatever migrations it adds.

## Container image

```bash
docker build -t wobot-api:local .
```

- A uv builder stage installs the locked dependencies; the runtime stage runs as a
  non-root user and contains neither uv nor its download cache.
- One image serves every role: its default command starts the API, the migration job
  runs `alembic upgrade head`, and the ingestion job runs `wobot-ingest run`.
- The build stores tiktoken's encoding in the image, so counting tokens needs no
  network at run time.
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
| `CHECKPOINT_POOL_SIZE` | `2` | Most connections the chat agent's checkpoints hold, on a psycopg pool of their own |
| `OPENAI_API_KEY` | none | Ingestion and search only; from Secret Manager in the cloud |
| `OPENAI_TIMEOUT_SECONDS` | `60` | Longest wait for one OpenAI request |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Must match a row of `knowledge.embedding_configs` |
| `EXTRACTION_MODEL` | `gpt-5.6-luna` | Reads a speech's title, event and location; answers are cached per model |
| `VISION_MODEL` | `gpt-6.1-sol` | Reads images and PDF pages; chosen with `wobot-eval vision`; answers are cached per model |
| `AGENT_MODEL` | `gpt-5.6-luna` | The chat agent's model, until the phase B evaluation chooses one |
| `AGENT_REASONING_EFFORT` | `low` | Its reasoning effort; `default` sends none |
| `PUBLISH_MAX_DROP` | `0.2` | A source losing more than this share of its records holds the version for `accept` |
| `KNOWLEDGE_BUCKET` | none | Bucket for raw source files; unset, they go to `KNOWLEDGE_LOCAL_DIR` |
| `KNOWLEDGE_LOCAL_DIR` | `.data/knowledge` | Where local runs keep raw source files |
| `PRODUCT_CATALOG_FILE_ID` | none | Drive file that `wobot-ingest run` downloads when no `--catalog-file` is given; set on the job only |
