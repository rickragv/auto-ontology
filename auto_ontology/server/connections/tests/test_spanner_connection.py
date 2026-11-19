# SPDX-License-Identifier: Apache-2.0

"""A Spanner connection added in the UI, through the connections service.

Spanner has no host, user or password fields and no schema list, so these
check that the service paths built around those still work for it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from auto_ontology.connectors import spanner as spanner_module
from auto_ontology.connectors.spanner import SpannerDatabase
from auto_ontology.server.connections import service
from auto_ontology.server.connections.router import _serialize_connection


def _spanner_connection(**overrides: object) -> dict[str, object]:
    connection: dict[str, object] = {
        "type": "spanner",
        "project_id": "auto-ontology",
        "instance_id": "auto-ontology",
        "database": "music",
        "emulator_host": "host.docker.internal:9010",
        "table_allow_regex": "",
        "table_deny_regex": "",
    }
    connection.update(overrides)
    return connection


@pytest.fixture(autouse=True)
def _no_emulator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(spanner_module.EMULATOR_ENV_VAR, raising=False)


def test_connection_test_pings_spanner_and_returns_no_schemas() -> None:
    """Spanner has no ``get_schemas``, so the test falls back to ``ping``."""
    assert not hasattr(SpannerDatabase, "get_schemas")
    created: list[SpannerDatabase] = []
    real_create = service.create_connector

    def create(connection_string: str) -> SpannerDatabase:
        connector = real_create(connection_string)
        created.append(connector)
        return connector

    with (
        patch.object(service, "_database_already_connected", return_value=False),
        patch.object(service, "create_connector", side_effect=create),
        patch.object(SpannerDatabase, "ping") as ping,
        patch.object(SpannerDatabase, "close") as close,
    ):
        schemas = service.test_connection(_spanner_connection())

    assert schemas == []
    ping.assert_called_once_with()
    close.assert_called_once_with()
    assert len(created) == 1
    # Ingestion and query routing find the connector by this name.
    assert created[0].database_name == "music"


def test_connection_test_reports_a_missing_id_by_field_name() -> None:
    with patch.object(service, "_database_already_connected", return_value=False):
        with pytest.raises(ValueError, match="'instance_id'"):
            service.test_connection(_spanner_connection(instance_id=""))


def test_create_connection_stores_spanner_without_secrets() -> None:
    connection = _spanner_connection()

    with (
        patch.object(service, "_database_already_connected", return_value=False),
        patch.object(service, "is_vault_configured", return_value=False),
        patch.object(service, "insert_connection") as insert_connection,
        patch.object(service, "invalidate_connectors_cache"),
        patch.object(service, "refresh_chat_workers"),
        patch.object(service, "trigger_ingest") as trigger_ingest,
    ):
        service.create_connection(connection=connection)

    assert insert_connection.call_args.kwargs["database_name"] == "music"
    assert json.loads(insert_connection.call_args.kwargs["connection"]) == connection
    trigger_ingest.assert_called_once_with(connection)

    # Nothing in a Spanner connection is a credential, so nothing is hidden.
    assert _serialize_connection(connection) == {
        "database_name": "music",
        "connection": connection,
    }
