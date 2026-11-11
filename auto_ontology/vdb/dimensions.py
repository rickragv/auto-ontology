# SPDX-License-Identifier: Apache-2.0

"""Embedding size for the vector tables, and the guard against a table of another size.

``EMBED_DIMENSIONS`` sets how many dimensions the vector tables are created with.
Unset, :class:`~auto_ontology.vdb.postgres.PostgresVDB` keeps its own default.
It must match the output size of the embedding model (``EMBED_MODEL``).

A table keeps the size it was created with. Changing the setting, or the model,
afterwards would otherwise surface only as pgvector's ``expected N dimensions,
not M`` on the first write or search, so :func:`check_vector_size` turns that
into an error that says what happened and how to rebuild.
"""

from __future__ import annotations

import os

import psycopg

EMBED_DIMENSIONS_ENV = "EMBED_DIMENSIONS"

#: The widest ``halfvec`` column pgvector can build an HNSW index on.
#: ``PostgresVDB._create_vector_index`` converts every vector column to
#: ``halfvec`` and indexes it, so a wider table could be created but never
#: indexed, and every search would be a sequential scan.
MAX_INDEXED_DIMENSIONS = 4000

# One row when the table exists, none when it does not. Existence is decided by
# information_schema.tables, the same test as ``PostgresVDB._table_exists``; the
# catalog joins add the ``embedding`` column's type modifier, which for both
# pgvector ``vector(n)`` and ``halfvec(n)`` is ``n`` itself (their typmod_in
# functions return the declared dimension unchanged). It is -1 for a column
# declared without a size, and NULL when there is no ``embedding`` column.
_LOOKUP_SQL = """
    SELECT a.atttypmod
    FROM information_schema.tables AS t
    LEFT JOIN pg_catalog.pg_namespace AS n
        ON n.nspname = t.table_schema
    LEFT JOIN pg_catalog.pg_class AS c
        ON c.relnamespace = n.oid AND c.relname = t.table_name
    LEFT JOIN pg_catalog.pg_attribute AS a
        ON a.attrelid = c.oid AND a.attname = 'embedding' AND NOT a.attisdropped
    WHERE t.table_schema = %s AND t.table_name = %s
"""


class VectorSizeMismatchError(ValueError):
    """An existing vector table holds embeddings of a different size."""


def configured_embed_dimensions() -> int | None:
    """The ``EMBED_DIMENSIONS`` setting, or ``None`` when it is unset or empty.

    Read on every call, not at import, so a changed environment is honoured.
    Raises :class:`ValueError` when the value is not a whole number, is below 1,
    or is above :data:`MAX_INDEXED_DIMENSIONS`.
    """
    raw = os.environ.get(EMBED_DIMENSIONS_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{EMBED_DIMENSIONS_ENV} must be a whole number of dimensions, got {raw!r}."
        ) from None
    if value < 1:
        raise ValueError(f"{EMBED_DIMENSIONS_ENV} must be at least 1, got {raw!r}.")
    if value > MAX_INDEXED_DIMENSIONS:
        raise ValueError(
            f"{EMBED_DIMENSIONS_ENV} must be at most {MAX_INDEXED_DIMENSIONS}, "
            f"got {raw!r}: pgvector cannot build an HNSW index on a halfvec "
            f"column wider than {MAX_INDEXED_DIMENSIONS} dimensions, so every "
            "search would fall back to a sequential scan."
        )
    return value


def lookup_vector_table(
    connection_string: str, schema_name: str, table_name: str
) -> tuple[bool, int | None]:
    """Whether the table exists, and the size of its ``embedding`` column.

    One query on one connection, so callers can use it in place of an
    existence check at no extra cost. The size is ``None`` when it cannot be
    read: no ``embedding`` column, or one declared without a size.
    """
    with psycopg.connect(connection_string) as conn:
        with conn.cursor() as cur:
            cur.execute(_LOOKUP_SQL, (schema_name, table_name))
            row = cur.fetchone()
    if row is None:
        return False, None
    typmod = row[0]
    if typmod is None or typmod < 1:
        return True, None
    return True, int(typmod)


def check_vector_size(
    schema_name: str,
    table_name: str,
    existing: int | None,
    expected: int | None,
) -> None:
    """Raise :class:`VectorSizeMismatchError` if the two sizes are known and differ."""
    if existing is None or expected is None or existing == expected:
        return
    raise VectorSizeMismatchError(
        f"Vector table {schema_name}.{table_name} holds {existing}-dimension "
        f"embeddings, but the vector store is configured for {expected} "
        f"({EMBED_DIMENSIONS_ENV}; 2048 when unset). Stored embeddings cannot "
        f"be resized. Either set {EMBED_DIMENSIONS_ENV}={existing} and use the "
        "embedding model that produced them, or regenerate them at the new "
        f"size: drop {schema_name}.{table_name} and the other vector tables in "
        f"schema {schema_name} (they hold embeddings from the same model), "
        "then re-ingest every data source."
    )
