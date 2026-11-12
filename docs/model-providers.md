<!--
SPDX-License-Identifier: Apache-2.0
-->

# Running on other model providers

This fork can run the whole stack on chat, embedding and rerank models from any
provider (Ollama, Gemini, OpenAI, and others). It does this through an optional
[LiteLLM](https://docs.litellm.ai) gateway: one container that speaks the OpenAI
protocol to the backend and translates to each provider. The backend does not import
LiteLLM.

This is a fork feature, not part of upstream NVIDIA/auto-ontology. Every new setting is
optional. With none of them set, behaviour is the same as upstream.

The Ollama preset is tested. The Gemini and mixed presets have had one sanity run each
and no load testing. See
[What was tested](#what-was-tested) and [What was not tested](#what-was-not-tested)
before you rely on anything here.

## How model selection works

Each model role is configured by a triplet of variables: `<PREFIX>_ENDPOINT`,
`<PREFIX>_API_KEY` and `<PREFIX>_MODEL`. The four prefixes are:

| Prefix          | Role                                      |
| --------------- | ----------------------------------------- |
| `REASONING`     | Main chat and NL-to-SQL model.            |
| `NON_REASONING` | Faster steps such as entity extraction.   |
| `EMBED`         | Text embeddings.                          |
| `RERANK`        | Reranking of bridge-table candidates.     |

Each field is resolved in this order:

1. The `<PREFIX>_<FIELD>` variable.
2. The shared `DEFAULT_MODELS_<FIELD>` variable.
3. For `API_KEY`, `ENDPOINT` and `MODEL`, the legacy name (`NVIDIA_API_KEY`, `BASE_URL`,
   `MODEL_NAME`) is tried as part of step 2 when `DEFAULT_MODELS_<FIELD>` is empty.
4. A built-in default. There are two sets, both pointing at NVIDIA endpoints. The set is
   chosen by the prefix of the triplet's API key: `nvapi-` selects the
   `integrate.api.nvidia.com` set, and anything else (including no key) selects the
   `inference-api.nvidia.com` set.

Step 4 is why you should set every triplet in full when you do not use NVIDIA. A field
left empty does not fail. It silently falls back to an NVIDIA URL and model name, and
the request leaves your machine for that host. The preset env files in this fork set all
four triplets for that reason.

## Settings added by this fork

| Setting | Default | Allowed values | When a bad value fails |
| --- | --- | --- | --- |
| `REASONING_CLIENT`, `NON_REASONING_CLIENT`, `DEFAULT_MODELS_CLIENT` | `auto` | `auto`, `openai`, `nvidia` (case and surrounding spaces ignored) | `ValueError` at import of `auto_ontology/utils/llm_invoke.py`, so the backend does not start. |
| `LLM_REASONING_EFFORT` | unset (field not sent) | Any non-empty string the model accepts, for example `none` | Not validated by the backend. Sent only on the OpenAI-style client; the provider may reject it on the first chat call. |
| `EMBED_DIMENSIONS` | unset (2048 columns, as upstream) | Whole number from 1 to 4000 | `ValueError` when the vector store is first opened (`get_vdb`), not at import. |
| `EMBED_BATCH_SIZE` | 32 | Whole number, 1 or more | `ValueError` when the embedding parameters are first built, that is on the first embedding call. |
| `EMBED_MAX_CONCURRENT` | 32 | Whole number, 1 or more | Same as `EMBED_BATCH_SIZE`. |
| `EMBED_EXTRA_PARAMS` | `nvidia` | `nvidia` or `none` (case and surrounding spaces ignored; unset or blank means `nvidia`) | `ValueError` when the embedding parameters are first built, that is on the first embedding call, not at startup. |
| `RERANK_ENDPOINT=disabled` | unset (NVIDIA or configured endpoint) | The literal word `disabled`, any case | Not applicable; any other value is used as a URL. |

Details:

- **Client choice.** `auto` keeps the upstream rule: model names starting with
  `openai/`, `azure/` or `aws/` use the OpenAI-style client, and everything else uses
  `ChatNVIDIA`. `openai` forces the OpenAI-style client for any model name, which is
  what a gateway needs. `<PREFIX>_CLIENT` falls back to `DEFAULT_MODELS_CLIENT`.
- **Reasoning effort.** One value applies to both chat roles. `none` turns thinking off
  on Ollama models such as `qwen3.5`.
- **Embedding dimension.** The value must equal the output size of `EMBED_MODEL`. pgvector
  cannot index `halfvec` columns above 4000 dimensions, so larger values are rejected.
  A vector table keeps the size it was created with. If you change `EMBED_DIMENSIONS` or
  the embedding model on a database that already has vector tables, the first use of an
  existing table raises `VectorSizeMismatchError`. The message names both sizes and
  tells you how to recover; the full procedure is under
  [Changing the embedding model or dimension](#changing-the-embedding-model-or-dimension).
- **Deleting a datasource after a mismatch.** Deletes are not blocked by the size check,
  so a datasource can still be removed while the setting and the tables disagree. Writes
  and searches raise the error.
- **Embedding concurrency.** `EMBED_MAX_CONCURRENT` limits requests in flight per
  embedding call, not per process. Two ingests running at once each get their own pool,
  so the total can be higher than the setting.
- **Embedding request fields.** By default each embedding request carries the NVIDIA
  fields `input_type`, `truncate` and `modality`. `EMBED_EXTRA_PARAMS=none` sends a plain
  OpenAI embeddings body instead: only `model`, `input` and `encoding_format`. Use it for
  a provider or gateway that rejects or mistranslates the fields. The text prefix that
  `nemotron-3.5-embed-1b-nvfp4` uses for query and passage is part of the input, so
  `none` does not change it.
- **Rerank.** With `RERANK_ENDPOINT=disabled`, no request is sent. The only caller,
  bridge-table owner selection, takes the first candidate term and logs one INFO line.
  Without it, a missing rerank model logs an error with a traceback each time and takes
  the same fallback.

## Changing the embedding model or dimension

Vector tables keep the size they were created with. Stored vectors cannot be resized, and
vectors from different models cannot be compared.

**Option 1, no loss.** Set `EMBED_DIMENSIONS` back to the size the tables were built
with, and use the model that produced those vectors.

**Option 2, rebuild at the new size.**

1. Stop the backend and the ingestion service. Do not let a compilation pass run while
   the settings and the tables disagree: it stores Terms and SQL attributes without
   vectors, and later passes skip those tables.
2. Set the new `EMBED_MODEL` and `EMBED_DIMENSIONS`.
3. Drop both vector tables. This removes only embeddings:
   `DROP TABLE IF EXISTS vdb.data_objects_layer, vdb.semantic_layer;`
4. Start the services. The startup ingest recreates `vdb.data_objects_layer` at the new
   size and re-embeds every table and column.
5. Re-embed the semantic layer. **This does not happen on its own.** Compilation only
   works on tables that have no Term yet, so existing Terms, column attributes, SQL
   attributes, custom analyses and predictive analyses stay out of search, and there is
   no re-embed command. Either:
   - Re-save each item, which keeps everything. Term or column attribute: save its
     description. SQL attribute: save it with its SQL expression. Custom or predictive
     analysis: edit and save.
   - Or reset the semantic layer (`POST /api/semantic-compilation/reset`). It recompiles
     Terms, attributes and generated SQL attributes with the LLM, but deletes everything
     users wrote: edits to Terms and attributes, manual SQL attributes, custom analyses
     and predictive analyses, in every database.

This procedure comes from reading the code and one probe of the compilation work list. A
full drop and rebuild has not been run end to end.

## The gateway

`docker-compose.litellm.yml` adds one service, `litellm`, as a separate Compose file.
Upstream's `docker-compose.yml` is not changed.

- Image: `ghcr.io/berriai/litellm:v1.108.5`, pinned to an exact tag. It is the newest
  patch of the previous minor line. `v1.109.0` was released the same day and carries
  several changes that need action when upgrading, so it was not used. Check the
  release notes before you move the pin.
- Port: bound to `127.0.0.1:4000` on the host. Containers on the stack's network reach
  it at `http://litellm:4000/v1`.
- Stateless: no database, so no virtual keys, spend tracking or admin UI persistence.
  The master key is the only credential it accepts.
- Config: `deploy/litellm/<LITELLM_PRESET>.yaml`, mounted read-only. `LITELLM_PRESET`
  defaults to `ollama`.
- `LITELLM_MASTER_KEY` is required. Compose refuses to start the service without it. The
  backend sends the same value as its API key for each triplet. Generate one with:

  ```bash
  echo "sk-$(openssl rand -hex 32)"
  ```

  The gateway refuses to start when the key is unset, empty or a known example value.
  It does not reject `change-me`, so replace it yourself.

Commands:

```bash
# Whole stack plus gateway
docker compose -f docker-compose.yml -f docker-compose.litellm.yml up -d

# Gateway only
docker compose -f docker-compose.litellm.yml up -d
```

The gateway takes about 22 seconds to become healthy. The backend's chat retries cover
only about 6 seconds of that. The Compose file does not make the backend wait for the
gateway, because that would break gateway-only use. On a cold start, bring the gateway
up first, wait for it to report healthy, then start the rest of the stack.

## Ollama preset

Files: `deploy/litellm/ollama.yaml` (gateway) and `deploy/litellm/env.ollama.example`
(backend settings).

1. Install Ollama on the host and pull the models:

   ```bash
   ollama pull qwen3.5:9b
   ollama pull bge-m3
   ```

2. Copy the lines from `deploy/litellm/env.ollama.example` into `.env`. Remove the
   model settings from `.env.example` that they replace (`DEFAULT_MODELS_*`,
   `REASONING_*`, `NON_REASONING_*`, `EMBED_*`, `RERANK_*`). Replace `change-me` in
   `LITELLM_MASTER_KEY`.
3. Start the gateway first, then the stack, with the commands above.

Why the preset is written the way it is:

- **Chat uses `ollama_chat/`.** Ollama loads models at a 4096-token context and silently
  truncates longer prompts. A 10k-token prompt returned HTTP 200 and a wrong answer. Only
  the `ollama_chat/` route forwards `num_ctx`, so the preset sets it to 16384. A larger
  context costs GPU memory. On an 8 GB GPU with `bge-m3` also loaded, about 14% of
  `qwen3.5:9b` ran on the CPU, at roughly 13 tokens per second.
- **`LLM_REASONING_EFFORT=none`.** Thinking models took 20 to 28 seconds per call
  without it. A one-word answer took about 0.2 s and 2 tokens with it, against 3 to 4 s
  and 120 to 150 tokens without.
- **Embeddings use `openai/bge-m3` on Ollama's `/v1` endpoint.** The backend sends fields
  that only NVIDIA endpoints define (`input_type`, `truncate`, `modality`). Ollama's
  OpenAI-compatible endpoint ignores them. LiteLLM's native `ollama/` embedding route
  forwards `truncate` and Ollama rejects the request with HTTP 400. `drop_params` does
  not remove it.
- **`EMBED_DIMENSIONS=1024`** is the output size of `bge-m3`.
- **Timeout and concurrency lines** (`LLM_INVOKE_TIMEOUT_S=600`, `LLM_MAX_INFLIGHT=2`,
  `SEMANTIC_LLM_MAX_INFLIGHT=2`). A single local GPU serves one request at a time and is
  slow. The upstream defaults (120 s per call, 6 at once) suit hosted endpoints. On an 8
  GB laptop GPU, four concurrent calls took 152 to 262 s.
- **No `temperature` in the gateway config.** Setting it to 0 made structured-output
  content worse in two probes, so the model's own sampling defaults apply.
- **Rerank is off** (`RERANK_ENDPOINT=disabled`), because Ollama has no rerank API.

## Adding another provider

1. Add a model to a preset YAML in `deploy/litellm/` (or copy one to a new name and set
   `LITELLM_PRESET`). Give it an alias under `model_name`. The backend asks for the alias,
   so you can change the underlying model later without touching the backend settings.
2. Follow the alias rules. The alias must not start with `openai/`, `azure/` or `aws/`
   and must not contain `claude` or `anthropic`. The backend picks its client and its
   structured-output method from the model name.
3. Put provider keys in environment variables, never in a committed file. Read them in
   the YAML as `os.environ/NAME`, and pass them through in the `environment:` block of
   `docker-compose.litellm.yml` (the file already does this for `GEMINI_API_KEY`).
4. Set `EMBED_DIMENSIONS` to the output size of the embedding model. If vector tables
   exist at another size, see the notes above.
5. Set all four triplets, `REASONING_CLIENT=openai` and `NON_REASONING_CLIENT=openai`,
   to the gateway URL and your aliases.

For model strings, parameters and provider-specific notes, see
[LiteLLM's provider docs](https://docs.litellm.ai/docs/providers).

## What was tested

Date: 2026-11-11. Versions: LiteLLM 1.108.5, Ollama 0.40.6, chat model `qwen3.5:9b`,
embedding model `bge-m3` (1024 dimensions), RTX 3070 Laptop GPU with 8 GB. Windows host,
stack in Docker.

**Unit tests.** The new settings have tests for default unchanged, setting honoured and
bad value rejected. `uv run pytest auto_ontology/utils auto_ontology/semantic
auto_ontology/vdb -q` with no database passed except for one error that was already
there (`test_records.py` needs `POSTGRES_USER`). The vector-size tests also ran against a
throwaway `pgvector/pgvector:pg17` and passed. Ruff check and format check were clean.

**Run 1: whole stack, Ollama through the gateway, BIRD `financial` database.**

- The backend created `vdb.data_objects_layer` (63 rows) and `vdb.semantic_layer`
  (55 rows) as `halfvec(1024)` with an HNSW index, from `EMBED_DIMENSIONS=1024`.
- Ingest took 6.0 s at startup plus 168.0 s of semantic compilation. Result: 8 terms, 47
  column definitions, 8 joins. This matches an earlier direct-Ollama run.
- The gateway log showed 75 chat and 121 embedding calls, all HTTP 200, matching
  Ollama's own log one for one. No log line named an NVIDIA host. There were no errors,
  tracebacks, timeouts or structured-output validation failures.
- Structured output through the gateway: 15 of 15 responses were schema-valid, 2 to 15
  s each. Field content was right in only 2 or 3 of 5 runs per probe. A direct-Ollama
  run scored the same.
- **The test question was answered wrongly.** "Which district has the highest average
  salary, and what is it?" has the correct answer Hl.m. Praha, 12,541. As configured,
  the stack answered wrongly 3 times out of 3 (593 s, 647 s and 182 s). Two answers used
  column `A15` (crimes in 1995) and one averaged transaction amounts. Compiling from the
  schema alone labelled `A11` as "Total Revenue Or Sales Count", so no term meant salary.
- After loading the BIRD column descriptions (54 columns) and recompiling (86.9 s, same
  counts), one ask returned Hl.m. Praha, 12,541 in 46.6 s with no configuration change.
- Asked six more times in that state, it was right 6 times out of 6 (43 to 146 s,
  median about 59 s, no timeouts). Two asks needed one SQL repair round each. "Right"
  was judged on the returned rows: the answer text describes the query and does not
  state the district or the number.

**Run 2: Gemini chat and embeddings through the gateway (2026-11-13, sanity run).**
One ingest, one compile and one question on a paid Gemini key, fresh database, chat
`gemini-3.8-flash`, embeddings `gemini-embedding-001` at 768 dimensions, preset as
written (one chat call and one embedding request at a time, 10 texts per request).

- The backend created both vector tables as `halfvec(768)` with an HNSW index
  (63 and 55 rows).
- Ingest took 21.2 s and semantic compilation 147.8 s. Result: 8 terms, 47 column
  definitions, 8 joins, the same counts as run 1.
- The test question was answered correctly on the first ask, without column
  descriptions: Hl.m. Praha, 12,541, in 36.8 s.
- The gateway log showed 31 chat and 23 embedding calls, all HTTP 200. No rate-limit
  response, no traceback, no log line naming an NVIDIA host.
- So with a real key Gemini accepted the model names, the embedding request
  (`taskType: SEMANTIC_SIMILARITY`, `outputDimensionality: 768`) and the backend's
  structured-output schemas, and every reply the backend needed validated.

**Run 3: Ollama chat, Gemini embeddings (2026-11-13, sanity run).** Fresh database,
chat `qwen3.5:9b` on Ollama 0.40.6, embeddings as in run 2.

- Vector tables `halfvec(768)`, 63 and 55 rows. Ingest 19.2 s, compilation 165.7 s:
  8 terms, 47 column definitions, 8 joins.
- Without column descriptions the question was answered wrongly once (an empty result
  after 304.5 s), as in run 1. With the BIRD column descriptions loaded and a recompile
  (99.9 s; 9 terms, 47 column definitions, 8 joins) one ask returned Hl.m. Praha,
  12,541 in 90.3 s.
- 59 chat and 122 embedding calls in all, every one HTTP 200.
- One error was logged once during compilation, when SQLAlchemy closed a pooled
  database connection (`AttributeError` inside asyncpg). The compile finished
  successfully. It was not investigated.

**`EMBED_EXTRA_PARAMS`.** Checked with unit tests and against a local stand-in for
Google's API through LiteLLM 1.108.5: with `none`, no `taskType` is sent unless the
gateway config sets one. It was not sent to real Gemini.

**Check inside the rebuilt backend container.** With `RERANK_ENDPOINT=disabled`, a
rerank call raised `RerankDisabled` and sent no request. With `EMBED_BATCH_SIZE=5` and
`EMBED_MAX_CONCURRENT=2`, embedding 23 texts made exactly 5 gateway requests and returned
23 vectors of 1024 dimensions.

Read this plainly: with a 9B local model and no column descriptions, the stack gave a
wrong answer every time. This is a limitation of the small model and the missing
descriptions. The same limitation was seen on direct Ollama without the gateway, so it
is not caused by the gateway. With column descriptions loaded it was right 7 times out
of 7 on this one question; other questions were not measured.

## What was not tested

- Gemini beyond one sanity run per preset. Runs 2 and 3 used one small database and one
  question on a paid key. Unverified:
  - Any Gemini chat model other than `gemini-3.8-flash`, and `gemini-embedding-2`
    (which needs `EMBED_EXTRA_PARAMS=none`).
  - Embedding sizes other than 768.
  - Repeatability: each Gemini result is a single ask.
  - Retrieval quality with one task type for both stored rows and search strings.
  - Free-tier rate limits. The figures in the env files are third-party; Google shows
    limits only in AI Studio. Whether the lowered concurrency settings avoid failed
    steps on the free tier is not known; the paid-tier runs saw no rate-limit response.
  - What Gemini does with an embedding input over 2,048 tokens. LiteLLM drops the
    backend's `truncate` field.
- Any hosted provider other than Gemini.
- A working rerank model through the gateway. Without a rerank model the gateway answers
  `/rerank` with HTTP 400 and the backend falls back to the first candidate.
- `EMBED_BATCH_SIZE`, `EMBED_MAX_CONCURRENT` and `RERANK_ENDPOINT=disabled` in a full
  ingest. They have unit tests and one check inside the running backend container (see
  above). The run 1 dataset has no bridge tables, so the rerank fallback in bridge-table
  owner selection was never reached in a real ingest.
- Linux hosts. The Compose file maps `host.docker.internal` for them, but this was not
  run.
- Helm. The chart values are not updated for any of this.
- A regression run with upstream defaults and an NVIDIA key.

## Known issues that affect use

- A SQLite source file must be named `main.sqlite`. Other names produce three-part table
  names that SQLite rejects. This is an upstream issue, not changed here.
- On Windows, clone with `core.autocrlf=false`. Windows line endings break the
  entrypoint heredoc in `Dockerfile` and `frontend/prisma/migrate.sh`.
