# Wobot backend

The FastAPI service, the chat graph and the database migrations, packaged as one
container image. The API keeps each account's conversations and streams the chat graph's
replies; `wobot-chat` runs the same graph from a terminal. Google Cloud setup is in
[`infra/README.md`](../infra/README.md).

## Layout

| Path | Contents |
| --- | --- |
| `src/wobot/config.py` | Settings from environment variables, and from `.env` locally |
| `src/wobot/db.py` | Async engine: password login locally, Cloud SQL connector with IAM auth in the cloud |
| `src/wobot/models.py` | SQLAlchemy models; the migrations define the schema |
| `src/wobot/api/` | FastAPI app: Firebase ID token verification, the account and its settings, conversations, error envelope |
| `src/wobot/assets/` | The account's own voice and avatar, and the `wobot-assets` job that makes them; `frames.py` holds the 21 frame keys and their tree, which the API, the worker and the app share |
| `src/wobot/knowledge/` | Knowledge ingestion: sources → records → chunks → embeddings → versions, and search |
| `src/wobot/agent/` | The chat graph: the router, its four paths, each model node's prompt, the tools the loops call, and the conversation checkpoints |
| `src/wobot/eval/` | `wobot-eval`: labelled datasets scored against an index version, and model comparisons for each model node |
| `eval/` | Datasets, a person's gold labels, and comparisons such as [v1 against v2](eval/comparisons/agent-v1-v2.md) |
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

## API

Every route but `/health` takes a Firebase ID token (`Authorization: Bearer`), and the
token's email must be verified and on the allowlist, checked on each request. Errors
are `{"error": {"code", "message"}}`, malformed requests included (`INVALID_INPUT`); a
database that cannot be reached is `503 DATABASE_UNAVAILABLE`, never an empty answer.

| Route | What it does |
| --- | --- |
| `GET /v1/me` | Registers the account; returns it, its settings, its voice and avatar (the `active` version and the newer `candidate`, if any), and `avatar_mode`: true only while both are active |
| `PATCH /v1/me/settings` | Renames the assistant (1–40 characters, one line) given the `expected_version` last read; another device's change meanwhile is `409 REVISION_CONFLICT` |
| `DELETE /v1/me` | Deletes the account: needs a sign-in in the last 5 minutes (`401 REAUTH_REQUIRED`), marks it deleted, which refuses its every request from then on (`403 ACCOUNT_DELETED`), deletes its conversations and their checkpoints, and hands its voices and avatars to the asset job to remove; `202`, and safe to send again |
| `POST /v1/conversations` | Starts an empty conversation |
| `GET /v1/conversations` | The account's conversations, most recently active first, a `cursor` page at a time |
| `GET /v1/conversations/{id}/turns` | The latest turns, oldest first, with stopped and failed ones; `cursor` reads earlier ones |
| `DELETE /v1/conversations/{id}` | Deletes the graph's checkpoints, then the rows; safe to send again after a failure |
| `POST /v1/conversations/{id}/turns` | Sends a message (`turn_id` made by the app, `text`, `mode`: `text` or `avatar`); the reply streams back as server-sent events |
| `POST /v1/turns/{id}/cancel` | Stops the `attempt` given, keeping the `shown_text` the app had shown; answers how the turn stands, and changes nothing sent again or too late |
| `POST /v1/turns/{id}/retry` | Writes a stopped or failed reply again, given the `expected_attempt`; streams as a turn does, in the mode it was asked in |
| `POST /v1/turns/{id}/playback` | Avatar mode: how many sentences the `attempt` has `heard` (only moves forward), or `stopped` to end its speech; the text is written all the same |
| `POST /v1/turns/{id}/continue-reading` | Speaks the next items of a completed list reply, `from` where the last `completed` said, for the `attempt` shown; streams `speech` events, then `completed` with the `next` start |

Another account's conversation is answered `404`, as one that does not exist. A
conversation points at the checkpoint its last completed turn left
(`head_checkpoint_id`), and the database allows one running turn per conversation.

A turn is refused before its stream opens, as an ordinary HTTP error, when the message
is not 1–2,000 characters (`422`), the conversation is not the account's (`404`), a
reply is already being written in it (`409 TURN_IN_PROGRESS`) or the same `turn_id` was
sent before (`409 TURN_EXISTS`). In Avatar mode the account's own avatar and voice must
both be in use (`409 AVATAR_NOT_READY` or `VOICE_NOT_READY`). Otherwise the events are,
in order:

| Event | Data, besides `turn_id` and `attempt` |
| --- | --- |
| `accepted` | none: the turn is saved as running |
| `status` | `stage`: `searching` while a path looks things up, `writing` once only words are left |
| `text_delta` | `text`: the next words, from chat and knowledge answers only |
| `reply` | the whole reply: `text`, and `action` and `products` on the recommendation path. It replaces the deltas; the lists' record keys stay in the turn's row |
| `speech` | Avatar mode only, between the others: `index`, `text`, `emotion` and `audio` (base64 MP3) of the next sentence spoken, in order |
| `speech_error` | `index` of a sentence that could not be spoken; the text is unaffected |
| `title` | `title`: the conversation's name, on its first turn only, before the last event. Written by a model alongside the reply, or the question shortened when it cannot be |
| `completed` | none: the turn is saved and the conversation remembers it; in Avatar mode, after the last `speech`, or once the speech was stopped, with `next`: where reading a list goes on, or null |
| `error` | `code` (`PROVIDER_UNAVAILABLE`, `TURN_TIMEOUT` or `INTERNAL_ERROR`), `message`, `retryable`: the turn failed and is not remembered |

The graph runs inside the request, from the checkpoint the conversation points at, and
saves one checkpoint when it ends (`durability="exit"`). Each turn logs one `turn
finished` line with its route, each node's seconds and the time to the first status,
first word and first sentence spoken; no message text.

In Avatar mode the reply is also spoken in the account's voice (`wobot/speech/`, DEC-089):
streamed words are cut into sentences, the first at its first comma so the voice starts
early, and each sentence is one ElevenLabs request, two at a time, told in order. On
the chat path each sentence's feeling is one Jev question (`speech/emotions.py`,
DEC-095, DEC-097): it is the `speech` event's `emotion`, for the avatar's face, and an
audio tag such as `[excited]` for the voice; short of 0.7 confidence, slower than 0.8 s
or failed, it is neutral. The other paths say what the sources say, neutral, and wait
for no feeling. Code
says what is spoken of the replies it writes: a list's intro and first three items, a
recommendation's products and the needs they meet; never a link, phone number or
address. The speech is a layer over the turn: the text completes it and moves the
conversation's memory on, and a `playback` stop ends only the speech, read every second
like a `cancel`. What was spoken is kept in the turn's `speech`.

A list is read on a page of five items at a time (`continue-reading`, DEC-096), from the
reply the turn kept, which holds every item: no graph runs and nothing is searched, so
the list heard is the one shown. The app holds the cursor, so the same `from` always
gives the same page and asking again repeats nothing. It speaks in the voice in use now
(a replaced voice is gone from ElevenLabs), with no feeling, and is no turn: it does not
wait on one running, and stopping it only stops playback, the page being short. It is
refused with `409 NOTHING_TO_READ` for a reply that is no list, another attempt or a
start past the end.

How a turn can end besides completing, each saved by a compare-and-swap on the turn's
ID, attempt and status, so the first end written stands:

- **Stopped.** The app sends `cancel` with the words it had shown, and closes its
  stream. On Cloud Run the closing never reaches the instance, so the stop is the
  `cancel`: the running turn reads it from the database every second, stops the graph
  and ends its stream without a reply. A reply that completes after the stop is neither
  saved nor sent. A stream that ends without `completed` or `error` was ended
  elsewhere: read the turn. Run locally, a closed stream stops the turn at once too,
  and it keeps the words it sent.
- **Timed out.** A turn still running after 90 s fails with `TURN_TIMEOUT`.
- **Lost.** A turn still running after 3 min lost its request, such as to an instance
  that stopped; it is failed with `STREAM_INTERRUPTED` when its conversation is next
  read or written, so the conversation never stays locked.
- **Deleted.** A turn ending after its conversation was deleted deletes the checkpoint
  it saved; the delete and the turn's end hold the conversation's row in turn.

A retry is the conversation's last turn only, stopped or failed (`409
TURN_NOT_RETRYABLE` otherwise), and a stale `expected_attempt` is `409
REVISION_CONFLICT`. It runs again from the same checkpoint as the attempt before, so
the question is in the conversation's memory once, and the turn keeps only its latest
reply.

## Voices

Each account clones its own voice from a recorded sample (DEC-086). A version row is
also the job that makes it: `wobot-assets`, a Cloud Run job (`infra/README.md`, "Asset
job"), claims a queued version by moving it to running, which only one execution can
do, and ends it with a compare-and-swap naming its execution. The voice is named
`wobot-<voice_id>` at ElevenLabs, so an execution started again finds the clone an
earlier one made instead of making a second.

| Route | What it does |
| --- | --- |
| `POST /v1/voices` | Multipart `voice_id` (made by the app) and `sample`, an M4A of 60–180 s and at most 10 MB; stores it under `incoming/`, queues the version and starts the job: `202`. Sent again, it returns the same version; another account's ID is `409 ASSET_EXISTS`, and a second while one is being made `409 ASSET_IN_PROGRESS` |
| `GET /v1/voices/{id}` | The version's `status` (`queued`, `running`, `ready`, `failed`, `active`, `retired`, `deleted`) and `error_code`. A version queued for over 60 s is started again; one whose worker stopped beating 5 min ago fails as `WORKER_LOST` |
| `POST /v1/voices/{id}/preview` | A Chinese and an English sentence in the voice, as MP3 (`tts_model`); `409 VOICE_NOT_READY` until it is ready |
| `POST /v1/voices/{id}/activate` | Puts a ready voice to use, given the `expected_active` the app last saw (`409 REVISION_CONFLICT` otherwise). The one in use is retired first, in the same transaction, and removed at ElevenLabs by the job |
| `DELETE /v1/voices/{id}` | Takes the voice out of use at once, which ends Avatar mode, and has the job remove it at ElevenLabs; safe to send again |

A version that fails says why in `error_code`: `VOICE_REJECTED` (ElevenLabs refused the
sample), `PROVIDER_UNAVAILABLE`, `SAMPLE_MISSING`, `WORKER_LOST` or `INTERNAL_ERROR`.
The sample is deleted once cloned; the bucket's lifecycle removes any other within a day.

Measured on the first cloud run (2026-10-14, a 61 s sample): the job's execution started
4 s after `POST`, and the version was `ready` 25 s after it, nearly all of it the job's
cold start; removing the voice took 11 s from `DELETE`. ElevenLabs cloned it at no credit
cost; a preview costs its characters.

Without `ASSETS_JOB`, as locally, the API does the work in its own process, as its own
role, and media go to `.data/media/`. The job's command can also be run by hand:

```bash
DB_USER=wobot_assets_user uv run --env-file .env wobot-assets make voice|avatar <id>
DB_USER=wobot_assets_user uv run --env-file .env wobot-assets clean voice|avatar <id>
```

## Avatars

A photo becomes 21 frames: seven expressions, each with a closed, half-open and wide-open
mouth (spec 21). The image model (`avatar_image_model`, at `avatar_quality`) paints each
frame from the one it depends on: the base (`neutral_closed`) from the photo, each
expression's closed frame from the base, and its two open mouths from that closed frame,
so their eyes and brows match it. Frames have a transparent background (P24): the app
draws its own dark backdrop behind them, so no frame brings a colour that could shift
from one to the next, and a frame without transparency fails as `INVALID_IMAGE`. A frame
starts as soon as its own parent is done, up to
`avatar_concurrency` at once; the three levels, not the image rate limit, bound the time.
The client retries 429 and 5xx three times, as `Retry-After` asks.

The prompts are versioned in `assets/avatar_prompts.py`: changing one means bumping
`PROMPT_VERSION`, which a test pins with their hash, and each avatar records both. A
frame is stored as `avatars/{avatar_id}/{key}-{hash}.png`, so one made again never
overwrites another; its row keeps its SHA-256 and the tokens it cost. The photo is
stored upright, at most 1,024 pixels a side, and without its metadata, and deleted once
the base is made: the base is then never made again, and a new photo makes a new avatar.

| Route | What it does |
| --- | --- |
| `POST /v1/avatars` | Multipart `avatar_id` and `photo` (any image Pillow reads, at most 10 MB); queues the version with its 21 frames and starts the job: `202`. Sent again, it returns the same avatar |
| `GET /v1/avatars/{id}` | The version, `ready_frames` of 21, and each frame's `status`, `sha256` and `error_code`, in the canonical order. Restarts a lost start and gives up a lost worker, as for voices |
| `GET /v1/avatars/{id}/frames/{key}` | The frame as PNG, with its SHA-256 as `ETag`: `If-None-Match` answers `304` |
| `POST /v1/avatars/{id}/retry` | A failed avatar only: its failed frames, and those they would have made, are made again; ready ones stay |
| `POST /v1/avatars/{id}/frames/{key}/redo` | Before the avatar is in use: the frame and every frame made from it are made again (P9); not the base |
| `POST /v1/avatars/{id}/activate` | Only with all 21 frames made under the current expression schema (`409 AVATAR_NOT_READY`), as for voices |
| `DELETE /v1/avatars/{id}` | As for voices; the job removes the frames |

A frame that fails says why: `IMAGE_REJECTED` (the model refused the photo; take another),
`PROVIDER_UNAVAILABLE`, `INVALID_IMAGE` (not a 1,024-pixel PNG with a transparent background) or `INTERNAL_ERROR`; the
avatar fails with the first of these, or `PHOTO_MISSING` once the photo is gone. A
retry or a redo is refused with `409 ASSET_NOT_EDITABLE` when the avatar is not in a
state for it.

`wobot-assets paint` tries prompts on this machine, with no database or bucket: it writes
the frames, `sheet.png` (all of them on the app's backdrop, `avatar_backdrop`, a row per
expression) and a report of each one's seconds, tokens and cost to `.data/avatar/<run>/`,
never committed. Every frame is billed. Pillow reads no HEIC: convert an iPhone photo
first, with `sips -s format jpeg <photo.HEIC> --out <photo.jpg>`.

```bash
uv run --env-file .env wobot-assets paint --photo <photo> --frames neutral_closed --only --quality high
uv run --env-file .env wobot-assets paint --photo <photo>                    # all 21
uv run --env-file .env wobot-assets paint --photo <photo> --frames fear_closed --from .data/avatar/<run>
```

The last makes `fear_closed` and its two mouths again from an earlier run's base.

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
- Each product's facets (what it does, its form, where it is used, how it senses,
  where alerts are seen, its power and how many use it at once) come from
  `FACETS_MODEL` in the fixed vocabulary of `knowledge/facets.py`. A value is kept only
  with a span of the product's text, which a trigger checks too, and the model and prompt
  version are part of the product's revision. A product the model did not read holds the
  version: unread, it would never be found by its facets.
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

## Chat graph

`agent/graph.py` builds one LangGraph `StateGraph`. `classify` names each message's path
and every path ends with the reply; a test holds the drawing to
`tests/agent/expected_graph.mmd`.

| Path | Nodes | Model calls |
| --- | --- | --- |
| Chat | `classify` → `chat_reply` | 1 |
| Knowledge | `classify` → `rewrite_query` → `retrieve` → `answer` | 2 |
| Lists | `classify` → `list_agent` ⇄ `list_tools` → `write_list` | 2 or more |
| Recommendation | `classify` → `rec_agent` ⇄ `rec_tools` | 1, or 2 when it asks |

- Each model node has its own model and reasoning effort, chosen by its `wobot-eval`
  comparison and recorded in a decision (the settings below). Jev routes; when it fails
  twice, an OpenAI model does.
- Lists and product cards are written by code from the records, so a phone number or a
  link is never a model's. Talks, graduates, projects and publications come newest
  first, and what has no date last; products by category and name.
- A recommendation is judged by code. `rec_agent` turns the need into a query in the facet
  vocabulary; `find_products` (`agent/find.py`) keeps the products whose facets state
  every function and contradict no condition, ranks them, and shows them at once when
  nothing is left to ask. The agent asks when the products split on something the user
  has not said, and explains when none fits.
- Every prompt has a `PROMPT_VERSION`, which a test pins with the prompt's fingerprint.
- Only `chat_reply` has a persona (`agent/persona.py`, DEC-097): openly emotional, in
  Text and Avatar mode alike, adding no fact. Answers, lists and recommendations keep a
  plain tone: with it they grew longer and slower, and list intros began answering what
  their rules leave to another question. Code tells each writing node the language of
  the user's latest message, since from Chinese sources or Chinese turns a model
  answered English in Chinese. Every node that writes caps its reply's tokens, since a
  model at reasoning `none` once wrote 128,000.

```bash
DB_USER=wobot_api_user uv run --env-file .env wobot-chat [--thread <id>] [--name <name>]
```

`wobot-chat` prints each node with its seconds and what it did (paid calls). Conversations
are kept in the local database's checkpoint tables as the API's role, and `--thread`
takes one up again. In the cloud the checkpoints reach Cloud SQL through the service's
Cloud SQL connection, with an IAM token per connection (`infra/README.md`). Tracing goes to LangSmith only when run with
`--env-file .env` and `LANGSMITH_TRACING=true`; tests never send traces.

## Evaluation

`wobot-eval` scores an index version against labelled datasets with RAGAS: complete lists,
exact fields, retrieval, product facets, and vision readings against a person's
transcriptions, all from item identities. RAGAS is a dev dependency, so this runs from a
checkout. Labels are written by a person from the sources, never from system output. See
`eval/README.md`.

```bash
uv run wobot-eval check-gold
uv run wobot-eval run
DB_USER=wobot_ingest_user uv run wobot-eval vision --model <model> --model <model>
DB_USER=wobot_ingest_user uv run wobot-eval facets --model <model> --model <model>
DB_USER=wobot_api_user uv run --env-file .env wobot-eval judge <answer run>.json
```

`vision` compares vision models on the dev transcriptions, and `facets` models reading
the dev products' facets; both make paid calls, except for answers already cached. See
`eval/gold/README.md`. `route`, `chat`, `rewrite`, `answer`, `list-agent`, `write-list`
and `recommend` compare models for one model node each, `turns` plays conversations
through the configured graph, and `api` times the deployed API as a client; all make
paid calls. `judge` grades the replies an
`answer` run kept, by a rubric and by faithfulness, also paid. See `eval/README.md`.

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
| `OPENAI_API_KEY` | none | Ingestion, search and the chat graph; from Secret Manager in the cloud |
| `OPENAI_TIMEOUT_SECONDS` | `60` | Longest wait for one OpenAI request |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Must match a row of `knowledge.embedding_configs` |
| `EXTRACTION_MODEL` | `gpt-5.6-luna` | Reads a speech's title, event and location; answers are cached per model |
| `FACETS_MODEL` | `gpt-6-luna` | Reads each product's facets; chosen with `wobot-eval facets`; answers are cached per model |
| `VISION_MODEL` | `gpt-6.1-sol` | Reads images and PDF pages; chosen with `wobot-eval vision`; answers are cached per model |
| `TYPESAFE_API_KEY` | none | Jev, the router |
| `CLASSIFY_MODEL`, `CLASSIFY_EFFORT` | `jev-latest`, `none` | Routes each message (DEC-059) |
| `CLASSIFY_FALLBACK_MODEL`, `CLASSIFY_FALLBACK_EFFORT` | `gpt-6-luna`, `none` | Routes when Jev fails twice (DEC-067) |
| `CLASSIFY_TIMEOUT_SECONDS` | `15` | Longest wait for one route |
| `CHAT_MODEL`, `CHAT_EFFORT` | `gpt-5.6-luna`, `none` | Small talk and writing (DEC-060) |
| `REWRITE_MODEL`, `REWRITE_EFFORT` | `gpt-6-luna`, `none` | Rewrites a question for the search (DEC-061) |
| `ANSWER_MODEL`, `ANSWER_EFFORT` | `gpt-6-luna`, `none` | Answers from what the search found (DEC-062) |
| `LIST_AGENT_MODEL`, `LIST_AGENT_EFFORT` | `gpt-6-luna`, `none` | Chooses the list queries (DEC-063) |
| `WRITE_LIST_MODEL`, `WRITE_LIST_EFFORT` | `gpt-6-luna`, `none` | Introduces the lists (DEC-063) |
| `REC_AGENT_MODEL`, `REC_AGENT_EFFORT` | `gpt-6-luna`, `low` | Turns a need into a facet query (DEC-071) |
| `TITLE_MODEL`, `TITLE_EFFORT` | `gpt-6-luna`, `none` | Names a conversation from its first question (DEC-082); not a graph node, its titles are read by hand |
| `JUDGE_MODEL`, `JUDGE_EFFORT` | `gpt-6.1-sol`, `low` | Judges saved replies in `wobot-eval judge`; never a model a node's comparison tried |
| `CHECKPOINT_POOL_SIZE` | `2` | Connections the conversation checkpoints hold, on a psycopg pool of their own |
| `PUBLISH_MAX_DROP` | `0.2` | A source losing more than this share of its records holds the version for `accept` |
| `KNOWLEDGE_BUCKET` | none | Bucket for raw source files; unset, they go to `KNOWLEDGE_LOCAL_DIR` |
| `KNOWLEDGE_LOCAL_DIR` | `.data/knowledge` | Where local runs keep raw source files |
| `PRODUCT_CATALOG_FILE_ID` | none | Drive file that `wobot-ingest run` downloads when no `--catalog-file` is given; set on the job only |
