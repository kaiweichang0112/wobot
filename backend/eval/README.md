# Evaluation

`wobot-eval` scores an index version against labelled datasets with RAGAS metrics that
need no model, computed on item identities, so no judge runs and every number repeats.
Model comparisons for the chat graph's nodes play their datasets with the candidate
models (paid calls). Judge metrics come in V8.

| Path | Holds |
| --- | --- |
| `datasets/seed-v1.yaml` | The 29 seed questions, split 20 dev / 9 held out by scenario |
| `datasets/fixtures-v1.yaml` | Engineering checks of ingestion outside the 30 questions |
| `datasets/items-v1.yaml` | Questions about one item, for comparing chunk strategies |
| `datasets/route-v1.yaml` | Messages and every acceptable path: 24 dev / 12 held out |
| `datasets/knowledge-v1.yaml` | Questions for the knowledge path, with the items that answer them and what the reply must hold: 18 / 4 |
| `datasets/lists-v1.yaml` | Requests for lists, with the calls accepted and the items shown: 17 / 4 |
| `datasets/recommendation-v2.yaml` | Needs, with the products that fit, those never to show and the facet query that finds them: 15 / 3 |
| `datasets/turns-v1.yaml` | Conversations whose last turn is scored: 8 / 2 |
| `gold/` | Labels a person wrote from the sources; see `gold/README.md` |
| `comparisons/` | Write-ups that compare versions, such as [v1 against v2](comparisons/agent-v1-v2.md) |
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

## Model comparisons

Each model node of the chat graph gets its own model (DV11), compared on its dataset
with every candidate playing every dev case, three times by default:

```bash
uv run --env-file .env wobot-eval route --model jev-latest --model gpt-6-luna:none
uv run --env-file .env wobot-eval chat --model gpt-5.6-luna:none --model gpt-6-luna:none
uv run --env-file .env wobot-eval rewrite --model gpt-6-luna:none
uv run --env-file .env wobot-eval answer --model gpt-6-luna:none
uv run --env-file .env wobot-eval list-agent --model gpt-6-luna:none
uv run --env-file .env wobot-eval write-list --model gpt-6-luna:none
uv run --env-file .env wobot-eval recommend --model gpt-6-luna:low
uv run --env-file .env wobot-eval turns
DB_USER=wobot_ingest_user uv run --env-file .env wobot-eval facets --model gpt-6-luna
DB_USER=wobot_api_user uv run --env-file .env wobot-eval judge eval/runs/<stamp>-answer.json --candidate gpt-6-luna:none
uv run --env-file .env wobot-eval judge eval/runs/<stamp>-write-list.json --candidate gpt-6-luna:none
uv run wobot-eval judge-sample eval/runs/<stamp>-judge.json
uv run wobot-eval judge-agreement eval/runs/<stamp>-judge.json
```

- A candidate is `model:effort`; Jev takes no effort.
- Reports show cases right per run, p50 and p95 of the node or path, tokens and cost,
  and every play's node flow: each node, its seconds and what it did.
- The rule: keep the candidates within one case of the best, then take the lowest p95,
  then the cheapest among those close to it. A person reads the failures and the flows
  before choosing, and the choice is recorded in a decision.
- `recommend` also checks each turn's `find_products` call against the case's labelled
  query, and `facets` counts product and need pairs decided unlike a person's labels:
  a value can be right while the decision it feeds is wrong.
- `judge` grades the replies an `answer` run kept, without asking for them again, with
  `JUDGE_MODEL`, a model no comparison tried. A rubric passes or fails three criteria,
  each with a reason first: the reply does what the case's expected behaviour says, every
  fact it states is in what the search found, and it answers in the user's language.
  Faithfulness follows RAGAS's method, written here because RAGAS sends `gpt-6.1-sol`
  parameters it refuses: the reply broken into statements, each judged against the
  passages; the report gives the share supported and whether all are. A miss is put down
  to retrieval when the search found none of the case's labelled items. The judge is
  trusted only as far as it agrees with a person: `judge-sample` writes a blind sample to
  `gold/judge-labels.yaml`, and each judge report then gives the share of labelled
  replies decided alike and Cohen's kappa, which takes out agreement by chance. It must
  agree on 90% or more and fail every reply the person fails.
- `judge` grades a `write-list` run's replies too, on the words before the lists alone:
  code writes each item and the run already scores them. The rubric reads the lists as
  the writer read them and how many items of each were shown; the words are a sentence or
  two, so there is no faithfulness, and no database is read. Each path's prompts have
  their own version and their own labels, `gold/judge-labels-lists.yaml` for this one, so
  changing one path's prompt leaves the other's measurement standing.

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
overridden. The metrics that need no model do not use `instructor`; RAGAS's
Faithfulness ran under that override in V8's spike, once given the parameters a
reasoning model takes, and `judge` does not use it.

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
