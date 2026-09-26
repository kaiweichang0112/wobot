# Infrastructure runbook

How the Google Cloud environment for Wobot is built. Resources are created in
the Cloud Console first. Each section records the console path, the settings
that matter, the equivalent `gcloud` command, and a read-only command that
verifies the result.

Commands assume the variables from `env.sh`:

```sh
cp infra/env.sh.example infra/env.sh   # once; env.sh is gitignored
source infra/env.sh
```

One project is one environment. Everything regional lives in `asia-east1`.

## 1. Project and billing

_Pending._

## 2. APIs

_Pending._

## 3. Artifact Registry

_Pending._

## 4. Service accounts

_Pending._

## 5. Secret Manager

_Pending._

## 6. Cloud Storage

_Pending._

## 7. Cloud SQL

_Pending._

## 8. Database bootstrap

_Pending._

## 9. First deployment

_Pending._

## 10. Firebase Authentication

_Pending._

## 11. CI/CD with Workload Identity Federation

_Pending._

## Connection budget

_Pending._

## Cost controls

_Pending._
