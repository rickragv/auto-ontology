# SPDX-License-Identifier: Apache-2.0

"""BigQuery connector against real BigQuery, or an emulator.

Collected only when ``AUTO_ONTOLOGY_LIVE_BIGQUERY_PROJECT`` is set (see
``conftest.py``). With only that variable the tests run against that Google
Cloud project with Application Default Credentials -- this is the acceptance
test::

    gcloud auth application-default login
    AUTO_ONTOLOGY_LIVE_BIGQUERY_PROJECT=my-project uv run pytest \\
        auto_ontology/connectors/tests/test_bigquery_live.py -q

``AUTO_ONTOLOGY_LIVE_BIGQUERY_LOCATION`` (optional) sets the location of the
test dataset and of every job, e.g. ``EU``. With
``AUTO_ONTOLOGY_LIVE_BIGQUERY_EMULATOR=<host>:<port>`` the same tests target a
BigQuery emulator instead; the tests for what the emulator lacks (key
constraints, ``NOT NULL`` modes, billing limits, IAM, locations) are skipped
there, and only there.

The fixture dataset ``auto_ontology_live_test`` is created by
:mod:`dev_tools.fixtures.seed_bigquery` and deleted at the end. It holds a few
kilobytes; a full run bills a few hundred megabytes of BigQuery's 10 MB
per-query minimum, inside the free tier.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import threading
import time
from collections.abc import Iterator
from decimal import Decimal

import pytest

from auto_ontology.catalog.extract import data_for_populate_tabular
from auto_ontology.connectors import bigquery as bigquery_module
from auto_ontology.connectors.base import StatementTimeout
from auto_ontology.connectors.bigquery import (
    BigQueryDatabase,
    BigQueryError,
    format_connection_string,
)
from auto_ontology.connectors.db_errors import is_infrastructure_error
from auto_ontology.connectors.registry import create_connector
from auto_ontology.retrieval.text_to_sql.agents.sql_parse_validation import (
    quote_known_mixed_case_identifiers,
)
from auto_ontology.retrieval.text_to_sql.formatters_util import qualify_table
from dev_tools.fixtures import seed_bigquery

PROJECT = os.environ["AUTO_ONTOLOGY_LIVE_BIGQUERY_PROJECT"]
EMULATOR = os.environ.get("AUTO_ONTOLOGY_LIVE_BIGQUERY_EMULATOR") or None
LOCATION = os.environ.get("AUTO_ONTOLOGY_LIVE_BIGQUERY_LOCATION") or None
DATASET = "auto_ontology_live_test"

real_only = pytest.mark.skipif(
    EMULATOR is not None, reason="needs real BigQuery; the emulator lacks this"
)

# Never finishes within the caps below. RAND() keeps it out of the query cache.
# Smaller on the emulator, which keeps running a query its client abandoned.
_SIDE = 3000 if EMULATOR else 100_000
_SLOW_SQL = (
    f"SELECT COUNT(*) AS n FROM UNNEST(GENERATE_ARRAY(1, {_SIDE})) AS a "
    f"CROSS JOIN UNNEST(GENERATE_ARRAY(1, {_SIDE})) AS b WHERE RAND() >= 0"
)


def _url(dataset: str | None = DATASET, **params: object) -> str:
    return format_connection_string(
        project=PROJECT,
        dataset=dataset,
        emulator_host=EMULATOR,
        location=LOCATION,
        **params,  # type: ignore[arg-type]
    )


@pytest.fixture(scope="module")
def url() -> Iterator[str]:
    seeded = seed_bigquery.seed(
        PROJECT, DATASET, emulator_host=EMULATOR, location=LOCATION
    )
    yield seeded
    seed_bigquery.delete(PROJECT, DATASET, emulator_host=EMULATOR, location=LOCATION)


@pytest.fixture(scope="module")
def connector(url: str) -> Iterator[BigQueryDatabase]:
    database = create_connector(url)
    assert isinstance(database, BigQueryDatabase)
    yield database
    database.close()


def test_targets_what_the_environment_says(url: str) -> None:
    assert url == _url()
    assert ("emulator_host=" in url) == (EMULATOR is not None)


# ----------------------------------------------------------------------
# Introspection
# ----------------------------------------------------------------------


def test_get_tables(connector: BigQueryDatabase) -> None:
    assert connector.get_tables().values.tolist() == [
        [DATASET, "Albums", "base table"],
        [DATASET, "Concerts", "base table"],
        [DATASET, "SingerRevenue", "view"],
        [DATASET, "Singers", "base table"],
        [DATASET, "TicketSales", "base table"],
        [DATASET, "Venues", "base table"],
    ]


def test_get_columns(connector: BigQueryDatabase) -> None:
    frame = connector.get_columns()
    assert frame.is_nullable.dtype == bool
    singers = frame[frame.table_name == "Singers"].set_index("column_name")
    assert singers["data_type"].to_dict() == {
        "SingerId": "INT64",
        "FirstName": "STRING",
        "LastName": "STRING",
        "BirthDate": "DATE",
        "IsActive": "BOOL",
        "Genres": "ARRAY<STRING>",
        "Profile": "JSON",
        "Address": "STRUCT<city STRING, country STRING>",
    }
    assert list(singers["ordinal_position"]) == list(range(1, 9))
    assert singers.loc["FirstName", "is_nullable"]
    assert not singers.loc["Genres", "is_nullable"]
    albums = frame[frame.table_name == "Albums"].set_index("column_name")
    assert albums.loc["Price", "data_type"] == "NUMERIC"
    concerts = frame[frame.table_name == "Concerts"].set_index("column_name")
    assert concerts.loc["StartTime", "data_type"] == "TIMESTAMP"
    assert set(frame[frame.table_name == "SingerRevenue"].column_name) == {
        "SingerId",
        "LastName",
        "TotalRevenue",
    }


@real_only
def test_not_null_columns_are_not_nullable(connector: BigQueryDatabase) -> None:
    """The emulator reports every non-repeated column as NULLABLE."""
    frame = connector.get_columns()
    singers = frame[frame.table_name == "Singers"].set_index("column_name")
    assert not singers.loc["SingerId", "is_nullable"]
    assert not singers.loc["LastName", "is_nullable"]
    assert not singers.loc["IsActive", "is_nullable"]
    assert singers.loc["BirthDate", "is_nullable"]


def test_get_views(connector: BigQueryDatabase) -> None:
    frame = connector.get_views()
    assert frame[["table_schema", "table_name"]].values.tolist() == [
        [DATASET, "SingerRevenue"]
    ]
    assert "TicketRevenue" in frame.view_definition.iloc[0]


@real_only
def test_view_definition_is_the_text_as_written(connector: BigQueryDatabase) -> None:
    """The emulator returns its own rewritten SQL instead."""
    definition = connector.get_views().view_definition.iloc[0]
    assert "SUM(Concerts.TicketRevenue) AS TotalRevenue" in definition


@real_only
def test_get_pks(connector: BigQueryDatabase) -> None:
    """The emulator accepts PRIMARY KEY but does not report it."""
    keys = {
        table: list(group.sort_values("ordinal_position").column_name)
        for (_, table), group in connector.get_pks().groupby(
            ["table_schema", "table_name"]
        )
    }
    assert keys == {
        "Albums": ["SingerId", "AlbumId"],
        "Concerts": ["VenueId", "SingerId", "ConcertDate"],
        "Singers": ["SingerId"],
        "TicketSales": ["SaleId"],
        "Venues": ["VenueId"],
    }


@real_only
def test_get_fks_declared_and_multi_column(connector: BigQueryDatabase) -> None:
    """The emulator rejects FOREIGN KEY constraints."""
    d = DATASET
    assert {tuple(row) for row in connector.get_fks().values.tolist()} == {
        (d, "Albums", "SingerId", d, "Singers", "SingerId"),
        (d, "Concerts", "VenueId", d, "Venues", "VenueId"),
        (d, "Concerts", "SingerId", d, "Singers", "SingerId"),
        (d, "TicketSales", "VenueId", d, "Concerts", "VenueId"),
        (d, "TicketSales", "SingerId", d, "Concerts", "SingerId"),
        (d, "TicketSales", "ConcertDate", d, "Concerts", "ConcertDate"),
    }


def test_get_queries_is_empty(connector: BigQueryDatabase) -> None:
    frame = connector.get_queries()
    assert frame.empty and list(frame.columns) == ["end_time", "query_text"]


def test_ingestion_extract_accepts_the_catalog(connector: BigQueryDatabase) -> None:
    """What ingestion reads first, including its column-coverage check."""
    data = data_for_populate_tabular(connector)
    assert data["database_name"] == PROJECT
    assert len(data["tables"]) == 6
    assert len(data["fks"]) == (0 if EMULATOR else 6)
    assert len(data["pks"]) == (0 if EMULATOR else 8)


def test_metadata_bills_no_query_bytes(url: str) -> None:
    """Introspection uses the tables API, so a 1-byte limit does not stop it."""
    limited = BigQueryDatabase(_url(maximum_bytes_billed=1))
    try:
        assert len(limited.get_columns()) == 32
        assert len(limited.get_tables()) == 6
    finally:
        limited.close()


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def test_values_come_back_as_python_types(connector: BigQueryDatabase) -> None:
    frame = connector.execute(
        "SELECT s.SingerId, s.BirthDate, s.Genres, s.Profile, s.Address, "
        "c.StartTime, c.TicketRevenue "
        "FROM Singers AS s JOIN Concerts AS c ON c.SingerId = s.SingerId "
        "WHERE s.SingerId = 1"
    )
    row = frame.iloc[0]
    assert row.SingerId == 1
    assert row.BirthDate == dt.date(1970, 9, 3)
    assert list(row.Genres) == ["rock", "blues"]
    assert row.Profile == {"label": "Northside", "agent": {"name": "Ann"}}
    assert row.Address == {"city": "Seattle", "country": "US"}
    assert row.StartTime == dt.datetime(2024, 4, 5, 20, tzinfo=dt.timezone.utc)
    assert row.TicketRevenue == Decimal("125000")


def test_empty_result_keeps_its_columns(connector: BigQueryDatabase) -> None:
    frame = connector.execute("SELECT SingerId, LastName FROM Singers WHERE FALSE")
    assert frame.empty and list(frame.columns) == ["SingerId", "LastName"]


def test_names_as_the_prompts_write_them(connector: BigQueryDatabase) -> None:
    """Every relation, named exactly as the text-to-SQL prompt names it."""
    for schema, table in connector.get_tables()[
        ["table_schema", "table_name"]
    ].itertuples(index=False):
        name = qualify_table(PROJECT, schema, table, connector.dialect)
        assert name == f"{PROJECT}.{DATASET}.{table}"
        frame = connector.execute(f"SELECT COUNT(*) AS n FROM {name}")
        assert frame.n.iloc[0] > 0, name


def test_qualify_output_runs(connector: BigQueryDatabase) -> None:
    reference = connector.qualify(DATASET, "Singers")
    assert reference == f"`{PROJECT}`.`{DATASET}`.`Singers`"
    assert connector.execute(f"SELECT COUNT(*) AS n FROM {reference}").n.iloc[0] == 4


@pytest.mark.parametrize(
    "reference",
    [
        f"{PROJECT}.{DATASET}.Singers",
        f"`{PROJECT}`.{DATASET}.Singers",
        f"`{PROJECT}.{DATASET}.Singers`",
        f"`{PROJECT}`.`{DATASET}`.`Singers`",
        f"{PROJECT}.{DATASET}.`Singers`",
        f"{DATASET}.Singers",
        "Singers",
    ],
)
def test_name_shapes(connector: BigQueryDatabase, reference: str) -> None:
    sql = f"SELECT COUNT(*) AS n FROM {reference} AS s WHERE s.SingerId > 0"
    assert connector.execute(sql).n.iloc[0] == 4


def test_two_part_names_resolve_without_a_dataset_in_the_url(url: str) -> None:
    connector = BigQueryDatabase(_url(dataset=None))
    try:
        frame = connector.execute(f"SELECT COUNT(*) AS n FROM {DATASET}.Singers")
        assert frame.n.iloc[0] == 4
        assert DATASET in set(connector.get_tables().table_schema)
    finally:
        connector.close()


@real_only
def test_implicit_alias_of_a_quoted_full_path(connector: BigQueryDatabase) -> None:
    """BigQuery names ```p.d.Singers``` ``Singers``; the emulator does not."""
    frame = connector.execute(
        f"SELECT Singers.LastName FROM `{PROJECT}.{DATASET}.Singers` "
        "ORDER BY Singers.SingerId LIMIT 1"
    )
    assert frame.LastName.tolist() == ["Richards"]


def test_join_answers_a_revenue_question(connector: BigQueryDatabase) -> None:
    frame = connector.execute(
        f"SELECT s.LastName, SUM(c.TicketRevenue) AS revenue "
        f"FROM {PROJECT}.{DATASET}.Singers AS s "
        f"JOIN {PROJECT}.{DATASET}.Concerts AS c ON c.SingerId = s.SingerId "
        "WHERE c.TicketRevenue IS NOT NULL "
        "GROUP BY s.LastName ORDER BY revenue DESC"
    )
    assert frame.LastName.tolist() == ["Martin", "Smith", "Richards"]
    assert frame.revenue.iloc[0] == Decimal("210000.75")


def test_sqlglot_requoting_still_runs(connector: BigQueryDatabase) -> None:
    """Generated SQL is re-quoted by sqlglot (bigquery) before it is executed."""
    tables = [
        {"name": table, "columns": [{"name": name} for name in group.column_name]}
        for table, group in connector.get_columns().groupby("table_name")
    ]
    p, d = PROJECT, DATASET
    statements = [
        f"SELECT VenueId FROM {p}.{d}.Concerts "
        "WHERE ConcertDate >= DATE_SUB(DATE '2024-08-01', INTERVAL 90 DAY)",
        f"SELECT LastName FROM {p}.{d}.Singers "
        "WHERE JSON_VALUE(Profile, '$.label') = 'Northside'",
        f"SELECT LastName FROM {p}.{d}.Singers WHERE 'rock' IN UNNEST(Genres)",
        f"SELECT LastName FROM {p}.{d}.Singers WHERE Address.city = 'Lyon'",
        f"SELECT v.VenueName FROM {p}.{d}.Venues AS v "
        f"JOIN {p}.{d}.Concerts AS c ON c.VenueId = v.VenueId",
        f"SELECT r.LastName FROM {p}.{d}.SingerRevenue AS r "
        "WHERE r.TotalRevenue IS NOT NULL",
    ]
    for sql in statements:
        rewritten = quote_known_mixed_case_identifiers(sql, tables, connector.dialect)
        assert rewritten != sql
        assert not connector.execute(rewritten).empty, rewritten


def test_statement_timeout(connector: BigQueryDatabase) -> None:
    started = time.monotonic()
    with pytest.raises(StatementTimeout):
        connector.execute(_SLOW_SQL, timeout_s=1.0)
    assert time.monotonic() - started < 15
    # The connection is still usable afterwards.
    assert connector.execute("SELECT 1 AS x").x.iloc[0] == 1


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO Venues (VenueId, VenueName, City) VALUES (99, 'X', 'Y')",
        "UPDATE Venues SET City = 'Z' WHERE TRUE",
        "DELETE FROM Venues WHERE TRUE",
        "DROP TABLE Venues",
        "SELECT 1; DELETE FROM Venues WHERE TRUE",
    ],
)
def test_writes_are_refused(connector: BigQueryDatabase, statement: str) -> None:
    with pytest.raises(BigQueryError):
        connector.execute(statement)
    assert connector.execute("SELECT COUNT(*) AS n FROM Venues").n.iloc[0] == 3


def test_sql_errors_are_short_and_not_infrastructure(
    connector: BigQueryDatabase,
) -> None:
    statement = "SELECT NoSuchColumn FROM Singers"
    with pytest.raises(BigQueryError) as raised:
        connector.execute(statement)
    message = str(raised.value)
    assert "NoSuchColumn" in message
    assert "https://" not in message and "Job ID" not in message
    assert not is_infrastructure_error(raised.value, statement)


def test_ping(connector: BigQueryDatabase) -> None:
    connector.ping()


def test_concurrent_statements_share_one_connector(
    connector: BigQueryDatabase,
) -> None:
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

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert errors == []
    expected = {1: 2, 2: 4, 3: 5, 4: 6}
    assert results == {i: expected[i % 4 + 1] for i in range(8)}


# ----------------------------------------------------------------------
# Settings that only real BigQuery enforces
# ----------------------------------------------------------------------


@real_only
def test_maximum_bytes_billed_is_enforced(url: str) -> None:
    """Any query reading a table bills at least 10 MB, so 1 byte is too little."""
    limited = BigQueryDatabase(_url(maximum_bytes_billed=1))
    try:
        with pytest.raises(BigQueryError) as raised:
            limited.execute("SELECT * FROM Singers")
    finally:
        limited.close()
    assert "bytesBilledLimitExceeded" in str(raised.value)
    assert "maximum_bytes_billed=1" in str(raised.value)


@real_only
def test_location_is_applied_to_jobs(url: str) -> None:
    """A job in another location cannot see the dataset."""
    other = "US" if (LOCATION or "US").upper() == "EU" else "EU"
    elsewhere = BigQueryDatabase(
        format_connection_string(project=PROJECT, dataset=DATASET, location=other)
    )
    try:
        with pytest.raises(BigQueryError) as raised:
            elsewhere.execute("SELECT COUNT(*) AS n FROM Singers")
    finally:
        elsewhere.close()
    assert "notFound" in str(raised.value)
    assert other in str(raised.value)


@real_only
def test_permission_denied_reads_clearly() -> None:
    """Nobody outside Google may run jobs in ``bigquery-public-data``."""
    connector = BigQueryDatabase("bigquery://bigquery-public-data")
    try:
        with pytest.raises(BigQueryError) as raised:
            connector.execute("SELECT 1")
    finally:
        connector.close()
    message = str(raised.value)
    assert "BigQuery denied access in project 'bigquery-public-data'" in message
    assert "accessDenied" in message


# ----------------------------------------------------------------------
# Failures
# ----------------------------------------------------------------------


def test_wrong_dataset_is_named(url: str) -> None:
    missing = BigQueryDatabase(_url(dataset="ao_no_such_dataset"))
    try:
        with pytest.raises(BigQueryError, match="could not find dataset .*ao_no_such"):
            missing.ping()
        with pytest.raises(BigQueryError, match="could not find dataset .*ao_no_such"):
            missing.get_tables()
    finally:
        missing.close()


def test_unreachable_endpoint_is_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs against a closed local port in either mode; no credentials needed."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    monkeypatch.delenv(bigquery_module.EMULATOR_ENV_VAR, raising=False)
    monkeypatch.setattr(bigquery_module, "RETRY_BUDGET_S", 2.0)
    connector = BigQueryDatabase(
        f"bigquery://{PROJECT}/{DATASET}?emulator_host=127.0.0.1:{free_port}"
    )
    started = time.monotonic()
    try:
        with pytest.raises(BigQueryError) as raised:
            connector.ping()
    finally:
        connector.close()
    assert time.monotonic() - started < 15
    assert str(raised.value).startswith("Could not connect to BigQuery at http://")
    assert is_infrastructure_error(raised.value, "SELECT 1")


# ----------------------------------------------------------------------
# Seed script
# ----------------------------------------------------------------------


def test_seeding_twice_gives_the_same_dataset() -> None:
    dataset = "auto_ontology_live_reseed"
    kwargs = {"emulator_host": EMULATOR, "location": LOCATION}
    seed_bigquery.seed(PROJECT, dataset, **kwargs)  # type: ignore[arg-type]
    url = seed_bigquery.seed(PROJECT, dataset, **kwargs)  # type: ignore[arg-type]
    connector = BigQueryDatabase(url)
    try:
        counts = connector.execute(
            "SELECT (SELECT COUNT(*) FROM Singers) AS singers, "
            "(SELECT COUNT(*) FROM TicketSales) AS sales"
        )
        assert counts.values.tolist() == [[4, 6]]
    finally:
        connector.close()
        assert seed_bigquery.delete(PROJECT, dataset, **kwargs)  # type: ignore[arg-type]
    assert not seed_bigquery.delete(PROJECT, dataset, **kwargs)  # type: ignore[arg-type]


def test_seed_refuses_a_dataset_it_did_not_create() -> None:
    from google.cloud import bigquery

    dataset = "auto_ontology_live_unlabelled"
    client = seed_bigquery._client(PROJECT, EMULATOR, LOCATION)
    reference = f"{PROJECT}.{dataset}"
    try:
        client.delete_dataset(reference, delete_contents=True, not_found_ok=True)
        resource = bigquery.Dataset(reference)
        if LOCATION is not None:
            resource.location = LOCATION
        client.create_dataset(resource)
        kwargs = {"emulator_host": EMULATOR, "location": LOCATION}
        with pytest.raises(seed_bigquery.SeedRefused, match="did not create it"):
            seed_bigquery.seed(PROJECT, dataset, **kwargs)  # type: ignore[arg-type]
        with pytest.raises(seed_bigquery.SeedRefused, match="did not create it"):
            seed_bigquery.delete(PROJECT, dataset, **kwargs)  # type: ignore[arg-type]
        assert client.get_dataset(reference).dataset_id == dataset
    finally:
        client.delete_dataset(reference, delete_contents=True, not_found_ok=True)
        client.close()
