# SPDX-License-Identifier: Apache-2.0

"""Embedding size for the vector tables, and the guard against a table of another size.

``EMBED_DIMENSIONS`` sets how many dimensions the vector tables are created with.
Unset, :class:`~auto_ontology.vdb.postgres.PostgresVDB` keeps its own default.
It must match the output size of the embedding model (``EMBED_MODEL``).

A table keeps the size it was created with. Changing the setting, or the model,
afterwards would otherwise surface only as pgvector's ``expected N dimensions,
not M`` on the first write or search, so two checks turn that into an error
that says what happened: :func:`check_vector_size` compares a table with the
setting, and :func:`check_embedding_sizes` compares what the model returned
with the setting. Both are skipped when ``EMBED_DIMENSIONS`` is unset, except
that the table check then compares against the 2048 default.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence

import psycopg

EMBED_DIMENSIONS_ENV = "EMBED_DIMENSIONS"
EMBED_MODEL_ENV = "EMBED_MODEL"

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


class EmbeddingConfigError(ValueError):
    """An embedding setting is invalid, or does not fit the vector tables.

    Every later embed, write or search fails the same way and retrying cannot
    help, so the broad handlers on the compile, question and save paths let it
    through instead of logging it and carrying on. A ``ValueError``, so the
    routes that answer one with 422 answer this the same way.
    """


class VectorSizeMismatchError(EmbeddingConfigError):
    """An existing vector table holds embeddings of a different size."""


class EmbeddingSizeMismatchError(VectorSizeMismatchError):
    """The embedding model returned vectors of a size other than ``EMBED_DIMENSIONS``.

    A :class:`VectorSizeMismatchError`, because writing or searching with them
    would fail the same way against every table built at the configured size.
    """


def configured_embed_dimensions() -> int | None:
    """The ``EMBED_DIMENSIONS`` setting, or ``None`` when it is unset or empty.

    Read on every call, not at import, so a changed environment is honoured.
    Raises :class:`EmbeddingConfigError` when the value is not a whole number,
    is below 1, or is above :data:`MAX_INDEXED_DIMENSIONS`.
    """
    raw = os.environ.get(EMBED_DIMENSIONS_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise EmbeddingConfigError(
            f"{EMBED_DIMENSIONS_ENV} must be a whole number of dimensions, got {raw!r}."
        ) from None
    if value < 1:
        raise EmbeddingConfigError(
            f"{EMBED_DIMENSIONS_ENV} must be at least 1, got {raw!r}."
        )
    if value > MAX_INDEXED_DIMENSIONS:
        raise EmbeddingConfigError(
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
        "embedding model that produced them, or rebuild at the new size: drop "
        f"{schema_name}.{table_name} and the other vector tables in schema "
        f"{schema_name}, re-ingest every data source, then re-save or reset "
        "the semantic layer, which a re-ingest does not re-embed (see "
        "docs/model-providers.md)."
    )


def check_embedding_sizes(
    model_name: str,
    vectors: Iterable[Sequence[float] | None],
    expected: int | None,
) -> None:
    """Raise :class:`EmbeddingSizeMismatchError` if a vector's length is not *expected*.

    *expected* is :func:`configured_embed_dimensions`. ``None``, the setting
    unset, checks nothing. ``None`` vectors, for blank texts that were never
    sent, are skipped.
    """
    if expected is None:
        return
    for vector in vectors:
        if vector is not None and len(vector) != expected:
            raise EmbeddingSizeMismatchError(
                f"The embedding model {model_name!r} ({EMBED_MODEL_ENV}) returned "
                f"{len(vector)}-dimension vectors, but {EMBED_DIMENSIONS_ENV} is "
                f"{expected}. Use a model that returns {expected} dimensions, or "
                f"set {EMBED_DIMENSIONS_ENV}={len(vector)} and rebuild the vector "
                "tables at that size (see docs/model-providers.md)."
            )
