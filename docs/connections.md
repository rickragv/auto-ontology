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

## Vault

Vault is off by default. It turns on only when all four of these are set:

- `VAULT_ADDR`
- `VAULT_NAMESPACE`
- `VAULT_ROLE_ID`
- `VAULT_SECRET_ID`

`VAULT_AUTH_MOUNT` and `VAULT_KV_MOUNT` are optional overrides. A partial
configuration is ignored with a warning, and credentials fall back to plaintext
storage in Postgres.
