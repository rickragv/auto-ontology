# SPDX-License-Identifier: Apache-2.0

"""BigQuery connector, against a fake client: no server or credentials needed.

Table resources, schema fields and result rows are built with the real
library's classes from REST-shaped dicts, so attribute names are the library's.
The live tests are in ``test_bigquery_live.py`` and only run with
``AUTO_ONTOLOGY_LIVE_BIGQUERY_PROJECT`` set.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd
import pytest
import requests
import sqlglot
from google.api_core import exceptions as api
from google.auth import exceptions as auth
from google.cloud.bigquery import Row, SchemaField, Table
from google.cloud.bigquery.table import TableListItem

from auto_ontology.connectors import bigquery as bigquery_module
from auto_ontology.connectors.base import StatementTimeout
from auto_ontology.connectors.bigquery import (
    BigQueryDatabase,
    BigQueryError,
    check_read_only,
    format_connection_string,
)
from auto_ontology.connectors.db_errors import is_infrastructure_error
from auto_ontology.connectors.registry import CONNECTOR_REGISTRY, create_connector
from auto_ontology.retrieval.text_to_sql.agents.sql_parse_validation import (
    quote_known_mixed_case_identifiers,
)
from auto_ontology.retrieval.text_to_sql.formatters_util import qualify_table

PROJECT = "my-project-123"
URL = f"bigquery://{PROJECT}/music"
PROJECT_URL = f"bigquery://{PROJECT}"
EMULATOR_URL = URL + "?emulator_host=localhost:9050"

# ----------------------------------------------------------------------
# Fake client
# ----------------------------------------------------------------------


class FakeRows:
    """What ``query_and_wait`` returns: a schema and an iterable of ``Row``."""

    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.schema = [SchemaField(name, "STRING") for name in columns]
        index = {name: position for position, name in enumerate(columns)}
        self._rows = [Row(tuple(values), index) for values in rows]

    def __iter__(self) -> Any:
        return iter(self._rows)


@dataclass
class Query:
    sql: str
    kwargs: dict[str, Any]


Responder = Callable[[str], Any]


def _default_responder(sql: str) -> Any:
    return FakeRows(["x"], [[1]])


@dataclass
class FakeClient:
    """Records every call; answers queries with *responder*, metadata from dicts."""

    responder: Responder = _default_responder
    datasets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    errors: dict[str, BaseException] = field(default_factory=dict)
    queries: list[Query] = field(default_factory=list)
    calls: list[tuple[str, Any, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False

    def query_and_wait(self, sql: str, **kwargs: Any) -> Any:
        self.queries.append(Query(sql, kwargs))
        answer = self.responder(sql)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _record(self, name: str, target: Any, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, target, kwargs))
        if name in self.errors:
            raise self.errors[name]
        if str(target) in self.errors:
            raise self.errors[str(target)]

    def list_datasets(self, project: str, **kwargs: Any) -> list[Any]:
        from google.cloud.bigquery.dataset import DatasetListItem

        self._record("list_datasets", project, kwargs)
        # Not sorted, to check the connector sorts.
        return [
            DatasetListItem(
                {"datasetReference": {"projectId": project, "datasetId": name}}
            )
            for name in reversed(list(self.datasets))
        ]

    def list_tables(self, reference: str, **kwargs: Any) -> list[TableListItem]:
        self._record("list_tables", reference, kwargs)
        dataset = reference.split(".", 1)[1]
        if dataset not in self.datasets:
            raise api.NotFound(
                f"GET https://bigquery.googleapis.com/x: Not found: Dataset "
                f"{reference.replace('.', ':')}",
                errors=[
                    {
                        "reason": "notFound",
                        "message": f"Not found: Dataset {reference.replace('.', ':')}",
                    }
                ],
            )
        return [TableListItem(resource) for resource in self.datasets[dataset]]

    def get_table(self, reference: str, **kwargs: Any) -> Table:
        self._record("get_table", reference, kwargs)
        _, dataset, table = reference.split(".")
        for resource in self.datasets[dataset]:
            if resource["tableReference"]["tableId"] == table:
                return Table.from_api_repr(resource)
        raise api.NotFound(f"Not found: Table {reference}")

    def get_dataset(self, reference: str, **kwargs: Any) -> Any:
        self._record("get_dataset", reference, kwargs)
        return object()

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _no_emulator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bigquery_module.EMULATOR_ENV_VAR, raising=False)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Every connector's client is this fake (``_open`` is replaced)."""
    client = FakeClient()

    def fake_open(self: BigQueryDatabase) -> FakeClient:
        bigquery_module._check_emulator_environment(self._settings["emulator_host"])
        self._client = client  # type: ignore[assignment]
        return client

    monkeypatch.setattr(BigQueryDatabase, "_open", fake_open)
    return client


def api_error(
    cls: type[api.GoogleAPICallError], reason: str, message: str, *, job: bool = False
) -> api.GoogleAPICallError:
    """An error shaped as the client raises it.

    From a REST response the message starts with the method and URL; from a
    failed job it ends with the location and job id. Both carry ``errors``.
    """
    text = (
        f"{message}\n\nLocation: US\nJob ID: abc123\n"
        if job
        else f"POST https://bigquery.googleapis.com/bigquery/v2/projects/"
        f"{PROJECT}/queries?prettyPrint=false: {message}"
    )
    return cls(text, errors=[{"reason": reason, "message": message, "location": "q"}])


# ----------------------------------------------------------------------
# Connection string
# ----------------------------------------------------------------------


def test_parses_project_and_dataset() -> None:
    assert bigquery_module._parse_connection_string(URL) == {
        "project": PROJECT,
        "dataset": "music",
        "emulator_host": None,
        "location": None,
        "maximum_bytes_billed": None,
    }


@pytest.mark.parametrize("url", [PROJECT_URL, PROJECT_URL + "/"])
def test_dataset_is_optional(url: str) -> None:
    assert bigquery_module._parse_connection_string(url)["dataset"] is None


def test_parses_every_parameter_and_domain_scoped_project() -> None:
    settings = bigquery_module._parse_connection_string(
        "bigquery://example.com:my-project/Sales_2024?emulator_host=localhost:9050"
        "&location=us-central1&maximum_bytes_billed=1000000000"
    )
    assert settings == {
        "project": "example.com:my-project",
        "dataset": "Sales_2024",
        "emulator_host": "localhost:9050",
        "location": "us-central1",
        "maximum_bytes_billed": 1_000_000_000,
    }


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("postgresql://p/d", "Not a BigQuery URL"),
        ("bigquery+rest://p/d", "Not a BigQuery URL"),
        ("bigquery:///d", "empty project"),
        ("bigquery://user@p/d", "carry no user or password"),
        ("bigquery://user:secret@p/d", "carry no user or password"),
        ("bigquery://p/d#frag", "Unexpected"),
        ("bigquery://p/d/t", "at most one dataset"),
        ("bigquery://p/d/", "at most one dataset"),
        ("bigquery://p//", "at most one dataset"),
        ("bigquery://p/my-dataset", "Invalid BigQuery dataset id"),
        ("bigquery://p/my%20ds", "Invalid BigQuery dataset id"),
        ("bigquery://-p/d", "Invalid BigQuery project id"),
        ("bigquery://p/d?schema=x", "Unknown BigQuery URL parameter"),
        ("bigquery://p/d?emulator_host=a:1&emulator_host=b:2", "only once"),
        ("bigquery://p/d?emulator_host=http://localhost:9050", "no scheme"),
        ("bigquery://p/d?emulator_host=localhost", "no scheme"),
        ("bigquery://p/d?emulator_host=", "Invalid BigQuery emulator_host"),
        ("bigquery://p/d?location=", "Invalid BigQuery location"),
        ("bigquery://p/d?location=us%20east", "Invalid BigQuery location"),
        ("bigquery://p/d?maximum_bytes_billed=1e9", "maximum_bytes_billed"),
        ("bigquery://p/d?maximum_bytes_billed=10GB", "maximum_bytes_billed"),
        ("bigquery://p/d?maximum_bytes_billed=0", "maximum_bytes_billed"),
        ("bigquery://p/d?maximum_bytes_billed=-5", "maximum_bytes_billed"),
        ("bigquery://p/d?maximum_bytes_billed=", "maximum_bytes_billed"),
        (
            f"bigquery://p/d?maximum_bytes_billed={2**63}",
            "maximum_bytes_billed",
        ),
    ],
)
def test_rejects_malformed_urls(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        BigQueryDatabase(url)


def test_constructing_does_not_open_a_client(fake: FakeClient) -> None:
    connector = BigQueryDatabase(URL)
    assert connector._client is None
    assert fake.queries == [] and fake.calls == []


@pytest.mark.parametrize(
    "parts",
    [
        {"project": PROJECT},
        {"project": PROJECT, "dataset": "music"},
        {"project": "example.com:p", "dataset": "d_1", "location": "EU"},
        {"project": "p", "emulator_host": "host.docker.internal:9050"},
        {"project": "p", "dataset": "d", "emulator_host": "[::1]:9050"},
        {"project": "p", "maximum_bytes_billed": 10_485_760},
        {
            "project": "p",
            "dataset": "d",
            "emulator_host": "localhost:9050",
            "location": "asia-northeast1",
            "maximum_bytes_billed": 1,
        },
    ],
)
def test_format_parses_back_to_the_same_parts(parts: dict[str, Any]) -> None:
    url = format_connection_string(**parts)
    settings = bigquery_module._parse_connection_string(url)
    expected = {
        "project": None,
        "dataset": None,
        "emulator_host": None,
        "location": None,
        "maximum_bytes_billed": None,
        **parts,
    }
    assert settings == expected
    assert BigQueryDatabase(url).database_name == parts["project"]


def test_format_spells_the_documented_url() -> None:
    assert (
        format_connection_string(
            project=PROJECT,
            dataset="music",
            emulator_host="localhost:9050",
            location="EU",
            maximum_bytes_billed=5,
        )
        == f"{URL}?emulator_host=localhost:9050&location=EU&maximum_bytes_billed=5"
    )


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ({"project": ""}, "project id is required"),
        ({"project": "my project"}, "Invalid BigQuery project id"),
        ({"project": "p/x"}, "Invalid BigQuery project id"),
        ({"project": "p", "dataset": "a-b"}, "Invalid BigQuery dataset id"),
        ({"project": "p", "dataset": "a?x=1"}, "Invalid BigQuery dataset id"),
        ({"project": "p", "emulator_host": "localhost"}, "emulator_host"),
        ({"project": "p", "emulator_host": "a:1&location=EU"}, "emulator_host"),
        ({"project": "p", "location": "EU&x=1"}, "location"),
        ({"project": "p", "maximum_bytes_billed": 0}, "maximum_bytes_billed"),
        ({"project": "p", "maximum_bytes_billed": "100"}, "maximum_bytes_billed"),
        ({"project": "p", "maximum_bytes_billed": True}, "maximum_bytes_billed"),
    ],
)
def test_format_refuses_values_that_would_not_round_trip(
    parts: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        format_connection_string(**parts)


# ----------------------------------------------------------------------
# Emulator targeting and credentials (real library, no network)
# ----------------------------------------------------------------------


def test_env_emulator_without_url_emulator_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", "http://localhost:9050")
    with pytest.raises(ValueError, match="silently redirect") as raised:
        BigQueryDatabase(URL)
    assert "?emulator_host=localhost:9050" in str(raised.value)


def test_empty_env_emulator_counts_as_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", "")
    with pytest.raises(ValueError, match="silently redirect"):
        BigQueryDatabase(URL)


def test_env_and_url_emulator_must_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", "http://localhost:9999")
    with pytest.raises(ValueError, match="same emulator"):
        BigQueryDatabase(EMULATOR_URL)


@pytest.mark.parametrize(
    "value", ["http://localhost:9050", "http://localhost:9050/", "localhost:9050"]
)
def test_matching_env_and_url_emulator_is_allowed(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", value)
    connector = BigQueryDatabase(EMULATOR_URL)
    client = connector._open()
    assert client._connection.API_BASE_URL == "http://localhost:9050"


def test_env_set_after_construction_is_refused_at_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = BigQueryDatabase(URL)
    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", "http://localhost:9050")
    with pytest.raises(ValueError, match="silently redirect"):
        connector.execute("SELECT 1")


def test_url_emulator_targets_the_client_without_the_environment() -> None:
    from google.auth.credentials import AnonymousCredentials

    before = dict(os.environ)
    connector = BigQueryDatabase(EMULATOR_URL + "&location=EU")
    client = connector._open()
    assert client._connection.API_BASE_URL == "http://localhost:9050"
    assert isinstance(client._credentials, AnonymousCredentials)
    assert client.project == PROJECT
    assert client.location == "EU"
    assert dict(os.environ) == before
    connector.close()
    assert connector._client is None


def test_missing_default_credentials_is_a_clear_connection_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """An absent key file fails inside ``Client()``, before any request."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "none.json"))
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL).ping()
    message = str(raised.value)
    assert message.startswith(f"Could not connect to BigQuery project '{PROJECT}'")
    assert "gcloud auth application-default login" in message
    assert "GOOGLE_APPLICATION_CREDENTIALS" in message
    assert isinstance(raised.value.__cause__, auth.DefaultCredentialsError)
    assert is_infrastructure_error(raised.value)


def test_expired_credentials_read_as_a_connection_error(fake: FakeClient) -> None:
    fake.responder = lambda sql: auth.RefreshError("Reauthentication is needed.")
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL).execute("SELECT 1")
    assert "could not be refreshed" in str(raised.value)
    assert is_infrastructure_error(raised.value, "SELECT 1")


# ----------------------------------------------------------------------
# Identity and naming
# ----------------------------------------------------------------------


def test_registry_routes_bigquery_urls() -> None:
    assert CONNECTOR_REGISTRY["bigquery"] is BigQueryDatabase
    connector = create_connector(URL)
    assert isinstance(connector, BigQueryDatabase)
    assert connector.database_name == PROJECT
    assert connector.dialect == "bigquery"
    assert "bigquery" in sqlglot.dialects.DIALECTS or sqlglot.Dialect.get("bigquery")


@pytest.mark.parametrize(
    ("url", "schema", "table", "expected"),
    [
        (URL, "music", "Singers", f"`{PROJECT}`.`music`.`Singers`"),
        (URL, "sales", "Orders", f"`{PROJECT}`.`sales`.`Orders`"),
        (URL, None, "Singers", f"`{PROJECT}`.`music`.`Singers`"),
        (URL, "", "Singers", f"`{PROJECT}`.`music`.`Singers`"),
        (PROJECT_URL, None, "Singers", "`Singers`"),
        (URL, "music", "Odd`Name", f"`{PROJECT}`.`music`.`Odd\\`Name`"),
    ],
)
def test_qualify(url: str, schema: str | None, table: str, expected: str) -> None:
    assert BigQueryDatabase(url).qualify(schema, table) == expected


def test_prompt_names_are_the_three_part_name() -> None:
    assert qualify_table(PROJECT, "music", "Singers", "bigquery") == (
        f"{PROJECT}.music.Singers"
    )


# What a model copying the prompt, or its quoting habits, writes. Every shape
# is valid BigQuery for a table path, so the connector sends it unchanged; the
# shared catalog matching reads the table through sqlglot, which must split
# each one into the same parts.
_NAME_SHAPES = [
    f"{PROJECT}.music.Singers",
    f"`{PROJECT}`.music.Singers",
    f"`{PROJECT}.music.Singers`",
    f"`{PROJECT}`.`music`.`Singers`",
    "music.Singers",
]


@pytest.mark.parametrize("name", _NAME_SHAPES)
def test_sqlglot_reads_every_name_shape(name: str) -> None:
    tree = sqlglot.parse_one(f"SELECT s.LastName FROM {name} AS s", read="bigquery")
    table = tree.find(sqlglot.exp.Table)
    assert table is not None
    assert (table.db, table.name) == ("music", "Singers")
    assert table.catalog in (PROJECT, "")


@pytest.mark.parametrize("name", _NAME_SHAPES)
def test_mixed_case_requoting_keeps_the_path(name: str) -> None:
    sql = f"SELECT LastName FROM {name} WHERE SingerId = 1"
    tables = [{"name": "Singers", "columns": [{"name": "LastName"}]}]
    rewritten = quote_known_mixed_case_identifiers(sql, tables, "bigquery")
    table = sqlglot.parse_one(rewritten, read="bigquery").find(sqlglot.exp.Table)
    assert table is not None
    assert (table.db, table.name) == ("music", "Singers")


@pytest.mark.parametrize("name", _NAME_SHAPES)
def test_execute_sends_the_statement_unchanged(fake: FakeClient, name: str) -> None:
    sql = f"SELECT * FROM {name} WHERE n = 'music.Singers'"
    BigQueryDatabase(URL).execute(sql)
    assert fake.queries[-1].sql == sql


# ----------------------------------------------------------------------
# Read-only
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",
        "WITH t AS (SELECT 1 AS x) SELECT x FROM t",
        "(SELECT 1) UNION ALL (SELECT 2)",
        "((SELECT 1))",
        "-- leading comment\nSELECT 1",
        "# hash comment\nSELECT 1",
        "/* block; comment */ SELECT 1",
        "SELECT 1;",
        "SELECT 1;  ;\n",
        "SELECT 'a; DROP TABLE x' AS s",
        'SELECT """multi; line""" AS s',
        "SELECT 1 -- trailing; comment",
        "SELECT `odd;name` FROM t",
    ],
)
def test_reads_are_allowed(sql: str) -> None:
    check_read_only(sql)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("INSERT INTO t (x) VALUES (1)", "'INSERT'"),
        ("update t SET x = 1 WHERE TRUE", "'update'"),
        ("DELETE FROM t WHERE TRUE", "'DELETE'"),
        ("MERGE t USING s ON FALSE WHEN NOT MATCHED THEN INSERT ROW", "'MERGE'"),
        ("TRUNCATE TABLE t", "'TRUNCATE'"),
        ("CREATE TABLE t AS SELECT 1 AS x", "'CREATE'"),
        ("DROP TABLE t", "'DROP'"),
        ("ALTER TABLE t ADD COLUMN y INT64", "'ALTER'"),
        ("EXPORT DATA OPTIONS (uri='gs://b/*') AS SELECT 1", "'EXPORT'"),
        ("DECLARE x INT64; SELECT x", "'DECLARE'"),
        ("BEGIN SELECT 1; END", "'BEGIN'"),
        ("CALL ds.proc()", "'CALL'"),
        ("/* SELECT */ DELETE FROM t WHERE TRUE", "'DELETE'"),
        ("( DELETE FROM t WHERE TRUE )", "'DELETE'"),
        ("SELECT 1; DROP TABLE t", "more than one statement"),
        ("SELECT 1; SELECT 2", "more than one statement"),
        ("", "empty statement"),
        ("  -- only a comment", "empty statement"),
        ("'SELECT 1'", "read-only"),
    ],
)
def test_writes_and_scripts_are_refused_before_sending(
    fake: FakeClient, sql: str, message: str
) -> None:
    with pytest.raises(BigQueryError, match=message) as raised:
        BigQueryDatabase(URL).execute(sql)
    assert fake.queries == []
    assert not is_infrastructure_error(raised.value, sql)


@pytest.mark.parametrize("parameters", [[1], ["x", 2]])
def test_positional_parameters_are_refused(
    fake: FakeClient, parameters: list[Any]
) -> None:
    with pytest.raises(ValueError, match="positional parameters are not supported"):
        BigQueryDatabase(URL).execute("SELECT 1", parameters)
    assert fake.queries == []


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def test_execute_returns_a_frame_with_column_names(fake: FakeClient) -> None:
    fake.responder = lambda sql: FakeRows(["id", "name"], [[1, "a"], [2, "b"]])
    frame = BigQueryDatabase(URL).execute("SELECT id, name FROM Singers")
    assert isinstance(frame, pd.DataFrame)
    assert list(frame.columns) == ["id", "name"]
    assert frame.values.tolist() == [[1, "a"], [2, "b"]]


def test_empty_result_keeps_its_columns(fake: FakeClient) -> None:
    fake.responder = lambda sql: FakeRows(["id", "name"], [])
    frame = BigQueryDatabase(URL).execute("SELECT id, name FROM Singers LIMIT 0")
    assert frame.empty and list(frame.columns) == ["id", "name"]


def test_nested_values_are_kept_as_python_objects(fake: FakeClient) -> None:
    fake.responder = lambda sql: FakeRows(
        ["tags", "address"], [[["a", "b"], {"city": "Lyon"}]]
    )
    frame = BigQueryDatabase(URL).execute("SELECT tags, address FROM Singers")
    assert frame.tags.iloc[0] == ["a", "b"]
    assert frame.address.iloc[0] == {"city": "Lyon"}


def test_job_settings_without_a_cap(fake: FakeClient) -> None:
    BigQueryDatabase(URL).execute("SELECT 1")
    kwargs = fake.queries[-1].kwargs
    config = kwargs["job_config"]
    assert config.use_legacy_sql is False
    assert str(config.default_dataset) == f"{PROJECT}.music"
    assert config.maximum_bytes_billed is None
    assert config.job_timeout_ms is None
    assert kwargs["api_timeout"] == bigquery_module.REQUEST_TIMEOUT_S
    assert kwargs["wait_timeout"] is None
    assert kwargs["retry"].timeout == bigquery_module.RETRY_BUDGET_S
    assert kwargs["job_retry"].timeout == bigquery_module.JOB_RETRY_BUDGET_S


def test_no_default_dataset_without_one_in_the_url(fake: FakeClient) -> None:
    BigQueryDatabase(PROJECT_URL).execute("SELECT 1")
    assert fake.queries[-1].kwargs["job_config"].default_dataset is None


def test_maximum_bytes_billed_is_set_on_every_job(fake: FakeClient) -> None:
    connector = BigQueryDatabase(URL + "?maximum_bytes_billed=1000000")
    connector.execute("SELECT 1")
    connector.execute("SELECT 2", timeout_s=3)
    assert [q.kwargs["job_config"].maximum_bytes_billed for q in fake.queries] == [
        1_000_000,
        1_000_000,
    ]


def test_timeout_is_sent_to_the_job_and_bounds_the_client(fake: FakeClient) -> None:
    BigQueryDatabase(URL).execute("SELECT 2", timeout_s=7.5)
    kwargs = fake.queries[-1].kwargs
    assert int(kwargs["job_config"].job_timeout_ms) == 7500
    assert kwargs["api_timeout"] == 7.5
    assert kwargs["wait_timeout"] == 7.5
    assert kwargs["retry"].timeout == 7.5
    assert kwargs["job_retry"].timeout == 7.5


def test_a_cap_above_the_budgets_keeps_the_budgets(fake: FakeClient) -> None:
    BigQueryDatabase(URL).execute("SELECT 2", timeout_s=600)
    kwargs = fake.queries[-1].kwargs
    assert int(kwargs["job_config"].job_timeout_ms) == 600_000
    assert kwargs["retry"].timeout == bigquery_module.RETRY_BUDGET_S
    assert kwargs["job_retry"].timeout == bigquery_module.JOB_RETRY_BUDGET_S


def test_no_job_retry_on_an_emulator(fake: FakeClient) -> None:
    BigQueryDatabase(EMULATOR_URL).execute("SELECT 1")
    assert fake.queries[-1].kwargs["job_retry"] is None


@pytest.mark.parametrize(
    "error",
    [
        concurrent.futures.TimeoutError(),
        api_error(api.BadRequest, "timeout", "Job timed out after 2s", job=True),
        api_error(
            api.InternalServerError,
            "stopped",
            "Job execution was cancelled: Job timed out",
            job=True,
        ),
        api.RetryError(
            "Timeout of 2.0s exceeded", cause=requests.exceptions.ReadTimeout()
        ),
    ],
)
def test_timeouts_are_statement_timeouts(fake: FakeClient, error: Exception) -> None:
    fake.responder = lambda sql: error
    with pytest.raises(StatementTimeout) as raised:
        BigQueryDatabase(URL).execute("SELECT slow", timeout_s=2)
    assert raised.value.timeout_s == 2


def test_a_stopped_job_without_a_cap_is_a_bigquery_error(fake: FakeClient) -> None:
    fake.responder = lambda sql: api_error(
        api.InternalServerError, "stopped", "Job execution was cancelled", job=True
    )
    with pytest.raises(BigQueryError, match="^stopped: Job execution was cancelled$"):
        BigQueryDatabase(URL).execute("SELECT 1")


def test_ping_checks_the_dataset_then_runs_a_query(fake: FakeClient) -> None:
    BigQueryDatabase(URL).ping()
    assert [name for name, _, _ in fake.calls] == ["get_dataset"]
    assert fake.calls[0][1] == f"{PROJECT}.music"
    assert [q.sql for q in fake.queries] == ["SELECT 1"]


def test_ping_without_a_dataset_only_runs_a_query(fake: FakeClient) -> None:
    BigQueryDatabase(PROJECT_URL).ping()
    assert fake.calls == []
    assert [q.sql for q in fake.queries] == ["SELECT 1"]


def test_ping_names_a_missing_dataset(fake: FakeClient) -> None:
    fake.errors["get_dataset"] = api_error(
        api.NotFound, "notFound", f"Not found: Dataset {PROJECT}:music"
    )
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL).ping()
    assert str(raised.value) == (
        f"BigQuery could not find dataset '{PROJECT}.music'; check the project and "
        f"dataset in the connection string: notFound: Not found: Dataset "
        f"{PROJECT}:music"
    )


def test_close_closes_the_client(fake: FakeClient) -> None:
    connector = BigQueryDatabase(URL)
    connector.execute("SELECT 1")
    connector.close()
    assert fake.closed and connector._client is None


def test_close_without_a_client_is_a_no_op(fake: FakeClient) -> None:
    BigQueryDatabase(URL).close()
    assert not fake.closed


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


@pytest.mark.parametrize("job", [False, True])
def test_statement_errors_read_reason_and_server_message(
    fake: FakeClient, job: bool
) -> None:
    message = "Unrecognized name: Nope at [1:8]"
    error = api_error(api.BadRequest, "invalidQuery", message, job=job)
    fake.responder = lambda sql: error
    statement = "SELECT Nope FROM Singers"
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL).execute(statement)
    assert str(raised.value) == f"invalidQuery: {message}"
    assert raised.value.__cause__ is error
    assert not is_infrastructure_error(raised.value, statement)


def test_a_missing_table_in_a_query_is_the_statements_error(fake: FakeClient) -> None:
    fake.responder = lambda sql: api_error(
        api.NotFound, "notFound", f"Not found: Table {PROJECT}:music.Nope"
    )
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL).execute("SELECT * FROM music.Nope")
    assert str(raised.value) == f"notFound: Not found: Table {PROJECT}:music.Nope"


def test_an_error_without_structured_errors_drops_the_url(fake: FakeClient) -> None:
    fake.responder = lambda sql: api.BadRequest(
        f"POST https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/"
        "queries?prettyPrint=false: Invalid project ID"
    )
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL).execute("SELECT 1")
    assert str(raised.value) == "HTTP 400: Invalid project ID"


def test_bytes_billed_limit_names_the_setting(fake: FakeClient) -> None:
    message = (
        "Query exceeded limit for bytes billed: 1000. 10485760 or higher required."
    )
    fake.responder = lambda sql: api_error(
        api.BadRequest, "bytesBilledLimitExceeded", message, job=True
    )
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL + "?maximum_bytes_billed=1000").execute(
            "SELECT * FROM Singers"
        )
    text = str(raised.value)
    assert text.startswith(f"bytesBilledLimitExceeded: {message}")
    assert "maximum_bytes_billed=1000" in text


def test_access_denied_names_the_roles(fake: FakeClient) -> None:
    message = (
        f"Access Denied: Project {PROJECT}: User does not have "
        "bigquery.jobs.create permission in project my-project-123."
    )
    fake.responder = lambda sql: api_error(api.Forbidden, "accessDenied", message)
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL).execute("SELECT 1")
    text = str(raised.value)
    assert f"BigQuery denied access in project '{PROJECT}'" in text
    assert f"accessDenied: {message}" in text
    assert "BigQuery Job User" in text


def test_quota_errors_are_not_called_access_denied(fake: FakeClient) -> None:
    fake.responder = lambda sql: api_error(
        api.Forbidden, "quotaExceeded", "Quota exceeded: too many jobs"
    )
    with pytest.raises(BigQueryError, match="^quotaExceeded: Quota exceeded"):
        BigQueryDatabase(URL).execute("SELECT 1")


@pytest.mark.parametrize(
    "error",
    [
        api.RetryError(
            "Timeout of 30.0s exceeded",
            cause=requests.exceptions.ConnectionError("Connection refused"),
        ),
        api.RetryError("Timeout of 30.0s exceeded", cause=None),
        requests.exceptions.ConnectionError("Max retries exceeded"),
        auth.TransportError("Failed to retrieve token"),
    ],
)
def test_transport_failures_read_as_infrastructure(
    fake: FakeClient, error: Exception
) -> None:
    fake.responder = lambda sql: error
    statement = "SELECT 1"
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL).execute(statement)
    assert str(raised.value).startswith(
        "Could not connect to BigQuery at https://bigquery.googleapis.com"
    )
    assert is_infrastructure_error(raised.value, statement)


def test_a_read_timeout_without_a_cap_is_a_connection_error(fake: FakeClient) -> None:
    fake.responder = lambda sql: api.RetryError(
        "Timeout of 30.0s exceeded", cause=requests.exceptions.ReadTimeout("read")
    )
    with pytest.raises(BigQueryError, match="^Could not connect to BigQuery"):
        BigQueryDatabase(EMULATOR_URL).execute("SELECT 1")


def test_retries_that_ran_out_on_a_server_error_say_so(fake: FakeClient) -> None:
    cause = api_error(api.TooManyRequests, "rateLimitExceeded", "Exceeded rate limits")
    fake.responder = lambda sql: api.RetryError("Timeout of 30.0s exceeded", cause)
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(URL).execute("SELECT 1")
    assert str(raised.value) == (
        "BigQuery kept failing after retrying (Timeout of 30.0s exceeded): "
        "rateLimitExceeded: Exceeded rate limits"
    )


def test_other_exceptions_pass_through(fake: FakeClient) -> None:
    error = KeyError("unexpected")
    fake.responder = lambda sql: error
    with pytest.raises(KeyError) as raised:
        BigQueryDatabase(URL).execute("SELECT 1")
    assert raised.value is error


# ----------------------------------------------------------------------
# Introspection
# ----------------------------------------------------------------------


def _field(
    name: str,
    kind: str,
    mode: str = "NULLABLE",
    fields: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    resource: dict[str, Any] = {"name": name, "type": kind, "mode": mode, **extra}
    if fields is not None:
        resource["fields"] = fields
    return resource


def _table(
    dataset: str,
    table: str,
    kind: str = "TABLE",
    fields: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A REST table resource, as ``tables.get`` (and ``tables.list``) return it."""
    return {
        "tableReference": {
            "projectId": PROJECT,
            "datasetId": dataset,
            "tableId": table,
        },
        "type": kind,
        "schema": {"fields": fields or [_field("id", "INTEGER", "REQUIRED")]},
        **extra,
    }


def _fk(name: str, dataset: str, table: str, pairs: list[tuple[str, str]]) -> dict:
    return {
        "name": name,
        "referencedTable": {
            "projectId": PROJECT,
            "datasetId": dataset,
            "tableId": table,
        },
        "columnReferences": [
            {"referencingColumn": a, "referencedColumn": b} for a, b in pairs
        ],
    }


def _music() -> dict[str, list[dict[str, Any]]]:
    return {
        "music": [
            _table(
                "music",
                "Singers",
                fields=[
                    _field("SingerId", "INTEGER", "REQUIRED"),
                    _field("LastName", "STRING", "REQUIRED", maxLength="100"),
                    _field("Score", "FLOAT"),
                    _field("IsActive", "BOOLEAN", "REQUIRED"),
                    _field("Genres", "STRING", "REPEATED"),
                    _field(
                        "Address",
                        "RECORD",
                        fields=[
                            _field("city", "STRING"),
                            _field(
                                "geo",
                                "RECORD",
                                fields=[_field("lat", "FLOAT"), _field("lng", "FLOAT")],
                            ),
                        ],
                    ),
                    _field(
                        "Contracts",
                        "RECORD",
                        "REPEATED",
                        fields=[_field("label", "STRING"), _field("since", "DATE")],
                    ),
                    _field("Profile", "JSON"),
                    _field("Price", "NUMERIC", precision="10", scale="2"),
                    _field("Big", "BIGNUMERIC", precision="40"),
                    _field("Raw", "BYTES", maxLength="16"),
                    _field("Tour", "RANGE", rangeElementType={"type": "DATE"}),
                    _field("Seen", "TIMESTAMP"),
                ],
                tableConstraints={"primaryKey": {"columns": ["SingerId"]}},
            ),
            _table(
                "music",
                "Concerts",
                fields=[
                    _field("VenueId", "INTEGER", "REQUIRED"),
                    _field("SingerId", "INTEGER", "REQUIRED"),
                    _field("ConcertDate", "DATE", "REQUIRED"),
                ],
                tableConstraints={
                    "primaryKey": {"columns": ["VenueId", "SingerId", "ConcertDate"]},
                    "foreignKeys": [
                        _fk("FK_C_S", "music", "Singers", [("SingerId", "SingerId")]),
                        _fk("FK_C_V", "places", "Venues", [("VenueId", "VenueId")]),
                    ],
                },
            ),
            _table(
                "music",
                "TicketSales",
                fields=[
                    _field("SaleId", "INTEGER", "REQUIRED"),
                    _field("VenueId", "INTEGER"),
                    _field("SingerId", "INTEGER"),
                    _field("ConcertDate", "DATE"),
                ],
                tableConstraints={
                    "primaryKey": {"columns": ["SaleId"]},
                    "foreignKeys": [
                        _fk(
                            "FK_T_C",
                            "music",
                            "Concerts",
                            [
                                ("VenueId", "VenueId"),
                                ("SingerId", "SingerId"),
                                ("ConcertDate", "ConcertDate"),
                            ],
                        )
                    ],
                },
            ),
            _table(
                "music",
                "SingerRevenue",
                "VIEW",
                view={"query": "SELECT 1 AS id", "useLegacySql": False},
            ),
            _table("music", "TopSingers", "MATERIALIZED_VIEW"),
        ],
        "places": [
            _table(
                "places", "Venues", tableConstraints={"primaryKey": {"columns": ["id"]}}
            ),
            _table("places", "Imported", "EXTERNAL"),
            _table("places", "Frozen", "SNAPSHOT"),
        ],
    }


@pytest.fixture
def music(fake: FakeClient) -> FakeClient:
    fake.datasets = _music()
    return fake


def test_get_tables_maps_every_table_type(music: FakeClient) -> None:
    frame = BigQueryDatabase(PROJECT_URL).get_tables()
    assert list(frame.columns) == ["table_schema", "table_name", "table_type"]
    assert frame.values.tolist() == [
        ["music", "Concerts", "base table"],
        ["music", "SingerRevenue", "view"],
        ["music", "Singers", "base table"],
        ["music", "TicketSales", "base table"],
        ["music", "TopSingers", "materialized view"],
        ["places", "Frozen", "base table"],
        ["places", "Imported", "base table"],
        ["places", "Venues", "base table"],
    ]
    assert [name for name, _, _ in music.calls] == [
        "list_datasets",
        "list_tables",
        "list_tables",
    ]


def test_metadata_requests_are_bounded(music: FakeClient) -> None:
    BigQueryDatabase(PROJECT_URL).get_columns()
    assert music.calls
    for _, _, kwargs in music.calls:
        assert kwargs["timeout"] == bigquery_module.REQUEST_TIMEOUT_S
        assert kwargs["retry"].timeout == bigquery_module.RETRY_BUDGET_S


def test_a_dataset_in_the_url_restricts_introspection(music: FakeClient) -> None:
    frame = BigQueryDatabase(URL).get_tables()
    assert set(frame.table_schema) == {"music"}
    assert [(name, target) for name, target, _ in music.calls] == [
        ("list_tables", f"{PROJECT}.music")
    ]


def test_a_missing_dataset_names_the_connection_string(music: FakeClient) -> None:
    with pytest.raises(BigQueryError) as raised:
        BigQueryDatabase(f"bigquery://{PROJECT}/nope").get_tables()
    assert str(raised.value).startswith(
        f"BigQuery could not find dataset '{PROJECT}.nope'; check the project and "
        "dataset in the connection string: notFound: Not found: Dataset"
    )


def test_unknown_table_type_is_an_error(fake: FakeClient) -> None:
    fake.datasets = {"music": [_table("music", "X", "MODEL_SOMETHING")]}
    with pytest.raises(BigQueryError, match="MODEL_SOMETHING"):
        BigQueryDatabase(URL).get_tables()


def test_get_columns_types_and_nullability(music: FakeClient) -> None:
    frame = BigQueryDatabase(URL).get_columns()
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
    singers = frame[frame.table_name == "Singers"].set_index("column_name")
    assert singers["data_type"].to_dict() == {
        "SingerId": "INT64",
        "LastName": "STRING(100)",
        "Score": "FLOAT64",
        "IsActive": "BOOL",
        "Genres": "ARRAY<STRING>",
        "Address": "STRUCT<city STRING, geo STRUCT<lat FLOAT64, lng FLOAT64>>",
        "Contracts": "ARRAY<STRUCT<label STRING, since DATE>>",
        "Profile": "JSON",
        "Price": "NUMERIC(10, 2)",
        "Big": "BIGNUMERIC(40)",
        "Raw": "BYTES(16)",
        "Tour": "RANGE<DATE>",
        "Seen": "TIMESTAMP",
    }
    assert singers["is_nullable"].to_dict() == {
        "SingerId": False,
        "LastName": False,
        "Score": True,
        "IsActive": False,
        "Genres": False,
        "Address": True,
        "Contracts": False,
        "Profile": True,
        "Price": True,
        "Big": True,
        "Raw": True,
        "Tour": True,
        "Seen": True,
    }
    assert singers["ordinal_position"].tolist() == list(range(1, 14))
    # Views and materialized views are described too.
    assert {"SingerRevenue", "TopSingers"} <= set(frame.table_name)


def test_get_views(music: FakeClient) -> None:
    frame = BigQueryDatabase(URL).get_views()
    assert frame.values.tolist() == [["music", "SingerRevenue", "SELECT 1 AS id"]]
    described = [target for name, target, _ in music.calls if name == "get_table"]
    assert described == [f"{PROJECT}.music.SingerRevenue"]


def test_get_pks_from_table_constraints(music: FakeClient) -> None:
    frame = BigQueryDatabase(PROJECT_URL).get_pks()
    assert list(frame.columns) == [
        "table_schema",
        "table_name",
        "column_name",
        "ordinal_position",
    ]
    assert frame.ordinal_position.dtype == "int64"
    assert frame.values.tolist() == [
        ["music", "Concerts", "VenueId", 1],
        ["music", "Concerts", "SingerId", 2],
        ["music", "Concerts", "ConcertDate", 3],
        ["music", "Singers", "SingerId", 1],
        ["music", "TicketSales", "SaleId", 1],
        ["places", "Venues", "id", 1],
    ]
    # Views and other relation types carry no constraints and are not fetched.
    described = {target for name, target, _ in music.calls if name == "get_table"}
    assert f"{PROJECT}.music.SingerRevenue" not in described
    assert f"{PROJECT}.places.Imported" not in described


def test_get_fks_multi_column_and_across_datasets(music: FakeClient) -> None:
    frame = BigQueryDatabase(PROJECT_URL).get_fks()
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
        ["music", "Concerts", "VenueId", "places", "Venues", "VenueId"],
        ["music", "TicketSales", "VenueId", "music", "Concerts", "VenueId"],
        ["music", "TicketSales", "SingerId", "music", "Concerts", "SingerId"],
        ["music", "TicketSales", "ConcertDate", "music", "Concerts", "ConcertDate"],
    ]


def test_get_fks_leaves_out_keys_to_tables_not_ingested(
    music: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=bigquery_module.__name__):
        frame = BigQueryDatabase(URL).get_fks()
    assert ["music", "Concerts", "VenueId", "places", "Venues", "VenueId"] not in (
        frame.values.tolist()
    )
    assert len(frame) == 4
    assert "music.Concerts.FK_C_V -> my-project-123.places.Venues" in caplog.text


def test_get_fks_leaves_out_keys_to_another_project(
    fake: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    key = _fk("FK_X", "music", "Other", [("a", "b")])
    key["referencedTable"]["projectId"] = "other-project"
    fake.datasets = {
        "music": [_table("music", "T", tableConstraints={"foreignKeys": [key]})]
    }
    with caplog.at_level(logging.WARNING, logger=bigquery_module.__name__):
        frame = BigQueryDatabase(URL).get_fks()
    assert frame.empty and list(frame.columns)[0] == "table_schema"
    assert "other-project.music.Other" in caplog.text


def test_a_table_dropped_while_describing_is_an_error(music: FakeClient) -> None:
    music.errors[f"{PROJECT}.music.Singers"] = api_error(
        api.NotFound, "notFound", f"Not found: Table {PROJECT}:music.Singers"
    )
    with pytest.raises(BigQueryError, match="could not find table"):
        BigQueryDatabase(URL).get_columns()


@pytest.mark.parametrize(
    "method", ["get_tables", "get_columns", "get_views", "get_pks", "get_fks"]
)
def test_empty_dataset_gives_shaped_frames(fake: FakeClient, method: str) -> None:
    fake.datasets = {"music": []}
    frame = getattr(BigQueryDatabase(URL), method)()
    assert frame.empty
    assert "table_schema" in frame.columns and "table_name" in frame.columns


@pytest.mark.parametrize(
    "method", ["get_tables", "get_columns", "get_views", "get_pks", "get_fks"]
)
def test_empty_project_gives_shaped_frames(fake: FakeClient, method: str) -> None:
    frame = getattr(BigQueryDatabase(PROJECT_URL), method)()
    assert frame.empty
    assert "table_schema" in frame.columns and "table_name" in frame.columns


def test_frames_match_the_spanner_shapes(music: FakeClient) -> None:
    from auto_ontology.connectors import spanner

    connector = BigQueryDatabase(PROJECT_URL)
    assert list(connector.get_tables().columns) == spanner._TABLE_SCHEMA
    assert list(connector.get_columns().columns) == spanner._COLUMN_SCHEMA
    assert list(connector.get_views().columns) == spanner._VIEW_SCHEMA
    assert list(connector.get_pks().columns) == spanner._PK_SCHEMA
    assert list(connector.get_fks().columns) == spanner._FK_SCHEMA


def test_get_queries_is_empty_and_shaped(fake: FakeClient) -> None:
    frame = BigQueryDatabase(URL).get_queries()
    assert frame.empty and list(frame.columns) == ["end_time", "query_text"]
    assert fake.calls == [] and fake.queries == []


def test_every_listed_relation_has_columns(music: FakeClient) -> None:
    """The coverage rule ingestion enforces, on the fake catalog."""
    from auto_ontology.catalog.extract import data_for_populate_tabular

    data = data_for_populate_tabular(BigQueryDatabase(PROJECT_URL))
    assert data["database_name"] == PROJECT
    assert len(data["tables"]) == 8
    assert len(data["fks"]) == 5


# ----------------------------------------------------------------------
# Import cost
# ----------------------------------------------------------------------


def test_registry_import_does_not_load_the_bigquery_client() -> None:
    """The registry imports every connector at backend start."""
    code = (
        "import sys\n"
        "import auto_ontology.connectors.registry\n"
        "loaded = sorted(m for m in sys.modules if m.startswith("
        "('google.cloud.bigquery', 'google.cloud.bigquery_storage', 'db_dtypes')))\n"
        "print(loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_running_a_query_does_not_need_the_storage_api() -> None:
    """``google-cloud-bigquery-storage`` is not installed, and is not needed."""
    import importlib.util

    assert importlib.util.find_spec("google.cloud.bigquery_storage") is None
