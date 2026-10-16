# Wobot

Wobot is an AI spokesperson app for a gerontechnology research center and its
affiliated smart-care company. It answers questions grounded in approved
sources — the organizations' public websites and a product catalog — recommends
smart-care products neutrally, and talks through either a text chat or a
personalized cartoon avatar that speaks with the user's cloned voice.

It is built for an internal pilot of up to ten people.

**Status:** Phases 0 to F are done. The iOS app signs in with Google and chats over a
streamed turn API on Cloud Run, in text or as the user's own avatar speaking in their
cloned voice. A LangGraph router answers from versioned knowledge ingested monthly
from the two websites, their images, three PDFs and the product catalog, lists records
in full, and recommends products from facets the code checks; each of the graph's
model nodes was chosen by its own evaluation. Accounts can be deleted, a daily job
finishes what requests left undone, and errors and spending email the developer. In
Avatar mode the phone can ride a three-axis robot head: the app finds it over
Bluetooth, moves it with each spoken sentence's feeling, and keeps the phone turned
toward the user's face, found on the phone. Not done yet: the Android build and a
pilot with other users.

## Architecture

```mermaid
flowchart LR
    App["Flutter app<br/>iOS / Android"] --> Auth["Firebase Authentication<br/>Google Sign-In"]
    App --> API["Cloud Run service<br/>FastAPI + LangGraph"]
    App -- BLE --> Robot["Robot head<br/>Pico W"]
    API --> DB["Cloud SQL<br/>PostgreSQL + pgvector"]
    API --> AI["OpenAI / ElevenLabs"]
    Jobs["Cloud Run jobs<br/>migrations · ingestion · assets · sweep"] --> DB
    Jobs --> Store["Cloud Storage"]
    Jobs --> AI
```

- One public API service. Long-running work — schema migrations, knowledge
  ingestion, avatar and voice creation — runs as Cloud Run jobs.
- All Google Cloud resources live in `asia-east1` (Taiwan).
- The app never holds provider secrets or database credentials. The API
  verifies a Firebase ID token and an email allowlist on every request.
- Each workload runs as its own service account, and no service account key
  exists: the database accepts IAM logins, and CI deploys through Workload
  Identity Federation.

## Repository layout

| Path | Contents |
| --- | --- |
| [`app/`](app/README.md) | Flutter client on iOS (Android follows): text chat, Avatar mode, and the robot's motions and face tracking |
| [`backend/`](backend/README.md) | FastAPI service, database migrations, knowledge ingestion and its evaluation; LangGraph agents to come |
| [`infra/`](infra/README.md) | Google Cloud runbook, environment template, database bootstrap |
| [`firmware/`](firmware/README.md) | Raspberry Pi Pico W firmware for the robot head: motion primitives over BLE, a gaze layer and a lease |

## Development

- Backend: `docker compose up -d` starts PostgreSQL with pgvector, bootstrapped
  as in Cloud SQL; [`backend/README.md`](backend/README.md) covers the rest.
- App: [`app/README.md`](app/README.md) generates the Firebase config and runs
  the app against an API URL.
- Cloud: [`infra/README.md`](infra/README.md) records every Google Cloud
  resource, its console path, the equivalent `gcloud` command and a read-only
  check.

## Delivery

- Pull requests that touch the backend run lint, a migration of an empty
  database, a check that models and migrations agree, and the tests
  ([`backend-ci.yml`](.github/workflows/backend-ci.yml)).
- A merge to `main` deploys ([`backend-deploy.yml`](.github/workflows/backend-deploy.yml)):
  the same checks, then the image, the migration job and the API, in that order.
  The run fails unless `/health` reports the merged commit.

## Roadmap

| Phase | Scope |
| --- | --- |
| 0 · Deployment skeleton | Sign-in → Cloud Run → Cloud SQL end to end; migrations as a job; keyless CI/CD |
| A · Knowledge | Ingest websites, images, PDFs and the product catalog into versioned records, chunks and embeddings; monthly updates held when records go missing; RAGAS evaluation of ingestion and retrieval |
| B · Agentic RAG | LangGraph routing, retrieval tools, neutral recommendation, RAGAS evaluation |
| C · Identity & chat | Conversations, streaming, cancellation and retry in the app |
| D · Avatar & voice | 21-frame cartoon avatar, voice clone, spoken turns |
| E · Operations | Account deletion, a daily sweep, alerts and a budget (Android and the pilot come later) |
| F · Robot | The robot head in Avatar mode: connected by itself, a motion for each sentence's feeling, the phone turned toward the user's face |
