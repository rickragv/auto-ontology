# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``EMBED_EXTRA_PARAMS`` setting.

``nvidia`` (the default) keeps the ``input_type``, ``truncate`` and ``modality``
body fields; ``none`` sends a plain OpenAI embeddings body. The setting is read
when an ``EmbedParams`` is built, and an explicit ``nvidia_fields`` argument
wins over it. The endpoint is an ``httpx.MockTransport`` under the real client,
as in ``test_embedding.py``.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest
from openai import OpenAI

from auto_ontology.utils import embedding
from auto_ontology.utils.embedding import EmbedParams, embed_texts, get_embed_params
from auto_ontology.vdb.dimensions import EmbeddingConfigError

VAR = "EMBED_EXTRA_PARAMS"

Handler = Callable[[httpx.Request], httpx.Response]

NVIDIA_KEYS = {
    "model",
    "input",
    "encoding_format",
    "input_type",
    "truncate",
    "modality",
}
PLAIN_KEYS = {"model", "input", "encoding_format"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own settings out of every test."""
    for name in (VAR, "EMBED_BATCH_SIZE", "EMBED_MAX_CONCURRENT"):
        monkeypatch.delenv(name, raising=False)


def _params(**overrides: Any) -> EmbedParams:
    fields: dict[str, Any] = {
        "model_name": "acme/embed",
        "embed_invoke_url": "https://embed.example/v1",
        "api_key": "sk-test",
    }
    fields.update(overrides)
    return EmbedParams(**fields)


def _vector(text: str) -> list[float]:
    return [float(len(text)), float(ord(text[0]))]


def _ok(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    data = [
        {"object": "embedding", "index": i, "embedding": _vector(text)}
        for i, text in enumerate(inputs)
    ]
    return httpx.Response(200, json={"object": "list", "data": data, "model": "m"})


@pytest.fixture
def endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], list[httpx.Request]]:
    """Route the module's client through a recording stub; returns the requests."""

    def _install() -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def _record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _ok(request)

        def _client(params: EmbedParams) -> OpenAI:
            transport = httpx.MockTransport(_record)
            return embedding._new_client(params, httpx.Client(transport=transport))

        monkeypatch.setattr(embedding, "_client", _client)
        return seen

    return _install


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


@pytest.mark.parametrize("input_type", ["passage", "query"])
@pytest.mark.parametrize("value", [None, "", "   ", "nvidia", " Nvidia "])
def test_the_default_keeps_the_nvidia_fields(
    endpoint, monkeypatch: pytest.MonkeyPatch, value: str | None, input_type: str
) -> None:
    seen = endpoint()
    if value is not None:
        monkeypatch.setenv(VAR, value)

    embed_texts(["orders"], _params(), input_type)

    body = _body(seen[0])
    assert set(body) == NVIDIA_KEYS
    assert body["input_type"] == input_type
    assert body["truncate"] == "END"
    assert body["modality"] == "text"


@pytest.mark.parametrize("input_type", ["passage", "query"])
@pytest.mark.parametrize("value", ["none", " NONE ", "None"])
def test_none_sends_a_plain_openai_body(
    endpoint, monkeypatch: pytest.MonkeyPatch, value: str, input_type: str
) -> None:
    seen = endpoint()
    monkeypatch.setenv(VAR, value)

    vectors = embed_texts(["orders", "invoices"], _params(), input_type)

    assert vectors == [_vector("orders"), _vector("invoices")]
    assert _body(seen[0]) == {
        "model": "acme/embed",
        "input": ["orders", "invoices"],
        "encoding_format": "float",
    }


def test_an_explicit_true_beats_none_in_the_environment(
    endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = endpoint()
    monkeypatch.setenv(VAR, "none")

    embed_texts(["orders"], _params(nvidia_fields=True), "query")

    assert set(_body(seen[0])) == NVIDIA_KEYS


def test_an_explicit_false_beats_the_default(endpoint) -> None:
    seen = endpoint()

    embed_texts(["orders"], _params(nvidia_fields=False), "query")

    assert set(_body(seen[0])) == PLAIN_KEYS


def test_the_setting_is_read_when_the_params_are_built(
    endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = endpoint()
    before = _params()
    monkeypatch.setenv(VAR, "none")
    after = _params()

    embed_texts(["orders"], before, "passage")
    embed_texts(["orders"], after, "passage")

    assert set(_body(seen[0])) == NVIDIA_KEYS
    assert set(_body(seen[1])) == PLAIN_KEYS


@pytest.mark.parametrize("value", ["bogus", "openai", "nvidia,none", "0"])
def test_a_bad_value_is_rejected_with_the_allowed_choices(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(VAR, value)

    for build in (_params, get_embed_params):
        with pytest.raises(EmbeddingConfigError) as caught:
            build()
        assert str(caught.value) == (
            f"{VAR} must be 'nvidia' or 'none' (any case), or unset for "
            f"'nvidia'; got {value!r}."
        )


@pytest.mark.parametrize("value", [None, "none"])
@pytest.mark.parametrize("input_type", ["passage", "query"])
def test_the_text_prefix_still_applies(
    endpoint, monkeypatch: pytest.MonkeyPatch, value: str | None, input_type: str
) -> None:
    seen = endpoint()
    if value is not None:
        monkeypatch.setenv(VAR, value)

    embed_texts(
        ["orders"],
        _params(model_name="nvidia/nvidia/nemotron-3.5-embed-1b-nvfp4"),
        input_type,
    )

    assert _body(seen[0])["input"] == [f"{input_type}: orders"]


@pytest.mark.parametrize("value", [None, "none"])
def test_a_keyless_endpoint_sends_no_authorization_header(
    endpoint, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    seen = endpoint()
    if value is not None:
        monkeypatch.setenv(VAR, value)

    embed_texts(["orders"], _params(api_key=""), "query")

    assert "authorization" not in seen[0].headers
