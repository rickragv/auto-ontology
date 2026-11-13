# SPDX-License-Identifier: Apache-2.0

"""A vector table of another size ends a question with its message.

``VectorSizeMismatchError`` says which sizes differ and how to rebuild. On the
question path the vector searches, the extra-table retrieval and the node
wrapper all log other errors and carry on with no candidates, so a mismatch
cost minutes and ended in an unrelated answer. They must let this one through,
and any other ``EmbeddingConfigError`` (its base class, also raised for an
invalid ``EMBED_*`` setting): the run then stops at the first search, and the
client gets the message. Every other error keeps its old handling.

No database or network: collaborators are stubbed, and where the real guard
runs it is a real ``PostgresVDB`` with its catalog lookup and store stubbed.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType, SimpleNamespace
from typing import Any, Callable, NoReturn, cast
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import auto_ontology.utils.retriever as retriever_module
import auto_ontology.vdb.postgres as postgres_module
from auto_ontology.retrieval.data_access import relevant_tables
from auto_ontology.retrieval.entity_coverage.agents.coverage import CoverageGradeAgent
from auto_ontology.retrieval.entity_coverage.agents.question_extraction import (
    QuestionExtractionAgent,
)
from auto_ontology.retrieval.text_to_sql.agents import (
    candidates_preparation,
    candidates_retrieval,
)
from auto_ontology.retrieval.text_to_sql.agents.candidates_preparation import (
    CandidatePreparationAgent,
)
from auto_ontology.retrieval.text_to_sql.agents.question_intent import (
    QuestionIntentAgent,
)
from auto_ontology.retrieval.text_to_sql.base import BaseAgent, agent_wrapper
from auto_ontology.retrieval.text_to_sql.state import AgentState, TextToSQLPayload
from auto_ontology.utils import llm_invoke
from auto_ontology.utils.embedding import EmbedParams
from auto_ontology.utils.retriever import Retriever
from auto_ontology.vdb.dimensions import (
    EmbeddingConfigError,
    VectorSizeMismatchError,
    check_vector_size,
    configured_embed_dimensions,
)
from auto_ontology.vdb.postgres import PostgresVDB

_CONFIGURED = 768
_STORED = 1024
_QUESTION = "What was revenue last year?"


def _mismatch() -> VectorSizeMismatchError:
    """The error the guard raises for a 1024-dimension table opened at 768."""
    with pytest.raises(VectorSizeMismatchError) as excinfo:
        check_vector_size("vdb", "semantic_layer", _STORED, _CONFIGURED)
    return excinfo.value


@pytest.fixture(params=["size mismatch", "bad setting"])
def permanent(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> EmbeddingConfigError:
    """An error every later search would raise again."""
    if request.param == "size mismatch":
        return _mismatch()
    monkeypatch.setenv("EMBED_DIMENSIONS", "abc")
    with pytest.raises(EmbeddingConfigError) as excinfo:
        configured_embed_dimensions()
    monkeypatch.delenv("EMBED_DIMENSIONS")
    return excinfo.value


def _raiser(exc: BaseException) -> Callable[..., NoReturn]:
    def _raise(*args: Any, **kwargs: Any) -> NoReturn:
        raise exc

    return _raise


class _RanOn(BaseException):
    """Raised by a node after candidate retrieval: the run did not stop.

    A ``BaseException`` so that no ``except Exception`` on the way out turns
    it into a logged warning; it fails the test instead.
    """


def _stub_question_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the LLM steps before candidate retrieval with fixed answers."""
    monkeypatch.setattr(
        QuestionIntentAgent,
        "execute",
        lambda self, state: {"path_state": state["path_state"]},
    )
    monkeypatch.setattr(
        QuestionExtractionAgent,
        "execute",
        lambda self, state: {
            "path_state": {**state["path_state"], "entities": ["revenue"]}
        },
    )
    monkeypatch.setattr(
        candidates_retrieval, "custom_analysis_exists", lambda target_db: False
    )


@pytest.fixture
def mismatched_retriever(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Retriever, MagicMock]:
    """A real ``Retriever`` over a real ``PostgresVDB`` whose table holds 1024.

    Returns the retriever and the ``PGVectorStore.create_sync`` mock, which a
    search reaches only if the guard lets it through.
    """
    monkeypatch.setattr(
        postgres_module, "lookup_vector_table", lambda *args: (True, _STORED)
    )
    monkeypatch.setattr(PostgresVDB, "_get_engine", lambda self: object())
    create_sync = MagicMock(name="PGVectorStore.create_sync")
    monkeypatch.setattr(postgres_module.PGVectorStore, "create_sync", create_sync)
    monkeypatch.setattr(
        retriever_module,
        "embed_texts",
        lambda texts, params, input_type, **kw: [[0.1] * _CONFIGURED for _ in texts],
    )
    vdb = PostgresVDB(
        connection_string="postgresql://x/y",
        collection_name="semantic_layer",
        schema_name="vdb",
        vector_size=_CONFIGURED,
    )
    params = EmbedParams(model_name="m", embed_invoke_url="http://embed.invalid/v1")
    return Retriever(vdb, params), create_sync


# ---------------------------------------------------------------------------
# Candidate retrieval: one search per entity and label
# ---------------------------------------------------------------------------


def test_candidate_search_lets_a_permanent_error_through(
    monkeypatch: pytest.MonkeyPatch, permanent: EmbeddingConfigError
) -> None:
    error = permanent
    monkeypatch.setattr(candidates_retrieval, "search_semantic_index", _raiser(error))

    with pytest.raises(EmbeddingConfigError) as excinfo:
        candidates_retrieval._search_by_label(object(), "revenue", "ColumnAttribute", 2)

    assert excinfo.value is error


def test_candidate_search_still_returns_nothing_on_other_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        candidates_retrieval,
        "search_semantic_index",
        _raiser(RuntimeError("endpoint down")),
    )

    with caplog.at_level(logging.WARNING, logger=candidates_retrieval.logger.name):
        hits = candidates_retrieval._search_by_label(
            object(), "revenue", "ColumnAttribute", 2
        )

    assert hits == []
    assert any("search failed for entity" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Table search
# ---------------------------------------------------------------------------


def test_table_search_lets_a_permanent_error_through(
    monkeypatch: pytest.MonkeyPatch, permanent: EmbeddingConfigError
) -> None:
    error = permanent
    monkeypatch.setattr(relevant_tables, "search_semantic_index", _raiser(error))

    with pytest.raises(EmbeddingConfigError) as excinfo:
        relevant_tables.get_relevant_tables(object(), "orders")

    assert excinfo.value is error


def test_table_search_still_returns_nothing_on_other_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        relevant_tables, "search_semantic_index", _raiser(RuntimeError("endpoint down"))
    )

    with caplog.at_level(logging.ERROR, logger=relevant_tables.logger.name):
        assert relevant_tables.get_relevant_tables(object(), "orders") == []

    assert any(
        "get_relevant_tables: vector search failed" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Candidate preparation: the extra-table searches and their background thread
# ---------------------------------------------------------------------------


def test_extra_table_search_lets_a_permanent_error_through(
    monkeypatch: pytest.MonkeyPatch, permanent: EmbeddingConfigError
) -> None:
    error = permanent
    monkeypatch.setattr(candidates_preparation, "get_relevant_tables", _raiser(error))

    with pytest.raises(EmbeddingConfigError) as excinfo:
        CandidatePreparationAgent()._retrieve_additional_tables(
            object(), "q", ["e1"], "db"
        )

    assert excinfo.value is error


def test_extra_table_search_still_skips_a_failed_query(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fake_get_relevant_tables(
        retriever: object,
        query: str,
        k: int = 1,
        database_name: str | None = None,
        **kwargs: object,
    ) -> list[dict]:
        if query == "e1":
            raise RuntimeError("endpoint down")
        return [{"id": query, "name": query}]

    monkeypatch.setattr(
        candidates_preparation, "get_relevant_tables", fake_get_relevant_tables
    )

    with caplog.at_level(logging.WARNING):
        out = CandidatePreparationAgent()._retrieve_additional_tables(
            object(), "q", ["e1"], "db"
        )

    assert [t["id"] for t in out] == ["q"]
    assert any(
        "Table retrieval failed for query: e1" in r.getMessage() for r in caplog.records
    )


@pytest.fixture
def preparation(monkeypatch: pytest.MonkeyPatch) -> Callable[[BaseException], Any]:
    """Run candidate preparation while its extra-table retrieval raises."""
    monkeypatch.setattr(
        CandidatePreparationAgent,
        "_filter_tables_by_relevance",
        lambda self, state, question, tables, custom_analyses=None, attribute_join_paths=None: (
            tables,
            "",
        ),
    )
    mod = candidates_preparation
    monkeypatch.setattr(mod, "fetch_custom_analyses_with_sql", lambda ids: [])
    monkeypatch.setattr(mod, "get_relevant_tables_from_candidates", lambda c: [])
    monkeypatch.setattr(mod, "fetch_tables_by_ids", lambda ids: [])
    monkeypatch.setattr(mod, "find_connected_junction_tables", lambda ids: ([], []))
    monkeypatch.setattr(
        mod, "find_kept_table_bridges", lambda kept_ids, candidate_ids: ([], [], 0)
    )

    def run(error: BaseException) -> Any:
        monkeypatch.setattr(
            CandidatePreparationAgent, "_retrieve_additional_tables", _raiser(error)
        )
        state = cast(
            AgentState,
            {
                "initial_question": "Count orders",
                "data_retriever": object(),
                "path_state": {"target_db": "db"},
            },
        )
        return CandidatePreparationAgent().execute(state)

    return run


def test_preparation_lets_a_permanent_error_through(
    preparation: Callable[[BaseException], Any], permanent: EmbeddingConfigError
) -> None:
    error = permanent

    with pytest.raises(EmbeddingConfigError) as excinfo:
        preparation(error)

    assert excinfo.value is error


def test_preparation_still_goes_on_without_extra_tables_otherwise(
    preparation: Callable[[BaseException], Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = preparation(RuntimeError("endpoint down"))

    assert result["path_state"]["relevant_tables"] == []
    assert any(
        "Additional table retrieval failed" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# The node wrapper every graph node runs in
# ---------------------------------------------------------------------------


class _RaisingAgent(BaseAgent):
    def __init__(self, error: BaseException, failure_decision: str | None) -> None:
        super().__init__("raising", failure_decision=failure_decision)
        self.error = error

    def execute(self, state: AgentState) -> dict[str, Any]:
        raise self.error


@pytest.mark.parametrize("failure_decision", [None, "unconstructable"])
def test_node_wrapper_lets_a_permanent_error_through(
    failure_decision: str | None, permanent: EmbeddingConfigError
) -> None:
    error = permanent
    wrapped = agent_wrapper(_RaisingAgent(error, failure_decision))

    with pytest.raises(EmbeddingConfigError) as excinfo:
        wrapped(cast(AgentState, {"path_state": {}}))

    assert excinfo.value is error


@pytest.mark.parametrize("failure_decision", [None, "unconstructable"])
def test_node_wrapper_still_records_other_errors_in_the_state(
    failure_decision: str | None,
) -> None:
    error = RuntimeError("endpoint down")
    wrapped = agent_wrapper(_RaisingAgent(error, failure_decision))

    result = wrapped(cast(AgentState, {"path_state": {}}))

    assert result["path_state"]["error"] == {
        "type": "RuntimeError",
        "message": "endpoint down",
        "agent": "raising",
    }
    assert result.get("decision") == failure_decision


# ---------------------------------------------------------------------------
# End to end, from the real guard: the chat stream and the coverage route
# ---------------------------------------------------------------------------


@pytest.fixture(name="main")
def main_fixture(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the agent entry point without a configured LLM.

    ``main`` builds its LLM clients and compiles the real graph at import.
    """
    monkeypatch.setattr(llm_invoke, "get_llm_client", lambda **_kwargs: None)
    monkeypatch.setattr(
        llm_invoke, "get_non_reasoning_llm_client", lambda **_kwargs: None
    )
    return importlib.import_module("auto_ontology.retrieval.text_to_sql.main")


def test_chat_stream_ends_at_candidate_retrieval_with_the_message(
    main: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    mismatched_retriever: tuple[Retriever, MagicMock],
) -> None:
    """Through the real compiled graph, wrapper, thread pool, search and guard.

    ``stream_agent_response`` is what the chat worker relays: its ``error``
    event goes to the client unchanged as an SSE ``data:`` frame.
    """
    retriever, create_sync = mismatched_retriever
    _stub_question_steps(monkeypatch)
    monkeypatch.setattr(CandidatePreparationAgent, "execute", _raiser(_RanOn()))
    state = {
        "llm": None,
        "initial_question": _QUESTION,
        "messages": [],
        "decision": "",
        "path_state": {"target_db": "warehouse"},
        "data_retriever": retriever,
        "semantic_retriever": retriever,
    }
    monkeypatch.setattr(main, "_build_state", lambda payload: state)

    events = list(
        main.stream_agent_response(cast(TextToSQLPayload, {"question": _QUESTION}))
    )

    assert events[-1] == {
        "type": "error",
        "message": f"Agent failed after retrieve_candidates: {_mismatch()}",
        "node": "retrieve_candidates",
        "error_type": "VectorSizeMismatchError",
        "partial_answer": {},
    }
    assert not [e for e in events if e["type"] == "result"]
    create_sync.assert_not_called()


def test_coverage_route_answers_422_with_the_message(
    monkeypatch: pytest.MonkeyPatch,
    mismatched_retriever: tuple[Retriever, MagicMock],
) -> None:
    """``POST /api/question-entity-coverage`` runs the same candidate retrieval."""
    retriever, _ = mismatched_retriever
    monkeypatch.setattr(llm_invoke, "get_llm_client", lambda **_kwargs: None)
    coverage_main = importlib.import_module(
        "auto_ontology.retrieval.entity_coverage.main"
    )
    service = importlib.import_module("auto_ontology.server.metadata.service")
    router = importlib.import_module("auto_ontology.server.metadata.router")

    _stub_question_steps(monkeypatch)
    monkeypatch.setattr(CoverageGradeAgent, "execute", _raiser(_RanOn()))
    monkeypatch.setattr(coverage_main, "llm_client", object())
    monkeypatch.setattr(coverage_main, "fetch_custom_analyses", lambda: [])
    monkeypatch.setattr(service, "coverage_llm_client", object())
    monkeypatch.setattr(
        service,
        "_build_coverage_payload",
        lambda question, max_distance: {
            "question": question,
            "data_retriever": retriever,
            "semantic_retriever": retriever,
            "connectors": [SimpleNamespace(database_name="warehouse")],
            "acronyms": [],
            "max_distance": max_distance,
        },
    )

    with pytest.raises(HTTPException) as excinfo:
        router.entity_coverage(router.EntityCoverageRequest(question=_QUESTION))

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == str(_mismatch())
