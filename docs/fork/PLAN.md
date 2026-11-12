<!-- SPDX-License-Identifier: Apache-2.0 -->

# Fork plan: any model provider through a LiteLLM gateway

Fork of [NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology), based on upstream
commit `fc43a826`. Working branch: `litellm-gateway`.

## Goal

Run the stack with chat, embedding and rerank models from any provider (Ollama, Gemini,
OpenAI, Anthropic, Bedrock, and others) by changing configuration only. The two targets
that must be proven end to end:

1. **Local:** Ollama chat + Ollama embeddings (`bge-m3`, 1024 dimensions).
2. **Hosted:** Gemini chat + Gemini embeddings.

A mixed setup (Ollama chat + Gemini embeddings) is the third check.

## Why a fork

NVIDIA does not accept external pull requests. The changes touch core files
(`llm_invoke.py`, `embedding.py`, `vdb/postgres.py`, `rerank.py`), so they cannot ship as
an add-on beside a stock clone. Keep the diff small so it rebases cleanly and so NVIDIA
can lift individual commits if they choose to.

## Design decision: LiteLLM as a gateway, not a library

Add LiteLLM as an **optional container in front of the stack**. Do not import the
`litellm` Python package into `auto_ontology`.

- The stack already speaks the OpenAI protocol for chat (`ChatOpenAI`) and embeddings
  (`openai` SDK). A gateway needs no new client code.
- The backend diff stays at a few dozen lines. A library integration would rewrite every
  call site and conflict with upstream on each rebase.
- The gateway gives production features for free: one place for provider keys, fallbacks,
  rate limits, spend tracking.
- Users who do not want it are unaffected. Default behaviour with no new settings must be
  identical to upstream.

## What blocks other providers today

Verified by reading the code at `fc43a826`. Line numbers will drift; search for the names.

| # | Problem | Where |
|---|---|---|
| 1 | Vector tables are created at a fixed 2048 dimensions. Nothing passes another size. | `auto_ontology/vdb/postgres.py` (`kwargs.get("vector_size", 2048)`), `auto_ontology/vdb/__init__.py` (`get_vdb`) |
| 2 | Chat client is chosen by model-name prefix. Only `openai/`, `azure/`, `aws/` get the OpenAI-style client; every other name goes to `ChatNVIDIA`. A gateway model called `gemini/...` or `ollama/...` takes the NVIDIA path. | `auto_ontology/utils/llm_invoke.py` (`_build_client`) |
| 3 | No way to send `reasoning_effort`. Thinking models on Ollama took 20–28 s per call; about 1 s with it set to `none`. | same function |
| 4 | Every embedding request carries NVIDIA-only body fields: `input_type`, `truncate`, `modality`. Strict providers may reject them. | `auto_ontology/utils/embedding.py` (`embed_texts`, `extra_body`) |
| 5 | Embedding requests run up to 32 at once with batches of 32. That exceeds free-tier and small-quota limits on hosted providers. | `EmbedParams` defaults in the same file |
| 6 | Rerank cannot be switched off. Unset values fall back to an NVIDIA-hosted URL, so requests leave the machine. | `auto_ontology/utils/rerank.py`, `auto_ontology/utils/model_config.py` |
| 7 | With no key set, built-in defaults point every model at NVIDIA endpoints. | `model_config.py` (`_DEFAULTS_BY_KEY_PREFIX`) |

Rerank already accepts the Cohere-style `/rerank` shape (`results` / `relevance_score`),
which is what LiteLLM serves. It is used in `semantic/bridge_tables.py` and referenced in
`dal/terms.py`; a failure falls back to the first candidate.

## Changes

Each item is one commit with its own tests. All new settings are optional and default to
upstream behaviour.

### A. Backend (`auto_ontology/`)

**A1. Configurable embedding dimension.**
- New setting `EMBED_DIMENSIONS` (integer). Unset means 2048, as today.
- `get_vdb` passes it as `vector_size`.
- If the table already exists with a different dimension, fail at startup or first write
  with a message that names both sizes and says how to rebuild. Do not let the raw
  pgvector error surface.
- Optionally send `dimensions` in the embedding request when the setting is present, so
  models with adjustable output size (Gemini, OpenAI v3) return that size. Decide after
  checking how each target provider handles the field.
- Read the long comment above the `halfvec` conversion first. pgvector cannot build an
  HNSW index on `vector` above 2000 dimensions or on `halfvec` above 4000. Reject values
  above 4000 with a clear error.

**A2. Explicit chat client choice.**
- New optional field on the existing triplet convention: `REASONING_CLIENT`,
  `NON_REASONING_CLIENT`, falling back to `DEFAULT_MODELS_CLIENT`.
- Values: `auto` (default, today's prefix rule), `openai`, `nvidia`.
- `openai` selects `ChatOpenAI` for any model name.
- Resolve it through `model_config.resolve` or a sibling helper. Do not add a second
  config mechanism.

**A3. Optional `reasoning_effort`.**
- `LLM_REASONING_EFFORT`, sent only when set, only on the OpenAI-style client.
- A working three-line version is in the user's other clone at
  `E:\clone\ontology-layer\docs\auto-ontology-on-ollama\01-llm-reasoning-effort.patch`.

**A4. Embedding request compatibility.**
- First measure: send a request with the NVIDIA-only fields to (a) Ollama directly,
  (b) Gemini's OpenAI-compatible endpoint, (c) the LiteLLM gateway with `drop_params`.
  Record which accept, ignore or reject them.
- Then add the smallest switch that makes all targets work, for example
  `EMBED_EXTRA_PARAMS=nvidia|none` with `nvidia` as default.
- `input_type` matters for retrieval quality on models that distinguish query from
  passage. If a target provider needs its own form (Gemini has a task type), note it in
  the provider doc; do not build per-provider logic into the backend.

**A5. Embedding throughput settings.**
- `EMBED_BATCH_SIZE` and `EMBED_MAX_CONCURRENT`, defaulting to the current 32 and 32.

**A6. Rerank opt-out.**
- `RERANK_ENDPOINT=disabled` (or an equivalent explicit flag) makes callers take their
  existing fallback path without any network call.
- Check both call sites and confirm the fallback is safe before relying on it.

### B. Gateway (`deploy/litellm/`, new files only)

- `docker-compose.litellm.yml` as a separate Compose file, used with
  `docker compose -f docker-compose.yml -f docker-compose.litellm.yml up`. Do not edit
  upstream's `docker-compose.yml`.
- One `litellm` service. Pin the image to an exact version tag, never `latest`. Bind its
  port to `127.0.0.1`. Require a gateway master key from `.env`.
- Config presets:
  - `deploy/litellm/ollama.yaml` — chat and embeddings on the host's Ollama.
  - `deploy/litellm/gemini.yaml` — chat and embeddings on Gemini.
  - `deploy/litellm/mixed.yaml` — Ollama chat, Gemini embeddings.
- Matching env presets: `deploy/litellm/env.ollama.example`, `env.gemini.example`,
  `env.mixed.example`. Each sets all four triplets explicitly (reasoning, non-reasoning,
  embed, rerank) plus `EMBED_DIMENSIONS`, so nothing falls back to an NVIDIA endpoint.
- Provider keys are read by the gateway from environment variables. No key is written
  into any committed file.

### C. Docs

- `docs/model-providers.md`: how model selection works, the new settings, the three
  presets, how to add another provider, what was tested and what was not.
- A short section in `README.md` linking to it.
- Add the new settings to `.env.example`, commented out.

## Order of work

1. **Baseline.** `uv sync`, then `uv run pytest auto_ontology/utils auto_ontology/vdb -q`
   and `uv run ruff check auto_ontology/`. Record what passes before any change. If the
   suite needs services that are not running, note which tests and move on.
2. **A3, A1, A2** in that order (smallest first). Tests beside the existing ones in
   `auto_ontology/utils/tests/` and `auto_ontology/vdb/tests/`.
3. **B with the Ollama preset.** This is the fastest live check because the models are
   already on this machine and a direct Ollama run is known to work.
4. **A4, A5, A6**, driven by what the Ollama and Gemini runs show.
5. **Gemini preset**, then **mixed preset**. These need a Gemini API key from the user.
6. **C**, then a final pass of the full local checks in `CLAUDE.md`.

## Baseline (step 1, recorded 2026-11-11)

At `fc43a826` on `litellm-gateway`, before any change. Windows, uv 0.12.7, ruff 0.16.5,
no `.env` in this clone and no Postgres running.

- `uv sync`: clean.
- `uv run pytest auto_ontology/utils auto_ontology/vdb -q`: 173 passed, 3 skipped,
  1 error.
  - The 3 skips are `vdb/tests/test_vector_index.py` (lines 102, 113, 122): "postgres
    not reachable".
  - The 1 error is `vdb/tests/test_records.py::test_written_rows_come_back_from_a_search`:
    its `vdb` fixture raises `KeyError: 'POSTGRES_USER'` instead of skipping.
  - All four need a pgvector Postgres and `POSTGRES_*` set. They are the tests that
    exercise the vector table size, so A1 is not proven until they run against a live
    database.
- `uv run ruff check auto_ontology/`: all checks passed.
- `uv run ruff format --check auto_ontology/`: 434 files already formatted.

## Progress (step 2, recorded 2026-11-11)

A3, A2 and A1 are implemented with tests. Nothing is committed yet.

- A2 + A3: `auto_ontology/utils/llm_invoke.py` (+32/−4), 31 tests in
  `utils/tests/test_llm_client_choice.py`.
- A1: new `auto_ontology/vdb/dimensions.py`, hooks in `vdb/__init__.py` and
  `vdb/postgres.py` (+16/−1), 33 tests in `vdb/tests/test_dimensions.py` (27 pure,
  6 live). Live run against a throwaway `pgvector/pgvector:pg17`: 33 passed, whole
  `auto_ontology/vdb` 44 passed.
- `uv run pytest auto_ontology/utils auto_ontology/vdb -q` with no database:
  231 passed, 9 skipped, 1 error (the baseline `test_records.py` error).
- Ruff check and format check: clean.
- `uv run pytest auto_ontology dev_tools -q`: 1849 passed, 807 skipped, 21 failed,
  2 errors. None of the failures is in a changed file; every one is environmental on
  this Windows machine or upstream's own:
  - 18 in `retrieval/kumo/tests/test_wiring.py`: `kumo_relational_client` not installed.
  - `dal/tests/test_dal_surface.py`: compares reprs that contain object addresses.
  - `infra/tests/test_frontend_schema_qualified.py`: reads a file as cp1252.
  - `connectors/tests/test_nullability.py`: malformed Windows SQLite path.
  - `dev_tools/tests/test_licensing.py`: upstream's `CODE_OF_CONDUCT.md` has no SPDX line.
  - These were not run at the original baseline (which covered `utils` and `vdb` only),
    so "pre-existing" is inferred from the failure reasons, not from a before/after run.
- The licensing test checks every **tracked** file outside a short exempt list for
  `SPDX-License-Identifier` in its first 2000 bytes. New fork files (Python, YAML,
  Markdown, env examples) need that line before they are committed. The three new
  Python files have it.

## Progress (step 4, recorded 2026-11-11)

A5 and A6 are implemented with tests. A4 needs no backend change for Ollama and waits
on the Gemini measurement.

- A5: `EMBED_BATCH_SIZE` and `EMBED_MAX_CONCURRENT` in `auto_ontology/utils/embedding.py`
  (+24/-2), read each time `EmbedParams` is built, defaults 32 and 32. No production
  caller passes either value, so the settings reach every call site. 19 tests in
  `utils/tests/test_embedding_throughput.py`. The limit is per `embed_texts` call, not
  per process: parallel ingests each get their own pool.
- A6: `RERANK_ENDPOINT=disabled` makes `rerank_hits` raise `RerankDisabled` before any
  request; `_pick_owner_term_id` takes the first candidate Term, as on an endpoint
  error, and logs one INFO line. 14 tests in `utils/tests/test_rerank_disabled.py` and
  `semantic/tests/test_bridge_rerank_disabled.py`. The Ollama preset now uses it.
- `uv run pytest auto_ontology/utils auto_ontology/semantic auto_ontology/vdb -q` with
  no database: 387 passed, 9 skipped, 1 error (the baseline `test_records.py` error).
  Ruff check and format check clean. Not exercised live: no run has rebuilt the image
  with A5/A6, and the test dataset has no bridge tables.

- Live check after rebuilding the backend image from `65bbc2f5`: all five containers
  healthy; inside the backend, rerank resolves to `disabled` and raises `RerankDisabled`
  with no request; with `EMBED_BATCH_SIZE=5` and `EMBED_MAX_CONCURRENT=2`, 23 texts made
  exactly 5 gateway embedding requests and returned 23 vectors of 1024 dimensions.
- Step 6 (docs) done for what exists: `docs/model-providers.md`, a README section and
  commented settings in `.env.example`. Gemini and mixed presets (step 5) are still
  blocked on a Gemini API key; the docs list them as planned and untested.

## Step 5 (drafts only, recorded 2026-11-12)

No Gemini API key, so nothing below was sent to Gemini. Draft files:
`deploy/litellm/gemini.yaml`, `mixed.yaml`, `env.gemini.example`, `env.mixed.example`,
each marked UNTESTED. Models: `gemini-3.5-flash-lite` (chat), `gemini-embedding-001` at
1536 dimensions (embeddings). Offline checks with LiteLLM 1.108.5:

- Both configs load. With `GEMINI_API_KEY` empty, chat and embeddings answer HTTP 500
  "Missing Gemini API key" in 5 to 6 s, raised before any request is built (checked
  with the gateway on `--network none`). The backend treats 500 as transient and
  retries, so a missing key surfaces slowly.
- **A4 on Gemini, read from the body LiteLLM sent to a local stand-in for Google:** the
  native `gemini/` embedding route turns the backend's `input_type` into `taskType`
  with the raw value (`passage`, `query`), which is not a Gemini task type.
  `drop_params` and `additional_drop_params` do not remove it. `truncate`, `modality`
  and `encoding_format` are removed. A `task_type` set in `litellm_params` replaces the
  mapped value, so the presets fix it to `SEMANTIC_SIMILARITY`. `gemini-embedding-2`
  takes no task type, so it needs the A4 backend switch first.
- Chat body for `gemini-3.5-flash-lite`: no temperature; `max_output_tokens: 8192`;
  `response_json_schema` with `$defs`, `$ref` and `additionalProperties: false` kept;
  `reasoning_effort: none` becomes `thinkingLevel: minimal`; every system message,
  including one after a user turn, moves into `system_instruction`. The repo's own
  `invoke_with_structured_output` parsed the stand-in's reply.
- The Ollama chat alias in `mixed.yaml` answered one request (HTTP 200, 2 tokens).

**Update 2026-11-12: A4 implemented.** `EMBED_EXTRA_PARAMS=nvidia|none` (default `nvidia`;
+18/-1 in `auto_ontology/utils/embedding.py`). `none` sends a plain OpenAI body: `model`,
`input`, `encoding_format`. Tests: `auto_ontology/utils/tests/test_embedding_extra_params.py`,
29 tests, passing. Against a local stand-in for Google through LiteLLM 1.108.5, with `none`
no `taskType` is sent unless the gateway config sets one. Not exercised against real
Gemini; the presets still use `gemini-embedding-001` with a fixed `task_type`.

**Update 2026-11-13: chat model changed to `gemini-3.8-flash`** at the user's request
(Gemini 3.x flash only). Still untested: three keys were tried directly against
Google and none could run a model (HTTP 401 invalid key; HTTP 402 credits depleted;
HTTP 403 Gemini API not enabled in the project). The second key's model list does
include `gemini-3.8-flash`, `gemini-embedding-001` and `gemini-embedding-2`. The
request-body findings above were read for `gemini-3.5-flash-lite`; its default
thinking level and rate limits may not apply to `gemini-3.8-flash`.

## Gateway findings (step 3, recorded 2026-11-11)

Gateway alone, LiteLLM 1.108.5, Ollama 0.40.6, RTX 3070 Laptop 8 GB. Full stack not
yet run.

- Files: `docker-compose.litellm.yml`, `deploy/litellm/ollama.yaml`,
  `deploy/litellm/env.ollama.example`.
- Chat route is `ollama_chat/`. Ollama loads `qwen3.5:9b` at a 4096-token context and
  truncates longer prompts silently (a 10k-token prompt returned HTTP 200 and a wrong
  answer). Only `ollama_chat/` forwards `num_ctx`; Ollama's `/v1` ignores it. The preset
  sets 16384.
- `reasoning_effort: none` is honoured on both routes: about 0.2 s and 2 tokens for a
  one-word answer, against 3–4 s and 120–150 tokens without it.
- Embedding route is `openai/bge-m3` on Ollama's `/v1`. **A4 measurement:** the
  backend's NVIDIA-only fields are accepted and ignored there (vectors bit-identical)
  and by Ollama's `/v1` directly. LiteLLM's native `ollama/` embedding route rejects
  them (HTTP 400 on `truncate: "END"`, not removed by `drop_params`). So Ollama needs
  no backend change for A4; Gemini still to measure.
- Embedding throughput at the backend's defaults (32 per batch, 32 concurrent):
  1024 rows in 15.7 s through the gateway, 18.6 s direct. No errors.
- `temperature: 0` in the gateway config made structured-output content worse on both
  probes, so the preset leaves the model's sampling defaults.
- The gateway takes about 22 s to become healthy; chat retries cover about 6 s. A
  `depends_on` on the backend services is not added, because it would break
  gateway-only use of the Compose file. Start order matters on a cold start.

## Verification

A change is not done until its check has been run and the result recorded.

**Unit level (every backend commit)**
- New tests cover: default unchanged, setting honoured, bad value rejected.
- `uv run ruff check auto_ontology/ && uv run ruff format --check auto_ontology/`
- `uv run pytest auto_ontology dev_tools -q`
- If a route docstring or model changed: `uv run python -m dev_tools.generate_backend_openapi`.

**Live matrix**

| Run | Chat | Embeddings | Dimension |
|---|---|---|---|
| 1 | Ollama `qwen3.5:9b` via gateway | Ollama `bge-m3` via gateway | 1024 |
| 2 | Gemini via gateway | Gemini via gateway | provider default or a set value |
| 3 | Ollama via gateway | Gemini via gateway | as run 2 |
| 4 | Upstream defaults, no new settings | — | regression check; needs an NVIDIA key, skip and say so if none |

For each run: ingest the BIRD `financial` SQLite database (file named `main.sqlite`; see
known issues), confirm the vector tables have the expected dimension, then ask
"Which district has the highest average salary, and what is it?". The correct answer is
Hl.m. Praha, 12,541. Record counts of terms, column definitions and joins, time taken,
and the answer. Write results into `docs/model-providers.md` with the date and model
versions. Report failures as failures.

Reference result from the earlier direct-Ollama run: about 6 minutes, 8 terms, 47 column
definitions, 8 joins.

### Run 1 result (2026-11-11): passes except the as-configured answer

Whole stack in Docker (`-p aofork`, fresh Postgres volume), chat `qwen3.5:9b` and
embeddings `bge-m3` through the gateway, built from the tree that became `94a9af67`.
Extra settings carried over from the earlier clone: `LLM_INVOKE_TIMEOUT_S=600`,
`LLM_MAX_INFLIGHT=2`, `SEMANTIC_LLM_MAX_INFLIGHT=2`.

- **Vector tables:** `vdb.data_objects_layer` (63 rows) and `vdb.semantic_layer`
  (55 rows) are `halfvec(1024)` with an HNSW index, created by the backend itself from
  `EMBED_DIMENSIONS=1024`. No pre-creation script.
- **Ingest:** 6.0 s startup ingest + 168.0 s semantic compilation. 8 terms, 47 column
  definitions, 8 joins: identical to the reference.
- **Gateway only:** 75 chat and 121 embedding calls in the gateway log, all HTTP 200,
  matching Ollama's own log one for one. No log line mentions an NVIDIA host. 0 errors,
  0 tracebacks, 0 timeouts, 0 structured-output validation failures.
- **Answer as configured: wrong 3 times out of 3** (593 s, 647 s, 182 s). Two used
  `A15` (crimes in 1995), one averaged transaction amounts. Compiling from the schema
  alone labelled `A11` "Total Revenue Or Sales Count", so no term meant salary. The
  earlier direct-Ollama kit recorded the same limitation; it is not a gateway fault.
- **Variation, labelled:** with the BIRD column dictionary loaded
  (`load_bird_descriptions.py`, 54 columns) and a recompile (86.9 s, same counts), one
  ask returned **Hl.m. Praha, 12,541** in 46.6 s. Configuration unchanged.
- **Reliability with the dictionary (2026-11-11):** six more asks, 6 of 6 correct, 43 to
  146 s (median about 59 s), no timeouts, 40 chat calls all HTTP 200, two asks with one
  SQL repair round each. Judged on the returned rows; the answer text never states the
  district or the number.
- **Rerank was never called** (this dataset has no bridge tables), so run 1 says
  nothing about the rerank fallback or A6.
- **Follow-ups applied to the preset:** `env.ollama.example` now sets the timeout and
  the two concurrency limits; the `num_ctx` comment in `ollama.yaml` no longer claims
  the models fit in 8 GB (14% of the chat model ran on CPU, about 13 tokens/s).
- **Consequence for the matrix:** the question only tests the model path fairly when
  the column dictionary is loaded. Runs 2 and 3 should record both with and without it.

### Runs 2 and 3 result (2026-11-13): pass, sanity level only

The user asked for Gemini to be used for light checks only (paid key), chat on
`gemini-3.8-flash`, embeddings at 768 dimensions. Each run: fresh database in a separate
Compose project (`-p aogem`), image built from `c80949b7`, one ingest, one compile.
Presets changed to 768 before the runs.

| | Run 2 (Gemini + Gemini) | Run 3 (Ollama chat + Gemini embeddings) |
|---|---|---|
| Vector tables | `halfvec(768)`, HNSW, 63 / 55 rows | same |
| Ingest | 21.2 s | 19.2 s |
| Compile | 147.8 s; 8 terms, 47 column definitions, 8 joins | 165.7 s; 8 / 47 / 8 |
| Question, no descriptions | correct, 36.8 s (1 ask) | wrong, empty result, 304.5 s (1 ask) |
| Question, BIRD descriptions | not run | correct, 90.3 s (1 ask) after a 99.9 s recompile (9 / 47 / 8) |
| Gateway calls | 31 chat, 23 embedding, all 200 | 59 chat, 122 embedding, all 200 |

- No rate-limit response, no NVIDIA host in any log. Run 3 logged one SQLAlchemy/asyncpg
  error while closing a pooled connection; the compile succeeded; not investigated.
- Not done, by the user's instruction: repeat asks, other models or sizes, rate-limit
  measurement. `EMBED_EXTRA_PARAMS=none` and `gemini-embedding-2` were not run in the
  stack (direct call only: both embedding models return the size asked for).
- The Ollama stack (`-p aofork`) was stopped for the runs and restarted after; its
  `.env` is kept as `.env.ollama.local`, the Gemini one as `.env.gemini.local` (both
  git-excluded).

## Known upstream issues that affect testing

Drafted bug reports, with code references, are in
`E:\clone\auto-ontology-ollama\upstream-issues\`. None is filed yet.

- SQLite files not named `main.sqlite` produce three-part table names that SQLite
  rejects. Name the test file `main.sqlite`. Fixing this is out of scope here.
- On Windows, clone with `core.autocrlf=false`. This clone already was. CRLF breaks the
  entrypoint heredoc in `Dockerfile` and `frontend/prisma/migrate.sh`.

## Out of scope

- New database connectors (BigQuery, Spanner, SQL Server). Planned next, in a separate
  branch.
- Frontend changes, unless a model setting is surfaced in the UI and breaks.
- Helm chart changes. Note in the docs that Helm values are not updated.
- Per-provider logic inside `auto_ontology`. Provider differences belong in gateway
  config.

## Open questions

Settle each by measurement, then record the answer here.

1. Does `ChatOpenAI` structured output work through the gateway for Gemini and for
   Ollama models? The stack relies on it.
   **Ollama part answered 2026-11-11: yes.** Through the gateway (`ao-chat` =
   `ollama_chat/qwen3.5:9b`, `num_ctx` 16384, `LLM_REASONING_EFFORT=none`), using the
   repo's own `_build_client(client_kind="openai")` and
   `invoke_with_structured_output`: 15/15 responses schema-valid, 2–15 s each. Field
   *content* was right in only 2–3 of 5 runs per probe; the direct-Ollama path scored
   the same, so that is model quality, not the gateway.
   **Gemini part answered 2026-11-13: yes, at sanity level.** Run 2 compiled and
   answered with `gemini-3.8-flash` through the gateway: 31 chat calls, all HTTP 200,
   no validation failure in the backend log.
2. Which rerank providers work through the gateway's `/rerank` route, and is rerank
   worth enabling in the local preset at all?
   **Partly answered 2026-11-11:** not enabled in the Ollama preset (Ollama has no
   rerank API). With the rerank triplet pointed at the gateway and no rerank model
   configured, `/rerank` answers HTTP 400 in about 50 ms with no outbound call, and
   `_pick_owner_term_id` takes the first candidate. Each fallback logs an ERROR with a
   traceback, which A6 would remove. No working rerank provider has been tested.
3. Does Gemini's embedding model accept a `dimensions` value through the gateway, and
   which value gives a table pgvector can index?
   **From documentation, not measured (2026-11-12):** `dimensions` in `litellm_params`
   reaches Gemini as `outputDimensionality` (seen in the request body against a local
   stand-in; the backend sends no `dimensions` itself). `gemini-embedding-001` returns
   3072 by default, accepts 128 to 3072 and Google recommends 768, 1536 or 3072; its
   MTEB table gives 68.17 at 1536 and 67.99 at 768. All three fit the 4000 `halfvec`
   limit; 768 and 1536 also fit 2000. Google does not normalise this model's vectors
   below 3072, and the backend does not either; the index uses cosine distance, so
   length does not matter.
   **Measured 2026-11-13:** yes. Presets now use 768 (the user's choice); runs 2 and 3
   stored 768-value vectors in `halfvec(768)` tables. Asked directly for 1536, both
   `gemini-embedding-001` and `gemini-embedding-2` returned 1536 values.
4. Does `nim_http_max_concurrent=32` trip Gemini rate limits at the reference dataset
   size?
   **From documentation, not measured (2026-11-12):** Google publishes per-project
   limits only in AI Studio. Third-party figures (scriptbyai.com, 2026-11-05): free
   tier embeddings 100 requests and 30,000 input tokens a minute;
   `gemini-3.5-flash-lite` 10 requests a minute and 500 a day. Google does not say
   whether a batch of N texts counts as 1 request or N. At the defaults one chunk is up
   to 1,024 texts sent at once. Run 1 wrote 63 and 55 rows and made 121 embedding calls
   in all, so whether the reference dataset trips the free tier depends on that
   counting rule and on text length, neither known. The presets use
   `EMBED_BATCH_SIZE=10` and `EMBED_MAX_CONCURRENT=1`, so any one request fits a
   minute's quota even if each text counts and hits the 2,048-token input limit. Chat is
   likely the tighter limit: at 10 a minute even one call in flight can exceed it, and
   the backend retries a chat 429 for only about 6 s.
   **Not measured, by decision (2026-11-13):** the key is paid and for light checks
   only. Runs 2 and 3 at the preset's lowered limits saw no 429.
5. Current stable LiteLLM image version to pin. Look it up; do not guess.
   **Answered 2026-11-11:** pin `ghcr.io/berriai/litellm:v1.108.5` (tag confirmed with
   `docker manifest inspect`). LiteLLM no longer publishes `-stable` tags; releases are
   plain `vX.Y.Z` with several minor lines patched in parallel. `v1.109.0` came out the
   same day and carries five "action required" changes, so the pin is the newest patch
   of the previous minor. Note for the Gemini preset: 1.109 release notes say newer
   Gemini Flash models reject `temperature`/`top_p`/`top_k` unless `drop_params: true`.

6. (Raised by A1, 2026-11-11.) Does a re-ingest regenerate the embeddings for custom
   analyses, SQL attributes and PQL analyses?
   **Answered 2026-11-12: no, and it is wider than that.** A re-ingest re-embeds tables
   and columns only. Existing Terms, column attributes, SQL attributes, custom analyses
   and PQL analyses are embedded when saved or first compiled; compilation only works on
   tables with no Term yet, and nothing re-embeds the rest (`embed_all_semantic_nodes`
   has no callers). Recovery is to re-save each item or reset the semantic layer, which
   deletes user-written content. From code reading plus one probe of the compilation
   work list; no end-to-end drop and rebuild was run. The procedure is in
   `docs/model-providers.md` and the mismatch error message now points to it. Also
   found: while a mismatch exists, compilation swallows the size error and stores Terms
   and SQL attributes without vectors.
   **Corrected and fixed 2026-11-13.** The Term stage never hid this error: the
   pipeline calls `create_index()` before the table loop, outside any handler, so a
   compile fails there first. The error was hidden in the SQL-attribute stages
   (`sql_attribute_suggester.py`, `bridge_tables.py`, reached when no embedder is built
   up front) and on two save paths (Term edit, column sample-values edit), which
   answered 200. All four now let `VectorSizeMismatchError` through; the two saves
   answer 422 with the message. Other exceptions are handled as before. 12 tests, no
   database; not run against a live stack. The catalog write still commits before the
   failing vector write, so the row in flight is left without a vector. A model whose
   output size differs from the table raises pgvector's own error, which is still
   swallowed.
7. (Raised by A1.) With a dimension mismatch, deleting one datasource fails in
   `delete_by_database` after the catalog transaction has committed.
   **Fixed 2026-11-12 (`99406a20`).** Confirmed: the user got HTTP 200, the catalog rows
   were deleted and the vectors were left behind; before the guard the delete succeeded.
   Deletes now skip the guard; writes, searches and the reset at ingest start still
   raise, and the reset checks before deleting previous rows. 12 tests; the whole
   `auto_ontology/vdb` directory passed 56/56 against a throwaway pgvector.
8. (Raised while answering 6, not verified.) Scheduled re-ingests build data-layer rows
   from freshly parsed source tables; these may not carry curated descriptions or
   existing catalog ids. If so it affects every re-ingest, not only recovery.
   **Answered 2026-11-13 from code reading, nothing run: partly real, and upstream
   behaviour.** Catalog ids are stable (`get_table_ids` / `get_column_ids` merge stored
   ids by natural key) and stored descriptions survive (`description` is left out of
   the fields a re-ingest overwrites), so Terms, column attributes and joins are not
   orphaned. The vector half is real: `run_ingest` builds the data-layer embedding text
   from the freshly parsed frames, where only the Databricks connector fills a
   description, then resets that database's rows in `vdb.data_objects_layer`. A
   description edited in the UI is embedded at edit time
   (`update_node_properties`) and replaced by source-only text at the next ingest. No
   test pins either half. The code involved is unchanged from `fc43a826`. Not fixed:
   it would mean building the re-ingest frames from the catalog, a change to upstream
   ingest code outside this branch's goal.

## Local environment notes

- The user's earlier clone at `E:\clone\auto-ontology` holds a working `.env` and may
  have a running stack with the same container names and ports (3200–3203, 5532). Stop
  that stack before starting this one. If a port is held, stop the process holding it
  and reuse the port; do not move to another port.
- Copy that clone's `.env` here by hand as a starting point. It contains real secrets:
  never print, log or commit its values.
- Ollama runs on the host at `http://host.docker.internal:11434` from inside containers.
- **Do not run the full test suite with `.env` pointing at a live stack.**
  `auto_ontology/tests/conftest.py` calls `load_dotenv()`, so once `.env` exists
  `uv run pytest auto_ontology dev_tools` sees `POSTGRES_*` and the roughly 800
  database tests stop skipping and run against whatever Postgres `.env` names. On
  2026-11-12 this hit the running `aofork` stack: the run was stopped after more than
  10 minutes, the live data was checked and found intact, and one leftover throwaway
  database (`difftest_*`) was dropped. Narrower runs (`auto_ontology/utils`,
  `semantic`, `vdb`) do not load that conftest. For the full suite, export `POSTGRES_*`
  for a disposable `pgvector/pgvector:pg17` first (existing variables win over `.env`),
  run `uv run alembic upgrade head` against it, then run pytest. That alone is not
  enough: a second attempt that way still loaded the model endpoints from `.env`
  (`http://litellm:4000`, which does not resolve from the host) and managed 37 tests in
  32 minutes before it was stopped. The reliable way is to run from a clean export with
  no `.env`: `git archive HEAD | tar -x -C <dir>`, then run pytest in `<dir>` with the
  repo's virtualenv.
- **Full suite on the committed tree `c2b822f3`, clean export, no database
  (2026-11-12):** 1919 passed, 812 skipped, 20 failed, 2 errors in 83 s. The failures
  are the same environmental set as on 2026-11-11 (18 kumo, `test_dal_surface`,
  `test_frontend_schema_qualified`, plus the `test_nullability` and `test_records`
  errors); the licensing test skips outside a git checkout. The database tests have
  still not been run to completion against a real Postgres, apart from
  `auto_ontology/vdb` (56 passed) and `dal/tests/test_reset.py` (14 passed).
  A third attempt, from the clean export against a disposable migrated Postgres with no
  `.env`, hung at the same place (about 28 tests in, the connection idle after
  `DELETE FROM catalog_database`), so the hang is not caused by `.env`. It was not
  diagnosed. Whether it also happens on upstream `fc43a826` is unknown.
- **Diagnosed 2026-11-13: not a hang, and not the fork.** With `POSTGRES_HOST=localhost`
  and a Postgres published on `127.0.0.1` only, every new psycopg connection on this
  Windows machine waits up to 130 s: `localhost` resolves to `::1` first, the refusal
  there takes 2 s at socket level and psycopg 3.3.4 does not act on it. Test 28 in
  collection order (`catalog/tests/test_incremental_ingest.py::test_first_ingest_creates_the_catalog`)
  is the first to open new connections; it took 186 s. Upstream `fc43a826` behaves the
  same. With `POSTGRES_HOST=127.0.0.1` the suite finishes in about 2 minutes. Anything
  on the host that reaches the stack's Postgres through `localhost` pays the same delay.
- **Full suite against Postgres, clean export, fixtures seeded (2026-11-13):**

  | Tree | Passed | Skipped | Failed | Errors |
  |---|---|---|---|---|
  | upstream `fc43a826` | 2589 | 4 | 21 | 1 |
  | fork `bbd87bdd` | 2727 | 4 | 21 | 1 |
  | fork `c80949b7` | 2739 | 4 | 21 | 1 |

  The 21 failures and 1 error are the same tests in all three (18 kumo,
  `test_dal_surface`, `test_frontend_schema_qualified`, and two SQLite tests that build
  a broken Windows path). No shared test differs between fork and upstream; every
  fork-added test passes. Recipe: export the tree with `git archive`, start
  `pgvector/pgvector:pg17` on a spare port, export `POSTGRES_*` with host `127.0.0.1`,
  set `PYTHONPATH` to the export, then `alembic upgrade head`,
  `python -m dev_tools.fixtures.seed_fixtures`,
  `python -m dev_tools.fixtures.seed_graph_fixture --reset` (with `CONNECTION_STRINGS`
  naming the seeded `pagila` database and `sqlite:dev_tools/fixtures/sql/chinook.sqlite`),
  then `python -m pytest auto_ontology dev_tools -q`.
