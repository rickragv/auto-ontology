# SPDX-License-Identifier: Apache-2.0

"""Spanner connector, against a fake client: no server needed.

The emulator-backed tests are in ``test_spanner_emulator.py`` and only run with
``AUTO_ONTOLOGY_LIVE_SPANNER=1``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import pandas as pd
import pytest
from google.api_core import exceptions as api

from auto_ontology.connectors import spanner as spanner_module
from auto_ontology.connectors.base import StatementTimeout
from auto_ontology.connectors.connection_string_factory import build_connection_string
from auto_ontology.connectors.db_errors import is_infrastructure_error
from auto_ontology.connectors.registry import CONNECTOR_REGISTRY, create_connector
from auto_ontology.connectors.spanner import (
    SpannerDatabase,
    SpannerError,
    strip_database_qualifier,
)

URL = "spanner://my-project/my-instance/music"
EMULATOR_URL = URL + "?emulator_host=localhost:9010"

# ----------------------------------------------------------------------
# Fake client
# ----------------------------------------------------------------------


@dataclass
class _State:
    name: str


class FakeChannel:
    def __init__(self, state: str = "READY") -> None:
        self.state = state
        self.callbacks: list[Callable[[Any], None]] = []

    def subscribe(self, callback: Callable[[Any], None], try_to_connect: bool) -> None:
        self.callbacks.append(callback)
        callback(_State(self.state))

    def unsubscribe(self, callback: Callable[[Any], None]) -> None:
        self.callbacks.remove(callback)

    def set_state(self, state: str) -> None:
        self.state = state
        for callback in list(self.callbacks):
            callback(_State(state))


class FakeTransport:
    def __init__(self, channel: FakeChannel) -> None:
        self.grpc_channel = channel
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


@dataclass
class FakeField:
    name: str


class FakeResult:
    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.fields = [FakeField(name) for name in columns]
        self._rows = rows

    def __iter__(self) -> Iterator[list[Any]]:
        return iter(self._rows)


Responder = Callable[[str, "FakeDatabase"], Any]


@dataclass
class FakeDatabase:
    responder: Responder
    channel: FakeChannel = field(default_factory=FakeChannel)
    statements: list[tuple[str, float | None]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.spanner_api = type("Api", (), {})()
        self.spanner_api.transport = FakeTransport(self.channel)

    @property
    def transport(self) -> FakeTransport:
        return self.spanner_api.transport

    def snapshot(self) -> "FakeSnapshot":
        return FakeSnapshot(self)


class FakeSnapshot:
    def __init__(self, database: FakeDatabase) -> None:
        self._database = database

    def __enter__(self) -> "FakeSnapshot":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute_sql(self, sql: str, timeout: float | None = None) -> FakeResult:
        self._database.statements.append((sql, timeout))
        if self._database.transport.closed.is_set():
            raise ValueError("Cannot invoke RPC: Channel closed!")
        answer = self._database.responder(sql, self._database)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def googlesql(sql: str, database: FakeDatabase) -> FakeResult | None:
    if "DATABASE_OPTIONS" in sql:
        return FakeResult(["OPTION_VALUE"], [["GOOGLE_STANDARD_SQL"]])
    return None


def respond(table: dict[str, FakeResult | BaseException]) -> Responder:
    """Answer the dialect probe, then the first entry whose key is in the SQL."""

    def responder(sql: str, database: FakeDatabase) -> Any:
        dialect = googlesql(sql, database)
        if dialect is not None:
            return dialect
        for marker, answer in table.items():
            if marker in sql:
                return answer
        return FakeResult(["x"], [[1]])

    return responder


@pytest.fixture(autouse=True)
def _no_emulator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(spanner_module.EMULATOR_ENV_VAR, raising=False)


class Opened(list):
    """Every fake database a client open returned, plus the ones queued next."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: list[FakeDatabase] = []


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> Opened:
    """Make every client open return the next queued fake database.

    With nothing queued, a new fake answering like the previous one is used.
    """
    databases = Opened()

    def fake_open(settings: dict[str, str | None]) -> tuple[object, FakeDatabase]:
        spanner_module._check_emulator_environment(settings["emulator_host"])
        if databases.pending:
            database = databases.pending.pop(0)
        else:
            database = FakeDatabase(
                databases[-1].responder if databases else respond({})
            )
        databases.append(database)
        return object(), database

    monkeypatch.setattr(spanner_module, "_open_client", fake_open)
    return databases


def connector_with(
    opened: Opened,
    table: dict[str, FakeResult | BaseException] | None = None,
    responder: Responder | None = None,
    url: str = URL,
) -> SpannerDatabase:
    opened.pending.append(FakeDatabase(responder or respond(table or {})))
    return SpannerDatabase(url)


# ----------------------------------------------------------------------
# Connection string
# ----------------------------------------------------------------------


def test_parses_project_instance_and_database() -> None:
    settings = spanner_module._parse_connection_string(URL)
    assert settings == {
        "project": "my-project",
        "instance": "my-instance",
        "database": "music",
        "emulator_host": None,
    }


def test_parses_emulator_host_and_domain_scoped_project() -> None:
    settings = spanner_module._parse_connection_string(
        "spanner://example.com:my-project/inst/db_1?emulator_host=localhost:9010"
    )
    assert settings["project"] == "example.com:my-project"
    assert settings["database"] == "db_1"
    assert settings["emulator_host"] == "localhost:9010"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("postgresql://p/i/d", "Not a Spanner URL"),
        ("spanner+grpc://p/i/d", "Not a Spanner URL"),
        ("spanner://p/i", "exactly a project, an instance and a database"),
        ("spanner://p/i/d/x", "exactly a project, an instance and a database"),
        ("spanner://p/i/d/", "exactly a project, an instance and a database"),
        ("spanner://p", "exactly a project, an instance and a database"),
        ("spanner:///i/d", "empty project"),
        ("spanner://p//d", "empty instance"),
        ("spanner://user@p/i/d", "carry no user or password"),
        ("spanner://user:secret@p/i/d", "carry no user or password"),
        ("spanner://p/i/d#frag", "Unexpected"),
        ("spanner://p/i/my%20db", "Invalid Spanner database id"),
        ("spanner://p/1inst/d", "Invalid Spanner instance id"),
        ("spanner://p/i/INFORMATION_SCHEMA", "system schema"),
        ("spanner://p/i/spanner_sys", "system schema"),
        ("spanner://p/i/d?schema=x", "Unknown Spanner URL parameter"),
        ("spanner://p/i/d?emulator_host=a:1&emulator_host=b:2", "only once"),
        ("spanner://p/i/d?emulator_host=http://localhost:9010", "no scheme"),
        ("spanner://p/i/d?emulator_host=localhost", "no scheme"),
        ("spanner://p/i/d?emulator_host=", "Invalid emulator_host"),
    ],
)
def test_rejects_malformed_urls(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SpannerDatabase(url)


def test_constructing_does_not_open_a_client(opened: Opened) -> None:
    SpannerDatabase(URL)
    assert opened == []


# ----------------------------------------------------------------------
# Connections added in the UI (connection_string_factory)
# ----------------------------------------------------------------------


def _ui_connection(**overrides: object) -> dict[str, object]:
    connection: dict[str, object] = {
        "type": "spanner",
        "project_id": "my-project",
        "instance_id": "my-instance",
        "database": "music",
        "emulator_host": "",
    }
    connection.update(overrides)
    return connection


def test_factory_builds_a_real_spanner_url_without_emulator() -> None:
    assert build_connection_string(_ui_connection()) == URL


def test_factory_adds_the_emulator_host() -> None:
    connection = _ui_connection(emulator_host=" localhost:9010 ")
    assert build_connection_string(connection) == EMULATOR_URL


@pytest.mark.parametrize(
    ("project", "instance", "database", "emulator_host"),
    [
        ("my-project", "my-instance", "music", None),
        ("example.com:my-project", "inst_1", "db-2", "localhost:9010"),
        ("p", "i", "d", "host.docker.internal:9010"),
        ("p", "i", "d", "[::1]:9010"),
        ("123-project", "Inst", "Music_DB", "10.0.0.5:65535"),
    ],
)
def test_factory_output_parses_back_to_the_same_parts(
    project: str, instance: str, database: str, emulator_host: str | None
) -> None:
    url = build_connection_string(
        _ui_connection(
            project_id=project,
            instance_id=instance,
            database=database,
            emulator_host=emulator_host or "",
        )
    )
    assert spanner_module._parse_connection_string(url) == {
        "project": project,
        "instance": instance,
        "database": database,
        "emulator_host": emulator_host,
    }
    assert SpannerDatabase(url).database_name == database


@pytest.mark.parametrize("field", ["project_id", "instance_id", "database"])
@pytest.mark.parametrize("blank", ["", "   ", None])
def test_factory_requires_every_id(field: str, blank: str | None) -> None:
    with pytest.raises(ValueError, match=f"missing required field: '{field}'"):
        build_connection_string(_ui_connection(**{field: blank}))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"project_id": "my/project"}, "Invalid Spanner project id"),
        ({"project_id": "my project"}, "Invalid Spanner project id"),
        ({"instance_id": "a/b"}, "Invalid Spanner instance id"),
        ({"instance_id": "1inst"}, "Invalid Spanner instance id"),
        ({"database": "my db"}, "Invalid Spanner database id"),
        ({"database": "music?x=1"}, "Invalid Spanner database id"),
        ({"database": "music%20"}, "Invalid Spanner database id"),
        ({"database": "INFORMATION_SCHEMA"}, "system schema"),
        ({"emulator_host": "localhost"}, "Invalid Spanner emulator host"),
        ({"emulator_host": "http://localhost:9010"}, "Invalid Spanner emulator host"),
        ({"emulator_host": "a:1&emulator_host=b:2"}, "Invalid Spanner emulator host"),
    ],
)
def test_factory_refuses_values_that_would_not_round_trip(
    overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_connection_string(_ui_connection(**overrides))


# ----------------------------------------------------------------------
# Emulator targeting
# ----------------------------------------------------------------------


def test_env_emulator_without_url_emulator_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "localhost:9010")
    with pytest.raises(ValueError, match="silently redirect"):
        SpannerDatabase(URL)


def test_env_and_url_emulator_must_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "localhost:9999")
    with pytest.raises(ValueError, match="Make them equal"):
        SpannerDatabase(EMULATOR_URL)


def test_empty_env_emulator_counts_as_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "")
    with pytest.raises(ValueError, match="silently redirect"):
        SpannerDatabase(URL)


def test_matching_env_and_url_emulator_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "localhost:9010")
    assert SpannerDatabase(EMULATOR_URL).database_name == "music"


def test_env_set_after_construction_is_refused_at_open(
    monkeypatch: pytest.MonkeyPatch, opened: Opened
) -> None:
    connector = connector_with(opened)
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "localhost:9010")
    with pytest.raises(ValueError, match="silently redirect"):
        connector.execute("SELECT 1")


def test_url_emulator_targets_the_client_without_the_environment() -> None:
    """Uses the real library: building a client makes no network call."""
    from google.auth.credentials import AnonymousCredentials

    before = dict(__import__("os").environ)
    client, database = spanner_module._open_client(
        spanner_module._parse_connection_string(EMULATOR_URL)
    )
    assert client._emulator_host == "localhost:9010"
    assert isinstance(client.credentials, AnonymousCredentials)
    assert database.name == "projects/my-project/instances/my-instance/databases/music"
    assert dict(__import__("os").environ) == before


def test_missing_default_credentials_is_a_clear_connection_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Uses the real library: an absent key file fails before any network call."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "none.json"))
    with pytest.raises(SpannerError) as raised:
        spanner_module._open_client(spanner_module._parse_connection_string(URL))
    message = str(raised.value)
    assert "Could not connect to Spanner project 'my-project'" in message
    assert "credentials" in message
    assert is_infrastructure_error(raised.value)


# ----------------------------------------------------------------------
# Identity and naming
# ----------------------------------------------------------------------


def test_registry_routes_spanner_urls() -> None:
    assert CONNECTOR_REGISTRY["spanner"] is SpannerDatabase
    connector = create_connector(URL)
    assert isinstance(connector, SpannerDatabase)
    assert connector.database_name == "music"
    assert connector.dialect == "bigquery"
    assert SpannerDatabase.dialect.fget(None) == "bigquery"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("schema", "table", "expected"),
    [
        ("music", "Singers", "`Singers`"),
        (None, "Singers", "`Singers`"),
        ("", "Singers", "`Singers`"),
        ("sales", "Promotions", "`sales`.`Promotions`"),
        ("music", "Odd`Name", "`Odd\\`Name`"),
    ],
)
def test_qualify(schema: str | None, table: str, expected: str) -> None:
    assert SpannerDatabase(URL).qualify(schema, table) == expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM music.Singers", "SELECT * FROM Singers"),
        ("SELECT * FROM MUSIC.Singers", "SELECT * FROM Singers"),
        ("SELECT * FROM `music`.Singers", "SELECT * FROM Singers"),
        ("SELECT * FROM `music`.`Singers`", "SELECT * FROM `Singers`"),
        ("SELECT * FROM `music.Singers`", "SELECT * FROM `Singers`"),
        ("SELECT * FROM music . Singers", "SELECT * FROM Singers"),
        ("SELECT * FROM music.sales.Promotions", "SELECT * FROM sales.Promotions"),
        (
            "SELECT * FROM `music.sales.Promotions`",
            "SELECT * FROM `sales`.`Promotions`",
        ),
        (
            "SELECT music.Singers.LastName FROM music.Singers",
            "SELECT Singers.LastName FROM Singers",
        ),
        (
            "SELECT s.LastName FROM music.Singers s JOIN music.Albums a "
            "ON a.SingerId = s.SingerId",
            "SELECT s.LastName FROM Singers s JOIN Albums a ON a.SingerId = s.SingerId",
        ),
        # Literals, comments and later path segments are left alone.
        (
            "SELECT 'music.Singers' AS t FROM music.Singers -- music.Singers\n"
            '/* music.Singers */ WHERE x = "music.Singers"',
            "SELECT 'music.Singers' AS t FROM Singers -- music.Singers\n"
            '/* music.Singers */ WHERE x = "music.Singers"',
        ),
        (
            "SELECT r'music.x', b'''music.y''', \"\"\"music.z\"\"\" FROM music.T",
            "SELECT r'music.x', b'''music.y''', \"\"\"music.z\"\"\" FROM T",
        ),
        ("SELECT 'it\\'s music.T' FROM music.T", "SELECT 'it\\'s music.T' FROM T"),
        ("SELECT s.music.x FROM music.T s", "SELECT s.music.x FROM T s"),
        ("SELECT t.music FROM music.T t", "SELECT t.music FROM T t"),
        ("SELECT musicals.x FROM musical.T", "SELECT musicals.x FROM musical.T"),
        ("SELECT 1.5 FROM music.T", "SELECT 1.5 FROM T"),
        # Nothing to remove.
        ("SELECT * FROM Singers", "SELECT * FROM Singers"),
    ],
)
def test_strip_database_qualifier(sql: str, expected: str) -> None:
    assert strip_database_qualifier(sql, "music") == expected


@pytest.mark.parametrize(
    "sql",
    [
        # The id is a name the statement defines itself: left as written.
        "SELECT music.LastName FROM Singers AS music",
        "SELECT music.LastName FROM Singers music",
        "WITH music AS (SELECT 1 AS x) SELECT music.x FROM music",
        "SELECT music FROM Singers",
    ],
)
def test_strip_leaves_statements_that_use_the_id_as_their_own_name(sql: str) -> None:
    assert strip_database_qualifier(sql, "music") == sql


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM ao-music.Singers", "SELECT * FROM Singers"),
        ("SELECT * FROM `ao-music`.`Singers`", "SELECT * FROM `Singers`"),
        ("SELECT * FROM `ao-music.Singers`", "SELECT * FROM `Singers`"),
        ("SELECT * FROM AO-MUSIC.sales.P", "SELECT * FROM sales.P"),
        ("SELECT ao - music FROM T", "SELECT ao - music FROM T"),
        ("SELECT 'ao-music.T' FROM ao-music.T", "SELECT 'ao-music.T' FROM T"),
    ],
)
def test_strip_hyphenated_database_ids(sql: str, expected: str) -> None:
    assert strip_database_qualifier(sql, "ao-music") == expected


def test_execute_sends_the_stripped_statement(opened: Opened) -> None:
    connector = connector_with(opened)
    connector.execute("SELECT * FROM `music`.Singers WHERE n = 'music.Singers'")
    assert opened[0].statements[-1] == (
        "SELECT * FROM Singers WHERE n = 'music.Singers'",
        None,
    )


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def test_execute_returns_a_frame_with_column_names(opened: Opened) -> None:
    connector = connector_with(
        opened, {"FROM Singers": FakeResult(["id", "name"], [[1, "a"], [2, "b"]])}
    )
    frame = connector.execute("SELECT id, name FROM Singers")
    assert list(frame.columns) == ["id", "name"]
    assert frame.values.tolist() == [[1, "a"], [2, "b"]]


def test_empty_result_keeps_its_columns(opened: Opened) -> None:
    connector = connector_with(opened, {"FROM Singers": FakeResult(["id"], [])})
    frame = connector.execute("SELECT id FROM Singers WHERE FALSE")
    assert frame.empty and list(frame.columns) == ["id"]


@pytest.mark.parametrize("parameters", [[1], ["x", 2]])
def test_positional_parameters_are_refused(
    opened: Opened, parameters: list[Any]
) -> None:
    connector = connector_with(opened)
    with pytest.raises(ValueError, match="positional parameters are not supported"):
        connector.execute("SELECT 1", parameters)
    assert opened == []


@pytest.mark.parametrize("parameters", [None, []])
def test_no_parameters_is_fine(opened: Opened, parameters: list[Any] | None) -> None:
    assert len(connector_with(opened).execute("SELECT 1", parameters)) == 1


def test_timeout_is_sent_as_the_rpc_deadline(opened: Opened) -> None:
    connector = connector_with(opened)
    connector.execute("SELECT 2", timeout_s=7.5)
    assert opened[0].statements[-1] == ("SELECT 2", 7.5)


def test_deadline_exceeded_is_a_statement_timeout(opened: Opened) -> None:
    connector = connector_with(
        opened, {"slow": api.DeadlineExceeded("Deadline Exceeded")}
    )
    with pytest.raises(StatementTimeout) as raised:
        connector.execute("SELECT slow", timeout_s=3)
    assert raised.value.timeout_s == 3


def test_deadline_without_a_cap_is_a_spanner_error(opened: Opened) -> None:
    connector = connector_with(
        opened, {"slow": api.DeadlineExceeded("Deadline Exceeded")}
    )
    with pytest.raises(SpannerError):
        connector.execute("SELECT slow")


@pytest.mark.parametrize(
    "error",
    [
        api.ServiceUnavailable("failed to connect to all addresses"),
        api.RetryError("Timeout of 30.0s exceeded", cause=None),
    ],
)
def test_transport_failures_read_as_infrastructure(
    opened: Opened, error: Exception
) -> None:
    connector = connector_with(opened, {"boom": error})
    statement = "SELECT boom"
    with pytest.raises(SpannerError) as raised:
        connector.execute(statement)
    assert str(raised.value).startswith("Could not connect to Spanner at")
    assert is_infrastructure_error(raised.value, statement)
    assert opened[0].transport.closed.is_set()


def test_sql_errors_do_not_read_as_infrastructure(opened: Opened) -> None:
    connector = connector_with(
        opened, {"Nope": api.InvalidArgument("Table not found: Nope [at 1:15]")}
    )
    statement = "SELECT * FROM Nope"
    with pytest.raises(SpannerError, match="Table not found") as raised:
        connector.execute(statement)
    assert not is_infrastructure_error(raised.value, statement)


class _Payload:
    """Stands in for the protobuf ``details`` the server attaches to an error."""

    def __repr__(self) -> str:
        return (
            'type_url: "type.googleapis.com/googlesql.ErrorMessageModeForPayload"\n'
            'value: "\\010\\002"\n'
        )


def _with_request_id(error: api.GoogleAPICallError) -> api.GoogleAPICallError:
    """Tag *error* the way the Spanner client does before raising it."""
    error.request_id = "1.84.1.0.3.1"
    error.message = f"{error.message}, request_id = 1.84.1.0.3.1"
    return error


def test_statement_errors_read_status_and_server_message(opened: Opened) -> None:
    server_message = (
        "Table not found: Nope [at 1:15]\nSELECT * FROM Nope\n              ^"
    )
    error = _with_request_id(api.InvalidArgument(server_message, details=[_Payload()]))
    assert "type_url" in str(error)  # what the connector used to pass on
    connector = connector_with(opened, {"Nope": error})
    statement = "SELECT * FROM Nope"
    with pytest.raises(SpannerError) as raised:
        connector.execute(statement)
    assert str(raised.value) == f"INVALID_ARGUMENT: {server_message}"
    assert raised.value.__cause__ is error
    assert not is_infrastructure_error(raised.value, statement)


def test_an_error_without_a_grpc_status_is_named_by_its_class(
    opened: Opened,
) -> None:
    connector = connector_with(opened, {"odd": api.GoogleAPICallError("odd failure")})
    with pytest.raises(SpannerError) as raised:
        connector.execute("SELECT odd")
    assert str(raised.value) == "GoogleAPICallError: odd failure"


def test_missing_database_drops_the_request_id(opened: Opened) -> None:
    connector = connector_with(
        opened,
        responder=lambda sql, db: _with_request_id(
            api.NotFound("Database not found: music", details=[_Payload()])
        ),
    )
    with pytest.raises(SpannerError) as raised:
        connector.execute("SELECT 1")
    assert str(raised.value).endswith(
        "in the connection string: NOT_FOUND: Database not found: music"
    )


def test_missing_database_names_the_path(opened: Opened) -> None:
    connector = connector_with(
        opened,
        responder=lambda sql, db: api.NotFound("Database not found: music"),
    )
    with pytest.raises(SpannerError, match="could not find projects/my-project/"):
        connector.execute("SELECT 1")


def test_postgresql_dialect_is_refused(opened: Opened) -> None:
    connector = connector_with(
        opened,
        responder=lambda sql, db: FakeResult(["option_value"], [["POSTGRESQL"]]),
    )
    with pytest.raises(SpannerError, match="PostgreSQL-dialect Spanner databases"):
        connector.execute("SELECT 1")
    assert opened[0].transport.closed.is_set()
    # Refused again on the next call rather than remembered as open.
    with pytest.raises(SpannerError, match="PostgreSQL-dialect"):
        connector.execute("SELECT 1")
    assert len(opened) == 2


def test_dialect_is_checked_once_per_connection(opened: Opened) -> None:
    connector = connector_with(opened)
    connector.execute("SELECT 1")
    connector.execute("SELECT 2")
    probes = [sql for sql, _ in opened[0].statements if "DATABASE_OPTIONS" in sql]
    assert len(probes) == 1


def _blocking_until_closed(sql: str, database: FakeDatabase) -> Any:
    """A statement that only ends when its channel is closed, as on a dead server."""
    dialect = googlesql(sql, database)
    if dialect is not None:
        return dialect
    database.transport.closed.wait(10)
    return ValueError("Cannot invoke RPC: Channel closed!")


def test_a_channel_that_stays_down_ends_the_statement(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spanner_module, "UNAVAILABLE_BUDGET_S", 0.3)
    connector = connector_with(opened, responder=_blocking_until_closed)
    connector._current()  # open the connection while the channel is up
    opened[0].channel.set_state("TRANSIENT_FAILURE")

    statement = "SELECT * FROM Singers"
    with pytest.raises(SpannerError, match="Could not connect to Spanner") as raised:
        connector.execute(statement)
    assert is_infrastructure_error(raised.value, statement)
    assert opened[0].transport.closed.is_set()

    # The next statement opens a fresh client.
    opened.pending.append(FakeDatabase(respond({})))
    assert len(connector.execute("SELECT 1")) == 1
    assert len(opened) == 2


def test_a_ready_channel_is_never_abandoned(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spanner_module, "UNAVAILABLE_BUDGET_S", 0.1)
    release = threading.Event()

    def slow(sql: str, database: FakeDatabase) -> Any:
        if googlesql(sql, database) is not None:
            return googlesql(sql, database)
        release.wait(1.0)
        return FakeResult(["x"], [[1]])

    connector = connector_with(opened, responder=slow)
    threading.Timer(0.6, release.set).start()
    assert len(connector.execute("SELECT 1")) == 1
    assert not opened[0].transport.closed.is_set()


def test_backstop_fires_when_the_server_deadline_does_not(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spanner_module, "DEADLINE_GRACE_S", 0.1)
    connector = connector_with(opened, responder=_blocking_until_closed)
    with pytest.raises(StatementTimeout):
        connector.execute("SELECT * FROM Singers", timeout_s=0.2)
    assert opened[0].transport.closed.is_set()


def test_close_closes_the_channel(opened: Opened) -> None:
    connector = connector_with(opened)
    connector.execute("SELECT 1")
    connector.close()
    assert opened[0].transport.closed.is_set()
    assert opened[0].channel.callbacks == []


def test_close_without_a_connection_is_a_no_op(opened: Opened) -> None:
    SpannerDatabase(URL).close()
    assert opened == []


def test_ping_runs_a_statement(opened: Opened) -> None:
    connector = connector_with(opened)
    connector.ping()
    assert opened[0].statements[-1] == ("SELECT 1", None)


# ----------------------------------------------------------------------
# Introspection
# ----------------------------------------------------------------------

_TABLES = FakeResult(
    ["TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE"],
    [
        ["", "Singers", "BASE TABLE"],
        ["", "SingerRevenue", "VIEW"],
        ["sales", "Promotions", "BASE TABLE"],
    ],
)
_COLUMNS = FakeResult(
    [
        "TABLE_SCHEMA",
        "TABLE_NAME",
        "COLUMN_NAME",
        "DATA_TYPE",
        "IS_NULLABLE",
        "ORDINAL_POSITION",
    ],
    [
        ["", "Singers", "SingerId", "INT64", False, 1],
        ["", "Singers", "Genres", "ARRAY<STRING(50)>", True, 2],
        ["sales", "Promotions", "PromotionId", "INT64", False, 1],
    ],
)
_PKS = FakeResult(
    ["TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "ORDINAL_POSITION"],
    [
        ["", "Singers", "SingerId", 1],
        ["", "Albums", "SingerId", 1],
        ["", "Albums", "AlbumId", 2],
        ["sales", "Promotions", "PromotionId", 1],
    ],
)
_FK_COLUMNS = [
    "TABLE_SCHEMA",
    "TABLE_NAME",
    "COLUMN_NAME",
    "REFERENCED_SCHEMA",
    "REFERENCED_TABLE",
    "REFERENCED_COLUMN",
    "CONSTRAINT_SCHEMA",
    "CONSTRAINT_NAME",
    "REFERENCED_CONSTRAINT_TYPE",
]
_DECLARED = FakeResult(
    _FK_COLUMNS,
    [
        ["", "Concerts", "SingerId", "", "Singers", "SingerId", "", "FK_C", "PRIMARY KEY"],
        ["sales", "Promotions", "VenueId", "", "Venues", "VenueId", "sales", "FK_P", "PRIMARY KEY"],
    ],
)  # fmt: skip
_PARENTS = FakeResult(
    ["TABLE_SCHEMA", "TABLE_NAME", "PARENT_TABLE_NAME"], [["", "Albums", "Singers"]]
)


def _introspection(**overrides: FakeResult | BaseException) -> dict[str, Any]:
    table: dict[str, Any] = {
        "PARENT_TABLE_NAME IS NOT NULL": _PARENTS,
        "REFERENTIAL_CONSTRAINTS": _DECLARED,
        "INDEX_COLUMNS": _PKS,
        "INFORMATION_SCHEMA.VIEWS": FakeResult(
            ["TABLE_SCHEMA", "TABLE_NAME", "VIEW_DEFINITION"],
            [["", "SingerRevenue", "SELECT 1"]],
        ),
        "INFORMATION_SCHEMA.COLUMNS": _COLUMNS,
        "INFORMATION_SCHEMA.TABLES": _TABLES,
    }
    table.update(overrides)
    return table


def test_get_tables_reports_the_default_schema_as_the_database(
    opened: Opened,
) -> None:
    frame = connector_with(opened, _introspection()).get_tables()
    assert list(frame.columns) == ["table_schema", "table_name", "table_type"]
    assert frame.values.tolist() == [
        ["music", "Singers", "base table"],
        ["music", "SingerRevenue", "view"],
        ["sales", "Promotions", "base table"],
    ]
    sql = opened[0].statements[-1][0]
    assert "NOT IN ('INFORMATION_SCHEMA', 'SPANNER_SYS')" in sql


def test_unknown_table_type_is_an_error(opened: Opened) -> None:
    tables = FakeResult(
        ["TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE"], [["", "X", "SYNONYM"]]
    )
    connector = connector_with(
        opened, _introspection(**{"INFORMATION_SCHEMA.TABLES": tables})
    )
    with pytest.raises(SpannerError, match="SYNONYM"):
        connector.get_tables()


def test_get_columns_shape_and_bool_nullability(opened: Opened) -> None:
    frame = connector_with(opened, _introspection()).get_columns()
    assert list(frame.columns) == [
        "table_schema",
        "table_name",
        "column_name",
        "data_type",
        "is_nullable",
        "ordinal_position",
    ]
    assert frame.is_nullable.dtype == bool
    assert frame.ordinal_position.dtype == "int64"
    assert frame["table_schema"].tolist() == ["music", "music", "sales"]
    assert frame["data_type"].tolist()[1] == "ARRAY<STRING(50)>"
    normalised = " ".join(opened[0].statements[-1][0].split())
    assert "IS_NULLABLE = 'YES'" in normalised
    assert "SPANNER_TYPE" in normalised


def test_get_views(opened: Opened) -> None:
    frame = connector_with(opened, _introspection()).get_views()
    assert frame.values.tolist() == [["music", "SingerRevenue", "SELECT 1"]]


def test_get_pks(opened: Opened) -> None:
    frame = connector_with(opened, _introspection()).get_pks()
    assert list(frame.columns) == [
        "table_schema",
        "table_name",
        "column_name",
        "ordinal_position",
    ]
    assert frame.values.tolist()[0] == ["music", "Singers", "SingerId", 1]
    assert "INDEX_TYPE = 'PRIMARY_KEY'" in opened[0].statements[-1][0]


def test_get_fks_declared_and_interleaved(opened: Opened) -> None:
    frame = connector_with(opened, _introspection()).get_fks()
    assert list(frame.columns) == [
        "table_schema",
        "table_name",
        "column_name",
        "referenced_schema",
        "referenced_table",
        "referenced_column",
    ]
    assert frame.values.tolist() == [
        ["music", "Concerts", "SingerId", "music", "Singers", "SingerId"],
        ["sales", "Promotions", "VenueId", "music", "Venues", "VenueId"],
        ["music", "Albums", "SingerId", "music", "Singers", "SingerId"],
    ]


def test_get_fks_leaves_out_ambiguous_unique_index_keys(
    opened: Opened, caplog: pytest.LogCaptureFixture
) -> None:
    declared = FakeResult(
        _FK_COLUMNS,
        [
            ["", "C", "PRegion", "", "P", "Code", "", "FK_C_P", "UNIQUE"],
            ["", "C", "PCode", "", "P", "Region", "", "FK_C_P", "UNIQUE"],
            ["", "D", "PCode", "", "P", "Code", "", "FK_D_P", "UNIQUE"],
        ],
    )
    connector = connector_with(
        opened,
        _introspection(
            **{
                "REFERENTIAL_CONSTRAINTS": declared,
                "PARENT_TABLE_NAME IS NOT NULL": FakeResult(
                    ["TABLE_SCHEMA", "TABLE_NAME", "PARENT_TABLE_NAME"], []
                ),
            }
        ),
    )
    with caplog.at_level(logging.WARNING, logger=spanner_module.__name__):
        frame = connector.get_fks()
    assert frame.values.tolist() == [["music", "D", "PCode", "music", "P", "Code"]]
    assert "FK_C_P" in caplog.text


def test_get_fks_unresolved_reference_is_an_error(opened: Opened) -> None:
    declared = FakeResult(
        _FK_COLUMNS,
        [["", "C", "X", None, None, None, "", "FK_X", None]],
    )
    connector = connector_with(
        opened, _introspection(**{"REFERENTIAL_CONSTRAINTS": declared})
    )
    with pytest.raises(SpannerError, match="FK_X"):
        connector.get_fks()


def test_interleaved_child_without_parent_key_is_an_error(
    opened: Opened,
) -> None:
    parents = FakeResult(
        ["TABLE_SCHEMA", "TABLE_NAME", "PARENT_TABLE_NAME"], [["", "Albums", "Gone"]]
    )
    connector = connector_with(
        opened, _introspection(**{"PARENT_TABLE_NAME IS NOT NULL": parents})
    )
    with pytest.raises(SpannerError, match="Gone"):
        connector.get_fks()


@pytest.mark.parametrize(
    "method", ["get_tables", "get_columns", "get_views", "get_pks", "get_fks"]
)
def test_empty_database_gives_shaped_frames(opened: Opened, method: str) -> None:
    empty = {
        marker: FakeResult(result.fields and [f.name for f in result.fields], [])
        for marker, result in _introspection().items()
        if isinstance(result, FakeResult)
    }
    frame = getattr(connector_with(opened, empty), method)()
    assert frame.empty
    assert "table_schema" in frame.columns and "table_name" in frame.columns


def test_get_queries_is_empty_and_shaped(opened: Opened) -> None:
    frame = SpannerDatabase(URL).get_queries()
    assert frame.empty and list(frame.columns) == ["end_time", "query_text"]
    assert opened == []


@pytest.mark.parametrize("schema", ["music", "MUSIC"])
def test_named_schema_spelled_like_the_database_is_refused(
    opened: Opened, schema: str
) -> None:
    tables = FakeResult(
        ["TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE"], [[schema, "T", "BASE TABLE"]]
    )
    connector = connector_with(
        opened, _introspection(**{"INFORMATION_SCHEMA.TABLES": tables})
    )
    with pytest.raises(SpannerError, match="same name as database"):
        connector.get_tables()


def test_every_listed_relation_has_columns(opened: Opened) -> None:
    """The coverage rule ingestion enforces, on the fake catalog."""
    from auto_ontology.catalog.extract import _validate_relation_column_coverage

    tables = FakeResult(
        ["TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE"],
        [["", "Singers", "BASE TABLE"], ["sales", "Promotions", "BASE TABLE"]],
    )
    connector = connector_with(
        opened, _introspection(**{"INFORMATION_SCHEMA.TABLES": tables})
    )
    _validate_relation_column_coverage(connector.get_tables(), connector.get_columns())


# ----------------------------------------------------------------------
# Import cost
# ----------------------------------------------------------------------


def test_registry_import_does_not_load_the_spanner_client() -> None:
    """The registry imports every connector at backend start."""
    code = (
        "import sys\n"
        "import auto_ontology.connectors.registry\n"
        "loaded = sorted(m for m in sys.modules if m == 'grpc' "
        "or m.startswith(('grpc.', 'google.cloud.spanner')))\n"
        "print(loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_frames_are_plain_pandas(opened: Opened) -> None:
    assert isinstance(connector_with(opened).execute("SELECT 1"), pd.DataFrame)
