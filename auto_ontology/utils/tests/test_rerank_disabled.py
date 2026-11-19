# SPDX-License-Identifier: Apache-2.0

"""Tests for switching rerank off with ``RERANK_ENDPOINT=disabled``.

Only the word ``disabled`` (any case, surrounding whitespace ignored) disables
the reranker, and it must do so before any request leaves the process. Every
other value is upstream's to interpret: it is passed on unchanged and unchecked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from auto_ontology.utils import rerank
from auto_ontology.utils.rerank import RerankDisabled, rerank_hits

_VAR = "RERANK_ENDPOINT"
_REPO = Path(__file__).resolve().parents[3]

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

    with pytest.raises(RerankDisabled) as excinfo:
        rerank_hits("fruit", _HITS, rerank_invoke_url=value, model_name="m")

    assert seen == []
    assert "switched off" in str(excinfo.value)


def test_disabled_with_no_hits_returns_empty_without_raising(server: Server) -> None:
    seen = server({"rankings": []})

    result = rerank_hits("fruit", [], rerank_invoke_url="disabled", model_name="m")

    assert result == []
    assert seen == []


# --- The setting ------------------------------------------------------------


def _imported_endpoint(value: str | None, **extra: str) -> str:
    """What a fresh interpreter's rerank module holds with ``RERANK_ENDPOINT=value``.

    Printed with ``repr`` so surrounding whitespace survives the pipe.
    """
    env = {**os.environ, **extra}
    env.pop(_VAR, None)
    if value is not None:
        env[_VAR] = value
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from auto_ontology.utils import rerank;"
            "print(repr(rerank.get_rerank_kwargs()['rerank_invoke_url']))",
        ],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize(
    "raw",
    [
        "https://nim.example/v1/ranking",
        " http://localhost:8015 ",
        " Disabled ",
        "none",
        "localhost:8015/v1",
        "   ",
    ],
)
def test_a_set_value_is_passed_on_unchecked_and_untrimmed(raw: str) -> None:
    """As upstream: the import never fails; only ``rerank_hits`` reads ``disabled``."""
    assert _imported_endpoint(raw) == repr(raw)


@pytest.mark.parametrize("raw", [None, ""])
def test_unset_or_empty_takes_the_shared_default(raw: str | None) -> None:
    assert (
        _imported_endpoint(raw, DEFAULT_MODELS_ENDPOINT="not-checked")
        == "'not-checked'"
    )


@pytest.mark.parametrize("value", ["none", "off", "disable", "disabled/v1"])
def test_only_the_exact_word_disables(server: Server, value: str) -> None:
    """A near-miss is a URL to upstream: the request is attempted, not skipped."""
    seen = server(
        {"rankings": [{"index": 0, "logit": 1.0}, {"index": 1, "logit": 0.0}]}
    )

    rerank_hits("fruit", _HITS, rerank_invoke_url=value, model_name="m")

    assert len(seen) == 1
    assert str(seen[0].url).startswith(value)


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
