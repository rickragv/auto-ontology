# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``EMBED_BATCH_SIZE`` and ``EMBED_MAX_CONCURRENT`` settings.

They size the embedding requests and the number in flight, so a provider with
a small quota can be throttled. Unset, both keep their old value of 32. The
endpoint is an ``httpx.MockTransport`` under the real client, as in
``test_embedding.py``.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import httpx
import pytest
from openai import OpenAI

from auto_ontology.utils import embedding
from auto_ontology.utils.embedding import (
    EmbedParams,
    embed_chunk_size,
    embed_texts,
    get_embed_params,
)
from auto_ontology.vdb.dimensions import EmbeddingConfigError

BATCH_VAR = "EMBED_BATCH_SIZE"
CONCURRENT_VAR = "EMBED_MAX_CONCURRENT"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own settings out of every test."""
    monkeypatch.delenv(BATCH_VAR, raising=False)
    monkeypatch.delenv(CONCURRENT_VAR, raising=False)


def _params(**overrides: int) -> EmbedParams:
    return EmbedParams(
        model_name="nvidia/embed",
        embed_invoke_url="https://embed.example/v1",
        api_key="sk-test",
        **overrides,
    )


def _texts(count: int) -> list[str]:
    return [f"t{i}" for i in range(count)]


def _vector(text: str) -> list[float]:
    """A vector that encodes which input text it answers."""
    return [float(text.removeprefix("t"))]


def _respond(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    data = [
        {"object": "embedding", "index": i, "embedding": _vector(text)}
        for i, text in enumerate(inputs)
    ]
    return httpx.Response(200, json={"object": "list", "data": data, "model": "m"})


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], list[int]]:
    """Route the client through *handler*; returns the size of each request."""

    def _install(handler: Handler) -> list[int]:
        sizes: list[int] = []
        lock = threading.Lock()

        def _record(request: httpx.Request) -> httpx.Response:
            with lock:
                sizes.append(len(json.loads(request.content)["input"]))
            return handler(request)

        def _client(params: EmbedParams) -> OpenAI:
            transport = httpx.MockTransport(_record)
            return embedding._new_client(params, httpx.Client(transport=transport))

        monkeypatch.setattr(embedding, "_client", _client)
        return sizes

    return _install


class _Gauge:
    """A handler that records the most requests ever in flight."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now = 0
        self.peak = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self._now += 1
            self.peak = max(self.peak, self._now)
        try:
            time.sleep(0.05)
            return _respond(request)
        finally:
            with self._lock:
                self._now -= 1


@pytest.fixture
def pool_sizes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The ``max_workers`` of every executor ``embed_texts`` creates."""
    sizes: list[int] = []

    def _pool(max_workers: int) -> ThreadPoolExecutor:
        sizes.append(max_workers)
        return ThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr(embedding, "ThreadPoolExecutor", _pool)
    return sizes


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_settings_keep_the_old_defaults(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        monkeypatch.setenv(BATCH_VAR, value)
        monkeypatch.setenv(CONCURRENT_VAR, value)

    for params in (_params(), get_embed_params()):
        assert params.inference_batch_size == 32
        assert params.nim_http_max_concurrent == 32
        assert embed_chunk_size(params) == 1024


def test_seventy_texts_go_out_in_requests_of_32_32_and_6(endpoint) -> None:
    sizes = endpoint(_respond)

    vectors = embed_texts(_texts(70), _params(), "passage")

    assert sorted(sizes, reverse=True) == [32, 32, 6]
    assert vectors == [_vector(t) for t in _texts(70)]


# ---------------------------------------------------------------------------
# Honoured
# ---------------------------------------------------------------------------


def test_the_settings_size_requests_and_cap_concurrency(
    monkeypatch: pytest.MonkeyPatch, endpoint, pool_sizes: list[int]
) -> None:
    monkeypatch.setenv(BATCH_VAR, "5")
    monkeypatch.setenv(CONCURRENT_VAR, "2")
    gauge = _Gauge()
    sizes = endpoint(gauge)

    params = _params()
    vectors = embed_texts(_texts(23), params, "passage")

    assert (params.inference_batch_size, params.nim_http_max_concurrent) == (5, 2)
    assert embed_chunk_size(params) == 10
    assert sorted(sizes) == [3, 5, 5, 5, 5]
    assert pool_sizes == [2]
    assert 1 <= gauge.peak <= 2
    assert vectors == [_vector(t) for t in _texts(23)]


def test_one_request_at_a_time_when_concurrency_is_one(
    monkeypatch: pytest.MonkeyPatch, endpoint
) -> None:
    monkeypatch.setenv(BATCH_VAR, "4")
    monkeypatch.setenv(CONCURRENT_VAR, "1")
    gauge = _Gauge()
    sizes = endpoint(gauge)

    vectors = embed_texts(_texts(13), _params(), "passage")

    assert sorted(sizes) == [1, 4, 4, 4]
    assert gauge.peak == 1
    assert vectors == [_vector(t) for t in _texts(13)]


def test_the_environment_is_read_each_time_params_are_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BATCH_VAR, "7")
    first = _params()
    monkeypatch.setenv(BATCH_VAR, "9")
    second = _params()
    monkeypatch.delenv(BATCH_VAR)
    third = _params()

    assert first.inference_batch_size == 7
    assert second.inference_batch_size == 9
    assert third.inference_batch_size == 32


def test_explicit_arguments_beat_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BATCH_VAR, "5")
    monkeypatch.setenv(CONCURRENT_VAR, "2")

    params = _params(inference_batch_size=11, nim_http_max_concurrent=3)

    assert params.inference_batch_size == 11
    assert params.nim_http_max_concurrent == 3


def test_whitespace_around_the_number_is_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BATCH_VAR, "  8 ")
    monkeypatch.setenv(CONCURRENT_VAR, "\t4\n")

    params = _params()

    assert params.inference_batch_size == 8
    assert params.nim_http_max_concurrent == 4


def test_setting_one_variable_leaves_the_other_at_its_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BATCH_VAR, "6")

    params = _params()

    assert params.inference_batch_size == 6
    assert params.nim_http_max_concurrent == 32


# ---------------------------------------------------------------------------
# Rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [BATCH_VAR, CONCURRENT_VAR])
@pytest.mark.parametrize("bad", ["abc", "0", "-3", "2.5", "1_024", "+8", "٣", "８"])
def test_a_bad_value_is_rejected_naming_the_variable_and_the_value(
    monkeypatch: pytest.MonkeyPatch, name: str, bad: str
) -> None:
    monkeypatch.setenv(name, bad)

    with pytest.raises(EmbeddingConfigError) as caught:
        _params()

    assert str(caught.value) == (
        f"{name} must be a whole number of 1 or more in plain digits, or unset "
        f"for 32; got {bad!r}."
    )


def test_an_explicit_argument_still_avoids_a_bad_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BATCH_VAR, "abc")

    params = _params(inference_batch_size=4)

    assert params.inference_batch_size == 4
