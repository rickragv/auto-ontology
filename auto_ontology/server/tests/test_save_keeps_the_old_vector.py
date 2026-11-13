# SPDX-License-Identifier: Apache-2.0

"""A save during a vector size mismatch keeps the item's old vector.

Each save that re-embeds an item deletes its stored vector and then writes the
new one. The size guard runs on the write, so it used to fire after the delete:
the save answered with the mismatch, and the old vector, still the right size
for the table, was gone. These deletes now run the guard first and delete
nothing, and every save reads its ``EMBED_*`` settings before its delete, so
an invalid one deletes nothing either. A plain delete of the item still goes
ahead, as deleting a whole datasource does: it never reads or writes a vector.

Real ``PostgresVDB`` instances over a stubbed table; every SQL statement they
would send is recorded instead. No database or network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock

import pandas as pd
import pytest

import auto_ontology.utils as utils_pkg
import auto_ontology.vdb as vdb_pkg
import auto_ontology.vdb.postgres as postgres_module
from auto_ontology.semantic.embed import SemanticEmbedder
from auto_ontology.server.custom_analyses import service as custom_service
from auto_ontology.server.datasources import service as datasources_service
from auto_ontology.server.pql_analyses import service as pql_service
from auto_ontology.server.sql_attributes import service as sql_attr_service
from auto_ontology.server.terms import service as term_service
from auto_ontology.utils import embedding as embedding_module
from auto_ontology.utils.embedding import EmbedParams
from auto_ontology.vdb.dimensions import EmbeddingConfigError, VectorSizeMismatchError
from auto_ontology.vdb.postgres import PostgresVDB
from auto_ontology.vdb.records import write_embedded_rows

_CONFIGURED = 768
_STORED = 1024


class _Table:
    """The vector table as the stores see it, at *stored* dimensions.

    Records every statement sent over a psycopg connection, which for these
    paths means the deletes. The store a write would open is a mock, and the
    endpoint returns vectors of the configured size, so a save that skipped
    the check would delete and then fail on its write.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, stored: int) -> None:
        self.statements: list[Any] = []
        monkeypatch.setattr(
            embedding_module,
            "embed_texts",
            lambda texts, *a, **kw: [[0.1] * _CONFIGURED for _ in texts],
        )
        monkeypatch.setattr(
            postgres_module, "lookup_vector_table", lambda *args: (True, stored)
        )
        monkeypatch.setattr(postgres_module.psycopg, "connect", self._connect)
        monkeypatch.setattr(PostgresVDB, "_get_engine", lambda self: object())
        self.create_sync = MagicMock(name="PGVectorStore.create_sync")
        monkeypatch.setattr(
            postgres_module.PGVectorStore, "create_sync", self.create_sync
        )

    def _connect(self, *args: Any, **kwargs: Any) -> MagicMock:
        conn = MagicMock(name="connection")
        cursor = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = lambda statement, *a: self.statements.append(
            statement
        )
        cursor.rowcount = 1
        return conn

    def vdb(self) -> PostgresVDB:
        return PostgresVDB(
            connection_string="postgresql://x/y",
            collection_name="semantic_layer",
            schema_name="vdb",
            vector_size=_CONFIGURED,
        )


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], _Table]:
    def install(stored: int) -> _Table:
        return _Table(monkeypatch, stored)

    return install


def _params() -> EmbedParams:
    return EmbedParams(model_name="m", embed_invoke_url="http://embed.invalid/v1")


def _embedder(vdb: PostgresVDB) -> SemanticEmbedder:
    return SemanticEmbedder(database_name="warehouse", embed_params=_params(), vdb=vdb)


def _write_one(**kwargs: Any) -> None:
    """Stands in for an analysis embed: one configured-size row into its store."""
    row = {"text": "x1", "metadata": {"id": "x1", "embedding": [0.1] * _CONFIGURED}}
    write_embedded_rows(kwargs["vdb"], [row])


# ---------------------------------------------------------------------------
# PostgresVDB.delete_by_id
# ---------------------------------------------------------------------------


def test_a_checked_delete_on_a_table_of_another_size_deletes_nothing(
    table: Callable[[int], _Table],
) -> None:
    stub = table(_STORED)

    with pytest.raises(VectorSizeMismatchError):
        stub.vdb().delete_by_id("a1", check_size=True)

    assert stub.statements == []


@pytest.mark.parametrize("stored", [_STORED, _CONFIGURED])
def test_a_plain_delete_goes_ahead_on_a_table_of_any_size(
    table: Callable[[int], _Table], stored: int
) -> None:
    stub = table(stored)

    assert stub.vdb().delete_by_id("a1") == 1

    assert len(stub.statements) == 1


def test_a_checked_delete_on_a_matching_table_deletes(
    table: Callable[[int], _Table],
) -> None:
    stub = table(_CONFIGURED)

    assert stub.vdb().delete_by_id("a1", check_size=True) == 1

    assert len(stub.statements) == 1


@pytest.mark.parametrize("check_size", [False, True])
def test_a_delete_from_a_missing_table_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, table: Callable[[int], _Table], check_size: bool
) -> None:
    stub = table(_STORED)
    monkeypatch.setattr(
        postgres_module, "lookup_vector_table", lambda *args: (False, None)
    )

    assert stub.vdb().delete_by_id("a1", check_size=check_size) == 0

    assert stub.statements == []


# ---------------------------------------------------------------------------
# The saves that delete and re-embed
# ---------------------------------------------------------------------------


def _column_attribute_save(monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB) -> None:
    """PATCH /terms/{term_id}/column-attributes/{attr_id}."""
    monkeypatch.setattr(
        term_service.attributes_dal,
        "update_column_attribute",
        lambda *args, **kwargs: {"id": "a1", "name": "amount", "term_name": "Order"},
    )
    monkeypatch.setattr(
        term_service, "build_semantic_embedder", lambda name, reset: _embedder(vdb)
    )
    term_service.update_column_attribute("t1", "a1", description="d")


def _term_save(monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB) -> None:
    """PATCH /terms/{term_id}."""
    monkeypatch.setattr(
        term_service.terms_dal,
        "fetch_term_and_column_attributes_for_embedding",
        lambda term_id: ({"id": term_id, "name": "Order"}, [{"id": "a1"}]),
    )
    monkeypatch.setattr(
        term_service, "build_semantic_embedder", lambda name, reset: _embedder(vdb)
    )
    term_service.refresh_term_embeddings("t1", refresh_dependent_attrs=True)


def _catalog_save(monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB) -> None:
    """PATCH on a Table or Column: the data-layer rows, embedded first."""
    svc = datasources_service
    monkeypatch.setattr(
        svc,
        "fetch_tables_and_columns_by_node_ids",
        lambda node_ids: (pd.DataFrame([{"id": "c1"}]), pd.DataFrame(), "warehouse"),
    )
    rows = pd.DataFrame([{"text": "c1", "metadata": {"id": "c1"}}])
    monkeypatch.setattr(
        svc,
        "CatalogEmbeddingRowsOp",
        lambda database_name: SimpleNamespace(process=lambda pair: rows),
    )
    monkeypatch.setattr(utils_pkg, "get_embed_params", _params)
    monkeypatch.setattr(
        embedding_module,
        "embed_rows",
        lambda records, params, what: [
            {**r, "metadata": {**r["metadata"], "embedding": [0.1] * _CONFIGURED}}
            for r in records
        ],
    )
    monkeypatch.setattr(vdb_pkg, "get_data_vdb", lambda: vdb)
    svc._refresh_vdb_embeddings(["c1"])


def _sample_values_save(monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB) -> None:
    """A Column's sample values: its ColumnAttributes' semantic rows."""
    svc = datasources_service
    monkeypatch.setattr(
        svc,
        "fetch_column_attribute_embedding_contexts_by_column_id",
        lambda column_id: [({"name": "Order"}, [{"id": "a1", "name": "amount"}])],
    )
    monkeypatch.setattr(
        svc, "build_semantic_embedder", lambda name, reset: _embedder(vdb)
    )
    svc._refresh_semantic_column_attribute_embeddings("c1")


def _sql_attribute_save(monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB) -> None:
    """PATCH /sql-attributes/{attr_id}."""
    monkeypatch.setattr(sql_attr_service, "get_semantic_vdb", lambda: vdb)
    monkeypatch.setattr(sql_attr_service, "get_embed_params", _params)
    monkeypatch.setattr(
        sql_attr_service,
        "fetch_sql_attribute_docs",
        lambda attr_id: [
            {"id": attr_id, "name": "n", "label": "SqlAttribute", "text": "n"}
        ],
    )
    sql_attr_service._reembed_sql_attribute("a1", "warehouse")


def _custom_analysis_save(monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB) -> None:
    """PUT /custom-analyses/{analysis_id}."""
    svc = custom_service
    monkeypatch.setattr(svc, "get_custom_analysis_by_id", lambda analysis_id: {})
    monkeypatch.setattr(svc, "find_analysis_by_name", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "find_analysis_by_sql", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "get_dialects", lambda: [])
    monkeypatch.setattr(svc, "get_schemas", lambda: [])
    monkeypatch.setattr(svc, "validate_sql", lambda *args: MagicMock())
    monkeypatch.setattr(svc, "detach_existing_sql_edges", lambda analysis_id: None)
    monkeypatch.setattr(
        svc, "_persist_analysis_with_sql", lambda *args: {"id": "x1", "name": "n"}
    )
    monkeypatch.setattr(svc, "fetch_database_name_for_analysis", lambda a: "warehouse")
    monkeypatch.setattr(svc, "embed_custom_analyses", _write_one)
    monkeypatch.setattr(utils_pkg, "get_embed_params", _params)
    monkeypatch.setattr(vdb_pkg, "get_semantic_vdb", lambda: vdb)
    svc.update_custom_analysis("x1", "n", "d", "SELECT 1")


def _pql_analysis_save(monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB) -> None:
    """PUT /pql-analyses/{analysis_id}."""
    svc = pql_service
    monkeypatch.setattr(svc, "get_pql_analysis_by_id", lambda analysis_id: {})
    monkeypatch.setattr(svc, "find_pql_analysis_by_name", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "find_pql_analysis_by_pql", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "upsert_pql_analysis_node", lambda *args: None)
    monkeypatch.setattr(svc, "embed_pql_analyses", _write_one)
    monkeypatch.setattr(utils_pkg, "get_embed_params", _params)
    monkeypatch.setattr(vdb_pkg, "get_semantic_vdb", lambda: vdb)
    svc.update_pql_analysis("x1", "n", "d", "PREDICT x")


_SAVES = [
    _column_attribute_save,
    _term_save,
    _catalog_save,
    _sample_values_save,
    _sql_attribute_save,
    _custom_analysis_save,
    _pql_analysis_save,
]


@pytest.mark.parametrize("save", _SAVES, ids=lambda save: save.__name__.strip("_"))
def test_a_save_on_a_table_of_another_size_keeps_the_old_vector(
    monkeypatch: pytest.MonkeyPatch,
    table: Callable[[int], _Table],
    save: Callable[[pytest.MonkeyPatch, PostgresVDB], None],
) -> None:
    stub = table(_STORED)

    with pytest.raises(VectorSizeMismatchError):
        save(monkeypatch, stub.vdb())

    assert stub.statements == []
    stub.create_sync.assert_not_called()


@pytest.mark.parametrize(
    ("name", "value"),
    [("EMBED_BATCH_SIZE", "0"), ("EMBED_EXTRA_PARAMS", "nvdia")],
)
@pytest.mark.parametrize("save", _SAVES, ids=lambda save: save.__name__.strip("_"))
def test_a_save_with_a_bad_embedding_setting_keeps_the_old_vector(
    monkeypatch: pytest.MonkeyPatch,
    table: Callable[[int], _Table],
    save: Callable[[pytest.MonkeyPatch, PostgresVDB], None],
    name: str,
    value: str,
) -> None:
    """The setting is read before the delete, on a table of the right size."""
    stub = table(_CONFIGURED)
    monkeypatch.setenv(name, value)

    with pytest.raises(EmbeddingConfigError, match=name):
        save(monkeypatch, stub.vdb())

    assert stub.statements == []


# ---------------------------------------------------------------------------
# The deletes, which must still work during a mismatch
# ---------------------------------------------------------------------------


def _custom_analysis_delete(
    monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB
) -> object:
    svc = custom_service
    monkeypatch.setattr(svc, "get_custom_analysis_by_id", lambda analysis_id: {})
    monkeypatch.setattr(svc, "delete_custom_analysis_node", lambda analysis_id: None)
    monkeypatch.setattr(vdb_pkg, "get_semantic_vdb", lambda: vdb)
    return svc.delete_custom_analysis("x1")


def _pql_analysis_delete(monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB) -> object:
    svc = pql_service
    monkeypatch.setattr(svc, "get_pql_analysis_by_id", lambda analysis_id: {})
    monkeypatch.setattr(svc, "delete_pql_analysis_node", lambda analysis_id: None)
    monkeypatch.setattr(vdb_pkg, "get_semantic_vdb", lambda: vdb)
    return svc.delete_pql_analysis("x1")


def _sql_attribute_delete(monkeypatch: pytest.MonkeyPatch, vdb: PostgresVDB) -> object:
    svc = sql_attr_service
    monkeypatch.setattr(svc, "get_sql_attribute_by_id", lambda attr_id: {})
    monkeypatch.setattr(svc, "delete_sql_attribute_node", lambda attr_id: None)
    monkeypatch.setattr(svc, "get_semantic_vdb", lambda: vdb)
    return svc.delete_sql_attribute("x1")


@pytest.mark.parametrize(
    "delete",
    [_custom_analysis_delete, _pql_analysis_delete, _sql_attribute_delete],
    ids=lambda delete: delete.__name__.strip("_"),
)
def test_deleting_an_item_still_removes_its_vector_on_a_table_of_another_size(
    monkeypatch: pytest.MonkeyPatch,
    table: Callable[[int], _Table],
    delete: Callable[[pytest.MonkeyPatch, PostgresVDB], object],
) -> None:
    stub = table(_STORED)

    assert delete(monkeypatch, stub.vdb()) == {"id": "x1"}

    assert len(stub.statements) == 1
