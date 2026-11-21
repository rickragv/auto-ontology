<!-- SPDX-License-Identifier: Apache-2.0 -->

# Plan: BigQuery as a data source

Branch `bigquery-connector`, from the fork's `main` at `69b6dac5`. Requested on
2026-11-22: BigQuery as a source database, used like Postgres and Cloud Spanner to
build the ontology and answer questions. The app's own store stays Postgres.

On 2026-11-22 the owner added: the code must be written for a real GCP project, and
is to be tested on one once pushed, not on an emulator. So:

- Real BigQuery with Application Default Credentials is the primary path. Nothing in
  the connector is shaped around an emulator.
- The live tests and the seed script take a real project id and run against real
  BigQuery. They are the acceptance test.
- This machine has `gcloud` but no Application Default Credentials, so the live tests
  could not be run here. Until the owner runs them, development checks use the
  community emulator `ghcr.io/goccy/bigquery-emulator` (Google ships none). It is a
  development aid only; passing on it is **not** counted as tested.

The Spanner connector (`docs/fork/SPANNER.md`, `auto_ontology/connectors/spanner.py`) is
the model for structure, tests and error handling.

## Scope

- In: connect, ingest (tables, columns, declared keys, sample values), semantic
  compilation, question answering with generated SQL run on BigQuery.
- Out: BigQuery as the app's store; query history (`get_queries` returns an empty
  frame); the schema allowlist in the UI; the Storage Read API.

## Decisions

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Driver | `google-cloud-bigquery`, native client, results through the REST API | No source connector uses SQLAlchemy. No Storage API, so no extra dependency or permission. |
| 2 | Dependency | Hard dependency, imported lazily inside the connector | Same as Trino and Spanner. |
| 3 | Connection string | `bigquery://<project>` or `bigquery://<project>/<dataset>`, with optional `?emulator_host=<host>:<port>`, `?location=<region>`, `?maximum_bytes_billed=<n>` | Carries no secret. Without a dataset every dataset in the project is ingested. |
| 4 | Credentials | Application Default Credentials (`gcloud auth application-default login`, `GOOGLE_APPLICATION_CREDENTIALS`, or the metadata server). Anonymous only when the URL names an emulator | No secret in the connection. |
| 5 | Emulator targeting | Explicit in the URL. A process-wide `BIGQUERY_EMULATOR_HOST` that the URL does not ask for is an error | No silent redirect; same rule as Spanner. |
| 6 | sqlglot dialect | `bigquery` | Native. |
| 7 | Table naming | Database is the project, schema is the dataset: `<project>.<dataset>.<table>` | It is BigQuery's own three-part name, so prompts need no rewriting. A project id with a hyphen must be backticked; to be checked. |
| 8 | Writes | Statements other than `SELECT`/`WITH` are refused before they are sent | BigQuery has no read-only session. Fail loudly, not silently. |
| 9 | Cost | `maximum_bytes_billed` is passed to every query job when set; unset means BigQuery's own default (no limit) | Profiling scans columns and real BigQuery bills by bytes scanned. Optional, so the default is the library's. |
| 10 | Keys | Declared (unenforced) primary and foreign keys are read when the service reports them | They are the only relationship metadata BigQuery has. |
| 11 | Metadata source | Whatever is correct and cheap on real BigQuery (the tables API costs no query bytes; key constraints come from `INFORMATION_SCHEMA` or the table resource). One path, no fallback from one to the other | Chosen for the real service, not for what the emulator happens to support. |
| 12 | Add in the UI | Yes, after the backend path is proven | "Like Postgres". |
| 13 | Live tests | Not collected unless `AUTO_ONTOLOGY_LIVE_BIGQUERY_PROJECT=<project>` is set; then they run against that real project with ADC. Adding `AUTO_ONTOLOGY_LIVE_BIGQUERY_EMULATOR=<host>:<port>` points the same tests at an emulator | Upstream CI fails on more than two skipped tests. One test suite, so what passes on the emulator is what the owner runs on GCP. |
| 14 | Test data | The seed script creates one small dataset (a few KB) in the given project and can delete it again | Cost stays within BigQuery's free tier. |

## Steps

Each step is committed and pushed to `bigquery-connector` when its check has run.

1. This plan.
2. Dependency, `auto_ontology/connectors/bigquery.py`, registry entry, unit tests with a
   fake client.
3. Seed script and live tests (opt-in), written for a real project.
4. Run through the stack: ingest, compile, questions. On the emulator here; on the
   owner's project when credentials are available.
5. Add-in-the-UI support: connection-string factory and frontend enum.
6. Docs: `docs/connections.md`, README, this file's results section.

## Known risks

- The emulator may not report key constraints or may reject SQL that real BigQuery
  accepts (and the reverse). Anything proven only on the emulator is weak evidence.
- Nested and repeated columns (`STRUCT`, `ARRAY`) are common in BigQuery; shared
  profiling code treats some array types as text (see SPANNER.md, shared-code findings).
- The emulator proves nothing about IAM, quotas, job latency, locations or billing.

## Results

To be filled in as each step is run.
