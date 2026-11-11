# SPDX-License-Identifier: Apache-2.0

"""Tests for bridge-owner selection when rerank is disabled.

Disabled must give the same owner as a failing endpoint, without logging an
error for what is a deliberate setting.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from auto_ontology.semantic import bridge_tables

_DESCRIPTION = "the customer who placed the order"


def _terms(first_name: str = "Customer") -> list[dict[str, str]]:
    return [
        {"id": "t1", "name": first_name, "description": "a buyer"},
        {"id": "t2", "name": "Product", "description": "a thing sold"},
    ]


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Configure rerank as disabled and forbid any HTTP; returns urls attempted."""
    attempts: list[str] = []

    def _no_http(*args: Any, **kwargs: Any) -> httpx.Response:
        attempts.append("post")
        raise AssertionError("an HTTP request was attempted")

    monkeypatch.setattr(httpx, "post", _no_http)
    monkeypatch.setattr(httpx.Client, "send", _no_http)
    monkeypatch.setattr(
        bridge_tables,
        "get_rerank_kwargs",
        lambda: {
            "rerank_invoke_url": "disabled",
            "model_name": "m",
            "api_key": "",
        },
    )
    return attempts


def test_disabled_takes_the_first_term_with_one_info_line(
    disabled: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=bridge_tables.logger.name):
        owner = bridge_tables._pick_owner_term_id(_terms(), _DESCRIPTION)

    assert owner == "t1"
    assert disabled == []
    info = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info) == 1
    assert "rerank disabled" in info[0].getMessage().lower()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_disabled_ignores_a_blank_first_term_and_still_takes_it(
    disabled: list[str],
) -> None:
    terms = [
        {"id": "t1", "name": "", "description": ""},
        {"id": "t2", "name": "Product", "description": "a thing sold"},
    ]

    assert bridge_tables._pick_owner_term_id(terms, _DESCRIPTION) == "t1"
    assert disabled == []


def test_disabled_matches_the_failing_endpoint_fallback_minus_the_error(
    disabled: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    disabled_owner = bridge_tables._pick_owner_term_id(_terms(), _DESCRIPTION)

    def _boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(bridge_tables, "rerank_hits", _boom)
    with caplog.at_level(logging.DEBUG, logger=bridge_tables.logger.name):
        failing_owner = bridge_tables._pick_owner_term_id(_terms(), _DESCRIPTION)

    assert failing_owner == disabled_owner == "t1"
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_an_enabled_ranking_still_picks_the_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge_tables,
        "get_rerank_kwargs",
        lambda: {"rerank_invoke_url": "https://r.example", "model_name": "m"},
    )
    monkeypatch.setattr(
        bridge_tables,
        "rerank_hits",
        lambda query, hits, **kw: [
            {**hits[1], "_rerank_score": 0.9},
            {**hits[0], "_rerank_score": 0.1},
        ],
    )

    assert bridge_tables._pick_owner_term_id(_terms(), _DESCRIPTION) == "t2"
