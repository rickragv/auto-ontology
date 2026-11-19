# SPDX-License-Identifier: Apache-2.0

"""Google Cloud Spanner connector implementing Auto Ontology's SQLDatabase ABC.

Spanner is reached with the official ``google-cloud-spanner`` client, used
natively rather than through its DB-API layer. Every statement runs in a
single-use **read-only snapshot**, so a generated ``INSERT``, ``UPDATE`` or DDL
statement is rejected by the server instead of being applied.

Connection string
-----------------
::

    spanner://<project>/<instance>/<database>
    spanner://<project>/<instance>/<database>?emulator_host=<host>:<port>

The URL carries no secret. Without ``emulator_host`` the client authenticates
with Application Default Credentials (``gcloud auth application-default
login``, ``GOOGLE_APPLICATION_CREDENTIALS``, or the metadata server).
**That path is implemented but has only been exercised against the emulator's
anonymous path, never against a real Cloud Spanner project.**

With ``emulator_host`` the client talks to that emulator with anonymous
credentials over an insecure channel, chosen per client (the library's
``client_options={"api_endpoint": ...}`` with ``AnonymousCredentials``), so the
URL alone decides where the connection goes. The library also honours the
process-wide ``SPANNER_EMULATOR_HOST`` and lets it win over anything passed to
the client, which would silently redirect a production URL to an emulator. So:

* ``SPANNER_EMULATOR_HOST`` set and the URL has no ``emulator_host`` -> error.
* Both set to different values -> error.
* Both set to the same value -> allowed.

Only GoogleSQL-dialect databases are supported. A PostgreSQL-dialect database
is detected on the first statement (``INFORMATION_SCHEMA.DATABASE_OPTIONS``)
and refused.

Naming
------
Spanner accepts ``Table`` or ``schema.Table``, never a database qualifier, and
its default schema is the empty string. The text-to-SQL prompts always name a
table as ``<database>.<schema>.<table>`` (collapsing the schema when it equals
the database). So, as :mod:`auto_ontology.connectors.heavydb` does:

* The default schema is reported as the database id, so prompts show
  ``music.Singers``; a named schema keeps its name (``music.sales.Promotions``).
* :meth:`SpannerDatabase.execute` removes a leading ``<database>.`` qualifier,
  in any quoting, before the statement is sent (see :func:`strip_database_qualifier`).
* :meth:`SpannerDatabase.qualify` emits ```Table``` or ```schema`.`Table```.

A named schema spelled like the database id would make those two readings
ambiguous, so introspection refuses it.

Relationships
-------------
Declared foreign keys come from ``INFORMATION_SCHEMA``. An interleaved table is
also reported as a foreign key from the leading columns of its primary key to
its parent's primary key -- Spanner's main parent/child relationship.

Server loss
-----------
The client library restarts a streaming read whenever the server answers
``UNAVAILABLE``, with no limit; once a session exists, a server that goes away
makes a statement spin forever. Each statement therefore runs on a worker
thread while the caller watches the gRPC channel: if it has not been ``READY``
for :data:`UNAVAILABLE_BUDGET_S` seconds, the channel is closed (which ends the
worker) and :class:`SpannerError` is raised. A statement with ``timeout_s`` is
also abandoned if it outlives the cap plus a short grace, as a backstop to the
server-side deadline.

Example
-------
::

    CONNECTION_STRINGS=spanner://my-project/my-instance/music
    CONNECTION_STRINGS=spanner://auto-ontology/auto-ontology/music?emulator_host=localhost:9010
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import parse_qs, urlparse

import pandas as pd

from auto_ontology.catalog.constants import TableTypes
from auto_ontology.connectors.base import SQLDatabase, StatementTimeout

if TYPE_CHECKING:  # pragma: no cover - typing only
    from google.cloud.spanner_v1.client import Client
    from google.cloud.spanner_v1.database import Database

logger = logging.getLogger(__name__)

#: The variable the client library reads to redirect every client to an emulator.
EMULATOR_ENV_VAR = "SPANNER_EMULATOR_HOST"

#: Seconds a statement may wait on a channel that is not ``READY`` before the
#: connection is abandoned. Matches the library's own session-creation budget.
UNAVAILABLE_BUDGET_S = 30.0

#: Extra seconds past ``timeout_s`` before the client gives up on a statement
#: whose server-side deadline did not fire.
DEADLINE_GRACE_S = 5.0

# How often the waiting caller re-checks the channel and the deadline. A
# finished statement wakes the caller at once; this only bounds detection.
_POLL_S = 0.25

_PUBLIC_ENDPOINT = "spanner.googleapis.com"

# Spanner's own metadata schemas, never ingested.
_SYSTEM_SCHEMAS = ("INFORMATION_SCHEMA", "SPANNER_SYS")
_SYSTEM_SCHEMA_LIST = ", ".join(f"'{name}'" for name in _SYSTEM_SCHEMAS)

_ALLOWED_QUERY_PARAMETERS = frozenset({"emulator_host"})

# Permissive on purpose: these catch a malformed URL (a stray character, a
# missing segment) without second-guessing Google's id rules, which the server
# enforces anyway. ``:`` is allowed in a project for domain-scoped projects
# (``example.com:my-project``).
_PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:_-]*")
_INSTANCE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_DATABASE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_EMULATOR_HOST_RE = re.compile(
    r"[A-Za-z0-9.\-]+:[0-9]{1,5}|\[[0-9A-Fa-f:.]+\]:[0-9]{1,5}"
)

_GOOGLESQL = "GOOGLE_STANDARD_SQL"

_TABLE_SCHEMA = ["table_schema", "table_name", "table_type"]
_COLUMN_SCHEMA = [
    "table_schema",
    "table_name",
    "column_name",
    "data_type",
    "is_nullable",
    "ordinal_position",
]
_VIEW_SCHEMA = ["table_schema", "table_name", "view_definition"]
_PK_SCHEMA = ["table_schema", "table_name", "column_name", "ordinal_position"]
_FK_SCHEMA = [
    "table_schema",
    "table_name",
    "column_name",
    "referenced_schema",
    "referenced_table",
    "referenced_column",
]

_TABLE_TYPES = {"BASE TABLE": TableTypes.BASE_TABLE, "VIEW": TableTypes.VIEW}


class SpannerError(RuntimeError):
    """An error reported by Spanner, or reaching it.

    Statement errors read ``<gRPC status>: <the server's message>``, and transport
    failures start with "Could not connect to Spanner", so
    :mod:`auto_ontology.connectors.db_errors` tells "the SQL was wrong" apart
    from "the database is unreachable" the same way it does for every engine.
    """


# ----------------------------------------------------------------------
# Connection string
# ----------------------------------------------------------------------


def _parse_connection_string(connection_string: str) -> dict[str, str | None]:
    """Parse ``spanner://project/instance/database[?emulator_host=host:port]``.

    Raises:
        ValueError: anything else -- naming the part that is wrong and the
            form that is accepted.
    """
    form = "spanner://<project>/<instance>/<database>[?emulator_host=<host>:<port>]"
    parsed = urlparse(connection_string)
    if parsed.scheme.lower() != "spanner":
        raise ValueError(f"Not a Spanner URL (expected {form}): {connection_string}")
    if "@" in parsed.netloc:
        raise ValueError(
            "Spanner connection strings carry no user or password; credentials "
            f"come from Application Default Credentials. Expected {form}"
        )
    if parsed.fragment or parsed.params:
        raise ValueError(f"Unexpected '#' or ';' in Spanner URL; expected {form}")

    project = parsed.netloc
    segments = parsed.path.split("/")[1:] if parsed.path.startswith("/") else []
    if len(segments) != 2:
        raise ValueError(
            f"Spanner URL must name exactly a project, an instance and a "
            f"database; got path {parsed.path!r}. Expected {form}"
        )
    instance, database = segments
    for label, value, pattern in (
        ("project", project, _PROJECT_RE),
        ("instance", instance, _INSTANCE_RE),
        ("database", database, _DATABASE_RE),
    ):
        if not value:
            raise ValueError(f"Spanner URL has an empty {label}; expected {form}")
        if not pattern.fullmatch(value):
            raise ValueError(
                f"Invalid Spanner {label} id {value!r}: use letters, digits, "
                f"'-' and '_' (expected {form})"
            )
    if database.upper() in _SYSTEM_SCHEMAS:
        raise ValueError(
            f"{database!r} is a Spanner system schema, not a database; name the "
            "database that holds your tables"
        )

    query = parse_qs(parsed.query, keep_blank_values=True)
    unknown = sorted(set(query) - _ALLOWED_QUERY_PARAMETERS)
    if unknown:
        raise ValueError(
            f"Unknown Spanner URL parameter(s) {unknown}; the only one accepted "
            "is emulator_host"
        )
    emulator_host: str | None = None
    if "emulator_host" in query:
        values = query["emulator_host"]
        if len(values) != 1:
            raise ValueError("emulator_host may be given only once")
        emulator_host = values[0]
        if not _EMULATOR_HOST_RE.fullmatch(emulator_host):
            raise ValueError(
                f"Invalid emulator_host {emulator_host!r}: expected host:port "
                "with no scheme, e.g. localhost:9010"
            )

    return {
        "project": project,
        "instance": instance,
        "database": database,
        "emulator_host": emulator_host,
    }


def format_connection_string(
    *,
    project: str,
    instance: str,
    database: str,
    emulator_host: str | None = None,
) -> str:
    """Build the URL :func:`_parse_connection_string` reads back into these parts.

    Used for connections added in the UI. Every character an accepted id or
    emulator host may contain is URL-safe, so nothing is percent-encoded: a
    value that would need encoding is not a valid id and is refused here,
    naming the field, rather than turned into a URL that parses differently.

    Raises:
        ValueError: a part is empty or is not a valid id or ``host:port``.
    """
    id_rule = "a letter, then letters, digits, '-' and '_'"
    for label, value, pattern, rule in (
        ("project", project, _PROJECT_RE, "letters, digits, '-', '_', '.' and ':'"),
        ("instance", instance, _INSTANCE_RE, id_rule),
        ("database", database, _DATABASE_RE, id_rule),
    ):
        if not value:
            raise ValueError(f"Spanner {label} id is required")
        if not pattern.fullmatch(value):
            raise ValueError(f"Invalid Spanner {label} id {value!r}: use {rule}")
    url = f"spanner://{project}/{instance}/{database}"
    if emulator_host is not None:
        if not _EMULATOR_HOST_RE.fullmatch(emulator_host):
            raise ValueError(
                f"Invalid Spanner emulator host {emulator_host!r}: expected "
                "host:port with no scheme, e.g. localhost:9010"
            )
        url += f"?emulator_host={emulator_host}"
    # Also applies the checks that are not about characters (system schemas).
    _parse_connection_string(url)
    return url


def _check_emulator_environment(emulator_host: str | None) -> None:
    """Refuse a ``SPANNER_EMULATOR_HOST`` the URL did not ask for.

    The client library reads the variable in every ``Client()`` and prefers it
    to the endpoint it is given, so a mismatch would send the connection
    somewhere other than the URL says. An empty value counts as set: the
    library then builds an insecure channel to an empty address.
    """
    env_host = os.environ.get(EMULATOR_ENV_VAR)
    if env_host is None:
        return
    if emulator_host is None:
        raise ValueError(
            f"{EMULATOR_ENV_VAR} is set to {env_host!r}, which would silently "
            "redirect this Spanner connection to an emulator, but the URL has "
            f"no emulator_host. Unset {EMULATOR_ENV_VAR}, or add "
            f"?emulator_host={env_host} to the URL to use that emulator."
        )
    if env_host != emulator_host:
        raise ValueError(
            f"{EMULATOR_ENV_VAR} is {env_host!r} but the URL asks for "
            f"emulator_host={emulator_host!r}; the client library would use the "
            "environment variable. Make them equal or unset the variable."
        )


# ----------------------------------------------------------------------
# Database qualifier removal
# ----------------------------------------------------------------------

_IDENT_START = re.compile(r"[A-Za-z_]")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"[0-9][A-Za-z0-9_.]*")
_STRING_PREFIX = re.compile(r"(?:[rR][bB]?|[bB][rR]?)(?=['\"])")


@dataclass
class _Token:
    kind: str  # "ident", "quoted", "dot", "other"
    start: int
    end: int
    text: str  # identifier text; for "quoted" the content between backticks


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    """Return the index just past the literal or identifier opening at *start*.

    Handles triple quotes and backslash escapes (which also guard the closing
    quote in raw strings). An unterminated one runs to the end of the text.
    """
    if quote in "'\"" and sql.startswith(quote * 3, start):
        end = start + 3
        while end < len(sql):
            if sql[end] == "\\":
                end += 2
                continue
            if sql.startswith(quote * 3, end):
                return end + 3
            end += 1
        return len(sql)
    end = start + 1
    while end < len(sql):
        char = sql[end]
        if char == "\\":
            end += 2
            continue
        if char == quote:
            return end + 1
        end += 1
    return len(sql)


def _tokenize(sql: str, hyphenated: str | None) -> list[_Token]:
    """Split *sql* into code tokens, dropping comments and string literals.

    *hyphenated* is a database id containing ``-``: GoogleSQL cannot spell it
    as one bare identifier, but a model copying the prompt writes it bare, so
    it is recognised as a single identifier token.
    """
    tokens: list[_Token] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
        elif sql.startswith("--", index) or char == "#":
            newline = sql.find("\n", index)
            index = length if newline < 0 else newline + 1
        elif sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            index = length if close < 0 else close + 2
        elif char in "'\"":
            index = _skip_quoted(sql, index, char)
            tokens.append(_Token("other", index, index, ""))
        elif char == "`":
            end = _skip_quoted(sql, index, "`")
            tokens.append(_Token("quoted", index, end, sql[index + 1 : end - 1]))
            index = end
        elif (prefix := _STRING_PREFIX.match(sql, index)) is not None:
            index = _skip_quoted(sql, prefix.end(), sql[prefix.end()])
            tokens.append(_Token("other", index, index, ""))
        elif _IDENT_START.match(char):
            if (
                hyphenated
                and sql[index : index + len(hyphenated)].lower() == hyphenated
                and not _continues_identifier(sql, index + len(hyphenated))
            ):
                end = index + len(hyphenated)
            else:
                end = _IDENT.match(sql, index).end()  # type: ignore[union-attr]
            tokens.append(_Token("ident", index, end, sql[index:end]))
            index = end
        elif char.isdigit():
            end = _NUMBER.match(sql, index).end()  # type: ignore[union-attr]
            tokens.append(_Token("other", index, end, ""))
            index = end
        elif char == ".":
            tokens.append(_Token("dot", index, index + 1, "."))
            index += 1
        else:
            tokens.append(_Token("other", index, index + 1, char))
            index += 1
    return tokens


def _continues_identifier(sql: str, index: int) -> bool:
    return index < len(sql) and (sql[index].isalnum() or sql[index] in "_-")


def _backticked(name: str) -> str:
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def strip_database_qualifier(sql: str, database: str) -> str:
    """Remove a leading ``<database>.`` from every table path in *sql*.

    Recognised spellings, any letter case: ``db.T``, ```db`.T``, ```db.T```
    (rewritten to ```T```), ```db`.`T``` and ``db.schema.T``; a database id
    with ``-`` also bare (``my-db.T``). Only the qualifier is removed: string
    literals, comments and every other byte of the statement are left as they
    are.

    A qualifier is only recognised at the start of a path (not after another
    ``.``), and only when the statement does not also use the database id as a
    name of its own -- an alias, a CTE or a bare column spelled the same -- in
    which case the statement is returned unchanged rather than guessed at.
    """
    target = database.lower()
    tokens = _tokenize(sql, target if "-" in target else None)

    def names_database(token: _Token) -> bool:
        return token.kind in ("ident", "quoted") and token.text.lower() == target

    edits: list[tuple[int, int, str]] = []
    for position, token in enumerate(tokens):
        previous = tokens[position - 1] if position else None
        after_dot = previous is not None and previous.kind == "dot"
        following = tokens[position + 1] if position + 1 < len(tokens) else None
        if names_database(token):
            if following is None or following.kind != "dot":
                if not after_dot:
                    # The statement uses the id as a name of its own.
                    return sql
                continue
            target_token = tokens[position + 2] if position + 2 < len(tokens) else None
            if (
                after_dot
                or target_token is None
                or target_token.kind not in ("ident", "quoted")
            ):
                continue
            edits.append((token.start, target_token.start, ""))
        elif (
            token.kind == "quoted"
            and not after_dot
            and "\\" not in token.text
            and token.text.lower().startswith(target + ".")
        ):
            rest = token.text[len(target) + 1 :]
            parts = rest.split(".")
            if all(parts):
                replacement = ".".join(_backticked(part) for part in parts)
                edits.append((token.start, token.end, replacement))

    if not edits:
        return sql
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        pieces.append(sql[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(sql[cursor:])
    return "".join(pieces)


# ----------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------


class _ChannelWatch:
    """Track how long the data channel has been away from ``READY``."""

    def __init__(self, channel: Any) -> None:
        self._channel = channel
        self._lock = threading.Lock()
        # Not READY until the first RPC connects; a statement's own start time
        # bounds how far back this counts (see unready_for).
        self._unready_since: float | None = time.monotonic()
        channel.subscribe(self._on_change, try_to_connect=False)

    def _on_change(self, state: Any) -> None:
        name = getattr(state, "name", str(state))
        with self._lock:
            if name == "READY":
                self._unready_since = None
            elif self._unready_since is None:
                self._unready_since = time.monotonic()

    def unready_for(self, since: float) -> float:
        """Seconds the channel has been away from READY, counted from *since*."""
        with self._lock:
            if self._unready_since is None:
                return 0.0
            return max(0.0, time.monotonic() - max(self._unready_since, since))

    def stop(self) -> None:
        self._channel.unsubscribe(self._on_change)


@dataclass
class _Handle:
    """One open client: the database, its channel watch, and whether it closed."""

    client: "Client"
    database: "Database"
    watch: _ChannelWatch
    closed: threading.Event = field(default_factory=threading.Event)


def _open_client(settings: dict[str, str | None]) -> tuple["Client", "Database"]:
    """Build the client and database objects. No network traffic happens here.

    The library is imported here, not at module import: the registry imports
    every connector when the backend starts, and a deployment with no Spanner
    connection should not load gRPC.
    """
    from google.auth.credentials import AnonymousCredentials
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import spanner

    emulator_host = settings["emulator_host"]
    _check_emulator_environment(emulator_host)
    kwargs: dict[str, Any] = {
        "project": settings["project"],
        # The built-in metrics install a process-wide OpenTelemetry provider and
        # export to Cloud Monitoring; a connector must do neither.
        "disable_builtin_metrics": True,
    }
    if emulator_host is not None:
        kwargs["credentials"] = AnonymousCredentials()
        kwargs["client_options"] = {"api_endpoint": emulator_host}
    try:
        client = spanner.Client(**kwargs)
    except DefaultCredentialsError as error:
        raise SpannerError(
            f"Could not connect to Spanner project {settings['project']!r}: no "
            "Google Cloud credentials were found. Run `gcloud auth "
            "application-default login` or set GOOGLE_APPLICATION_CREDENTIALS "
            f"({error})"
        ) from error
    database = client.instance(str(settings["instance"])).database(
        str(settings["database"])
    )
    return client, database


def _fetch(database: "Database", sql: str, timeout_s: float | None) -> pd.DataFrame:
    """Run *sql* in a single-use read-only snapshot and collect the rows."""
    kwargs: dict[str, Any] = {}
    if timeout_s is not None:
        kwargs["timeout"] = timeout_s
    with database.snapshot() as snapshot:
        result = snapshot.execute_sql(sql, **kwargs)
        rows = [list(row) for row in result]
        columns = [field.name for field in (result.fields or [])]
    return pd.DataFrame(rows, columns=columns)


def _describe(error: BaseException) -> str:
    """Return ``STATUS: message`` for a Google API error, ``str(error)`` otherwise.

    ``str()`` of a ``GoogleAPICallError`` ends with the raw ``details`` protos
    (``[type_url: "type.googleapis.com/googlesql.ErrorMessageModeForPayload"
    value: "\\010\\002"]``), and the Spanner client appends ``, request_id =
    ...`` to the message. Neither helps a reader or the SQL-repair prompt, which
    both receive this text. Spanner's own message, including the statement and
    caret it echoes, is kept whole. The original exception, request id
    included, stays chained as ``__cause__``.
    """
    from google.api_core import exceptions as api

    if not isinstance(error, api.GoogleAPICallError):
        return str(error)
    message = str(error.message)
    request_id = getattr(error, "request_id", None)
    if request_id:
        message = message.removesuffix(f", request_id = {request_id}")
    if error.grpc_status_code is not None:
        status = error.grpc_status_code.name
    elif error.code is not None:
        status = f"HTTP {error.code}"
    else:
        status = type(error).__name__
    return f"{status}: {message}"


class SpannerDatabase(SQLDatabase):
    """Concrete :class:`SQLDatabase` backed by Google Cloud Spanner.

    Parameters
    ----------
    connection_string:
        ``spanner://<project>/<instance>/<database>[?emulator_host=host:port]``

    Construction only parses the URL; the client is created on the first
    statement, so loading the registry never touches the network.
    """

    supports_statement_timeout = True

    def __init__(self, connection_string: str) -> None:
        self._settings = _parse_connection_string(connection_string)
        _check_emulator_environment(self._settings["emulator_host"])
        self._database_id = str(self._settings["database"])
        self._endpoint = self._settings["emulator_host"] or _PUBLIC_ENDPOINT
        self._path = (
            f"projects/{self._settings['project']}/instances/"
            f"{self._settings['instance']}/databases/{self._database_id}"
        )
        # _open_lock serialises opening a handle; _lock guards swapping it.
        self._open_lock = threading.Lock()
        self._lock = threading.Lock()
        self._handle: _Handle | None = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def dialect(self) -> str:
        """Return this engine's sqlglot dialect name.

        Must be a member of ``sqlglot.dialects.DIALECTS`` -- callers pass it
        straight to sqlglot without translation. See ``CONNECTOR_REGISTRY``.

        sqlglot has no Spanner dialect. Spanner's GoogleSQL is the language
        BigQuery speaks, which sqlglot does know.
        """
        return "bigquery"

    @property
    def database_name(self) -> str:
        """The Spanner database id; also the ``table_schema`` of the default schema."""
        return self._database_id

    def qualify(self, schema: Optional[str], table: str) -> str:
        """Return ```Table``` for the default schema, ```schema`.`Table``` otherwise.

        The default schema is reported as the database id (see the module
        docstring), and Spanner accepts no database qualifier, so that schema
        is dropped rather than emitted.
        """
        if not schema or schema == self._database_id:
            return _backticked(table)
        return f"{_backticked(schema)}.{_backticked(table)}"

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _current(self) -> _Handle:
        """Return the open handle, opening one (and checking the dialect) if needed.

        A new handle is published only after its dialect check passes, so a
        concurrent statement never runs on a database about to be refused.
        """
        handle = self._handle
        if handle is not None:
            return handle
        with self._open_lock:
            if self._handle is not None:
                return self._handle
            client, database = _open_client(self._settings)
            watch = _ChannelWatch(database.spanner_api.transport.grpc_channel)
            handle = _Handle(client=client, database=database, watch=watch)
            try:
                dialect = self._run(
                    handle,
                    "SELECT OPTION_VALUE FROM INFORMATION_SCHEMA.DATABASE_OPTIONS "
                    "WHERE OPTION_NAME = 'database_dialect'",
                    None,
                )
            except BaseException:
                self._abandon(handle)
                raise
            values = [str(value).upper() for value in dialect.iloc[:, 0].tolist()]
            if values != [_GOOGLESQL]:
                self._abandon(handle)
                if values == ["POSTGRESQL"]:
                    raise SpannerError(
                        f"PostgreSQL-dialect Spanner databases are not supported "
                        f"({self._path}); only GoogleSQL databases can be connected."
                    )
                raise SpannerError(
                    f"Could not determine the dialect of Spanner database "
                    f"{self._path}: database_dialect is {values!r}"
                )
            with self._lock:
                self._handle = handle
            logger.info(
                "Opened Spanner connection to %s at %s", self._path, self._endpoint
            )
            return handle

    def _abandon(self, handle: _Handle) -> None:
        """Close *handle*'s channel, ending any statement still running on it."""
        with self._lock:
            if self._handle is handle:
                self._handle = None
        if handle.closed.is_set():
            return
        handle.closed.set()
        try:
            handle.watch.stop()
        except Exception:
            logger.debug(
                "Failed to unsubscribe from the Spanner channel", exc_info=True
            )
        handle.database.spanner_api.transport.close()

    def _run(self, handle: _Handle, sql: str, timeout_s: float | None) -> pd.DataFrame:
        """Run *sql* on a worker thread, giving up if the server stops answering."""
        outcome: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                outcome["frame"] = _fetch(handle.database, sql, timeout_s)
            except BaseException as error:  # noqa: BLE001 - re-raised on the caller
                outcome["error"] = error
            finally:
                done.set()

        started = time.monotonic()
        threading.Thread(target=work, name="spanner-statement", daemon=True).start()
        backstop = None if timeout_s is None else started + timeout_s + DEADLINE_GRACE_S
        while not done.wait(_POLL_S):
            unready = handle.watch.unready_for(started)
            if unready > UNAVAILABLE_BUDGET_S:
                self._abandon(handle)
                raise SpannerError(
                    f"Could not connect to Spanner at {self._endpoint}: no usable "
                    f"connection for {unready:.0f}s while running a statement on "
                    f"{self._path}"
                )
            if backstop is not None and time.monotonic() > backstop:
                self._abandon(handle)
                raise StatementTimeout.since(timeout_s, started)

        error = outcome.get("error")
        if error is None:
            return outcome["frame"]
        translated = self._translate(error, handle, timeout_s, started)
        if translated is error:
            raise error
        raise translated from error

    def _translate(
        self,
        error: BaseException,
        handle: _Handle,
        timeout_s: float | None,
        started: float,
    ) -> BaseException:
        """Map a client error onto the connector's error types."""
        from google.api_core import exceptions as api

        if handle.closed.is_set():
            return SpannerError(
                f"Could not connect to Spanner at {self._endpoint}: the connection "
                f"was closed while the statement was running ({_describe(error)})"
            )
        if isinstance(error, api.DeadlineExceeded) and timeout_s is not None:
            return StatementTimeout.since(timeout_s, started)
        if isinstance(error, (api.ServiceUnavailable, api.RetryError)):
            self._abandon(handle)
            return SpannerError(
                f"Could not connect to Spanner at {self._endpoint}: {_describe(error)}"
            )
        if isinstance(error, api.NotFound):
            return SpannerError(
                f"Spanner could not find {self._path}; check the project, "
                f"instance and database in the connection string: {_describe(error)}"
            )
        if isinstance(error, api.GoogleAPICallError):
            return SpannerError(_describe(error))
        return error

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        sql: str,
        parameters: Optional[list] = None,
        *,
        timeout_s: float | None = None,
    ) -> pd.DataFrame:
        """Run *sql* in a read-only snapshot and return the rows as a DataFrame.

        A leading ``<database>.`` qualifier is removed first (see
        :func:`strip_database_qualifier`). Writes and DDL are rejected by the
        server, since a snapshot cannot change data.

        Parameters
        ----------
        sql:
            A GoogleSQL query.
        parameters:
            Not supported. Spanner binds parameters by name (``@id``) with a
            declared type per parameter; a positional list carries neither, and
            nothing in Auto Ontology passes parameters to a connector.
        timeout_s:
            Optional cap, sent as the gRPC deadline of the statement. Raises
            :class:`StatementTimeout` when it fires.

        Raises:
            ValueError: *parameters* was supplied.
            SpannerError: the server rejected the statement or could not be
                reached.
            StatementTimeout: the statement outlived *timeout_s*.
        """
        if parameters:
            raise ValueError(
                "Spanner binds parameters by name with declared types; positional "
                "parameters are not supported. Inline the values instead."
            )
        statement = strip_database_qualifier(sql, self._database_id)
        return self._run(self._current(), statement, timeout_s)

    def _introspect(self, sql: str) -> pd.DataFrame:
        """Run an introspection query, unaltered, and lower-case its column names."""
        frame = self._run(self._current(), sql, None)
        frame.columns = [str(column).lower() for column in frame.columns]
        return frame

    def _schema_label(self, schema: str) -> str:
        """Report the default schema ``''`` as the database id.

        Raises:
            SpannerError: a named schema is spelled like the database id, so a
                ``<database>.<name>`` reference would be ambiguous.
        """
        if schema == "":
            return self._database_id
        if schema.casefold() == self._database_id.casefold():
            raise SpannerError(
                f"Spanner schema {schema!r} has the same name as database "
                f"{self._database_id!r}. Its tables cannot be told apart from the "
                "default schema's in generated SQL, so this database cannot be "
                "ingested as is."
            )
        return schema

    def _label_schemas(self, frame: pd.DataFrame, *columns: str) -> pd.DataFrame:
        for column in columns:
            frame[column] = [self._schema_label(str(v)) for v in frame[column]]
        return frame

    # ------------------------------------------------------------------
    # Schema introspection
    # ------------------------------------------------------------------

    def get_tables(self) -> pd.DataFrame:
        frame = self._introspect(f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA NOT IN ({_SYSTEM_SCHEMA_LIST})
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        if frame.empty:
            return pd.DataFrame(columns=_TABLE_SCHEMA)
        unknown = sorted(set(frame["table_type"]) - set(_TABLE_TYPES))
        if unknown:
            raise SpannerError(
                f"Unexpected Spanner TABLE_TYPE value(s) {unknown}; expected one "
                f"of {sorted(_TABLE_TYPES)}"
            )
        frame["table_type"] = frame["table_type"].map(_TABLE_TYPES)
        return self._label_schemas(frame, "table_schema")[_TABLE_SCHEMA]

    def get_columns(self) -> pd.DataFrame:
        frame = self._introspect(f"""
            SELECT
                TABLE_SCHEMA,
                TABLE_NAME,
                COLUMN_NAME,
                SPANNER_TYPE AS DATA_TYPE,
                IS_NULLABLE = 'YES' AS IS_NULLABLE,
                ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA NOT IN ({_SYSTEM_SCHEMA_LIST})
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """)
        if frame.empty:
            return pd.DataFrame(columns=_COLUMN_SCHEMA)
        frame["is_nullable"] = frame["is_nullable"].astype(bool)
        frame["ordinal_position"] = frame["ordinal_position"].astype("int64")
        return self._label_schemas(frame, "table_schema")[_COLUMN_SCHEMA]

    def get_views(self) -> pd.DataFrame:
        frame = self._introspect(f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION
            FROM INFORMATION_SCHEMA.VIEWS
            WHERE TABLE_SCHEMA NOT IN ({_SYSTEM_SCHEMA_LIST})
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        if frame.empty:
            return pd.DataFrame(columns=_VIEW_SCHEMA)
        return self._label_schemas(frame, "table_schema")[_VIEW_SCHEMA]

    def _primary_keys(self) -> pd.DataFrame:
        """Primary-key columns with the raw schema name (``''`` for the default)."""
        frame = self._introspect(f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.INDEX_COLUMNS
            WHERE INDEX_TYPE = 'PRIMARY_KEY'
              AND TABLE_SCHEMA NOT IN ({_SYSTEM_SCHEMA_LIST})
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """)
        if frame.empty:
            return pd.DataFrame(columns=_PK_SCHEMA)
        frame["ordinal_position"] = frame["ordinal_position"].astype("int64")
        return frame[_PK_SCHEMA]

    def get_pks(self) -> pd.DataFrame:
        return self._label_schemas(self._primary_keys(), "table_schema")

    def get_fks(self) -> pd.DataFrame:
        """Declared foreign keys plus one per interleaved table.

        A declared key pairs each referencing column with the referenced
        column at its ``POSITION_IN_UNIQUE_CONSTRAINT``. When a multi-column
        key references a unique index rather than a primary key, that pairing
        cannot be trusted -- the emulator reports positions in the order of the
        foreign key's columns, not the index's -- so such a key is left out
        with a warning instead of being reported with columns crossed.
        """
        parts = [
            part
            for part in (
                self._declared_foreign_keys(),
                self._interleaved_foreign_keys(),
            )
            if not part.empty
        ]
        if not parts:
            return pd.DataFrame(columns=_FK_SCHEMA)
        frame = pd.concat(parts, ignore_index=True).drop_duplicates(ignore_index=True)
        return self._label_schemas(frame, "table_schema", "referenced_schema")[
            _FK_SCHEMA
        ]

    def _declared_foreign_keys(self) -> pd.DataFrame:
        frame = self._introspect(f"""
            SELECT
                fk.TABLE_SCHEMA,
                fk.TABLE_NAME,
                fk.COLUMN_NAME,
                ref.TABLE_SCHEMA AS REFERENCED_SCHEMA,
                ref.TABLE_NAME AS REFERENCED_TABLE,
                ref.COLUMN_NAME AS REFERENCED_COLUMN,
                rc.CONSTRAINT_SCHEMA,
                rc.CONSTRAINT_NAME,
                tc.CONSTRAINT_TYPE AS REFERENCED_CONSTRAINT_TYPE
            FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS AS rc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS fk
              ON fk.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
             AND fk.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            LEFT JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc
              ON tc.CONSTRAINT_SCHEMA = rc.UNIQUE_CONSTRAINT_SCHEMA
             AND tc.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME
            LEFT JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS ref
              ON ref.CONSTRAINT_SCHEMA = rc.UNIQUE_CONSTRAINT_SCHEMA
             AND ref.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME
             AND ref.ORDINAL_POSITION = fk.POSITION_IN_UNIQUE_CONSTRAINT
            WHERE fk.TABLE_SCHEMA NOT IN ({_SYSTEM_SCHEMA_LIST})
            ORDER BY fk.TABLE_SCHEMA, fk.TABLE_NAME, rc.CONSTRAINT_NAME,
                     fk.ORDINAL_POSITION
        """)
        if frame.empty:
            return pd.DataFrame(columns=_FK_SCHEMA)
        unresolved = frame[frame["referenced_column"].isna()]
        if not unresolved.empty:
            names = sorted(set(unresolved["constraint_name"]))
            raise SpannerError(
                f"Could not resolve the referenced columns of Spanner foreign "
                f"key(s) {names} in {self._path}"
            )
        key_columns = frame.groupby(["constraint_schema", "constraint_name"])[
            "column_name"
        ].transform("size")
        ambiguous = (frame["referenced_constraint_type"] != "PRIMARY KEY") & (
            key_columns > 1
        )
        if ambiguous.any():
            names = sorted(set(frame.loc[ambiguous, "constraint_name"]))
            logger.warning(
                "Leaving out Spanner foreign key(s) %s in %s: a multi-column key "
                "referencing a unique index rather than a primary key cannot be "
                "paired column by column reliably",
                names,
                self._path,
            )
            frame = frame.loc[~ambiguous]
        return frame[_FK_SCHEMA].reset_index(drop=True)

    def _interleaved_foreign_keys(self) -> pd.DataFrame:
        """One row per parent key column, from child key prefix to parent key.

        ``PARENT_TABLE_NAME`` is unqualified; Spanner interleaves a table only
        in a parent of the same schema.
        """
        parents = self._introspect(f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, PARENT_TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE PARENT_TABLE_NAME IS NOT NULL
              AND TABLE_SCHEMA NOT IN ({_SYSTEM_SCHEMA_LIST})
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        if parents.empty:
            return pd.DataFrame(columns=_FK_SCHEMA)
        keys = self._primary_keys()
        by_table: dict[tuple[str, str], list[str]] = {
            (str(schema), str(table)): list(group["column_name"])
            for (schema, table), group in keys.groupby(
                ["table_schema", "table_name"], sort=False
            )
        }
        rows: list[dict[str, str]] = []
        for schema, child, parent in parents.itertuples(index=False):
            child_key = by_table.get((schema, child), [])
            parent_key = by_table.get((schema, parent))
            if not parent_key or len(child_key) < len(parent_key):
                raise SpannerError(
                    f"Interleaved table {schema + '.' if schema else ''}{child} in "
                    f"{self._path}: parent {parent!r} key {parent_key} does not "
                    f"prefix the child key {child_key}"
                )
            for child_column, parent_column in zip(child_key, parent_key):
                rows.append(
                    {
                        "table_schema": schema,
                        "table_name": child,
                        "column_name": child_column,
                        "referenced_schema": schema,
                        "referenced_table": parent,
                        "referenced_column": parent_column,
                    }
                )
        return pd.DataFrame(rows, columns=_FK_SCHEMA)

    def get_queries(self, hours: int = 24) -> pd.DataFrame:
        """Query history is not read, so this is always empty.

        Spanner keeps only aggregated statistics (``SPANNER_SYS.QUERY_STATS_*``,
        normalised text in fixed windows), not a per-execution history.
        """
        return pd.DataFrame(columns=["end_time", "query_text"])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> None:
        """Verify the endpoint, the credentials, the database and its dialect."""
        self.execute("SELECT 1")

    def close(self) -> None:
        with self._lock:
            handle = self._handle
        if handle is not None:
            self._abandon(handle)
