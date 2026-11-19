<!-- SPDX-License-Identifier: Apache-2.0 -->

# Plan: Cloud Spanner as a data source

Branch `spanner-connector`, from the fork's `main` at `0b06767a`. Requested on
2026-11-19: Spanner as a source database, used like Postgres to build the ontology and
answer questions. Tested on the local emulator only. The app's own store stays Postgres.

## Scope

- In: connect, ingest (tables, columns, keys, sample values), semantic compilation,
  question answering with generated SQL run on Spanner.
- GoogleSQL-dialect databases only. A PostgreSQL-dialect database is refused with a
  clear error.
- Out: Spanner as the app's store; query history (`get_queries` returns an empty
  frame, as Trino does); the schema allowlist in the UI.

## Decisions

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Driver | `google-cloud-spanner`, native client, read-only snapshots | No source connector uses SQLAlchemy. A snapshot cannot write, so generated DML fails loudly. |
| 2 | Dependency | Hard dependency, imported lazily inside the connector | Same as Trino. No Dockerfile change. A backend that never opens a Spanner connection never imports grpc. |
| 3 | Connection string | `spanner://<project>/<instance>/<database>` with optional `?emulator_host=<host>:<port>` | Parses with the existing registry code; carries no secret, so log redaction needs no change. |
| 4 | Credentials | Emulator: anonymous. Otherwise Application Default Credentials | No secret in the connection. The non-emulator path is written but **not tested**. |
| 5 | Emulator targeting | Explicit in the URL. A process-wide `SPANNER_EMULATOR_HOST` that the URL does not ask for is an error, not a silent redirect | No silent fallback. Exact mechanism to be confirmed against the library. |
| 6 | sqlglot dialect | `bigquery` | sqlglot has no Spanner dialect; BigQuery is its GoogleSQL one. |
| 7 | Table naming | Default schema is reported as the database id; `execute()` removes a leading `<database>.` qualifier; `qualify()` is overridden | Prompts always write `<database>.<schema>.<table>`, which Spanner rejects. HeavyDB solves the same problem this way, with no edit to shared code. |
| 8 | Interleaved tables | Reported as foreign keys from the child's key prefix to the parent's key | It is the main relationship in Spanner schemas. |
| 9 | Add in the UI | Yes, as a second step after the backend path is proven | The user asked for "like Postgres". |
| 10 | Emulator tests in CI | Not collected unless `AUTO_ONTOLOGY_LIVE_SPANNER=1` | Upstream CI fails on more than two skipped tests. |

## Steps

Each step is committed and pushed to `spanner-connector` when its check has run.

1. This plan.
2. Dependency, `auto_ontology/connectors/spanner.py`, registry entry, unit tests with a
   fake client.
3. Emulator seed script and emulator tests (opt-in).
4. Run through the real stack on the emulator: ingest, compile, one question.
5. Add-in-the-UI support: connection-string factory and frontend enum.
6. Docs: `docs/connections.md`, README, this file's results section.

## Known risks

- sqlglot rewrites GoogleSQL when it re-quotes identifiers (`INTERVAL 1 DAY` becomes
  `INTERVAL '1' DAY`, and so on). Each rewrite must be checked on the emulator.
- Prompts carry no Spanner rules, so the model may write Postgres habits (`::` casts,
  `ILIKE`). Fixing that needs a dialect-keyed rule shared with a future BigQuery
  connector; not in this change unless the stack run shows it is needed.
- The emulator does not prove IAM, real latency, session-pool behaviour or query
  statistics.

## Results

To be filled in as steps finish.
