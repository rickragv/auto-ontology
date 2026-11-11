# SPDX-License-Identifier: Apache-2.0

"""The vector size guard covers writes and searches, not deletes.

Deleting rows never reads or writes a vector, so a table of another size must
not stop a datasource from being deleted. Writes and searches still refuse, and
so does a reset, which clears the way for a write.

The first group needs no database. The live group builds a real table and skips
when Postgres is not reachable or ``POSTGRES_*`` is not set.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import psycopg
import pytest

import auto_ontology.vdb.postgres as postgres_module
from auto_ontology.vdb.dimensions import VectorSizeMismatchError, lookup_vector_table
from auto_ontology.vdb.postgres import PostgresVDB


def _unit(size: int) -> list[float]:
    vector = [0.0] * size
    vector[0] = 1.0
    return vector


def _record(size: int, database_name: str, node_id: str) -> dict[str, Any]:
    """A row in the shape ``write_to_index`` takes."""
    return {
        "text": f"{node_id} table",
        "document_type": "text",
        "metadata": {
            "embedding": _unit(size),
            "id": node_id,
            "label": "Table",
            "database_name": database_name,
        },
    }


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


class _Lookups:
    """Stands in for ``lookup_vector_table``: the table exists at *size*."""

    def __init__(self, size: int | None) -> None:
        self.size = size
        self.calls = 0

    def __call__(self, *args: Any) -> tuple[bool, int | None]:
        self.calls += 1
        return True, self.size


_Stub = Callable[[int | None], tuple[_Lookups, MagicMock]]


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _Stub:
    """Patch the catalog lookup, the engine and the store, then return the doubles.

    Patched on the class, not an instance, so a reset in the constructor sees them.
    """

    def install(size: int | None) -> tuple[_Lookups, MagicMock]:
        lookups = _Lookups(size)
        monkeypatch.setattr(postgres_module, "lookup_vector_table", lookups)
        monkeypatch.setattr(PostgresVDB, "_get_engine", lambda self: object())
        store = MagicMock(name="PGVectorStore")
        store.get.return_value = {"ids": ["a", "b"]}
        store.similarity_search_with_score_by_vector.return_value = []
        create_sync = MagicMock(return_value=store)
        monkeypatch.setattr(postgres_module.PGVectorStore, "create_sync", create_sync)
        return lookups, create_sync

    return install


def _vdb(size: int, **kwargs: Any) -> PostgresVDB:
    return PostgresVDB(connection_string="postgresql://x/y", vector_size=size, **kwargs)


def test_a_delete_goes_ahead_on_a_table_of_another_size(stub: _Stub) -> None:
    lookups, create_sync = stub(1024)
    assert _vdb(768).delete_by_database("sales") == ["a", "b"]
    store = create_sync.return_value
    store.get.assert_called_once_with(where={"database_name": "sales"}, include=[])
    store.delete.assert_called_once_with(filter={"database_name": "sales"})
    assert lookups.calls == 1


@pytest.mark.parametrize("size", [1024, 768])
def test_a_store_opened_for_a_delete_is_not_kept(stub: _Stub, size: int) -> None:
    stub(size)
    vdb = _vdb(768)
    vdb.delete_by_database("sales")
    assert vdb._store is None


def test_a_write_after_a_delete_is_still_refused(stub: _Stub) -> None:
    lookups, create_sync = stub(1024)
    vdb = _vdb(768)
    vdb.delete_by_database("sales")
    with pytest.raises(VectorSizeMismatchError):
        vdb.write_to_index([_record(768, "sales", "t1")])
    create_sync.return_value.add_embeddings.assert_not_called()
    # One lookup per store opened: the delete's, then the write's own.
    assert lookups.calls == 2


def test_a_write_is_refused_before_a_store_is_built(stub: _Stub) -> None:
    _, create_sync = stub(1024)
    with pytest.raises(VectorSizeMismatchError):
        _vdb(768).write_to_index([_record(768, "sales", "t1")])
    create_sync.assert_not_called()


def test_a_search_is_refused_before_a_store_is_built(stub: _Stub) -> None:
    _, create_sync = stub(1024)
    with pytest.raises(VectorSizeMismatchError):
        _vdb(768).retrieval([_unit(768)])
    create_sync.assert_not_called()


def test_a_reset_is_refused_before_anything_is_deleted(stub: _Stub) -> None:
    _, create_sync = stub(1024)
    with pytest.raises(VectorSizeMismatchError):
        _vdb(768, database_name="sales", reset=True)
    # No store was built, so nothing could have been read or deleted.
    create_sync.assert_not_called()


def test_a_reset_of_a_matching_table_looks_it_up_once(stub: _Stub) -> None:
    lookups, create_sync = stub(768)
    vdb = _vdb(768, database_name="sales", reset=True)
    store = create_sync.return_value
    store.delete.assert_called_once_with(filter={"database_name": "sales"})
    assert vdb.write_to_index([_record(768, "sales", "t1")]) == 1
    store.add_embeddings.assert_called_once()
    # The reset, the delete and the write all share the store the reset opened.
    assert lookups.calls == 1
    create_sync.assert_called_once()


# ---------------------------------------------------------------------------
# Live Postgres
# ---------------------------------------------------------------------------


def _connection_string() -> str:
    try:
        user = os.environ["POSTGRES_USER"]
        password = os.environ["POSTGRES_PASSWORD"]
    except KeyError as exc:
        pytest.skip(f"postgres not configured: {exc.args[0]} unset")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DATABASE", "auto_ontology")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _exec(dsn: str, statement: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(statement)
        conn.commit()


class _Collection:
    """A real collection, created at 1024 dimensions, in a private schema."""

    table = "coll"

    def __init__(self, dsn: str, schema: str) -> None:
        self.dsn = dsn
        self.schema = schema
        self.opened: list[PostgresVDB] = []

    def open(self, size: int, **kwargs: Any) -> PostgresVDB:
        """The collection as a store configured for *size* dimensions."""
        vdb = PostgresVDB(
            connection_string=self.dsn,
            collection_name=self.table,
            schema_name=self.schema,
            vector_size=size,
            **kwargs,
        )
        self.opened.append(vdb)
        return vdb

    def count(self, database_name: str | None = None) -> int:
        query = f'SELECT count(*) FROM "{self.schema}".{self.table}'
        params: tuple[str, ...] = ()
        if database_name is not None:
            query += " WHERE database_name = %s"
            params = (database_name,)
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
        assert row is not None
        return int(row[0])


@pytest.fixture
def collection() -> Iterator[_Collection]:
    """One ``keep`` row and two ``drop`` rows in a 1024-dimension table."""
    dsn = _connection_string()
    try:
        psycopg.connect(dsn, connect_timeout=3).close()
    except Exception as exc:  # noqa: BLE001 -- no database, nothing to assert
        pytest.skip(f"postgres not reachable: {exc}")
    schema = f"guardtest_{uuid.uuid4().hex[:8]}"
    _exec(dsn, "CREATE EXTENSION IF NOT EXISTS vector")
    _exec(dsn, f'CREATE SCHEMA "{schema}"')
    coll = _Collection(dsn, schema)
    try:
        written = coll.open(1024).write_to_index(
            [
                _record(1024, "keep", "k1"),
                _record(1024, "drop", "d1"),
                _record(1024, "drop", "d2"),
            ]
        )
        assert written == 3
        assert lookup_vector_table(dsn, schema, coll.table) == (True, 1024)
        yield coll
    finally:
        for vdb in coll.opened:
            vdb.close()
        _exec(dsn, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_live_a_delete_removes_the_rows_from_a_table_of_another_size(
    collection: _Collection,
) -> None:
    ids = collection.open(768).delete_by_database("drop")
    assert len(ids) == 2
    assert collection.count("drop") == 0
    assert collection.count("keep") == 1
    # Deleting rows leaves the table as it was.
    assert lookup_vector_table(collection.dsn, collection.schema, "coll") == (
        True,
        1024,
    )


def test_live_a_search_of_a_table_of_another_size_is_refused(
    collection: _Collection,
) -> None:
    with pytest.raises(VectorSizeMismatchError):
        collection.open(768).retrieval([_unit(768)])
    # At its own size the same table answers, so the refusal is the guard's.
    [hits] = collection.open(1024).retrieval([_unit(1024)])
    assert len(hits) == 3


def test_live_a_write_to_a_table_of_another_size_is_refused(
    collection: _Collection,
) -> None:
    with pytest.raises(VectorSizeMismatchError):
        collection.open(768).write_to_index([_record(768, "drop", "d3")])
    assert collection.count() == 3


def test_live_a_reset_of_a_table_of_another_size_keeps_the_previous_rows(
    collection: _Collection,
) -> None:
    """The ingest path: ``get_vdb(database_name=..., reset=True)``."""
    with pytest.raises(VectorSizeMismatchError):
        collection.open(768, database_name="drop", reset=True)
    assert collection.count("drop") == 2
