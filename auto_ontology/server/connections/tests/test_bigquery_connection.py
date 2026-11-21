# SPDX-License-Identifier: Apache-2.0

"""A BigQuery connection added in the UI, through the connections service.

BigQuery has no host, user or password fields and no schema list, and its
``database`` field holds the project id, so these check that the service paths
built around those still work for it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from auto_ontology.connectors import bigquery as bigquery_module
from auto_ontology.connectors.bigquery import BigQueryDatabase
from auto_ontology.server.connections import service
from auto_ontology.server.connections.router import _serialize_connection


def _bigquery_connection(**overrides: object) -> dict[str, object]:
    connection: dict[str, object] = {
        "type": "bigquery",
        "database": "auto-ontology-test",
        "dataset": "auto_ontology_music",
        "location": "",
        "maximum_bytes_billed": "1000000000",
        "emulator_host": "host.docker.internal:9050",
        "table_allow_regex": "",
        "table_deny_regex": "",
    }
    connection.update(overrides)
    return connection


@pytest.fixture(autouse=True)
def _no_emulator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bigquery_module.EMULATOR_ENV_VAR, raising=False)


def test_connection_test_pings_bigquery_and_returns_no_schemas() -> None:
    """BigQuery has no ``get_schemas``, so the test falls back to ``ping``."""
    assert not hasattr(BigQueryDatabase, "get_schemas")
    created: list[BigQueryDatabase] = []
    real_create = service.create_connector

    def create(connection_string: str) -> BigQueryDatabase:
        connector = real_create(connection_string)
        created.append(connector)
        return connector

    with (
        patch.object(service, "_database_already_connected", return_value=False),
        patch.object(service, "create_connector", side_effect=create),
        patch.object(BigQueryDatabase, "ping") as ping,
        patch.object(BigQueryDatabase, "close") as close,
    ):
        schemas = service.test_connection(_bigquery_connection())

    assert schemas == []
    ping.assert_called_once_with()
    close.assert_called_once_with()
    assert len(created) == 1
    # Ingestion and query routing find the connector by this name, and the
    # service stores and looks up the connection by ``database``.
    assert created[0].database_name == "auto-ontology-test"


def test_connection_test_requires_the_project() -> None:
    with patch.object(service, "_database_already_connected", return_value=False):
        with pytest.raises(ValueError, match="Database name is required"):
            service.test_connection(_bigquery_connection(database=""))


def test_connection_test_reports_a_bad_limit_by_name() -> None:
    with patch.object(service, "_database_already_connected", return_value=False):
        with pytest.raises(ValueError, match="maximum_bytes_billed"):
            service.test_connection(_bigquery_connection(maximum_bytes_billed="1 GB"))


def test_a_second_connection_to_the_same_project_is_refused() -> None:
    """Routing is by project, so a second dataset needs no second connection."""
    with patch.object(service, "_database_already_connected", return_value=True):
        with pytest.raises(ValueError, match="already exists"):
            service.test_connection(_bigquery_connection(dataset="other"))


def test_create_connection_stores_bigquery_without_secrets() -> None:
    connection = _bigquery_connection()

    with (
        patch.object(service, "_database_already_connected", return_value=False),
        patch.object(service, "is_vault_configured", return_value=False),
        patch.object(service, "insert_connection") as insert_connection,
        patch.object(service, "invalidate_connectors_cache"),
        patch.object(service, "refresh_chat_workers"),
        patch.object(service, "trigger_ingest") as trigger_ingest,
    ):
        service.create_connection(connection=connection)

    assert insert_connection.call_args.kwargs["database_name"] == "auto-ontology-test"
    assert json.loads(insert_connection.call_args.kwargs["connection"]) == connection
    trigger_ingest.assert_called_once_with(connection)

    # Nothing in a BigQuery connection is a credential, so nothing is hidden.
    assert _serialize_connection(connection) == {
        "database_name": "auto-ontology-test",
        "connection": connection,
    }
