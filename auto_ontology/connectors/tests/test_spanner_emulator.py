# SPDX-License-Identifier: Apache-2.0

"""Spanner connector against a running Cloud Spanner emulator.

Collected only with ``AUTO_ONTOLOGY_LIVE_SPANNER=1`` (see ``conftest.py``).
The emulator is found at ``SPANNER_EMULATOR_HOST`` when set, else
``localhost:9010``::

    docker run -d -p 9010:9010 -p 9020:9020 gcr.io/cloud-spanner-emulator/emulator
    AUTO_ONTOLOGY_LIVE_SPANNER=1 uv run pytest \\
        auto_ontology/connectors/tests/test_spanner_emulator.py -q

The fixture database is (re)created by
:mod:`dev_tools.fixtures.seed_spanner_emulator` under its own project and
instance, so it does not disturb a database seeded by hand. Its id contains a
hyphen on purpose: that is the hardest spelling for the qualifier removal.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from auto_ontology.catalog.extract import data_for_populate_tabular
from auto_ontology.connectors import spanner as spanner_module
from auto_ontology.connectors.base import StatementTimeout
from auto_ontology.connectors.db_errors import is_infrastructure_error
from auto_ontology.connectors.registry import create_connector
from auto_ontology.connectors.spanner import SpannerDatabase, SpannerError
from auto_ontology.retrieval.text_to_sql.agents.sql_parse_validation import (
    quote_known_mixed_case_identifiers,
)
from auto_ontology.retrieval.text_to_sql.formatters_util import qualify_table
from dev_tools.fixtures import seed_spanner_emulator

HOST = os.environ.get("SPANNER_EMULATOR_HOST") or "localhost:9010"
PROJECT = "auto-ontology-test"
INSTANCE = "auto-ontology-test"
DATABASE = "ao-test-music"

# Large enough that the emulator cannot finish it within the caps used below.
_SLOW_SQL = (
    "SELECT COUNT(*) AS n FROM UNNEST(GENERATE_ARRAY(1, 15000)) AS a "
    "CROSS JOIN UNNEST(GENERATE_ARRAY(1, 15000)) AS b"
)


@pytest.fixture(scope="module")
def url() -> str:
    return seed_spanner_emulator.seed(HOST, PROJECT, INSTANCE, DATABASE)


@pytest.fixture(scope="module")
def connector(url: str) -> Iterator[SpannerDatabase]:
    database = create_connector(url)
    assert isinstance(database, SpannerDatabase)
    yield database
    database.close()


# ----------------------------------------------------------------------
# Introspection
# ----------------------------------------------------------------------


def test_get_tables(connector: SpannerDatabase) -> None:
    rows = connector.get_tables().values.tolist()
    assert rows == [
        [DATABASE, "Albums", "base table"],
        [DATABASE, "Concerts", "base table"],
        [DATABASE, "SingerRevenue", "view"],
        [DATABASE, "Singers", "base table"],
        [DATABASE, "TicketSales", "base table"],
        [DATABASE, "Venues", "base table"],
        ["sales", "Promotions", "base table"],
    ]


def test_get_columns(connector: SpannerDatabase) -> None:
    frame = connector.get_columns()
    assert frame.is_nullable.dtype == bool
    singers = frame[frame.table_name == "Singers"].set_index("column_name")
    assert singers.loc["SingerId", "data_type"] == "INT64"
    assert not singers.loc["SingerId", "is_nullable"]
    assert singers.loc["FirstName", "is_nullable"]
    assert singers.loc["Genres", "data_type"] == "ARRAY<STRING(50)>"
    assert singers.loc["Profile", "data_type"] == "JSON"
    assert singers.loc["IsActive", "data_type"] == "BOOL"
    assert list(singers["ordinal_position"]) == list(range(1, 8))
    promotions = frame[frame.table_name == "Promotions"]
    assert set(promotions.table_schema) == {"sales"}
    assert set(frame[frame.table_name == "SingerRevenue"].column_name) == {
        "SingerId",
        "LastName",
        "TotalRevenue",
    }


def test_get_views(connector: SpannerDatabase) -> None:
    frame = connector.get_views()
    assert frame[["table_schema", "table_name"]].values.tolist() == [
        [DATABASE, "SingerRevenue"]
    ]
    assert "SUM(Concerts.TicketRevenue)" in frame.view_definition.iloc[0]


def test_get_pks(connector: SpannerDatabase) -> None:
    keys = {
        (schema, table): list(group.sort_values("ordinal_position").column_name)
        for (schema, table), group in connector.get_pks().groupby(
            ["table_schema", "table_name"]
        )
    }
    assert keys == {
        (DATABASE, "Albums"): ["SingerId", "AlbumId"],
        (DATABASE, "Concerts"): ["VenueId", "SingerId", "ConcertDate"],
        (DATABASE, "Singers"): ["SingerId"],
        (DATABASE, "TicketSales"): ["SaleId"],
        (DATABASE, "Venues"): ["VenueId"],
        ("sales", "Promotions"): ["PromotionId"],
    }


def test_get_fks_declared_and_interleaved(connector: SpannerDatabase) -> None:
    rows = {tuple(row) for row in connector.get_fks().values.tolist()}
    db = DATABASE
    assert rows == {
        # Declared.
        (db, "Concerts", "VenueId", db, "Venues", "VenueId"),
        (db, "Concerts", "SingerId", db, "Singers", "SingerId"),
        (db, "TicketSales", "VenueId", db, "Concerts", "VenueId"),
        (db, "TicketSales", "SingerId", db, "Concerts", "SingerId"),
        (db, "TicketSales", "ConcertDate", db, "Concerts", "ConcertDate"),
        ("sales", "Promotions", "VenueId", db, "Venues", "VenueId"),
        # Interleaved: Albums in Singers.
        (db, "Albums", "SingerId", db, "Singers", "SingerId"),
    }


def test_get_queries_is_empty(connector: SpannerDatabase) -> None:
    frame = connector.get_queries()
    assert frame.empty and list(frame.columns) == ["end_time", "query_text"]


def test_ingestion_extract_accepts_the_catalog(connector: SpannerDatabase) -> None:
    """What ingestion reads first, including its column-coverage check."""
    data = data_for_populate_tabular(connector)
    assert data["database_name"] == DATABASE
    assert len(data["tables"]) == 7
    assert len(data["fks"]) == 7


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def test_select_with_limit(connector: SpannerDatabase) -> None:
    frame = connector.execute(
        "SELECT SingerId, LastName FROM Singers ORDER BY SingerId LIMIT 2"
    )
    assert frame.values.tolist() == [[1, "Richards"], [2, "Smith"]]


def test_names_as_the_prompts_write_them(connector: SpannerDatabase) -> None:
    """Every relation, named exactly as the text-to-SQL prompt names it."""
    names = {
        f"{schema}.{table}": qualify_table(DATABASE, schema, table, connector.dialect)
        for schema, table in connector.get_tables()[
            ["table_schema", "table_name"]
        ].itertuples(index=False)
    }
    assert names[f"{DATABASE}.Singers"] == f"{DATABASE}.Singers"
    assert names["sales.Promotions"] == f"{DATABASE}.sales.Promotions"
    for name in names.values():
        frame = connector.execute(f"SELECT COUNT(*) AS n FROM {name}")
        assert frame.n.iloc[0] > 0, name


@pytest.mark.parametrize(
    "reference",
    [
        f"`{DATABASE}`.Singers",
        f"`{DATABASE}`.`Singers`",
        f"`{DATABASE}.Singers`",
        f"{DATABASE.upper()}.Singers",
    ],
)
def test_quoted_database_qualifiers(connector: SpannerDatabase, reference: str) -> None:
    assert connector.execute(f"SELECT COUNT(*) AS n FROM {reference}").n.iloc[0] == 4


def test_quoted_path_into_a_named_schema(connector: SpannerDatabase) -> None:
    frame = connector.execute(
        f"SELECT Code FROM `{DATABASE}.sales.Promotions` ORDER BY 1"
    )
    assert frame.Code.tolist() == ["AUSTIN15", "SPRING24"]


def test_join_answers_a_revenue_question(connector: SpannerDatabase) -> None:
    frame = connector.execute(
        f"SELECT s.LastName, SUM(c.TicketRevenue) AS revenue "
        f"FROM {DATABASE}.Singers AS s "
        f"JOIN {DATABASE}.Concerts AS c ON c.SingerId = s.SingerId "
        "WHERE c.TicketRevenue IS NOT NULL "
        "GROUP BY s.LastName ORDER BY revenue DESC"
    )
    assert frame.LastName.tolist() == ["Martin", "Smith", "Richards"]
    assert str(frame.revenue.iloc[0]) == "210000.75"


def test_sqlglot_requoting_still_runs(connector: SpannerDatabase) -> None:
    """Generated SQL is re-quoted by sqlglot (bigquery) before it is executed."""
    tables = [
        {
            "name": table,
            "columns": [{"name": name} for name in group.column_name],
        }
        for table, group in connector.get_columns().groupby("table_name")
    ]
    statements = [
        f"SELECT VenueId FROM {DATABASE}.Concerts "
        "WHERE ConcertDate >= DATE_SUB(DATE '2024-08-01', INTERVAL 90 DAY)",
        f"SELECT LastName FROM {DATABASE}.Singers "
        "WHERE JSON_VALUE(Profile, '$.label') = 'Northside'",
        f"SELECT LastName FROM {DATABASE}.Singers WHERE 'rock' IN UNNEST(Genres)",
        f"SELECT p.Code FROM {DATABASE}.sales.Promotions AS p "
        f"JOIN {DATABASE}.Venues AS v ON v.VenueId = p.VenueId",
        f"SELECT r.LastName FROM {DATABASE}.SingerRevenue AS r "
        "WHERE r.TotalRevenue IS NOT NULL",
    ]
    for sql in statements:
        rewritten = quote_known_mixed_case_identifiers(sql, tables, connector.dialect)
        assert rewritten != sql
        assert not connector.execute(rewritten).empty, rewritten


def test_statement_timeout(connector: SpannerDatabase) -> None:
    started = time.monotonic()
    with pytest.raises(StatementTimeout):
        connector.execute(_SLOW_SQL, timeout_s=0.5)
    assert time.monotonic() - started < 5
    # The connection is still usable afterwards.
    assert connector.execute("SELECT 1 AS x").x.iloc[0] == 1


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO Venues (VenueId, VenueName, City) VALUES (99, 'X', 'Y')",
        "UPDATE Venues SET City = 'Z' WHERE TRUE",
        "DELETE FROM Venues WHERE TRUE",
        "DROP TABLE Venues",
    ],
)
def test_writes_are_refused(connector: SpannerDatabase, statement: str) -> None:
    with pytest.raises(SpannerError):
        connector.execute(statement)
    assert connector.execute("SELECT COUNT(*) AS n FROM Venues").n.iloc[0] == 3


def test_ping(connector: SpannerDatabase) -> None:
    connector.ping()


def test_concurrent_statements_share_one_connector(connector: SpannerDatabase) -> None:
    """Profiling runs tables on a thread pool against one connector."""
    results: dict[int, int] = {}
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            frame = connector.execute(
                f"SELECT COUNT(*) AS n FROM Albums WHERE SingerId <= {index % 4 + 1}"
            )
            results[index] = int(frame.n.iloc[0])
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    expected = {1: 2, 2: 4, 3: 5, 4: 6}
    assert results == {i: expected[i % 4 + 1] for i in range(16)}


# ----------------------------------------------------------------------
# Failures
# ----------------------------------------------------------------------


def test_wrong_database_id(url: str) -> None:
    missing = SpannerDatabase(url.replace(f"/{DATABASE}?", "/no-such-db?"))
    try:
        with pytest.raises(SpannerError, match="could not find .*no-such-db"):
            missing.ping()
    finally:
        missing.close()


def test_postgresql_dialect_database_is_refused(url: str) -> None:
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import spanner
    from google.cloud.spanner_admin_database_v1 import DatabaseDialect

    client = spanner.Client(
        project=PROJECT,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": HOST},
        disable_builtin_metrics=True,
    )
    instance = client.instance(INSTANCE)
    database = instance.database(
        "ao-test-pg", database_dialect=DatabaseDialect.POSTGRESQL
    )
    if not database.exists():
        database.create().result(120)

    connector = SpannerDatabase(url.replace(f"/{DATABASE}?", "/ao-test-pg?"))
    try:
        with pytest.raises(SpannerError, match="PostgreSQL-dialect"):
            connector.ping()
    finally:
        connector.close()


def test_unreachable_emulator_is_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    monkeypatch.setattr(spanner_module, "UNAVAILABLE_BUDGET_S", 2.0)
    connector = SpannerDatabase(
        f"spanner://{PROJECT}/{INSTANCE}/{DATABASE}?emulator_host=127.0.0.1:{free_port}"
    )
    started = time.monotonic()
    try:
        with pytest.raises(SpannerError) as raised:
            connector.ping()
    finally:
        connector.close()
    assert time.monotonic() - started < 15
    assert is_infrastructure_error(raised.value, "SELECT 1")


class _Proxy:
    """A TCP relay to the emulator that can be cut, as if the server died."""

    def __init__(self, target: tuple[str, int]) -> None:
        self._target = target
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._sockets: list[socket.socket] = []
        self._stopped = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            upstream = socket.create_connection(self._target)
            self._sockets += [client, upstream]
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(
                    target=self._pump, args=(source, sink), daemon=True
                ).start()

    @staticmethod
    def _pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass

    def cut(self) -> None:
        self._stopped.set()
        self._listener.close()
        for sock in self._sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def test_server_lost_after_connecting_does_not_hang(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the channel watch the client library retries this forever."""
    host, port = HOST.rsplit(":", 1)
    proxy = _Proxy((host, int(port)))
    monkeypatch.setattr(spanner_module, "UNAVAILABLE_BUDGET_S", 2.0)
    connector = SpannerDatabase(
        f"spanner://{PROJECT}/{INSTANCE}/{DATABASE}"
        f"?emulator_host=127.0.0.1:{proxy.port}"
    )
    try:
        assert connector.execute("SELECT 1 AS x").x.iloc[0] == 1
        proxy.cut()
        started = time.monotonic()
        with pytest.raises(SpannerError, match="Could not connect to Spanner"):
            connector.execute("SELECT 2 AS x")
        assert time.monotonic() - started < 15
    finally:
        connector.close()


def test_seeding_twice_gives_the_same_database() -> None:
    database = "ao-test-reseed"
    seed_spanner_emulator.seed(HOST, PROJECT, INSTANCE, database)
    url = seed_spanner_emulator.seed(HOST, PROJECT, INSTANCE, database)
    connector = SpannerDatabase(url)
    try:
        counts = connector.execute(
            "SELECT (SELECT COUNT(*) FROM Singers) AS singers, "
            "(SELECT COUNT(*) FROM TicketSales) AS sales"
        )
        assert counts.values.tolist() == [[4, 6]]
    finally:
        connector.close()
