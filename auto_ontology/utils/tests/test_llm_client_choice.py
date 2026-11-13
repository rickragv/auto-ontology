# SPDX-License-Identifier: Apache-2.0

"""Tests for choosing the chat client class and the optional reasoning effort."""

from typing import Any

import pytest
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langchain_openai import ChatOpenAI

from auto_ontology.utils import llm_invoke
from auto_ontology.utils.llm_invoke import _build_client, _resolve_client_kind

# A local, closed port: ChatNVIDIA only probes hosted URLs at construction, so
# nothing here touches the network.
_LOCAL_URL = "http://127.0.0.1:9/v1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test independent of the developer's or CI's environment."""
    for name in (
        "REASONING_CLIENT",
        "NON_REASONING_CLIENT",
        "DEFAULT_MODELS_CLIENT",
        "LLM_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)


def _build(model: str, **kwargs: Any) -> Any:
    return _build_client(
        model=model,
        api_key="test-key",
        base_url=_LOCAL_URL,
        temperature=0.0,
        max_tokens=16,
        **kwargs,
    )


def _payload(client: ChatOpenAI) -> dict[str, Any]:
    return client._get_request_payload("hello")


# --- Defaults are unchanged -------------------------------------------------


def test_client_kind_defaults_to_auto_for_both_roles() -> None:
    assert _resolve_client_kind("REASONING") == "auto"
    assert _resolve_client_kind("NON_REASONING") == "auto"


@pytest.mark.parametrize("model", ["gemini/x", "ollama/x", "nvidia/some-model"])
def test_auto_sends_non_openai_family_names_to_nvidia(model: str) -> None:
    assert isinstance(_build(model), ChatNVIDIA)


@pytest.mark.parametrize("model", ["openai/x", "azure/x", "aws/x"])
def test_auto_sends_openai_family_names_to_openai(model: str) -> None:
    assert isinstance(_build(model), ChatOpenAI)
    assert isinstance(_build(model, client_kind="auto"), ChatOpenAI)


# --- The setting is honoured ------------------------------------------------


@pytest.mark.parametrize("model", ["ollama/x", "gemini/x", "plain-name"])
def test_openai_kind_forces_openai_for_any_model_name(model: str) -> None:
    assert isinstance(_build(model, client_kind="openai"), ChatOpenAI)


@pytest.mark.parametrize("model", ["openai/x", "azure/x", "aws/x"])
def test_nvidia_kind_forces_nvidia_even_for_openai_family_names(model: str) -> None:
    assert isinstance(_build(model, client_kind="nvidia"), ChatNVIDIA)


def test_role_specific_setting_beats_the_shared_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEFAULT_MODELS_CLIENT", "nvidia")
    monkeypatch.setenv("REASONING_CLIENT", "openai")

    assert _resolve_client_kind("REASONING") == "openai"


def test_shared_default_applies_to_both_roles_when_specific_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEFAULT_MODELS_CLIENT", "openai")

    assert _resolve_client_kind("REASONING") == "openai"
    assert _resolve_client_kind("NON_REASONING") == "openai"


def test_one_role_can_differ_from_the_other(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REASONING_CLIENT", "openai")
    monkeypatch.setenv("NON_REASONING_CLIENT", "nvidia")

    assert _resolve_client_kind("REASONING") == "openai"
    assert _resolve_client_kind("NON_REASONING") == "nvidia"


def test_setting_for_one_role_leaves_the_other_on_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REASONING_CLIENT", "openai")

    assert _resolve_client_kind("NON_REASONING") == "auto"


@pytest.mark.parametrize("raw", ["  OpenAI  ", "OPENAI", "openai"])
def test_value_is_stripped_and_lowercased(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("REASONING_CLIENT", raw)

    assert _resolve_client_kind("REASONING") == "openai"


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_value_means_auto(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("REASONING_CLIENT", raw)

    assert _resolve_client_kind("REASONING") == "auto"


@pytest.mark.parametrize("raw", ["", "   "])
def test_a_blank_role_setting_falls_back_to_the_shared_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("REASONING_CLIENT", raw)
    monkeypatch.setenv("DEFAULT_MODELS_CLIENT", "openai")

    assert _resolve_client_kind("REASONING") == "openai"


def test_a_role_setting_is_used_without_reading_the_shared_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REASONING_CLIENT", "nvidia")
    monkeypatch.setenv("DEFAULT_MODELS_CLIENT", "bogus")

    assert _resolve_client_kind("REASONING") == "nvidia"


# --- Bad values are rejected ------------------------------------------------


def test_unknown_value_raises_an_error_naming_the_setting_and_the_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NON_REASONING_CLIENT", "anthropic")

    with pytest.raises(ValueError) as excinfo:
        _resolve_client_kind("NON_REASONING")

    assert str(excinfo.value) == (
        "NON_REASONING_CLIENT must be 'auto', 'openai' or 'nvidia' (any case), "
        "or unset for DEFAULT_MODELS_CLIENT; got 'anthropic'."
    )


@pytest.mark.parametrize("role", ["", "   "])
def test_an_unknown_shared_default_is_named_as_the_variable_at_fault(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    monkeypatch.setenv("REASONING_CLIENT", role)
    monkeypatch.setenv("DEFAULT_MODELS_CLIENT", " bogus ")

    with pytest.raises(ValueError) as excinfo:
        _resolve_client_kind("REASONING")

    assert str(excinfo.value) == (
        "DEFAULT_MODELS_CLIENT must be 'auto', 'openai' or 'nvidia' (any case), "
        "or unset for 'auto'; got ' bogus '."
    )


# --- LLM_REASONING_EFFORT ---------------------------------------------------


def test_reasoning_effort_is_absent_from_the_request_when_unset() -> None:
    assert "reasoning_effort" not in _payload(_build("openai/x"))


def test_reasoning_effort_is_absent_from_the_request_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")

    assert "reasoning_effort" not in _payload(_build("openai/x"))


def test_reasoning_effort_is_sent_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REASONING_EFFORT", "none")

    payload = _payload(_build("ollama/x", client_kind="openai"))

    assert payload["reasoning_effort"] == "none"


def test_reasoning_effort_is_read_on_every_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _build("openai/x")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    second = _build("openai/x")

    assert "reasoning_effort" not in _payload(first)
    assert _payload(second)["reasoning_effort"] == "low"


def test_reasoning_effort_is_not_applied_to_nvidia_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_REASONING_EFFORT", "none")

    assert isinstance(_build("openai/x", client_kind="nvidia"), ChatNVIDIA)


# --- Wiring -----------------------------------------------------------------


@pytest.fixture
def _recorded_builds(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_build(**kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(llm_invoke, "_build_client", fake_build)
    monkeypatch.setattr(llm_invoke, "_API_KEY", "key")
    monkeypatch.setattr(llm_invoke, "_NON_REASONING_API_KEY", "key")
    monkeypatch.setattr(llm_invoke, "_remember_non_reasoning", lambda client: None)
    return calls


def test_reasoning_client_passes_its_client_kind_through(
    monkeypatch: pytest.MonkeyPatch, _recorded_builds: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(llm_invoke, "_CLIENT_KIND", "openai")
    monkeypatch.setattr(llm_invoke, "_NON_REASONING_CLIENT_KIND", "nvidia")

    llm_invoke.get_llm_client()

    assert [c["client_kind"] for c in _recorded_builds] == ["openai"]


def test_non_reasoning_client_passes_its_client_kind_through(
    monkeypatch: pytest.MonkeyPatch, _recorded_builds: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(llm_invoke, "_CLIENT_KIND", "openai")
    monkeypatch.setattr(llm_invoke, "_NON_REASONING_CLIENT_KIND", "nvidia")

    llm_invoke.get_non_reasoning_llm_client()

    assert [c["client_kind"] for c in _recorded_builds] == ["nvidia"]
