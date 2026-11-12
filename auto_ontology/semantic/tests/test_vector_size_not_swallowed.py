# SPDX-License-Identifier: Apache-2.0

"""A vector table of another size fails compilation and saves visibly.

``VectorSizeMismatchError`` says which sizes differ and how to rebuild. The
broad handlers on the compilation and save paths log other errors and carry on;
they must let this one through, or the catalog fills with rows that have no
vector and nothing reports it. Every other error keeps its old handling.

No database or network: collaborators are stubbed, and where the real guard
runs it is a real ``PostgresVDB`` with its catalog lookup and store stubbed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, NoReturn
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import auto_ontology.vdb.postgres as postgres_module
from auto_ontology.catalog.constants import Labels
from auto_ontology.semantic import bridge_tables, pipeline, sql_attribute_suggester
from auto_ontology.semantic.embed import SemanticEmbedder
from auto_ontology.server.datasources import service as datasources_service
from auto_ontology.server.sql_attributes import service as sql_attr_service
from auto_ontology.server.terms import router as terms_router
from auto_ontology.utils import embedding as embedding_module
from auto_ontology.utils.embedding import EmbedParams
from auto_ontology.vdb.dimensions import VectorSizeMismatchError, check_vector_size
from auto_ontology.vdb.postgres import PostgresVDB

_CONFIGURED = 768
_STORED = 1024


def _mismatch() -> VectorSizeMismatchError:
    """The error the guard raises for a 1024-dimension table opened at 768."""
    with pytest.raises(VectorSizeMismatchError) as excinfo:
        check_vector_size("vdb", "semantic_layer", _STORED, _CONFIGURED)
    return excinfo.value


def _raiser(exc: BaseException) -> Callable[..., NoReturn]:
    def _raise(*args: Any, **kwargs: Any) -> NoReturn:
        raise exc

    return _raise


def _params() -> EmbedParams:
    return EmbedParams(model_name="m", embed_invoke_url="http://embed.invalid/v1")


class _Guard:
    """Real ``PostgresVDB`` instances over a stubbed table of a chosen size."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, stored: int) -> None:
        monkeypatch.setattr(
            postgres_module, "lookup_vector_table", lambda *args: (True, stored)
        )
        monkeypatch.setattr(PostgresVDB, "_get_engine", lambda self: object())
        self.create_sync = MagicMock(name="PGVectorStore.create_sync")
        monkeypatch.setattr(
            postgres_module.PGVectorStore, "create_sync", self.create_sync
        )

    def vdb(self) -> PostgresVDB:
        return PostgresVDB(
            connection_string="postgresql://x/y",
            collection_name="semantic_layer",
            schema_name="vdb",
            vector_size=_CONFIGURED,
        )


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], _Guard]:
    def install(stored: int) -> _Guard:
        return _Guard(monkeypatch, stored)

    return install


# ---------------------------------------------------------------------------
# Compilation, Terms: the guard runs before any table, outside every handler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored", [_STORED, _CONFIGURED])
def test_compilation_checks_the_size_before_any_table(
    guard: Callable[[int], _Guard], monkeypatch: pytest.MonkeyPatch, stored: int
) -> None:
    """Why the per-table handlers in pipeline and visit_enter need no change.

    ``compile_semantic_layer`` opens the embedder's store before the tables, and
    the store it keeps is reused by every inline Term embed, so a mismatch fails
    the whole compilation here and never reaches a per-table handler.
    """
    embedder = SemanticEmbedder(
        database_name="warehouse", embed_params=_params(), vdb=guard(stored).vdb()
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_all_tables_without_term",
        lambda database_name: [{"id": "t1", "name": "orders"}],
    )
    monkeypatch.setattr(pipeline, "is_distinct_value_probing_enabled", lambda: True)
    monkeypatch.setattr(
        pipeline,
        "fetch_table_context",
        lambda table_id: {"columns": [{"name": "amount"}]},
    )
    process_table = MagicMock(name="process_table")
    monkeypatch.setattr(pipeline, "process_table", process_table)

    def compile_() -> int:
        return pipeline.compile_semantic_layer(
            "warehouse", domain_summary=MagicMock(), embedder=embedder
        )

    if stored == _CONFIGURED:
        assert compile_() == 1
        process_table.assert_called_once()
        return
    with pytest.raises(VectorSizeMismatchError):
        compile_()
    process_table.assert_not_called()


# ---------------------------------------------------------------------------
# Compilation, SqlAttributes from query history
# ---------------------------------------------------------------------------


@pytest.fixture
def suggester(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Two Terms, one suggestion each; returns the ``create_sql_attribute`` mock."""
    rows = [
        {
            "term_id": f"t{i}",
            "term_name": f"Term{i}",
            "term_description": "",
            "sqls": [{"sql_id": f"s{i}", "sql_text": "SELECT amount FROM orders"}],
        }
        for i in (1, 2)
    ]
    mod = sql_attribute_suggester
    monkeypatch.setattr(mod, "fetch_terms_with_sqls", lambda *a, **kw: rows)
    monkeypatch.setattr(mod, "fetch_table_schema_map", lambda database_name: {})
    monkeypatch.setattr(
        mod, "_rank_expressions", lambda sqls: [("amount > 0", 1.0, ["s1"])]
    )
    monkeypatch.setattr(
        mod,
        "_judge_with_llm",
        lambda term_name, description, top: [
            mod._SuggestedAttr(
                name=f"{term_name} positive",
                description="d",
                expression=f"amount > 0 /* {term_name} */",
            )
        ],
    )
    monkeypatch.setattr(
        mod, "_build_valid_sql", lambda expression, *a: f"SELECT {expression}"
    )
    create = MagicMock(name="create_sql_attribute", return_value={"id": "a1"})
    monkeypatch.setattr(mod, "create_sql_attribute", create)
    return create


def test_suggester_lets_a_size_mismatch_through(suggester: MagicMock) -> None:
    error = _mismatch()
    suggester.side_effect = error

    with pytest.raises(VectorSizeMismatchError) as excinfo:
        sql_attribute_suggester.suggest_sql_attributes("warehouse")

    assert excinfo.value is error


def test_suggester_still_skips_a_failed_create(
    suggester: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    suggester.side_effect = RuntimeError("endpoint down")

    with caplog.at_level(logging.WARNING, logger=sql_attribute_suggester.logger.name):
        assert sql_attribute_suggester.suggest_sql_attributes("warehouse") == 0

    assert suggester.call_count == 2
    skipped = [
        r for r in caplog.records if "unexpected error creating" in r.getMessage()
    ]
    assert len(skipped) == 2


def test_suggester_still_logs_a_failed_term_and_goes_on(
    suggester: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        sql_attribute_suggester, "_judge_with_llm", _raiser(RuntimeError("llm down"))
    )

    with caplog.at_level(logging.ERROR, logger=sql_attribute_suggester.logger.name):
        assert sql_attribute_suggester.suggest_sql_attributes("warehouse") == 0

    suggester.assert_not_called()
    failed = [r for r in caplog.records if "Error processing term" in r.getMessage()]
    assert len(failed) == 2


# ---------------------------------------------------------------------------
# Compilation, bridge-table SqlAttributes
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Two bridge candidates that reach the create; returns its mock."""
    mod = bridge_tables
    monkeypatch.setattr(
        mod,
        "fetch_bridge_table_candidates",
        lambda database_name: [
            {"table_id": "b1", "table_name": "order_product"},
            {"table_id": "b2", "table_name": "order_coupon"},
        ],
    )
    monkeypatch.setattr(mod, "mark_table_as_junction", lambda table_id: None)
    monkeypatch.setattr(
        mod,
        "_target_terms_for_bridge",
        lambda candidate: [{"id": "t1", "name": "Order", "description": ""}],
    )
    monkeypatch.setattr(
        mod,
        "_generate_bridge_sql_attribute",
        lambda candidate: mod._BridgeSqlAttributeProposal(
            name=f"{candidate['table_name']} link",
            description="d",
            expression=f"SELECT * FROM {candidate['table_name']}",
        ),
    )
    monkeypatch.setattr(mod, "_pick_owner_term_id", lambda terms, description: "t1")
    create = MagicMock(name="create_sql_attribute")
    monkeypatch.setattr(mod, "create_sql_attribute", create)
    return create


def test_bridge_stops_on_a_size_mismatch(bridge: MagicMock) -> None:
    error = _mismatch()
    bridge.side_effect = error

    with pytest.raises(VectorSizeMismatchError) as excinfo:
        bridge_tables.build_bridge_tables_sql_attributes("warehouse")

    assert excinfo.value is error
    bridge.assert_called_once()


def test_bridge_still_skips_a_failed_create(
    bridge: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    bridge.side_effect = [RuntimeError("endpoint down"), {"id": "a2"}]

    with caplog.at_level(logging.ERROR, logger=bridge_tables.logger.name):
        assert bridge_tables.build_bridge_tables_sql_attributes("warehouse") == 1

    assert bridge.call_count == 2
    failed = [
        r for r in caplog.records if "unexpected error creating" in r.getMessage()
    ]
    assert len(failed) == 1


def test_bridge_mismatch_from_the_real_guard_reaches_the_caller(
    bridge: MagicMock,
    guard: Callable[[int], _Guard],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real ``create_sql_attribute``, embed and vector store.

    The SqlAttribute row is persisted before its embedding is written, so the
    first one stays in the catalog without a vector; the error stops the rest.
    """
    stub = guard(_STORED)
    svc = sql_attr_service
    monkeypatch.setattr(bridge_tables, "create_sql_attribute", svc.create_sql_attribute)
    monkeypatch.setattr(svc, "_resolve_database_name", lambda connector: "warehouse")
    monkeypatch.setattr(svc, "find_attr_by_name", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "get_slim_term_by_id", lambda term_id: {"name": "Order"})
    monkeypatch.setattr(svc, "find_attr_by_expression", lambda **kw: None)
    monkeypatch.setattr(svc, "get_dialects", lambda database_name: [])
    monkeypatch.setattr(svc, "get_schemas", lambda database_name: [])
    monkeypatch.setattr(svc, "validate_sql", lambda *a: MagicMock())
    persist = MagicMock(
        name="_persist_attr_with_sql", return_value={"id": "a1", "name": "n"}
    )
    monkeypatch.setattr(svc, "_persist_attr_with_sql", persist)
    monkeypatch.setattr(svc, "link_to_term", lambda attr_id, term_id: None)
    monkeypatch.setattr(svc, "get_embed_params", _params)
    monkeypatch.setattr(svc, "get_semantic_vdb", stub.vdb)
    monkeypatch.setattr(
        svc,
        "fetch_sql_attribute_docs",
        lambda attr_id: [
            {"id": attr_id, "name": "n", "label": "SqlAttribute", "text": "n: d"}
        ],
    )
    monkeypatch.setattr(
        embedding_module,
        "embed_texts",
        lambda texts, params, input_type, **kw: [[0.1] * _CONFIGURED for _ in texts],
    )

    with pytest.raises(VectorSizeMismatchError):
        bridge_tables.build_bridge_tables_sql_attributes("warehouse")

    persist.assert_called_once()
    stub.create_sync.assert_not_called()


# ---------------------------------------------------------------------------
# Save: PATCH /terms/{term_id}
# ---------------------------------------------------------------------------


@pytest.fixture
def term_patch(monkeypatch: pytest.MonkeyPatch) -> Callable[[BaseException], Any]:
    """Patch a Term's description while its re-embed raises the given error."""
    monkeypatch.setattr(terms_router, "edit_subject", lambda term_id: None)
    monkeypatch.setattr(terms_router, "resolve_internal_user", lambda *a, **kw: None)
    monkeypatch.setattr(
        terms_router.terms_dal,
        "update_term",
        lambda term_id, **kw: {
            "id": term_id,
            "name": "Order",
            "old_name": "Order",
            "description": kw["description"],
        },
    )
    monkeypatch.setattr(
        terms_router.terms_dal, "get_term_certification", lambda term_id: None
    )

    def run(error: BaseException) -> Any:
        monkeypatch.setattr(
            terms_router.term_service, "refresh_term_embeddings", _raiser(error)
        )
        return terms_router.update_term(
            MagicMock(name="request"),
            "t1",
            terms_router.TermUpdate(description="new"),
        )

    return run


def test_term_save_answers_422_with_the_message_on_a_size_mismatch(
    term_patch: Callable[[BaseException], Any],
) -> None:
    error = _mismatch()

    with pytest.raises(HTTPException) as excinfo:
        term_patch(error)

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == str(error)
    assert excinfo.value.__cause__ is error


def test_term_save_still_succeeds_when_the_re_embed_fails_otherwise(
    term_patch: Callable[[BaseException], Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=terms_router.logger.name):
        response = term_patch(RuntimeError("endpoint down"))

    assert response["data"]["description"] == "new"
    assert any(
        "Failed to refresh Term embeddings" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Save: a Column's sample values, which also re-embed its ColumnAttributes
# ---------------------------------------------------------------------------


@pytest.fixture
def column_patch(monkeypatch: pytest.MonkeyPatch) -> Callable[[BaseException], Any]:
    """Patch a Column's sample values while the semantic re-embed raises."""
    svc = datasources_service
    monkeypatch.setattr(svc, "_reject_uneditable_sample_values", lambda node_id: None)
    monkeypatch.setattr(
        svc,
        "patch_catalog_node",
        lambda node_id, properties: {
            "id": node_id,
            "label": Labels.COLUMN,
            "props": {"sample_values": '["a", "b"]'},
        },
    )
    monkeypatch.setattr(svc, "_refresh_vdb_embeddings", lambda node_ids: None)

    def run(error: BaseException) -> Any:
        monkeypatch.setattr(
            svc, "_refresh_semantic_column_attribute_embeddings", _raiser(error)
        )
        return svc.update_node_properties("c1", {"sample_values": ["a", "b"]})

    return run


def test_column_save_lets_a_size_mismatch_through(
    column_patch: Callable[[BaseException], Any],
) -> None:
    error = _mismatch()

    with pytest.raises(VectorSizeMismatchError) as excinfo:
        column_patch(error)

    assert excinfo.value is error


def test_column_save_still_succeeds_when_the_re_embed_fails_otherwise(
    column_patch: Callable[[BaseException], Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=datasources_service.logger.name):
        result = column_patch(RuntimeError("endpoint down"))

    assert result is not None
    assert result["id"] == "c1"
    assert any(
        "Failed to refresh semantic ColumnAttribute" in r.getMessage()
        for r in caplog.records
    )
