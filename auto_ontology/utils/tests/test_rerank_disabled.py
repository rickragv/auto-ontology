# SPDX-License-Identifier: Apache-2.0

"""Tests for switching rerank off with ``RERANK_ENDPOINT=disabled``.

Only the word ``disabled`` (any case, surrounding whitespace ignored) disables
the reranker, and it must do so before any request leaves the process. Any
other set value must be an ``http://`` or ``https://`` URL: anything else would
fail on every call, and each caller would quietly take its unranked order, so
it stops the import instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from auto_ontology.utils import model_config, rerank
from auto_ontology.utils.rerank import RerankDisabled, rerank_hits
from auto_ontology.utils.rerank_endpoint import resolve_rerank_endpoint

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


@pytest.mark.parametrize("raw", [None, ""])
def test_unset_or_empty_resolves_exactly_as_upstream(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    """Including a shared default that is not a URL: that one is upstream's."""
    monkeypatch.setenv("DEFAULT_MODELS_ENDPOINT", "not-checked")
    if raw is None:
        monkeypatch.delenv(_VAR, raising=False)
    else:
        monkeypatch.setenv(_VAR, raw)

    assert resolve_rerank_endpoint() == model_config.resolve("RERANK", "ENDPOINT")
    assert resolve_rerank_endpoint() == "not-checked"


@pytest.mark.parametrize("raw", ["disabled", " Disabled ", "DISABLED"])
def test_disabled_in_any_case_resolves_to_disabled(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(_VAR, raw)

    assert resolve_rerank_endpoint() == "disabled"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://nim.example/v1/ranking", "https://nim.example/v1/ranking"),
        (" http://localhost:8015 ", "http://localhost:8015"),
        ("HTTPS://nim.example/x", "HTTPS://nim.example/x"),
        ("https://disabled.example/v1", "https://disabled.example/v1"),
    ],
)
def test_a_url_is_kept_without_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv(_VAR, raw)

    assert resolve_rerank_endpoint() == expected


@pytest.mark.parametrize(
    "raw",
    [
        "off",
        "none",
        "false",
        "disable",
        "disabled/v1",
        "localhost:8015/v1",
        "ftp://nim.example",
        "   ",
    ],
)
def test_any_other_value_is_refused_naming_the_setting_and_the_choices(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(_VAR, raw)

    with pytest.raises(ValueError) as excinfo:
        resolve_rerank_endpoint()

    message = str(excinfo.value)
    assert message.startswith(f"{_VAR} must be")
    assert repr(raw) in message
    for accepted in ("http://", "https://", "'disabled'", "unset"):
        assert accepted in message


def _import_rerank(value: str) -> subprocess.CompletedProcess[str]:
    """Import the module in a fresh interpreter with ``RERANK_ENDPOINT=value``."""
    env = {**os.environ, _VAR: value}
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from auto_ontology.utils import rerank; print(rerank._RERANK_ENDPOINT)",
        ],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_bad_value_stops_the_import() -> None:
    result = _import_rerank("none")

    assert result.returncode != 0
    assert "ValueError: RERANK_ENDPOINT must be" in result.stderr


def test_a_good_value_is_what_the_module_uses() -> None:
    result = _import_rerank(" Disabled ")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "disabled"


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
