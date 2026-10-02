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

**Console:** project picker → New Project → name `Wobot`, edit the project ID,
select the billing account → Create.

The project ID is permanent and ends up in service account emails, the Cloud
SQL connection name, IAM database user names and bucket names, so choose a
readable one instead of the generated default.

Equivalent:

```sh
gcloud projects create $PROJECT_ID --name="Wobot"
gcloud billing projects link $PROJECT_ID --billing-account=<BILLING_ACCOUNT_ID>
```

Local defaults (CLI only):

```sh
gcloud config set project $PROJECT_ID
gcloud config set run/region $REGION
# User ADC belongs to no project; API quota and usage are charged here.
gcloud auth application-default set-quota-project $PROJECT_ID
```

Verify:

```sh
gcloud projects describe $PROJECT_ID          # lifecycleState: ACTIVE
gcloud billing projects describe $PROJECT_ID  # billingEnabled: true
```

## 2. APIs

**Console:** APIs & Services → Library → enable each API below.

| API | Why |
| --- | --- |
| `run.googleapis.com` | Cloud Run services and jobs |
| `sqladmin.googleapis.com` | Cloud SQL; the Python Connector also fetches its ephemeral certificates here |
| `artifactregistry.googleapis.com` | Container images |
| `secretmanager.googleapis.com` | Provider API keys |
| `iam.googleapis.com` | Service accounts |
| `iamcredentials.googleapis.com` | Short-lived service account tokens for Workload Identity Federation |
| `sts.googleapis.com` | Exchanges GitHub OIDC tokens for Google tokens |
| `firebase.googleapis.com` | Firebase on this project |
| `identitytoolkit.googleapis.com` | Firebase Authentication backend |
| `drive.googleapis.com` | The ingestion job downloads the product catalog from Google Drive |
| `cloudscheduler.googleapis.com` | Starts the ingestion job on its schedule |

Equivalent:

```sh
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com firebase.googleapis.com identitytoolkit.googleapis.com \
  drive.googleapis.com cloudscheduler.googleapis.com
```

`cloudbuild.googleapis.com` was enabled at first as well, but images are built
outside Cloud Build (section 9), so a new environment can skip it.

Verify:

```sh
gcloud services list --enabled --format="value(config.name)" | sort
```

## 3. Artifact Registry

**Console:** Artifact Registry → Repositories → Create repository.

| Setting | Value | Why |
| --- | --- | --- |
| Name | `wobot` | |
| Format / mode | Docker / Standard | |
| Location | Region `asia-east1` | Same region as Cloud Run: fast pulls, no cross-region egress |
| Immutable image tags | Disabled | Re-running CI for the same commit would fail to push its tag |
| Vulnerability scanning | Disabled | Container Scanning is billed per image |
| Cleanup | Delete artifacts (not dry run): `delete-all` + `keep-recent-10` | Keep wins over delete, so each image keeps its 10 newest versions |

Rollbacks can only target revisions whose image is still among those 10.

Image names: `$REGION-docker.pkg.dev/$PROJECT_ID/wobot/<image>:<git-sha>`.

Push with `--provenance=false`. By default buildx attaches a provenance
attestation, which turns each push into an image index plus two child
manifests; they may count as three versions against `keep-recent-10`. Children
of a kept index are never deleted, so this shortens the rollback window rather
than breaking images.

Equivalent:

```sh
gcloud artifacts repositories create wobot --repository-format=docker --location=$REGION
gcloud artifacts repositories set-cleanup-policies wobot --location=$REGION \
  --policy=cleanup-policy.json --no-dry-run
```

`cleanup-policy.json`:

```json
[
  {"name": "delete-all", "action": {"type": "Delete"}, "condition": {"tagState": "any"}},
  {"name": "keep-recent-10", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 10}}
]
```

Verify:

```sh
gcloud artifacts repositories describe wobot --location=$REGION
# two cleanupPolicies and no "cleanupPolicyDryRun: true"
```

## 4. Service accounts

**Console:** IAM & Admin → Service Accounts → Create service account. Skip the
"grant access to project" step: roles are granted later, on the specific
resources each account needs.

| Account | Identity of |
| --- | --- |
| `wobot-api` | The `wobot-api` Cloud Run service |
| `wobot-migrator` | The `wobot-migrate` Cloud Run job (Alembic) |
| `wobot-ingest` | The `wobot-ingest` Cloud Run job (knowledge ingestion) |
| `wobot-deployer` | GitHub Actions, through Workload Identity Federation |
| `wobot-scheduler` | Cloud Scheduler, starting the `wobot-ingest` job on its schedule |

One account per workload keeps a compromise contained: the API cannot change
the schema and cannot deploy, and ingestion cannot read private data. Every Cloud Run service and job names its account
explicitly; the default compute service account is never used as a runtime
identity.

Equivalent:

```sh
gcloud iam service-accounts create wobot-api \
  --description="Runtime identity of the wobot-api Cloud Run service"
```

Verify:

```sh
gcloud iam service-accounts list --format="table(email,displayName)"
# No project-level roles yet — expect empty output:
gcloud projects get-iam-policy $PROJECT_ID --flatten="bindings[].members" \
  --filter="bindings.members~^serviceAccount:wobot-" --format="table(bindings.role,bindings.members)"
# No account has a key, now or later — expect empty output:
for sa in $(gcloud iam service-accounts list --format="value(email)"); do
  gcloud iam service-accounts keys list --iam-account "$sa" --managed-by=user --format="value(name)"
done
```

## 5. Secret Manager

**Console:** Security → Secret Manager → Create secret. Paste the value, keep
automatic replication, no rotation or expiry.

| Secret | Accessor |
| --- | --- |
| `openai-api-key` | `wobot-api`, `wobot-ingest` |
| `elevenlabs-api-key` | none yet (voice features, phase D) |

Grant on the secret itself (secret → Permissions → Grant access → Secret
Manager Secret Accessor), never at project level: a project-level accessor can
read every current and future secret in the project.

Equivalent:

```sh
read -s KEY && printf %s "$KEY" | gcloud secrets create openai-api-key --data-file=- && unset KEY
for sa in wobot-api wobot-ingest; do
  gcloud secrets add-iam-policy-binding openai-api-key \
    --member="serviceAccount:$sa@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

`read -s` keeps the key off the screen and out of shell history; `printf %s`
avoids the trailing newline that `echo` would add.

Verify:

```sh
gcloud secrets list
gcloud secrets get-iam-policy openai-api-key   # only wobot-api and wobot-ingest, secretAccessor
# Prints a verdict, never the key:
[[ "$(gcloud secrets versions access latest --secret=openai-api-key | tail -c 1 | xxd -p)" == "0a" ]] \
  && echo "ends with newline" || echo "OK: no trailing newline"
```

## 6. Cloud Storage

**Console:** Cloud Storage → Buckets → Create. The "Equivalent code" button
shows the same settings as a `gcloud` command.

| Setting | Value | Why |
| --- | --- | --- |
| Name | `$PROJECT_ID-media` | Bucket names are global; the project ID prefix avoids collisions |
| Location | Region `asia-east1` | Same region as Cloud Run |
| Storage class | Standard | Frequently read |
| Public access prevention | Enforced | Holds private photos and recordings |
| Access control | Uniform | Permissions only through IAM, no per-object ACLs |
| Soft delete | Off (retention 0) | See below |
| Versioning / retention | Off | |

Raw photos and recordings must be purged soon after use; a 7-day soft delete
would keep them recoverable for a week. The cost: an accidental delete cannot
be undone. No access is granted yet; each service gets object roles when it
first needs the bucket.

Equivalent:

```sh
gcloud storage buckets create gs://$PROJECT_ID-media --location=$REGION \
  --default-storage-class=STANDARD --uniform-bucket-level-access \
  --public-access-prevention --soft-delete-duration=0
```

Verify:

```sh
gcloud storage buckets describe gs://$PROJECT_ID-media
# public_access_prevention: enforced, uniform_bucket_level_access: true,
# soft_delete_policy.retentionDurationSeconds: '0'
```

### Knowledge bucket

A second bucket, `$PROJECT_ID-knowledge` (`$KNOWLEDGE_BUCKET`), holds the raw
bytes of every source ingestion read, each object named by its SHA-256. Same
settings as the media bucket, except soft delete stays at its 7-day default:
nothing here is private, so being able to undo a delete matters more than
purging.

**Console:** the bucket → Permissions → Grant access → `wobot-ingest` →
Storage Object Creator and Storage Object Viewer.

| Role | Why |
| --- | --- |
| Storage Object Creator | Store a snapshot |
| Storage Object Viewer | Check whether one is already stored |

No role that can delete or overwrite: uploads carry `ifGenerationMatch=0`
("create only if absent"), and an object's name is its content's hash, so there
is never a reason to replace one. The cost: removing old snapshots will need
another identity.

Equivalent:

```sh
gcloud storage buckets create gs://$KNOWLEDGE_BUCKET --location=$REGION \
  --default-storage-class=STANDARD --uniform-bucket-level-access --public-access-prevention
for role in roles/storage.objectCreator roles/storage.objectViewer; do
  gcloud storage buckets add-iam-policy-binding gs://$KNOWLEDGE_BUCKET \
    --member="serviceAccount:wobot-ingest@$PROJECT_ID.iam.gserviceaccount.com" --role=$role
done
```

Verify:

```sh
gcloud storage buckets describe gs://$KNOWLEDGE_BUCKET
gcloud storage buckets get-iam-policy gs://$KNOWLEDGE_BUCKET --flatten="bindings[].members" \
  --filter="bindings.members~wobot-" --format="table(bindings.role,bindings.members)"
# 2 rows, both wobot-ingest: objectCreator and objectViewer
```

## 7. Cloud SQL

**Console:** SQL → Create instance → PostgreSQL → the "Sandbox" card, then
customize. Not the 30-day free trial instance: its preset cannot be changed,
it has no backups, and it stops serving after 30 days.

| Setting | Value | Why |
| --- | --- | --- |
| Edition | Enterprise | PG16+ defaults to Enterprise Plus, which has no shared-core machines |
| Version | PostgreSQL 18 | Matches the local `pgvector/pgvector:*-pg18` image |
| Instance ID | `wobot-pg` | Connection name `$PROJECT_ID:$REGION:wobot-pg` |
| Region / availability | `asia-east1`, single zone | Region is permanent; HA would double the cost |
| Machine | Shared core, 1 vCPU / 0.614 GB (`db-f1-micro`) | Cheapest tier; max_connections 25, no SLA |
| Storage | 10 GB SSD, automatic increase | |
| Connections | Public IP, no authorized networks, SSL mode "encrypted only" | Only IAM-authorized Connector / Auth Proxy connections get in |
| Backups | Automated, 7 retained, point-in-time recovery off | PITR's log storage isn't needed yet |
| Deletion protection | On | |
| Maintenance | Sunday 01:00 Asia/Taipei, timing "Week 2" (`stable`, 15–21 days after notice) | Clear of the Sunday 03:00 ingestion; a single environment should not take "Week 1" (`canary`) updates |
| Flags | `cloudsql.iam_authentication=on` | Enables IAM database login |

About US$9/month at list price (shared core + 10 GB SSD) for every hour the
instance runs — including while Cloud Run is scaled to zero.

Then:

- Databases → Create `wobot`.
- Users → Add user account → Google Cloud IAM, with no database roles, for
  `wobot-api@…`, `wobot-migrator@…`, `wobot-ingest@…` and the developer's Google
  account. Service
  accounts appear as `<name>@$PROJECT_ID.iam`. Adding an IAM user also grants it
  `roles/cloudsql.instanceUser`.
- IAM → Grant access → each of the three service accounts → Cloud SQL Client. Add roles with
  "Grant access" or "Add another role": changing a principal's existing role in
  the edit panel replaces it.

Three gates stand between a workload and a table:

| Gate | Checks | Granted by |
| --- | --- | --- |
| Connect | Ephemeral certificate from the Cloud SQL Admin API | `roles/cloudsql.client` |
| Log in | IAM token accepted in place of a password | `roles/cloudsql.instanceUser` + IAM database user |
| Tables | Read, write, DDL | PostgreSQL `GRANT` (section 8) |

Equivalent (times in `gcloud` flags are UTC):

```sh
gcloud sql instances create wobot-pg --database-version=POSTGRES_18 \
  --edition=enterprise --tier=db-f1-micro --region=$REGION --availability-type=zonal \
  --storage-type=SSD --storage-size=10 --storage-auto-increase \
  --database-flags=cloudsql.iam_authentication=on --ssl-mode=ENCRYPTED_ONLY \
  --backup-start-time=17:00 --retained-backups-count=7 --no-enable-point-in-time-recovery \
  --maintenance-window-day=SAT --maintenance-window-hour=17 \
  --maintenance-release-channel=production --deletion-protection
gcloud sql users set-password postgres --instance=wobot-pg --prompt-for-password
gcloud sql databases create wobot --instance=wobot-pg
gcloud sql users create wobot-api@$PROJECT_ID.iam --instance=wobot-pg --type=cloud_iam_service_account
gcloud sql users create <developer@gmail.com> --instance=wobot-pg --type=cloud_iam_user
for sa in wobot-api wobot-migrator wobot-ingest; do
  for role in roles/cloudsql.client roles/cloudsql.instanceUser; do
    gcloud projects add-iam-policy-binding $PROJECT_ID \
      --member="serviceAccount:$sa@$PROJECT_ID.iam.gserviceaccount.com" --role=$role
  done
done
```

Verify:

```sh
gcloud sql instances describe wobot-pg
gcloud sql users list --instance=wobot-pg
gcloud projects get-iam-policy $PROJECT_ID --flatten="bindings[].members" \
  --filter="bindings.members~^serviceAccount:wobot-" --format="table(bindings.role,bindings.members)"
# 6 rows: cloudsql.client and cloudsql.instanceUser for each service account
# (a seventh, run.developer for wobot-deployer, once section 11 is done)
```

## 8. Database bootstrap

Run once per environment, as the instance admin. Locally, `compose.yaml` runs
it through `infra/sql/local-init.sh`. In the cloud: SQL → `wobot-pg` → Cloud
SQL Studio, database `wobot`, user `postgres`, built-in authentication.

1. Confirm the pgvector version matches the local image
   (`pgvector/pgvector:0.8.5-pg18`):

   ```sql
   SELECT default_version FROM pg_available_extensions WHERE name = 'vector';
   ```

2. Run [`sql/bootstrap.sql`](sql/bootstrap.sql): pgvector, the group roles
   `wobot_migrator` / `wobot_api` / `wobot_ingest`, the `app` / `knowledge` /
   `ops` schemas owned by `wobot_migrator`, and CONNECT limited to the groups.

3. Put the IAM users into their groups. This part is environment-specific, so
   it stays out of the bootstrap file:

   ```sql
   GRANT wobot_migrator TO "wobot-migrator@<PROJECT_ID>.iam";
   GRANT wobot_api TO "wobot-api@<PROJECT_ID>.iam";
   GRANT wobot_ingest TO "wobot-ingest@<PROJECT_ID>.iam";
   GRANT wobot_migrator TO "<developer@gmail.com>";
   ```

   Quoted names are case-sensitive; copy them from `gcloud sql users list`.

Cloud SQL's `postgres` holds `cloudsqlsuperuser`, not superuser. On PG16+ a
non-superuser admin gets only ADMIN OPTION on the roles it creates, so the
bootstrap grants itself SET on `wobot_migrator` before handing it the schemas;
without that line `CREATE SCHEMA ... AUTHORIZATION` fails with
`must be able to SET ROLE`. The local admin is deliberately not a superuser
either, so this class of error shows up locally first.

Schemas belong to a group role rather than a login identity: replacing a
service account, or migrating as the developer, changes a membership instead of
object ownership. The cost: migrations must `SET ROLE wobot_migrator` before
creating anything, or new objects belong to whoever logged in.

Verify in Cloud SQL Studio:

```sql
-- app, knowledge and ops, all owned by wobot_migrator
SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace
WHERE nspname IN ('app', 'knowledge', 'ops');
-- exactly the four memberships above
SELECT g.rolname, m.rolname FROM pg_auth_members am
JOIN pg_roles g ON g.oid = am.roleid JOIN pg_roles m ON m.oid = am.member
WHERE g.rolname LIKE 'wobot\_%' AND m.rolname <> 'postgres';
```

End to end through all three gates, as the developer. No password: the proxy
signs in with an IAM token from ADC.

```sh
cloud-sql-proxy --auto-iam-authn --port 6543 $INSTANCE_CONNECTION_NAME
# in another terminal:
psql "host=127.0.0.1 port=6543 dbname=wobot user=<developer@gmail.com> sslmode=disable" \
  -c "SELECT current_user" -c "SET ROLE wobot_migrator" -c "SELECT current_user"
```

Port 6543 avoids the local compose database on 5432. `sslmode=disable` covers
only the localhost hop to the proxy; the proxy encrypts the connection to
Cloud SQL.

## 9. First deployment

Cloud SQL must be running: `gcloud sql instances describe wobot-pg
--format="value(state)"` prints `RUNNABLE`.

### Image

Built locally, not with Cloud Build: Cloud Build would run as the default
compute service account, which holds no roles here, and CI builds on GitHub
runners anyway (section 11). Cloud Run needs `linux/amd64`, so an Apple Silicon
Mac cross-builds:

```sh
cd backend
git status --short   # must print nothing: the tag names this commit
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/api:$(git rev-parse HEAD)"
gcloud auth configure-docker $REGION-docker.pkg.dev   # once
docker buildx build --platform linux/amd64 --provenance=false -t "$IMAGE" --push .
```

The credential helper hands docker a short-lived token from the gcloud login;
no password is stored.

Verify:

```sh
gcloud artifacts docker images list $REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO --include-tags
```

### Migration job

**Console:** Cloud Run → Jobs → Deploy container.

| Setting | Value | Why |
| --- | --- | --- |
| Image | `api` at the commit's tag | The console pins its digest; same image as the API |
| Name / region | `wobot-migrate`, `asia-east1` | |
| Tasks | 1 | Migrations never run in parallel |
| Command / arguments | `alembic` / `upgrade`, `head` | |
| Retries per failed task | 0 | A failed migration needs a person; a retry fails the same way and buries the first error |
| Task timeout | 10 minutes | |
| Environment | `GOOGLE_CLOUD_PROJECT`, `DB_MODE=cloudsql`, `INSTANCE_CONNECTION_NAME`, `DB_USER=wobot-migrator@$PROJECT_ID.iam` | No secrets |
| Service account | `wobot-migrator` | ADC in the job, and therefore the database login |
| Cloud SQL connections | None | The app connects through the Python Connector |

Equivalent:

```sh
gcloud run jobs deploy wobot-migrate --image "$IMAGE" --region $REGION \
  --command alembic --args upgrade,head --tasks 1 --max-retries 0 --task-timeout 10m \
  --service-account wobot-migrator@$PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,DB_MODE=cloudsql,INSTANCE_CONNECTION_NAME=$INSTANCE_CONNECTION_NAME,DB_USER=wobot-migrator@$PROJECT_ID.iam"
gcloud run jobs execute wobot-migrate --region $REGION --wait
```

`execute --wait` exits non-zero when the migration fails, which is what stops a
deploy before the API changes. Later deploys come from CI (section 11), which
only swaps the image with `gcloud run jobs update wobot-migrate --image "$IMAGE"`.

Verify:

```sh
gcloud run jobs executions list --job wobot-migrate --region $REGION   # 1 / 1 complete
```

Then in Cloud SQL Studio, signed in as the developer with IAM authentication:

```sql
SELECT version_num FROM ops.alembic_version;
-- every table owned by wobot_migrator
SELECT schemaname, tablename, tableowner FROM pg_tables WHERE schemaname IN ('app', 'ops');
```

### API service

**Console:** Cloud Run → Services → Deploy container.

| Setting | Value | Why |
| --- | --- | --- |
| Image | The job's digest | |
| Name / region | `wobot-api`, `asia-east1` | |
| Authentication | Allow public access | Users hold Firebase identities, not IAM ones; the app checks the token and the allowlist |
| Billing | Request-based | CPU only while serving, hence the connector's lazy refresh |
| Service scaling | Auto, min 0, max 3 | Scales to zero when idle; 3 × 4 pooled connections fit the budget |
| Startup CPU boost | On (the default) | Extra CPU while an instance starts shortens cold starts |
| Ingress | All | Phones reach it over the internet |
| Port / command | 8080 / image default | |
| Environment | `GOOGLE_CLOUD_PROJECT`, `APP_VERSION=<git sha>`, `DB_MODE=cloudsql`, `INSTANCE_CONNECTION_NAME`, `DB_USER=wobot-api@$PROJECT_ID.iam` | `APP_VERSION` is the only link from a digest-pinned revision back to its commit |
| Secret as variable | `OPENAI_API_KEY` from `openai-api-key`, version `1` | A pinned version changes only with a deploy, for every instance at once |
| Service account | `wobot-api` | |
| Cloud SQL connections | None | |

"Allow public access" disables the invoker IAM check
(`run.googleapis.com/invoker-iam-disabled`) instead of granting
`roles/run.invoker` to `allUsers`, so the service's IAM policy stays empty.

Equivalent:

```sh
gcloud run deploy wobot-api --image "$IMAGE" --region $REGION \
  --no-invoker-iam-check --ingress all --cpu-throttling --cpu-boost --min 0 --max 3 \
  --service-account wobot-api@$PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,APP_VERSION=$(git rev-parse HEAD),DB_MODE=cloudsql,INSTANCE_CONNECTION_NAME=$INSTANCE_CONNECTION_NAME,DB_USER=wobot-api@$PROJECT_ID.iam" \
  --set-secrets OPENAI_API_KEY=openai-api-key:1
```

`--cpu-throttling` is request-based billing. `--min` and `--max` apply to the
whole service; `--min-instances` and `--max-instances` would be per revision.

Measured on the first deployment: an idle instance stops after about 15 minutes,
and the next request waits about 7 s, almost all of it before uvicorn starts.
Warm requests take under 30 ms. `--min 1` would remove the wait at the cost of
an instance kept running.

Verify:

```sh
URL=$(gcloud run services describe wobot-api --region $REGION --format="value(status.url)")
curl -s "$URL/health"    # version is the deployed commit
curl -si "$URL/v1/me"    # 401
gcloud run services describe wobot-api --region $REGION
# Scaling: Auto (Min: 0, Max: 3); the secret appears only as openai-api-key:1
gcloud run services get-iam-policy wobot-api --region $REGION   # no bindings
```

### Allowlist

Cloud SQL Studio, signed in as the developer with IAM authentication. The
developer edits the table through `wobot_migrator`; the API can only read it.

```sql
INSERT INTO app.allowed_emails (email) VALUES ('<email in lowercase>');
```

Emails never go into the repository. Deleting a row locks that account out on
its next request.

### Rollback

Traffic follows the latest ready revision. To move it back:

```sh
gcloud run revisions list --service wobot-api --region $REGION
gcloud run services update-traffic wobot-api --region $REGION --to-revisions <revision>=100
```

While traffic is pinned to a revision, new deploys receive none, and the deploy
workflow's version check fails; hand it back with
`gcloud run services update-traffic wobot-api --region $REGION --to-latest`.
A rollback does not undo migrations, which is why schema changes follow
expand/contract: the previous revision must keep working on the newer schema.

### Ingestion job

Downloads the product catalog and reads the listed pages of the GRC and G-Tech sites,
the images the G-Tech pages show, the WhizToys documentation and the three PDFs the
G-Tech site links to over the internet, stores them as knowledge and publishes a version
(`backend/README.md`, "Knowledge ingestion"). Cloud Run jobs reach the internet by
default; nothing more is needed for the sites. It relies on what sections 2–8
set up for `wobot-ingest`: the Drive API, the account, the secret, the
knowledge bucket, the database user and its group role.

**Drive:** the job reads the catalog with a token carrying the `drive.readonly`
scope, which Cloud Run's metadata server issues on request. Drive access is not
an IAM role: the file's owner grants and revokes it, outside the project.

- If you own the file: Share → add
  `wobot-ingest@$PROJECT_ID.iam.gserviceaccount.com` as Viewer, with "Notify
  people" off.
- The current catalog belongs to another account and is shared as "anyone with
  the link can view". The account reads it by ID without being added, which
  works until the owner turns link sharing off; from then on every run fails
  and the published version stays as it was.

The file must be a stored `.xlsx`, not a Google Sheet. Its ID is the part of
its URL after `/d/`; it is set on the job and never enters the repository.

The WhizPad catalog, one of the three PDFs, is on Drive too, linked from the public
WhizPad page and shared the same way, so the account reads it by the ID that page
publishes, listed in `knowledge/profiles.py`. A local run cannot: the developer's
default credentials carry no Drive scope, so a local run is given a copy downloaded
from the page with `--document-file whizpad_catalog=<path>`.

**Console:** Cloud Run → Jobs → Deploy container.

| Setting | Value | Why |
| --- | --- | --- |
| Image | `api` at the commit's tag | Same image as the API |
| Name / region | `wobot-ingest`, `asia-east1` | |
| Tasks | 1 | Two runs would race to publish, and one would fail |
| Command / arguments | `wobot-ingest` / `run` | |
| Retries per failed task | 1 | A network or provider failure may pass the second time; stored rows are reused, so a retry repeats no paid call |
| Task timeout | 30 minutes | |
| Memory | 1 GiB | |
| Environment | `GOOGLE_CLOUD_PROJECT`, `APP_VERSION=<git sha>`, `DB_MODE=cloudsql`, `INSTANCE_CONNECTION_NAME`, `DB_USER=wobot-ingest@$PROJECT_ID.iam`, `KNOWLEDGE_BUCKET`, `PRODUCT_CATALOG_FILE_ID` | `APP_VERSION` is recorded on every run |
| Secret as variable | `OPENAI_API_KEY` from `openai-api-key`, version `1` | |
| Service account | `wobot-ingest` | Its database login, bucket, secret and Drive access all follow from it |
| Cloud SQL connections | None | |

Equivalent:

```sh
gcloud run jobs deploy wobot-ingest --image "$IMAGE" --region $REGION \
  --command wobot-ingest --args run --tasks 1 --max-retries 1 --task-timeout 30m --memory 1Gi \
  --service-account wobot-ingest@$PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,APP_VERSION=$(git rev-parse HEAD),DB_MODE=cloudsql,INSTANCE_CONNECTION_NAME=$INSTANCE_CONNECTION_NAME,DB_USER=wobot-ingest@$PROJECT_ID.iam,KNOWLEDGE_BUCKET=$KNOWLEDGE_BUCKET,PRODUCT_CATALOG_FILE_ID=<FILE_ID>" \
  --set-secrets OPENAI_API_KEY=openai-api-key:1
gcloud run jobs execute wobot-ingest --region $REGION --wait
```

The job exits non-zero when a run fails, so the execution shows as failed. A
run that finds the content unchanged ends as `no_change` and embeds nothing.
Measured on the first executions: after a 7–11 s start, publishing the 163
products took 6 s, and the `no_change` run that followed under 1 s.
The speeches are read by a model (`EXTRACTION_MODEL`, with the same key): the first
run asks about each of the ~280 talks, which took 2 minutes and about US$0.05 with
`gpt-5.6-luna` when measured locally. The answers stay in `knowledge.llm_extractions`, so later runs ask
only about new or edited talks. Images and PDF pages are read the same way by
`VISION_MODEL` (default in `backend/src/wobot/config.py`), once per file.
`--args run,--policy,dry-run` on `execute` builds and validates a version
without publishing it.

Exit codes: `published`, `no_change`, `validated`, `held` and `skipped_concurrent`
end with 0, since a retry would change none of them; a failure ends non-zero and the
task is retried once. A run first takes a PostgreSQL advisory lock, on one of the
job's pooled connections: a run started while another holds it, such as a manual
execution during a scheduled one, is recorded as `skipped_concurrent` and ends.

Verify:

```sh
gcloud run jobs executions list --job wobot-ingest --region $REGION --limit 3
# One structured line per stage; the last one carries the outcome:
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="wobot-ingest" AND jsonPayload.stage="finished"' \
  --limit 3 --format="table(timestamp, jsonPayload.status, jsonPayload.counts)"
gcloud storage ls gs://$KNOWLEDGE_BUCKET/sha256/   # one object per distinct file
```

Then in Cloud SQL Studio:

```sql
SELECT a.index_version_id, a.revision, a.published_at, v.status, v.validation_report
FROM knowledge.active_knowledge a JOIN knowledge.index_versions v USING (index_version_id);
SELECT started_at, status, code_version, counts, errors
FROM ops.ingestion_runs ORDER BY started_at DESC LIMIT 3;
```

### Held versions and rollback

A run that would empty a source, or lose more than `PUBLISH_MAX_DROP` (20%) of a
source's records, builds the version and holds it: the execution succeeds, the
published version stays as it was, and the `versioned` log line lists why. A page that
changed shape, or a model that stopped answering, looks just like items deleted at the
source, so a person decides. Each command's output is in the execution's logs:

```sh
gcloud run jobs execute wobot-ingest --region $REGION --args=report,<VERSION> --wait
gcloud run jobs execute wobot-ingest --region $REGION --args=accept,<VERSION> --wait
gcloud run jobs execute wobot-ingest --region $REGION --args=rollback,<VERSION> --wait
```

`report` lists the checks and the records each source added and lost. `accept`
publishes a held version as it was built, and refuses once another version was
published after it. `rollback` points back at a version published before, embedded
with the same model. Both move the pointer by compare-and-swap, as the job does.

### Schedule

Ingestion runs by itself once a month, on the first Sunday at 03:00 Taipei time.
Cloud Scheduler cannot name the first Sunday: a schedule restricting both the day of
the month and the day of the week runs when
[either matches](https://docs.cloud.google.com/scheduler/docs/configuring/cron-job-schedules).
So it starts the job every Sunday with `run --scheduled`, and the run goes ahead only
on the month's first Sunday (`backend/src/wobot/knowledge/schedule.py`); on the other
Sundays it ends before reading a setting or opening a connection. Manual executions
keep the job's own arguments and always run.

**Account:** create `wobot-scheduler` (section 4) with no project roles. Cloud Run →
Jobs → `wobot-ingest` → Permissions → Grant access → `wobot-scheduler@…` →
**Cloud Run Jobs Executor With Overrides**. It may start this job with other
arguments and cancel its executions, nothing more: Cloud Run Invoker cannot pass
arguments, and Cloud Run Developer, which the Cloud Run docs suggest for overrides,
could change the job's image.

**Console:** Cloud Scheduler → Create job. Not Cloud Run's Triggers → Add scheduler
trigger: that one sends no arguments, so every Sunday would be a full run.

| Setting | Value | Why |
| --- | --- | --- |
| Name / region | `wobot-ingest-monthly`, `asia-east1` | |
| Frequency | `0 3 * * 0` | Every Sunday at 03:00; the run picks the first |
| Time zone | `Asia/Taipei` | No daylight saving, so 03:00 never repeats or goes missing |
| Target | HTTP, `POST https://run.googleapis.com/v2/projects/<PROJECT_ID>/locations/asia-east1/jobs/wobot-ingest:run` | The Cloud Run Admin API's `jobs.run` |
| Headers | `Content-Type: application/json` | |
| Body | `{"overrides":{"containerOverrides":[{"args":["run","--scheduled"]}]}}` | Replaces the job's arguments for this execution only |
| Auth header | OAuth token, `wobot-scheduler`, scope `https://www.googleapis.com/auth/cloud-platform` | A Google API takes OAuth, not an OIDC ID token |

Equivalent:

```sh
gcloud iam service-accounts create wobot-scheduler \
  --description="Starts the wobot-ingest job on its schedule"
gcloud run jobs add-iam-policy-binding wobot-ingest --region $REGION \
  --member="serviceAccount:wobot-scheduler@$PROJECT_ID.iam.gserviceaccount.com" \
  --role=roles/run.jobsExecutorWithOverrides
gcloud scheduler jobs create http wobot-ingest-monthly --location $REGION \
  --schedule="0 3 * * 0" --time-zone="Asia/Taipei" \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/wobot-ingest:run" \
  --http-method=POST --headers="Content-Type=application/json" \
  --message-body='{"overrides":{"containerOverrides":[{"args":["run","--scheduled"]}]}}' \
  --oauth-service-account-email="wobot-scheduler@$PROJECT_ID.iam.gserviceaccount.com" \
  --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
```

Verify:

```sh
gcloud scheduler jobs describe wobot-ingest-monthly --location $REGION \
  --format="value(schedule, timeZone, state)"   # 0 3 * * 0  Asia/Taipei  ENABLED
gcloud run jobs get-iam-policy wobot-ingest --region $REGION   # wobot-scheduler: jobsExecutorWithOverrides only
# Force a run: on any day but the first Sunday, the execution ends at once.
gcloud scheduler jobs run wobot-ingest-monthly --location $REGION
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="wobot-ingest" AND jsonPayload.message:"scheduled run skipped"' \
  --limit 1 --format="value(timestamp)"
```

Cloud Scheduler includes three jobs per billing account at no charge, and a skipped
Sunday costs one container start of a few seconds. While Cloud SQL is stopped for a
long time, pause the schedule
(`gcloud scheduler jobs pause wobot-ingest-monthly --location $REGION`, `resume` to
restart it). Left running, a first Sunday with the instance stopped fails to connect,
is retried once and fails again: the execution shows as failed and the published
version stays as it was, though the API is down anyway while the instance is.
Other Sundays never connect, so they still succeed. Run the job by hand once the
instance is back.

## 10. Firebase Authentication

Firebase runs on the same project. The app signs in with Google through
Firebase, and the API verifies the resulting Firebase ID token without any
Firebase credentials: `firebase_admin` checks it against Google's public keys
and the project ID in `GOOGLE_CLOUD_PROJECT`.

### Firebase on the project

**Console:** [Firebase console](https://console.firebase.google.com) → Create a
project → add Firebase to the existing Google Cloud project → Google Analytics
off. The Firebase page inside the Google Cloud console only links there.

Equivalent:

```sh
firebase projects:addfirebase $PROJECT_ID
```

### Google sign-in

**Console:** Authentication → Sign-in method → Google → Enable. Set the
public-facing name, which Google's sign-in page shows, and the support email.
Leave every other provider disabled.

Enable it before generating the app's config: only then does the iOS config
include the OAuth client (`CLIENT_ID`, `REVERSED_CLIENT_ID`) that Google
Sign-In needs. There is no CLI equivalent short of calling the Identity Toolkit
admin API with an OAuth client secret.

Verify:

```sh
TOKEN=$(gcloud auth print-access-token)
# The response also holds the Google provider's OAuth client secret: keep only name and enabled.
curl -s -H "Authorization: Bearer $TOKEN" -H "X-Goog-User-Project: $PROJECT_ID" \
  "https://identitytoolkit.googleapis.com/admin/v2/projects/$PROJECT_ID/defaultSupportedIdpConfigs" \
  | jq '[.defaultSupportedIdpConfigs[] | {name, enabled}]'   # only google.com, enabled
curl -s -H "Authorization: Bearer $TOKEN" -H "X-Goog-User-Project: $PROJECT_ID" \
  "https://identitytoolkit.googleapis.com/admin/v2/projects/$PROJECT_ID/config" \
  | jq '.signIn | {email: (.email.enabled // false), phone: (.phoneNumber.enabled // false), anonymous: (.anonymous.enabled // false)}'   # all false
```

`X-Goog-User-Project` bills the calls to this project, since user credentials
carry no project of their own.

### iOS app

**Console:** Project settings → General → Your apps → Add app → iOS. Bundle ID
`com.kaiweichang.wobot`, nickname `Wobot iOS`, no App Store ID. Skip the
download, SDK and initialization steps: FlutterFire generates the config, and
the Flutter plugins add and start the SDK.

Equivalent:

```sh
firebase apps:create IOS "Wobot iOS" --bundle-id com.kaiweichang.wobot --project $PROJECT_ID
```

Verify:

```sh
firebase apps:list --project $PROJECT_ID   # Wobot iOS, IOS
```

The client config is generated into gitignored files, as described in
[`app/README.md`](../app/README.md). Android is registered before phase C, with
the SHA-1 of its signing key.

### Access and keys

- Firebase creates a user for every Google account that signs in. Whether the
  account may use Wobot is decided by the allowlist (section 9), on every
  request. The API does not check token revocation, so disabling a Firebase user
  takes up to an hour, until its ID token expires, to lock the account out.
- Adding Firebase also created the `firebase-adminsdk-fbsvc` service account,
  with Firebase admin roles and project-level Service Account Token Creator,
  which lets it act as any service account in the project. Wobot never uses it.
  Never generate a key for it (Firebase → Project settings → Service accounts);
  trimming its roles belongs to later hardening.
- Firebase created an iOS key and an unused Browser key. The iOS key ships inside
  the app, in `GoogleService-Info.plist` and the compiled options: it identifies
  the project to Firebase APIs and is not a secret. Both keys are limited to
  Firebase APIs; restricting the iOS key to the bundle ID, and App Check, come
  later.

```sh
# Restrictions only; the key strings are not printed.
gcloud services api-keys list --format="table(displayName, \
  restrictions.apiTargets.len():label=APIS, \
  restrictions.iosKeyRestrictions.allowedBundleIds.list():label=BUNDLE_IDS)"
```

End-to-end check: after signing in on a device, Authentication → Users lists the
account, and its User UID equals `account_id` in `app.accounts`.

## 11. CI/CD with Workload Identity Federation

Pull requests that touch the backend run `.github/workflows/backend-ci.yml`. A
push to main runs `.github/workflows/backend-deploy.yml`: the same checks, then
the image, the migration job and the API, as `wobot-deployer`. GitHub's OIDC
token is exchanged for short-lived credentials; no service account key exists.

Three checks stand between a workflow and the project:

| Check | Where |
| --- | --- |
| The token comes from this repository's main branch | The provider's attribute condition |
| That identity may impersonate `wobot-deployer` | The service account's IAM policy |
| What `wobot-deployer` may do | Its roles |

### Identity pool and provider

**Console:** IAM & Admin → Workload Identity Federation → Create pool: name
`GitHub Actions`, ID `github`. Add a provider: OpenID Connect, ID `wobot`,
issuer `https://token.actions.githubusercontent.com`, default audience.

| Google attribute | Token claim |
| --- | --- |
| `google.subject` | `assertion.sub` |
| `attribute.repository_id` | `assertion.repository_id` |
| `attribute.repository_owner_id` | `assertion.repository_owner_id` |
| `attribute.ref` | `assertion.ref` |

Attribute condition, with the IDs from
`gh api repos/<owner>/<repo> --jq '{repository_id: .id, owner_id: .owner.id}'`:

```
assertion.repository_owner_id == '<OWNER_ID>' && assertion.repository_id == '<REPO_ID>' && assertion.ref == 'refs/heads/main'
```

- Numeric IDs, not names: a deleted or renamed account or repository frees its
  name for someone else, while IDs are never reused. The owner is checked too,
  because a transferred repository keeps its ID.
- `ref` limits deploys to main, so a pushed branch cannot deploy what no pull
  request reviewed. Pull requests from forks get no OIDC token at all.

Equivalent:

```sh
gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc wobot \
  --location=global --workload-identity-pool=github --display-name="wobot repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository_owner_id == '<OWNER_ID>' && assertion.repository_id == '<REPO_ID>' && assertion.ref == 'refs/heads/main'"
```

Verify:

```sh
gcloud iam workload-identity-pools providers describe wobot --location=global \
  --workload-identity-pool=github \
  --format="yaml(name,state,oidc.issuerUri,oidc.allowedAudiences,attributeMapping,attributeCondition)"
# state ACTIVE; no allowedAudiences means the default audience
```

### Deployer permissions

**Console:**

- The `github` pool → Grant access → using service account impersonation →
  `wobot-deployer`, only identities whose `repository_id` is `<REPO_ID>`. Close
  the configuration file download: workflows need only the provider name.
- IAM → Grant access → `wobot-deployer` → Cloud Run Developer.
- Artifact Registry → select `wobot` → Permissions → Add principal →
  Artifact Registry Writer.
- Service Accounts → `wobot-api`, `wobot-migrator`, then `wobot-ingest` →
  Principals with access → Grant access → `wobot-deployer` → Service Account
  User.

| Role | Scope | Why |
| --- | --- | --- |
| Workload Identity User | `wobot-deployer`, for this repository ID | Workflows impersonate the deployer |
| Cloud Run Developer | Project | Update and run the job, deploy the service; no IAM changes |
| Artifact Registry Writer | `wobot` repository | Push images, but not delete them |
| Service Account User | `wobot-api`, `wobot-migrator`, `wobot-ingest` | Deploy code that runs as these three, and no other service account |

Impersonation rather than granting roles to the federated identity directly:
every API accepts a service account's token, while not every resource accepts
federated principals, and one named identity holds all deploy rights, so
disabling it stops every deploy.

`wobot-api` has the invoker IAM check disabled. Changing that setting needs
`run.services.setIamPolicy`, but a deploy that leaves it alone does not: Cloud
Run Developer was enough for the first CI deploy. The workflow therefore passes
no access flags.

Equivalent:

```sh
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
DEPLOYER=wobot-deployer@$PROJECT_ID.iam.gserviceaccount.com
gcloud iam service-accounts add-iam-policy-binding $DEPLOYER \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository_id/<REPO_ID>"
gcloud projects add-iam-policy-binding $PROJECT_ID --role=roles/run.developer \
  --member="serviceAccount:$DEPLOYER"
gcloud artifacts repositories add-iam-policy-binding $AR_REPO --location=$REGION \
  --role=roles/artifactregistry.writer --member="serviceAccount:$DEPLOYER"
for sa in wobot-api wobot-migrator wobot-ingest; do
  gcloud iam service-accounts add-iam-policy-binding $sa@$PROJECT_ID.iam.gserviceaccount.com \
    --role=roles/iam.serviceAccountUser --member="serviceAccount:$DEPLOYER"
done
```

The principal set names the project number, not the project ID.

Verify:

```sh
gcloud iam service-accounts get-iam-policy $DEPLOYER --format="yaml(bindings)"
gcloud projects get-iam-policy $PROJECT_ID --flatten="bindings[].members" \
  --filter="bindings.members:wobot-deployer@" --format="value(bindings.role)"   # only run.developer
gcloud artifacts repositories get-iam-policy $AR_REPO --location=$REGION --format="yaml(bindings)"
for sa in wobot-api wobot-migrator wobot-ingest; do
  gcloud iam service-accounts get-iam-policy $sa@$PROJECT_ID.iam.gserviceaccount.com --format="yaml(bindings)"
done
```

### Repository variables

**GitHub:** Settings → Secrets and variables → Actions → Variables.

| Variable | Value |
| --- | --- |
| `GCP_PROJECT_ID` | The project ID |
| `GCP_WIF_PROVIDER` | `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github/providers/wobot` |
| `GCP_DEPLOYER_SA` | `wobot-deployer@<PROJECT_ID>.iam.gserviceaccount.com` |

Variables, not secrets: none of them grants anything, the logs of a public
repository are public anyway, and masking would blank out image names. Keeping
them out of the workflow leaves it free of environment identifiers.
`gh variable set <NAME> --body <VALUE>` is the CLI; `gh variable list` verifies.

### Deploying

Merging a pull request that changes `backend/**` or either workflow deploys it:

1. The checks from `backend-ci.yml`, through `workflow_call`.
2. `api:<commit sha>`, built on the runner and pushed.
3. `wobot-migrate` updated to that image and executed; a failed migration stops
   the run before the API changes.
4. `wobot-api` deployed with that image and `APP_VERSION=<commit sha>`.
5. `/health` must report the commit, or the run fails.
6. `wobot-ingest` updated to that image and `APP_VERSION`, but not executed:
   ingestion runs by hand or on its schedule.

Deploys run one at a time; a newer push waits for the running one. To retry,
open the run and choose Re-run failed jobs, or run the workflow manually on
main. The first CI deploy took under three minutes from merge to serving.

Verify:

```sh
gcloud run revisions list --service wobot-api --region $REGION --limit 3 \
  --format="table(metadata.name, metadata.annotations['serving.knative.dev/creator'], metadata.creationTimestamp.date('%Y-%m-%d %H:%M'))"
gcloud run jobs executions list --job wobot-migrate --region $REGION --limit 2
# Both list wobot-deployer as the creator and runner of the newest entry.
```

## Connection budget

`db-f1-micro` allows `max_connections = 25`, and
`superuser_reserved_connections = 3` of those are held back: 22 remain for
Wobot's roles.

| Client | Connections |
| --- | --- |
| API: 3 instances × (pool 2 + overflow 2) | 12 |
| Migration job | 1 |
| Developer (Cloud SQL Studio or proxy) | 2 |
| Ingestion job (pool 2 + overflow 2, one holding the run lock) | 4 |
| **Total** | **19 of 22** |

Cap the API at the [service level](https://docs.cloud.google.com/run/docs/configuring/max-instances)
(`gcloud run services update wobot-api --max 3`), not only per revision:
`--max-instances` limits each revision separately, so during a deploy the old
and new revisions can both scale to it. Pools open connections lazily, so real
usage stays well below the table. If connection-slot errors appear, lower
`max_overflow` or move to `db-g1-small` (50 connections).

## Cost controls

- Cloud SQL bills for every hour the instance runs, even when Cloud Run has
  scaled to zero. Stop it when not developing: SQL → `wobot-pg` → Stop
  (`gcloud sql instances patch wobot-pg --activation-policy=NEVER`; `ALWAYS`
  starts it again). Storage and backups are still billed while it is stopped.
  For a long stop, pause the ingestion schedule too (section 9, "Schedule").
- Cloud Run bills only while handling requests (request-based billing, minimum
  0 instances), so the idle API costs nothing.
- Artifact Registry keeps only the 10 newest versions of each image.
