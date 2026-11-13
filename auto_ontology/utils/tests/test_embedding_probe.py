# SPDX-License-Identifier: Apache-2.0

"""``check_embedding_model_size`` and its call at the start of compilation.

The endpoint is an ``httpx.MockTransport`` under the real client. No database.
"""

from __future__ import annotations

import json
from typing import Callable
from unittest.mock import MagicMock

import httpx
import pytest
from openai import APIStatusError, OpenAI

from auto_ontology.semantic import pipeline
from auto_ontology.utils import embedding
from auto_ontology.utils.embedding import EmbedParams
from auto_ontology.utils.embedding_probe import check_embedding_model_size
from auto_ontology.vdb.dimensions import EmbeddingSizeMismatchError

_ENV = "EMBED_DIMENSIONS"
_PARAMS = EmbedParams(
    model_name="acme/embed-v2",
    embed_invoke_url="https://embed.example/v1",
    api_key="sk-test",
)


@pytest.fixture
def endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[int, int], list[httpx.Request]]:
    """Answer with vectors of *size* (or HTTP *status*); returns requests seen."""

    def _install(size: int, status: int = 200) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def _handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "refused"}})
            texts = json.loads(request.content)["input"]
            data = [
                {"object": "embedding", "index": i, "embedding": [0.5] * size}
                for i in range(len(texts))
            ]
            return httpx.Response(200, json={"object": "list", "data": data})

        def _client(params: EmbedParams) -> OpenAI:
            transport = httpx.MockTransport(_handle)
            return embedding._new_client(params, httpx.Client(transport=transport))

        monkeypatch.setattr(embedding, "_client", _client)
        return seen

    return _install


def test_unset_sends_nothing(endpoint: Callable[..., list[httpx.Request]]) -> None:
    seen = endpoint(3)

    check_embedding_model_size(_PARAMS)

    assert seen == []


def test_a_matching_model_passes_with_one_request(
    endpoint: Callable[..., list[httpx.Request]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = endpoint(4)
    monkeypatch.setenv(_ENV, "4")

    check_embedding_model_size(_PARAMS)

    assert len(seen) == 1
    assert len(json.loads(seen[0].content)["input"]) == 1


def test_a_model_of_another_size_raises(
    endpoint: Callable[..., list[httpx.Request]], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint(1024)
    monkeypatch.setenv(_ENV, "768")

    with pytest.raises(
        EmbeddingSizeMismatchError, match="1024.*EMBED_DIMENSIONS is 768"
    ):
        check_embedding_model_size(_PARAMS)


def test_a_refused_request_is_raised_unchanged(
    endpoint: Callable[..., list[httpx.Request]], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint(4, status=401)
    monkeypatch.setenv(_ENV, "4")

    with pytest.raises(APIStatusError):
        check_embedding_model_size(_PARAMS)


def _compile_with(
    monkeypatch: pytest.MonkeyPatch, tables: list[dict[str, str]]
) -> tuple[MagicMock, MagicMock]:
    """Run compilation over *tables* with table processing stubbed out."""
    process = MagicMock(return_value=None)
    monkeypatch.setattr(pipeline, "process_table", process)
    monkeypatch.setattr(
        pipeline, "fetch_all_tables_without_term", lambda *a, **k: tables
    )
    monkeypatch.setattr(pipeline, "load_domain_summary", lambda *a, **k: MagicMock())
    monkeypatch.setattr(pipeline, "reset_sampling_breaker", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "is_distinct_value_probing_enabled", lambda: True)
    embedder = MagicMock()
    embedder.embed_params = _PARAMS
    return process, embedder


def test_compilation_stops_before_any_table_on_a_wrong_size(
    endpoint: Callable[..., list[httpx.Request]], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint(1024)
    monkeypatch.setenv(_ENV, "768")
    table = {"id": "t1", "name": "orders", "schema_name": "main"}
    process, embedder = _compile_with(monkeypatch, [table])

    with pytest.raises(EmbeddingSizeMismatchError):
        pipeline.compile_semantic_layer("main", embedder=embedder)

    process.assert_not_called()


def test_compilation_with_no_tables_sends_nothing(
    endpoint: Callable[..., list[httpx.Request]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = endpoint(1024)
    monkeypatch.setenv(_ENV, "768")
    _, embedder = _compile_with(monkeypatch, [])

    assert pipeline.compile_semantic_layer("main", embedder=embedder) == 0
    assert seen == []
