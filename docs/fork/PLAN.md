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
   the same, so that is model quality, not the gateway. Gemini part still open.
2. Which rerank providers work through the gateway's `/rerank` route, and is rerank
   worth enabling in the local preset at all?
   **Partly answered 2026-11-11:** not enabled in the Ollama preset (Ollama has no
   rerank API). With the rerank triplet pointed at the gateway and no rerank model
   configured, `/rerank` answers HTTP 400 in about 50 ms with no outbound call, and
   `_pick_owner_term_id` takes the first candidate. Each fallback logs an ERROR with a
   traceback, which A6 would remove. No working rerank provider has been tested.
3. Does Gemini's embedding model accept a `dimensions` value through the gateway, and
   which value gives a table pgvector can index?
4. Does `nim_http_max_concurrent=32` trip Gemini rate limits at the reference dataset
   size?
5. Current stable LiteLLM image version to pin. Look it up; do not guess.
   **Answered 2026-11-11:** pin `ghcr.io/berriai/litellm:v1.108.5` (tag confirmed with
   `docker manifest inspect`). LiteLLM no longer publishes `-stable` tags; releases are
   plain `vX.Y.Z` with several minor lines patched in parallel. `v1.109.0` came out the
   same day and carries five "action required" changes, so the pin is the newest patch
   of the previous minor. Note for the Gemini preset: 1.109 release notes say newer
   Gemini Flash models reject `temperature`/`top_p`/`top_k` unless `drop_params: true`.

6. (Raised by A1, 2026-11-11.) Does a re-ingest regenerate the embeddings for custom
   analyses, SQL attributes and PQL analyses? They are embedded into `semantic_layer`
   when saved, so dropping the vector tables to change dimension may remove them from
   search until re-saved. Check before the docs give drop-and-re-ingest as the recovery.
7. (Raised by A1.) With a dimension mismatch, deleting one datasource fails in
   `delete_by_database` after the catalog transaction has committed. Decide whether the
   guard should apply to writes and searches only.

## Local environment notes

- The user's earlier clone at `E:\clone\auto-ontology` holds a working `.env` and may
  have a running stack with the same container names and ports (3200–3203, 5532). Stop
  that stack before starting this one. If a port is held, stop the process holding it
  and reuse the port; do not move to another port.
- Copy that clone's `.env` here by hand as a starting point. It contains real secrets:
  never print, log or commit its values.
- Ollama runs on the host at `http://host.docker.internal:11434` from inside containers.
