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
| 7 | Table naming | Database is the project, schema is the dataset: `<project>.<dataset>.<table>`. Statements are sent as written; `qualify()` emits `` `project`.`dataset`.`table` `` | It is BigQuery's own three-part name, so prompts need no rewriting. Checked: BigQuery accepts a hyphenated project unquoted at the start of a table path, and sqlglot's `bigquery` dialect parses every spelling (see Results). |
| 8 | Writes | Statements other than `SELECT`/`WITH` are refused before they are sent | BigQuery has no read-only session. Fail loudly, not silently. |
| 9 | Cost | `maximum_bytes_billed` is passed to every query job when set; unset means BigQuery's own default (no limit) | Profiling scans columns and real BigQuery bills by bytes scanned. Optional, so the default is the library's. |
| 10 | Keys | Declared (unenforced) primary and foreign keys are read when the service reports them | They are the only relationship metadata BigQuery has. |
| 11 | Metadata source | The tables API only (`datasets.list`, `tables.list`, `tables.get`); keys from each table's `tableConstraints`. `INFORMATION_SCHEMA` is never queried. One path, no fallback | Chosen for the real service, not for what the emulator happens to support. See Results for why. |
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

### Steps 2 and 3: connector, seed script, live tests (2026-11-22)

Commits `7c427639` (dependency), `67041760` (connector and unit tests), `30adf38a`
(seed script and live tests). Upstream files touched: `registry.py` +2,
`connectors/__init__.py` +2, `pyproject.toml` +1, `uv.lock` (3 new packages:
`google-cloud-bigquery`, `google-resumable-media`, `google-crc32c`). No
`google-cloud-bigquery-storage` or `db-dtypes`; rows come back through the REST API and
the DataFrame is built from them.

- `uv run pytest auto_ontology/connectors -q`: 644 passed, 11 skipped, 1 error (before:
  466 / 11 / 1; the error is upstream's SQLite test on a Windows path). The live suite is
  not collected without `AUTO_ONTOLOGY_LIVE_BIGQUERY_PROJECT`, so the skip count did not
  change.
- Live suite on the emulator (`ghcr.io/goccy/bigquery-emulator` v0.8.1, project
  `auto-ontology-test`): 34 passed, 8 skipped. The 8 are the real-only tests; run on the
  emulator with skips disabled, each fails for the emulator gap listed below and for
  nothing else.
- Ruff clean on `auto_ontology/ alembic/ dev_tools/`, `uv lock --check` clean, OpenAPI
  unchanged (the generator's output differed only in line endings).

Decision 11: the tables API, for three reasons. It bills no query bytes, while every
`INFORMATION_SCHEMA` query bills a 10 MB minimum. It is not subject to
`maximum_bytes_billed`, so a connection with a small limit can still be ingested
(a 1-byte limit is tested). And it returns the nested schema and the declared keys
(`tableConstraints`) directly. The cost is one `tables.get` per relation per pass,
run 8 at a time; views and other relation types are not fetched for keys.

How the rest was settled:

- Nested columns: one row per top-level column. A `STRUCT` keeps its fields in its type
  (`STRUCT<city STRING, country STRING>`), a repeated field is `ARRAY<...>`, and legacy
  names are rendered as GoogleSQL (`INTEGER` → `INT64`, `RECORD` → `STRUCT`), with
  `STRING(n)` and `NUMERIC(p, s)` kept. A `REPEATED` column is reported not nullable:
  BigQuery stores a NULL array as an empty one.
- Naming (decision 7): `my-project.ds.t`, `` `my-project`.ds.t ``,
  `` `my-project.ds.t` ``, `` `my-project`.`ds`.`t` ``, ``my-project.ds.`t` ``, `ds.t` and
  `t` (default dataset) all ran on the emulator, and sqlglot splits each into the same
  catalog, db and table, including after the mixed-case re-quoting. So nothing is
  rewritten, unlike Spanner.
- Read-only: a statement must start with `SELECT` or `WITH` (after comments and
  parentheses), and a `;` followed by more code is refused as a script. The emulator runs
  `SELECT 1; SELECT 2` as a script, so the client check is needed.
- Emulator targeting: the library takes `client_options.api_endpoint` over
  `BIGQUERY_EMULATOR_HOST`, but uses the variable for any client given no endpoint. The
  connector refuses the variable unless the URL names the same emulator; `http://host:port`
  and `host:port` count as the same.
- Timeouts and retries: the library retries requests for 10 minutes and re-runs jobs for
  40, with no per-request HTTP timeout. The connector passes, through the public API,
  `api_timeout` 60 s per request, `DEFAULT_RETRY.with_timeout(30)` and
  `DEFAULT_JOB_RETRY.with_timeout(60)`. With `timeout_s` it also sets `jobTimeoutMs`
  (BigQuery cancels the job) and `wait_timeout` (the client cancels it), and caps all
  budgets at `timeout_s`. A closed port fails in 2.1 s in the test (budget lowered to 2 s),
  about 30 s by default. A 1 s cap on the emulator raised `StatementTimeout` in 1.1 s.
- Errors: `<reason>: <message>` from BigQuery's structured error, without the method,
  URL or job footer the client adds. Missing ADC, expired ADC and transport failures start
  "Could not connect to BigQuery", so they count as infrastructure. `accessDenied` names
  the roles needed, `bytesBilledLimitExceeded` names the setting, and a `notFound` from a
  metadata call names the dataset in the connection string. A `notFound` from a query
  stays the statement's own error.
- Foreign keys to a table this connection does not ingest (another project, or another
  dataset when the URL names one) are left out with a warning.
- Seed script: DDL and one `INSERT` per table, using no streaming inserts. It refuses to
  replace or delete a dataset that lacks the label `created_by=auto_ontology_seed`,
  which is tested.

Added beyond the plan:

- Job re-runs are off when the URL names an emulator. The emulator gives every query
  error, a syntax error included, the reason `jobInternalError`, which the library treats
  as transient. With the library defaults a bad query on the emulator had not failed after
  3 minutes (the default job re-run budget is 40 minutes). Real BigQuery reports `invalidQuery` or `notFound` and is not affected.

### Emulator gaps found

| Gap | Effect here |
|---|---|
| `FOREIGN KEY` rejected (`CREATE TABLE` and `ALTER TABLE`) | Seed declares no foreign keys on the emulator and says so |
| `PRIMARY KEY ... NOT ENFORCED` accepted but not reported in `tableConstraints` | `get_pks` is empty on the emulator |
| `NOT NULL` columns reported as `NULLABLE` | Nullability is only checked for nullable and repeated columns |
| View definition returned as its own rewritten SQL | Only a column name is checked |
| `maximumBytesBilled` and `jobTimeoutMs` ignored | Timeout is enforced by the client's read timeout instead |
| Every query error has reason `jobInternalError` | Job re-runs off for emulator URLs |
| `` SELECT Singers.x FROM `p.ds.Singers` `` fails: no implicit alias for a quoted full path | Real-only test |
| No `INFORMATION_SCHEMA.TABLE_CONSTRAINTS` or `KEY_COLUMN_USAGE` | Not used |
| `jobs.query` answers only when the query finishes | Not relied on |
| No IAM, locations, billing or quotas | Real-only tests |

### Correct for real BigQuery but unproven here

None of this ran against real BigQuery: there are no credentials on this machine.

- Application Default Credentials end to end, and the `accessDenied` and expired-ADC
  messages. Only the missing-ADC message is proven (real library, absent key file).
- `tableConstraints` as BigQuery returns them: PK order, a multi-column FK, and keys
  declared with `ALTER TABLE ... ADD CONSTRAINT`.
- `REQUIRED` modes for `NOT NULL` columns.
- `maximum_bytes_billed` refusing a query with `bytesBilledLimitExceeded`, and the wording
  of that error.
- `jobTimeoutMs` cancelling a job, and what that error looks like: mapped from reason
  `timeout` or `stopped`, or from the client's own wait running out. Also whether BigQuery
  accepts a `jobTimeoutMs` as small as the caller's cap (the live test uses 1 s).
- `location` routing jobs: a job in another location should not find the dataset.
- The implicit alias of `` `project.dataset.table` ``.
- Retry budgets against real transient errors (`rateLimitExceeded`, 5xx).
- The cost of a full ingestion on a large project (one `tables.get` per relation per
  pass).

The owner's run exercises these real-only tests: `test_not_null_columns_are_not_nullable`,
`test_view_definition_is_the_text_as_written`, `test_get_pks`,
`test_get_fks_declared_and_multi_column`, `test_implicit_alias_of_a_quoted_full_path`,
`test_maximum_bytes_billed_is_enforced`, `test_location_is_applied_to_jobs` and
`test_permission_denied_reads_clearly`. It also runs the other 34 against real BigQuery,
including key counts in `test_ingestion_extract_accepts_the_catalog`, the timeout, and both
seed-script tests.

To run them:

    gcloud auth application-default login
    uv run python -m dev_tools.fixtures.seed_bigquery --project <project>
    AUTO_ONTOLOGY_LIVE_BIGQUERY_PROJECT=<project> uv run pytest \
        auto_ontology/connectors/tests/test_bigquery_live.py -q -rs
    uv run python -m dev_tools.fixtures.seed_bigquery --project <project> --delete

The live suite seeds and deletes its own datasets (`auto_ontology_live_test`,
`auto_ontology_live_reseed`, `auto_ontology_live_unlabelled`). The seed command is only
needed for trying the connector by hand. Set `AUTO_ONTOLOGY_LIVE_BIGQUERY_LOCATION` (and
`--location`) for a dataset outside `US`.

### Found in shared code, not changed

- `semantic/visit_enter.py:75`: the substring `string` makes `ARRAY<STRING>` and any
  `STRUCT<... STRING ...>` a text type, so profiling can run `SELECT DISTINCT` on them,
  which BigQuery rejects. The failure is caught. `time` and `date` in a `STRUCT` type
  likewise skip its sample values.
- `formatters_util.py:65`: a `JSON` column gets Postgres JSONB hints (`->>`), which are
  wrong for BigQuery (`JSON_VALUE`).
- `registry.get_connectors`: connectors are routed by `database_name`, which is the
  project here. Two connections to different datasets of the same project collide; use one
  project-wide connection.
- A domain-scoped project (`example.com:p`) appears unquoted in prompts, which BigQuery
  cannot parse. It is rare and not handled.
