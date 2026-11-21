<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Connections and credential storage

Auto-Ontology reads source databases from two places: connections added in the
UI, and the `CONNECTION_STRINGS` environment variable. Use one per deployment,
because the two code paths order them differently:

- **Answering questions** uses UI-managed connections when any exist, and falls
  back to `CONNECTION_STRINGS`.
- **Ingestion** uses `CONNECTION_STRINGS` whenever it's set, and reads
  UI-managed connections only when it's empty.

## Connections added in the UI

Add connections under **Settings → Connections**, or through the API
(`POST /api/connections/test`, then `POST /api/connections`; permission
`connection:manage`).

Each connection can be scoped with a schema allowlist plus
`table_allow_regex` / `table_deny_regex`, applied at catalog extraction. Deny
wins, and names are unqualified and case-sensitive.

Where the credentials live depends on whether Vault is configured:

- **Without Vault.** The full connection object, password included, is
  serialized to JSON and stored unencrypted on the catalog's database row in
  Postgres.
- **With Vault.** Credentials are written to Vault, keyed by database name, and
  the catalog row stores none. The database name is the lookup key.

## `CONNECTION_STRINGS`

A comma- or newline-separated list of connection URLs supplied at deploy time
(`.env` for Docker Compose, `--set connectionStrings=...` for Helm). It carries
no schema or table filters. `GET /api/connections/source` reports whether it is set.

The URL scheme selects the connector:

| Engine        | Scheme                      | Add in the UI |
| ------------- | --------------------------- | :-----------: |
| PostgreSQL    | `postgresql://`, `postgres://` | yes        |
| MySQL         | `mysql://`                  | yes           |
| Databricks    | `databricks://`             | yes           |
| Snowflake     | `snowflake://`              | yes           |
| ClickHouse    | `clickhouse://`             | yes           |
| Trino         | `trino://`                  | yes           |
| Apache Kyuubi | `kyuubi://`                 | yes           |
| HeavyDB       | `heavydb://`                | yes           |
| Cloud Spanner | `spanner://`                | yes           |
| BigQuery      | `bigquery://`               | yes           |
| DuckDB        | `duckdb://`                 | no            |
| SQLite        | `sqlite://`                 | no            |

The registry lives in
[`auto_ontology/connectors/registry.py`](../auto_ontology/connectors/registry.py).

### Cloud Spanner

Added by this fork. It has been tested only on the local Spanner emulator, not
on real Cloud Spanner.

```text
spanner://<project>/<instance>/<database>
spanner://<project>/<instance>/<database>?emulator_host=<host>:<port>
```

In the UI the fields are Project ID, Instance ID, Database ID and an optional
Emulator Host.

- **GoogleSQL databases only.** A PostgreSQL-dialect database is refused with
  an error on the first statement.
- **Credentials.** The URL carries none. Without `emulator_host` the server
  uses Application Default Credentials (`gcloud auth application-default
  login`, `GOOGLE_APPLICATION_CREDENTIALS`, or the metadata server). With
  `emulator_host` it connects to that emulator without credentials. If the
  process has `SPANNER_EMULATOR_HOST` set and the URL does not name the same
  emulator, the connection fails with an error instead of being redirected.
- **Table names.** Tables in the default schema are listed under the database
  id (`music.Singers`); a named schema keeps its name (`music.sales.Promotions`).
  The leading `<database>.` is removed from generated SQL before it runs.
- **Relationships.** Declared foreign keys are read, and an interleaved table is
  reported as a foreign key to its parent.
- **Read-only.** Every statement runs in a read-only snapshot, so writes and DDL
  fail.
- **No query history.** Spanner keeps only aggregated query statistics, so none
  are ingested.
- **Lost connection.** If the server stops answering for 30 seconds while a
  statement runs, the statement fails with "Could not connect to Spanner".

Emulator quick start (the seed script creates instance `auto-ontology` and
database `music` in project `auto-ontology`):

```sh
docker run -d -p 127.0.0.1:9010:9010 -p 127.0.0.1:9020:9020 gcr.io/cloud-spanner-emulator/emulator
uv run python -m dev_tools.fixtures.seed_spanner_emulator
CONNECTION_STRINGS=spanner://auto-ontology/auto-ontology/music?emulator_host=localhost:9010
# From inside the Compose containers, use emulator_host=host.docker.internal:9010
```

On Linux, `host.docker.internal` resolves only if the service has
`extra_hosts: ["host.docker.internal:host-gateway"]`. The emulator keeps its
data in memory; run the seed script again after it restarts.

### BigQuery

Added by this fork. It is written for a real Google Cloud project with
Application Default Credentials, but **it has not yet been run against real
BigQuery**: so far it has only run on a community emulator, which is weak
evidence (see [the plan](./fork/BIGQUERY.md)).

```text
bigquery://<project>
bigquery://<project>/<dataset>
bigquery://<project>/<dataset>?location=<location>&maximum_bytes_billed=<bytes>
```

- `<project>` holds the data and runs the query jobs, so it is billed for them.
- `<dataset>` limits ingestion to one dataset and is the default dataset of
  every query. Without it, every dataset in the project is ingested, except
  hidden ones (ids starting with `_`).
- `location` is where jobs run (`US`, `EU`, `us-central1`, …). If it is not
  set, BigQuery works it out from the tables a query reads.
- `maximum_bytes_billed` is set on every query job (see Cost below).

In the UI the fields are Project ID, and optional Dataset, Location, Maximum
Bytes Billed and Emulator Host. The project id is stored as the connection's
`database`, which names the connection.

- **Credentials.** The URL carries none. The server uses Application Default
  Credentials: `GOOGLE_APPLICATION_CREDENTIALS` (a credentials file), the file
  that `gcloud auth application-default login` writes, or the metadata server
  on Google Cloud. If none is found, or a token cannot be refreshed, the
  connection fails with "Could not connect to BigQuery project …" and says
  what to run.
- **Permissions.** The connector lists datasets and tables, reads each table's
  schema and keys (`tables.get`), and runs query jobs. That needs **BigQuery
  Job User** (`roles/bigquery.jobUser`) on the project in the URL, and
  **BigQuery Data Viewer** (`roles/bigquery.dataViewer`) on each dataset it
  reads, or on the project for a connection without a dataset. A view also
  needs read access to the tables it reads, unless it is an authorized view.
  A denied request fails with an error naming both roles. These roles come from
  Google's role descriptions and have not been checked on a real project.
- **Cost.** Reading metadata uses the tables API, which bills no query bytes.
  Queries do bill: semantic compilation profiles columns (sample values) with
  queries, and every question runs its generated SQL. Under on-demand pricing
  each query bills at least 10 MB for each table it reads. With
  `maximum_bytes_billed` set, a query that would bill more fails without being
  charged, and the error names the setting. Without it, the project's own
  limits apply. Metadata calls are not affected by it, so ingestion still
  works with a small limit.
- **Read-only.** BigQuery has no read-only session, so the connector refuses
  anything that is not a single `SELECT` or `WITH` query before sending it,
  and refuses a script (`;` followed by more SQL). Give the credentials only
  the two roles above, so that IAM also refuses writes.
- **Table names.** The database is the project and the schema is the dataset,
  so tables are named `<project>.<dataset>.<table>`, BigQuery's own three-part
  name. Generated SQL runs exactly as written; a hyphenated project id at the
  start of a table path needs no backticks.
- **Keys.** BigQuery keys are declared and not enforced
  (`PRIMARY KEY … NOT ENFORCED`). Declared primary and foreign keys are read
  from each table. A foreign key that points outside what the connection
  ingests (another project, or another dataset when the URL names one) is left
  out with a warning. If no keys are declared, joins are left to the model
  during semantic compilation.
- **One connection per project.** Connections are stored and routed by
  project id, so a second connection to another dataset of the same project
  is refused as already existing. To cover several datasets, leave the
  dataset out. Two `CONNECTION_STRINGS` entries for one project clash in the
  same way.
- **Nested columns.** Each top-level column is one column. A `STRUCT` keeps
  its fields in its type (`STRUCT<city STRING, country STRING>`). A repeated
  field is `ARRAY<…>` and is reported as not nullable, because BigQuery stores
  a NULL array as an empty one. Profiling may try `SELECT DISTINCT` on an
  `ARRAY` or `STRUCT` column, which BigQuery rejects. The failure is caught, and
  the column gets no sample values. A `JSON` column gets PostgreSQL-style
  hints in prompts (shared code), which do not apply to BigQuery.
- **Timeouts.** Each request may take up to 60 seconds, failed requests are
  retried for up to 30 seconds, and failed jobs are re-run for up to 60. An
  unreachable endpoint fails with "Could not connect to BigQuery" within 30
  seconds. The client gives up early when its next back-off would pass that
  limit, so it can fail sooner.
- **No query history.** `INFORMATION_SCHEMA.JOBS` would bill bytes and need
  job-listing permission, so no query history is read.

#### Docker Compose with Google credentials

The backend and ingestion containers need the credentials too. The optional
override `docker-compose.gcp-adc.yml` mounts a credentials file read-only into
`auto-ontology` and `ingestion-service` and sets
`GOOGLE_APPLICATION_CREDENTIALS` to it:

```sh
gcloud auth application-default login
export GCP_CREDENTIALS_FILE="$HOME/.config/gcloud/application_default_credentials.json"
docker compose -f docker-compose.yml -f docker-compose.litellm.yml \
  -f docker-compose.gcp-adc.yml up -d
```

- `GCP_CREDENTIALS_FILE` is required: Compose stops with an error if it is
  not set. On Windows the gcloud file is
  `%APPDATA%\gcloud\application_default_credentials.json`. A service-account
  key file works the same way.
- `GOOGLE_CLOUD_QUOTA_PROJECT` is optional. It sets the project billed for API
  quota when the credentials belong to a user.
- The containers run as uid 1000. Docker Desktop lets them read a bind-mounted
  file. On Linux the file must be readable by uid 1000, but gcloud creates it
  with mode `0600` for your own user.

The override has been checked with `docker compose config`. A dummy
credentials file mounted into the backend image was found and loaded. It has
not been run against real BigQuery.

#### Emulator (development only)

The community emulator `ghcr.io/goccy/bigquery-emulator` (Google provides none)
was used to develop this connector. It differs from real BigQuery in ways that
matter: it reports no keys, ignores `maximum_bytes_billed`, and reports every
query error as internal. The full list is in [the plan](./fork/BIGQUERY.md).
Name the emulator in the URL. If the process has `BIGQUERY_EMULATOR_HOST` set
and the URL does not name the same emulator, the connection fails rather than
being redirected.

```sh
docker run -d -p 127.0.0.1:9050:9050 -p 127.0.0.1:9060:9060 ghcr.io/goccy/bigquery-emulator --project=auto-ontology-test
uv run python -m dev_tools.fixtures.seed_bigquery --project auto-ontology-test --emulator-host 127.0.0.1:9050
CONNECTION_STRINGS=bigquery://auto-ontology-test/auto_ontology_music?emulator_host=localhost:9050
# From inside the Compose containers, use emulator_host=host.docker.internal:9050
```

The emulator keeps its data in memory, so run the seed script again after it
restarts.

## Vault

Vault is off by default. It turns on only when all four of these are set:

- `VAULT_ADDR`
- `VAULT_NAMESPACE`
- `VAULT_ROLE_ID`
- `VAULT_SECRET_ID`

`VAULT_AUTH_MOUNT` and `VAULT_KV_MOUNT` are optional overrides. A partial
configuration is ignored with a warning, and credentials fall back to plaintext
storage in Postgres.
