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

### Steps 2 and 3: connector and emulator tests (2026-11-19)

Commits `11e4c58c`, `5d040b29`, `5a5ac18d`. Upstream files touched: `registry.py` +2,
`connectors/__init__.py` +2, `pyproject.toml` +1, `uv.lock` (18 new packages).

- `uv run pytest auto_ontology/connectors -q`: 436 passed, 11 skipped, 1 error (before:
  329 / 11 / 1; the error is upstream's SQLite test on a Windows path). The emulator
  tests are not collected without `AUTO_ONTOLOGY_LIVE_SPANNER=1`, so the skip count did
  not change. With the flag: 134 passed.
- Ruff clean, OpenAPI unchanged, `uv lock --check` clean.
- Emulator targeting: `AnonymousCredentials` plus `client_options.api_endpoint`, per
  client. `SPANNER_EMULATOR_HOST` is not needed. Because the library lets that variable
  override the client, the connector refuses it when it is set and the URL does not name
  the same emulator.
- The emulator supports named schemas, views, JSON, ARRAY, interleaving and
  PostgreSQL-dialect databases (created one; the connector refuses it).
- sqlglot's re-quoting was run on 12 GoogleSQL statements; every rewrite
  (`INTERVAL '90' DAY`, `CAST('…' AS DATE)`, `NOT x IS NULL`, bare `CURRENT_DATE`) was
  accepted by the emulator.

Added beyond the plan:

- A guard against a lost server. The Google client retries `UNAVAILABLE` forever. Each
  statement runs on a worker thread; if the gRPC channel is not ready for 30 s the
  transport is closed and "Could not connect to Spanner at …" is raised. It depends on
  library internals (`transport.grpc_channel`), and would also end a real outage longer
  than 30 s.
- Built-in client metrics are switched off, so no process-wide OpenTelemetry provider is
  installed.
- A multi-column foreign key that points at a unique index, not a primary key, is left
  out with a warning: the emulator reports its column positions in the wrong order.
- The database qualifier is not stripped when the statement also uses the database id
  as an alias, CTE or column name.

### Step 4: through the stack on the emulator (2026-11-19)

Separate compose project `aospan` (own Postgres volume), Ollama preset
(`qwen3.5:9b`, `bge-m3`), connection
`spanner://auto-ontology/auto-ontology/music?emulator_host=host.docker.internal:9010`.

| Step | Result |
|---|---|
| Ingest | 9.3 s. 7 of 7 tables (one view, one in a named schema), 35 of 35 columns, 9 of 9 key columns, 7 of 7 foreign-key rows including the interleaved one. No errors or warnings. |
| Compile | Ready in 125 s. 7 terms, 28 column attributes, 7 joins, 0 SQL attributes. Sample values on 29 of 35 columns (the other 6 are date and timestamp columns, skipped by design). |
| Question 1, average album price per singer (interleaved join) | Correct, 72 s, one pass. |
| Question 2, tickets sold in Seattle | Correct, 37 s, one pass. |
| Question 3, tickets sold in Portland | Wrong (6, expected 3): the model joined `Concerts` on `VenueId` alone. The SQL ran. |
| Emulator stopped | Clear error after 30 s, classed as infrastructure, no SQL repair. The shared pipeline restarts once, so the request took 152 s. Catalog untouched by a failed re-ingest. |
| Emulator back | Next question and re-ingest worked without restarting any service. |

- No Postgres-only syntax in any generated statement, and Spanner rejected none for how
  names were written. The model wrote the qualifier three ways and all were stripped.
  No dialect rule was added.
- Single asks on a 9B local model; not a measure of answer quality.

### Steps 5 and 6: add in the UI, docs (2026-11-19)

Commits `39bc98d6` (error text), `8aa4f701` (UI), and the docs commit. Upstream files
touched: `connection_string_factory.py` +13, `frontend/enums/connection.ts` +23,
`frontend/types/connection.ts` +12, `docs/connections.md`, `README.md`,
`.env.example` +1.

- Form fields: Project ID, Instance ID, Database ID (the connection's `database`, so it
  equals the connector's `database_name`), optional Emulator Host. No secret fields.
  The factory calls `spanner.format_connection_string`, which refuses a blank or
  invalid id by name; every accepted character is URL-safe, so nothing is
  percent-encoded and the URL parses back to the same parts (tested).
- No schema picker. The connection test calls `ping()`, since the connector has no
  `get_schemas`.
- Error text: `str()` of a Google API error carried the raw details protos and the
  client's request id. Now `<gRPC status>: <Spanner's message>`; the original stays
  chained. Syntax error on the emulator, before:
  `400 Syntax error: Unexpected identifier "SELEC" [at 1:1]\nSELEC 1\n^, request_id = 1.84…1 [type_url: "type.googleapis.com/googlesql.ErrorMessageModeForPayload"\nvalue: "\010\002"\n]`;
  after: `INVALID_ARGUMENT: Syntax error: Unexpected identifier "SELEC" [at 1:1]\nSELEC 1\n^`.
- `pytest auto_ontology/connectors`: 466 passed, 11 skipped, 1 error (the same
  upstream SQLite test). `auto_ontology/server/connections`: 21 passed. With
  `AUTO_ONTOLOGY_LIVE_SPANNER=1`: 164 passed. Ruff clean, OpenAPI unchanged.
- Frontend: `pnpm install --frozen-lockfile`, `prettier --check`, `pnpm lint` (one
  existing warning in `common/UserMenu.tsx`), `pnpm build`: all pass.
- Through the stack (`aospan`, fresh Postgres volume, `CONNECTION_STRINGS` unset, both
  images rebuilt; the frontend bundle contains the Spanner option), signed in through
  the frontend gateway and called the routes the form calls:
  `POST /api/connections/test` → 200, `schemas: []`; `POST /api/connections` → 201;
  ingest ran by itself: 7 tables, 35 columns, 7 foreign-key rows in 7.3 s. Wrong
  database id → 422 `… NOT_FOUND: Database not found: projects/…/databases/nosuchdb`.
  Instance id `a/b` → 422 `Invalid Spanner instance id 'a/b': …`.

Not tested: the form in a browser (only the API it calls), editing a Spanner
connection, Vault storage, real Cloud Spanner.

### Found in shared code, not changed

- `semantic/semantic_fk.py:194-217`: a declared foreign key whose target column is
  itself a foreign-key column goes to model inference and loses its declared target.
  The three-column `TicketSales → Concerts` key was reduced to one column. Composite and
  interleaved keys are common in Spanner, so this matters more here.
- `retrieval/text_to_sql/main.py:290-301`: the whole pipeline is retried even after an
  infrastructure failure, which doubles the time to the error.
- `semantic/visit_enter.py:74`: `ARRAY<STRING(n)>` counts as a text type, so profiling
  can run `SELECT DISTINCT` on an array column, which Spanner rejects. The failure is
  caught. It did not happen in this run.
- `formatters_util.py:65`: a column type containing `json` gets Postgres JSONB hints.
  The model did not use them in this run.

### Not tested

- Real Cloud Spanner: credentials, IAM, latency, session pool, query limits.
- More than three questions; any model other than `qwen3.5:9b`.
- Large schemas, and schemas with many named schemas.
