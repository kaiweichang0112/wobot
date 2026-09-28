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

Equivalent:

```sh
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com firebase.googleapis.com identitytoolkit.googleapis.com
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
| `wobot-deployer` | GitHub Actions, through Workload Identity Federation |

One account per workload keeps a compromise contained: the API cannot change
the schema and cannot deploy. Every Cloud Run service and job names its account
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
  --filter="bindings.members:wobot-" --format="table(bindings.role,bindings.members)"
```

## 5. Secret Manager

**Console:** Security → Secret Manager → Create secret. Paste the value, keep
automatic replication, no rotation or expiry.

| Secret | Accessor |
| --- | --- |
| `openai-api-key` | `wobot-api` |
| `elevenlabs-api-key` | none yet (voice features, phase D) |

Grant on the secret itself (secret → Permissions → Grant access → Secret
Manager Secret Accessor), never at project level: a project-level accessor can
read every current and future secret in the project.

Equivalent:

```sh
read -s KEY && printf %s "$KEY" | gcloud secrets create openai-api-key --data-file=- && unset KEY
gcloud secrets add-iam-policy-binding openai-api-key \
  --member="serviceAccount:wobot-api@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

`read -s` keeps the key off the screen and out of shell history; `printf %s`
avoids the trailing newline that `echo` would add.

Verify:

```sh
gcloud secrets list
gcloud secrets get-iam-policy openai-api-key   # only wobot-api, secretAccessor
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
  `wobot-api@…`, `wobot-migrator@…` and the developer's Google account. Service
  accounts appear as `<name>@$PROJECT_ID.iam`. Adding an IAM user also grants it
  `roles/cloudsql.instanceUser`.
- IAM → Grant access → both service accounts → Cloud SQL Client. Add roles with
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
for sa in wobot-api wobot-migrator; do
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
  --filter="bindings.members:wobot-" --format="table(bindings.role,bindings.members)"
# 4 rows: cloudsql.client and cloudsql.instanceUser for each service account
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
-- exactly the three memberships above
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
deploy before the API changes. Later deploys only swap the image with
`gcloud run jobs update wobot-migrate --image "$IMAGE"`.

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
  --no-invoker-iam-check --ingress all --cpu-throttling --min 0 --max 3 \
  --service-account wobot-api@$PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,APP_VERSION=$(git rev-parse HEAD),DB_MODE=cloudsql,INSTANCE_CONNECTION_NAME=$INSTANCE_CONNECTION_NAME,DB_USER=wobot-api@$PROJECT_ID.iam" \
  --set-secrets OPENAI_API_KEY=openai-api-key:1
```

`--cpu-throttling` is request-based billing. `--min` and `--max` apply to the
whole service; `--min-instances` and `--max-instances` would be per revision.

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

While traffic is pinned to a revision, new deploys receive none; hand it back
with `gcloud run services update-traffic wobot-api --region $REGION --to-latest`.
A rollback does not undo migrations, which is why schema changes follow
expand/contract: the previous revision must keep working on the newer schema.

## 10. Firebase Authentication

_Pending._

## 11. CI/CD with Workload Identity Federation

_Pending._

## Connection budget

`db-f1-micro` allows `max_connections = 25`, and
`superuser_reserved_connections = 3` of those are held back: 22 remain for
Wobot's roles.

| Client | Connections |
| --- | --- |
| API: 3 instances × (pool 2 + overflow 2) | 12 |
| Migration job | 1 |
| Developer (Cloud SQL Studio or proxy) | 2 |
| Ingestion job (phase A) | 4 |
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
- Cloud Run bills only while handling requests (request-based billing, minimum
  0 instances), so the idle API costs nothing.
- Artifact Registry keeps only the 10 newest versions of each image.
