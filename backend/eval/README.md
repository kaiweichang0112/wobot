# Evaluation

`wobot-eval` scores an index version against labelled datasets with RAGAS metrics that
need no model, computed on item identities, so no judge runs and every number repeats.
RAGAS's judge metrics need the answer agent and come in phase B.

| Path | Holds |
| --- | --- |
| `datasets/seed-v1.yaml` | The 29 seed questions, split 20 dev / 9 held out by scenario |
| `datasets/fixtures-v1.yaml` | Engineering checks of ingestion outside the 30 questions |
| `datasets/items-v1.yaml` | Questions about one item, for comparing chunk strategies |
| `gold/` | Labels a person wrote from the sources; see `gold/README.md` |
| `runs/` | Reports, gitignored |

## Commands

From `backend/`, against the local database; the API's read-only role is enough.

```bash
uv run wobot-eval check-gold
uv run wobot-eval run
uv run wobot-eval run --dataset items-v1 --split dev --index-version <version>
```

`run` writes `runs/<time>-v<version>-k<k>.json` and `.md`, and records the commit, the
version, its embedding space and strategies, the extraction model and prompt, and the
hash of every dataset and gold file it read. Retrieval embeds each question once with the
version's embedding model; without `OPENAI_API_KEY` those cases stay pending.

## Checks

| Check | Compares | RAGAS metrics | Counted here |
| --- | --- | --- | --- |
| `list` | The records a structured filter returns, the query behind "list every …", with the labelled items | precision and recall: `IDBasedContextPrecision`, `IDBasedContextRecall` | F1, missing and unlabelled items |
| `fields` | Labelled values with the matched records' fields, after collapsing spaces | accuracy: the mean of `ExactMatch`; presence: the mean of `StringPresence`, a value that holds the label (an empty label needs an empty value) | mismatches |
| `retrieval` | The top k chunks of a semantic search, mapped to their records, with the labelled relevant items | recall@k and record precision: `IDBasedContextRecall`, `IDBasedContextPrecision` | MRR, chunk precision, tokens read, missed items |
| `transcription` | What the vision model read in one image or PDF page with what a person transcribed from it | text recall: the mean of `StringPresence` over the transcribed lines | value precision and recall on (value, unit) pairs, lines and values missed or misread |

- Labels name items as a person sees them and are matched by normalized text. A label
  that matches no record counts as missed and is listed: a typo in the label, or an item
  ingestion lost or garbled.
- Blank template entries are not labels. A case without labels is pending, never 0.
- The IDs are record keys: one talk, student, product or project each. RAGAS's ID-based
  metrics ignore order, so rank is measured by MRR, and they see records only, so chunk
  precision and tokens read show what a block of many records costs.

## RAGAS

ragas is a dev dependency: `uv sync` installs it, the API image does not. Two of its own
dependencies need pins in `[tool.uv]` of `pyproject.toml`. ragas 0.4.3 imports
`langchain_community.chat_models.vertexai`, which `langchain-community` 0.4 removed; and its
`instructor` caps `jiter` below 0.15, which `openai` 3.4 and later exceed, so `jiter` is
overridden. The metrics that need no model do not use `instructor`; a judge metric in
phase B will first be checked under that override.

- RAGAS posts a usage event to its maker for every score unless `RAGAS_DO_NOT_TRACK` is
  `true`. `wobot.eval` sets it on import, and a test keeps it set.
- The ID-based metrics exist only at the deprecated `ragas.metrics` path;
  `ragas.metrics.collections` lacks them. Moving past ragas 0.4 means finding them again.
- Samples are scored one at a time with `single_turn_ascore`, RAGAS's async API, rather
  than its synchronous `evaluate()`: the harness already runs in an event loop.

## Comparing chunk strategies

List blocks (one chunk per category and year) are the default; item chunks (one per
record, same header) are kept for comparison. Build an unpublished item version, then run
the same questions against both:

```bash
DB_USER=wobot_ingest_user uv run wobot-ingest run --sources grc_website --policy dry-run --chunking item
uv run wobot-eval run --dataset items-v1 --dataset seed-v1 --split dev
uv run wobot-eval run --dataset items-v1 --dataset seed-v1 --split dev --index-version <version>
```
