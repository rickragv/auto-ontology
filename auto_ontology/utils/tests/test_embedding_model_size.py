# SPDX-License-Identifier: Apache-2.0

"""The embedding model's output size is checked against ``EMBED_DIMENSIONS``.

A table built at the configured size refuses vectors of any other length, but
only once pgvector sees them: a search failed inside the store's own handler
and came back empty, and an ingest deleted the database's rows before its first
write failed. ``embed_texts`` now compares what the model returned with the
setting, so a mismatch stops the embed itself, before anything is searched,
deleted or written. With the setting unset nothing is checked.

The endpoint is an ``httpx.MockTransport`` under the real client, as in
``test_embedding.py``. No database.
"""

from __future__ import annotations

import json
from typing import Any, Callable, NoReturn
from unittest.mock import MagicMock

import httpx
import pandas as pd
import pytest
from fastapi import HTTPException
from openai import OpenAI

import auto_ontology.ingestion_service.ingest as ingest_module
from auto_ontology.semantic.embed import SemanticEmbedder
from auto_ontology.server.terms import router as terms_router
from auto_ontology.utils import embedding
from auto_ontology.utils.embedding import (
    EmbedParams,
    _embed_frame,
    _embed_with_retry,
    batch_embed_chunks,
    embed_rows,
    embed_texts,
)
from auto_ontology.utils.retriever import Retriever
from auto_ontology.vdb.dimensions import (
    EmbeddingConfigError,
    EmbeddingSizeMismatchError,
    VectorSizeMismatchError,
)

_ENV = "EMBED_DIMENSIONS"
_MODEL = "acme/embed-v2"


def _params(**overrides: Any) -> EmbedParams:
    fields: dict[str, Any] = {
        "model_name": _MODEL,
        "embed_invoke_url": "https://embed.example/v1",
        "api_key": "sk-test",
    }
    fields.update(overrides)
    return EmbedParams(**fields)


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], list[httpx.Request]]:
    """Answer every request with vectors of *size*; returns the requests seen."""

    def _install(size: int) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def _handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            texts = json.loads(request.content)["input"]
            data = [
                {"object": "embedding", "index": i, "embedding": [0.5] * size}
                for i in range(len(texts))
            ]
            return httpx.Response(
                200, json={"object": "list", "data": data, "model": _MODEL}
            )

        def _client(params: EmbedParams) -> OpenAI:
            transport = httpx.MockTransport(_handle)
            return embedding._new_client(params, httpx.Client(transport=transport))

        monkeypatch.setattr(embedding, "_client", _client)
        return seen

    return _install


def _no_sleep(seconds: float) -> NoReturn:
    raise AssertionError(f"retried after {seconds}s")


# ---------------------------------------------------------------------------
# embed_texts
# ---------------------------------------------------------------------------


def test_unset_checks_nothing(endpoint: Callable[[int], list]) -> None:
    endpoint(3)

    assert embed_texts(["orders"], _params(), "query") == [[0.5] * 3]


def test_a_matching_size_passes(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint(4)
    monkeypatch.setenv(_ENV, "4")

    assert embed_texts(["orders"], _params(), "query") == [[0.5] * 4]


def test_another_size_raises_naming_the_model_and_both_sizes(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint(6)
    monkeypatch.setenv(_ENV, "4")

    with pytest.raises(EmbeddingSizeMismatchError) as excinfo:
        embed_texts(["orders"], _params(), "query")

    message = str(excinfo.value)
    assert repr(_MODEL) in message
    assert "EMBED_MODEL" in message
    assert "returned 6-dimension vectors, but EMBED_DIMENSIONS is 4" in message
    assert "EMBED_DIMENSIONS=6" in message
    # Caught wherever a table of another size is.
    assert isinstance(excinfo.value, VectorSizeMismatchError)
    assert isinstance(excinfo.value, EmbeddingConfigError)


def test_concurrent_batches_are_checked_too(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = endpoint(6)
    monkeypatch.setenv(_ENV, "4")

    with pytest.raises(EmbeddingSizeMismatchError):
        embed_texts(
            ["a", "b", "c"],
            _params(inference_batch_size=1, nim_http_max_concurrent=2),
            "passage",
        )

    assert len(seen) == 3


def test_a_bad_setting_fails_before_any_request(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = endpoint(4)
    monkeypatch.setenv(_ENV, "abc")

    with pytest.raises(EmbeddingConfigError, match=_ENV):
        embed_texts(["orders"], _params(), "query")

    assert seen == []


def test_blank_texts_still_send_nothing(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = endpoint(6)
    monkeypatch.setenv(_ENV, "4")

    assert embed_texts(["", "  "], _params(), "query") == [None, None]
    assert seen == []


# ---------------------------------------------------------------------------
# Callers that catch every other embed error
# ---------------------------------------------------------------------------


def test_a_chunk_is_not_retried(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = endpoint(6)
    monkeypatch.setenv(_ENV, "4")
    params = _params()

    with pytest.raises(EmbeddingSizeMismatchError):
        _embed_with_retry(
            lambda part: _embed_frame(part, params),
            pd.DataFrame([{"text": "orders", "metadata": {}}]),
            label="chunk",
            sleep=_no_sleep,
        )

    assert len(seen) == 1


def test_chunked_embedding_stops_at_the_first_chunk(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = endpoint(6)
    monkeypatch.setenv(_ENV, "4")
    rows = [{"text": t, "metadata": {}} for t in ("a", "b", "c")]

    with pytest.raises(EmbeddingSizeMismatchError):
        list(batch_embed_chunks(rows, _params(), chunk_size=1))

    assert len(seen) == 1


def test_an_interactive_embed_raises_it_unwrapped(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not as ``RuntimeError``: the save routes answer this one with 422."""
    endpoint(6)
    monkeypatch.setenv(_ENV, "4")

    with pytest.raises(EmbeddingSizeMismatchError):
        embed_rows([{"text": "orders", "metadata": {}}], _params(), what="tabular")


# ---------------------------------------------------------------------------
# Search, ingest and save
# ---------------------------------------------------------------------------


def test_a_search_stops_before_the_store(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint(6)
    monkeypatch.setenv(_ENV, "4")
    store = MagicMock(name="PostgresVDB")

    with pytest.raises(EmbeddingSizeMismatchError):
        Retriever(store, _params()).query("revenue")

    store.retrieval.assert_not_called()


def test_an_ingest_stops_before_its_reset(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reset deletes the database's rows, so it must not be reached."""
    endpoint(6)
    monkeypatch.setenv(_ENV, "4")
    rows = [
        {"text": f"table t{i}", "metadata": {"id": f"t{i}", "label": "Table"}}
        for i in range(3)
    ]
    mod = ingest_module
    monkeypatch.setattr(mod, "get_embed_params", _params)
    monkeypatch.setattr(mod, "ingest_catalog", lambda connector: ([1], [1]))
    monkeypatch.setattr(mod, "is_pii_detection_enabled", lambda: False)
    monkeypatch.setattr(mod, "CatalogEmbeddingRowsOp", lambda **kw: lambda pair: rows)
    get_data_vdb = MagicMock(name="get_data_vdb")
    monkeypatch.setattr(mod, "get_data_vdb", get_data_vdb)
    connector = MagicMock(database_name="pagila", spec=["database_name"])

    with pytest.raises(EmbeddingSizeMismatchError):
        mod.run_ingest(connector)

    get_data_vdb.assert_not_called()


def test_a_term_save_answers_422_and_writes_nothing(
    endpoint: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint(6)
    monkeypatch.setenv(_ENV, "4")
    store = MagicMock(name="PostgresVDB")
    embedder = SemanticEmbedder(
        database_name="warehouse", embed_params=_params(), vdb=store
    )
    service = terms_router.term_service
    monkeypatch.setattr(
        service, "build_semantic_embedder", lambda database_name, reset: embedder
    )
    dal = terms_router.terms_dal
    monkeypatch.setattr(
        dal,
        "fetch_term_and_column_attributes_for_embedding",
        lambda term_id: ({"id": term_id, "name": "Order"}, []),
    )
    monkeypatch.setattr(
        dal,
        "update_term",
        lambda term_id, **kw: {"id": term_id, "name": "Order", "old_name": "Order"},
    )
    monkeypatch.setattr(dal, "get_term_certification", lambda term_id: None)
    monkeypatch.setattr(terms_router, "edit_subject", lambda term_id: None)
    monkeypatch.setattr(terms_router, "resolve_internal_user", lambda *a, **kw: None)

    with pytest.raises(HTTPException) as excinfo:
        terms_router.update_term(
            MagicMock(name="request"), "t1", terms_router.TermUpdate(description="d")
        )

    assert excinfo.value.status_code == 422
    assert "EMBED_DIMENSIONS is 4" in excinfo.value.detail
    store.run.assert_not_called()
