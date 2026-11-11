# SPDX-License-Identifier: Apache-2.0

"""Tests for switching rerank off with ``RERANK_ENDPOINT=disabled``.

Only the exact word (any case, surrounding whitespace ignored) disables the
reranker, and it must do so before any request leaves the process.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx
import pytest

from auto_ontology.utils import rerank
from auto_ontology.utils.rerank import RerankDisabled, rerank_hits

Server = Callable[..., list[httpx.Request]]

_HITS = [
    {"id": "a", "text": "apples"},
    {"id": "b", "text": "bananas"},
]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    """Answer the next POST with *body*; returns the requests seen.

    Any real ``httpx`` send raises, so an accidental call fails loudly.
    """

    def _no_real_send(*args: Any, **kwargs: Any) -> httpx.Response:
        raise AssertionError("a real HTTP request was attempted")

    monkeypatch.setattr(httpx.Client, "send", _no_real_send)

    def _install(body: dict[str, Any], status: int = 200) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def _post(url: str, **kwargs: Any) -> httpx.Response:
            request = httpx.Request(
                "POST", url, json=kwargs["json"], headers=kwargs["headers"]
            )
            seen.append(request)
            return httpx.Response(status, json=body, request=request)

        monkeypatch.setattr(rerank.httpx, "post", _post)
        return seen

    return _install


@pytest.mark.parametrize("value", ["disabled", " Disabled ", "DISABLED"])
def test_disabled_raises_before_any_request(server: Server, value: str) -> None:
    seen = server({"rankings": []})

    with pytest.raises(RerankDisabled):
        rerank_hits("fruit", _HITS, rerank_invoke_url=value, model_name="m")

    assert seen == []


def test_disabled_with_no_hits_returns_empty_without_raising(server: Server) -> None:
    seen = server({"rankings": []})

    result = rerank_hits("fruit", [], rerank_invoke_url="disabled", model_name="m")

    assert result == []
    assert seen == []


@pytest.mark.parametrize("value", ["off", "none", "false", "disabled/v1"])
def test_only_the_exact_word_disables(server: Server, value: str) -> None:
    seen = server(
        {"rankings": [{"index": 1, "logit": 2.0}, {"index": 0, "logit": 1.0}]}
    )

    ranked = rerank_hits("fruit", _HITS, rerank_invoke_url=value, model_name="m")

    assert len(seen) == 1
    assert [h["id"] for h in ranked] == ["b", "a"]


def test_a_normal_url_still_ranks(server: Server) -> None:
    seen = server(
        {"rankings": [{"index": 1, "logit": 4.0}, {"index": 0, "logit": -2.0}]}
    )

    ranked = rerank_hits(
        "fruit",
        _HITS,
        rerank_invoke_url="https://nim.example/v1/retrieval/x/reranking",
        model_name="m",
    )

    assert len(seen) == 1
    assert [h["id"] for h in ranked] == ["b", "a"]


def test_rerank_kwargs_pass_the_disabled_endpoint_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rerank, "_RERANK_ENDPOINT", "disabled")

    assert rerank.get_rerank_kwargs()["rerank_invoke_url"] == "disabled"
