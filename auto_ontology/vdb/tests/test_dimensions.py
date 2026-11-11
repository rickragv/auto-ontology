# SPDX-License-Identifier: Apache-2.0

"""``EMBED_DIMENSIONS``: parsing, wiring into ``get_vdb``, and the size guard.

The first groups need no database. The live groups build real tables and skip
when Postgres is not reachable or ``POSTGRES_*`` is not set.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Iterator
from unittest.mock import MagicMock

import psycopg
import pytest

import auto_ontology.vdb as vdb_pkg
import auto_ontology.vdb.postgres as postgres_module
from auto_ontology.vdb.dimensions import (
    VectorSizeMismatchError,
    check_vector_size,
    configured_embed_dimensions,
    lookup_vector_table,
)
from auto_ontology.vdb.postgres import PostgresVDB

_ENV = "EMBED_DIMENSIONS"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)


# ---------------------------------------------------------------------------
# configured_embed_dimensions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_means_no_setting(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    if raw is not None:
        monkeypatch.setenv(_ENV, raw)
    assert configured_embed_dimensions() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1024", 1024), (" 768 ", 768), ("4000", 4000), ("1", 1)],
)
def test_valid_values_parse(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv(_ENV, raw)
    assert configured_embed_dimensions() == expected


@pytest.mark.parametrize("raw", ["abc", "10.5", "0", "-5", "4001"])
def test_invalid_values_name_the_variable_and_the_value(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(_ENV, raw)
    with pytest.raises(ValueError) as excinfo:
        configured_embed_dimensions()
    assert _ENV in str(excinfo.value)
    assert repr(raw) in str(excinfo.value)


def test_the_value_is_read_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "512")
    assert configured_embed_dimensions() == 512
    monkeypatch.setenv(_ENV, "1536")
    assert configured_embed_dimensions() == 1536
    monkeypatch.delenv(_ENV)
    assert configured_embed_dimensions() is None


# ---------------------------------------------------------------------------
# check_vector_size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing", "expected"), [(1024, 1024), (None, 1024), (1024, None), (None, None)]
)
def test_sizes_that_agree_or_are_unknown_pass(
    existing: int | None, expected: int | None
) -> None:
    check_vector_size("vdb", "tbl", existing, expected)


def test_differing_sizes_raise_an_explanatory_error() -> None:
    with pytest.raises(VectorSizeMismatchError) as excinfo:
        check_vector_size("vdb", "tbl", 1024, 768)
    assert isinstance(excinfo.value, ValueError)
    message = str(excinfo.value)
    assert "vdb.tbl" in message
    assert "1024" in message
    assert "768" in message
    assert _ENV in message


# ---------------------------------------------------------------------------
# get_vdb wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock(name="PostgresVDB")
    monkeypatch.setattr(vdb_pkg, "PostgresVDB", fake)
    monkeypatch.setattr(
        vdb_pkg, "get_postgres_connection_string", lambda: "postgresql://x/y"
    )
    return fake


def test_unset_leaves_the_stores_own_default(recorder: MagicMock) -> None:
    vdb_pkg.get_vdb()
    assert "vector_size" not in recorder.call_args.kwargs


@pytest.mark.parametrize(
    "factory", [vdb_pkg.get_vdb, vdb_pkg.get_data_vdb, vdb_pkg.get_semantic_vdb]
)
def test_the_setting_reaches_every_factory(
    monkeypatch: pytest.MonkeyPatch, recorder: MagicMock, factory: Any
) -> None:
    monkeypatch.setenv(_ENV, "1024")
    factory()
    assert recorder.call_args.kwargs["vector_size"] == 1024


def test_a_bad_setting_stops_before_the_store_is_built(
    monkeypatch: pytest.MonkeyPatch, recorder: MagicMock
) -> None:
    monkeypatch.setenv(_ENV, "nope")
    with pytest.raises(ValueError, match=_ENV):
        vdb_pkg.get_vdb()
    recorder.assert_not_called()


def test_the_store_defaults_to_2048() -> None:
    assert PostgresVDB(connection_string="postgresql://x/y").vector_size == 2048


# ---------------------------------------------------------------------------
# _get_store guard, no database
# ---------------------------------------------------------------------------


@pytest.fixture
def guarded(monkeypatch: pytest.MonkeyPatch) -> tuple[PostgresVDB, MagicMock]:
    vdb = PostgresVDB(connection_string="postgresql://x/y", vector_size=2048)
    monkeypatch.setattr(vdb, "_get_engine", lambda: object())
    create_sync = MagicMock(return_value=object())
    monkeypatch.setattr(postgres_module.PGVectorStore, "create_sync", create_sync)
    return vdb, create_sync


def test_a_table_of_another_size_is_refused_before_a_store_is_built(
    monkeypatch: pytest.MonkeyPatch, guarded: tuple[PostgresVDB, MagicMock]
) -> None:
    vdb, create_sync = guarded
    monkeypatch.setattr(postgres_module, "lookup_vector_table", lambda *a: (True, 1024))
    with pytest.raises(VectorSizeMismatchError):
        vdb._get_store()
    create_sync.assert_not_called()


@pytest.mark.parametrize("size", [None, 2048])
def test_an_unreadable_or_matching_size_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
    guarded: tuple[PostgresVDB, MagicMock],
    size: int | None,
) -> None:
    vdb, create_sync = guarded
    monkeypatch.setattr(postgres_module, "lookup_vector_table", lambda *a: (True, size))
    assert vdb._get_store() is create_sync.return_value
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


@pytest.fixture
def pg() -> Iterator[tuple[str, str]]:
    """A connection string and a private schema, dropped afterwards."""
    dsn = _connection_string()
    schema = f"dimtest_{uuid.uuid4().hex[:8]}"
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 -- no database, nothing to assert
        pytest.skip(f"postgres not reachable: {exc}")
    with conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f'CREATE SCHEMA "{schema}"')
        conn.commit()
    try:
        yield dsn, schema
    finally:
        with psycopg.connect(dsn) as cleanup:
            with cleanup.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cleanup.commit()


def _exec(dsn: str, statement: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(statement)
        conn.commit()


def test_lookup_of_a_missing_table(pg: tuple[str, str]) -> None:
    dsn, schema = pg
    assert lookup_vector_table(dsn, schema, "absent") == (False, None)


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("embedding vector(1024)", 1024),
        ("embedding halfvec(3000)", 3000),
        ("embedding vector", None),
        ("other text", None),
    ],
)
def test_lookup_reads_the_embedding_column_size(
    pg: tuple[str, str], column: str, expected: int | None
) -> None:
    dsn, schema = pg
    _exec(dsn, f'CREATE TABLE "{schema}".t ({column})')
    assert lookup_vector_table(dsn, schema, "t") == (True, expected)


def test_a_table_is_built_at_the_configured_size_and_guarded_afterwards(
    pg: tuple[str, str],
) -> None:
    dsn, schema = pg
    table = "coll"

    def store(size: int) -> PostgresVDB:
        return PostgresVDB(
            connection_string=dsn,
            collection_name=table,
            schema_name=schema,
            vector_size=size,
        )

    store(1024).create_index()

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = %s::regclass AND attname = 'embedding'",
                (f'"{schema}".{table}',),
            )
            assert cur.fetchone() == ("halfvec(1024)",)
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = %s AND tablename = %s",
                (schema, table),
            )
            definitions = [row[0] for row in cur.fetchall()]
    assert any(
        "USING hnsw" in d and "embedding" in d and "halfvec_cosine_ops" in d
        for d in definitions
    )

    with pytest.raises(VectorSizeMismatchError):
        store(768).create_index()
    store(1024).create_index()
