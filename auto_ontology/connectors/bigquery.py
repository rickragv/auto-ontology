# SPDX-License-Identifier: Apache-2.0

"""Google BigQuery connector implementing Auto Ontology's SQLDatabase ABC.

BigQuery is reached with the official ``google-cloud-bigquery`` client. Query
results come back through the REST API (``jobs.query`` and
``jobs.getQueryResults``) and are turned into a DataFrame here, so neither the
BigQuery Storage API, ``pyarrow`` nor ``db-dtypes`` is needed.

Connection string
-----------------
::

    bigquery://<project>
    bigquery://<project>/<dataset>
    bigquery://<project>/<dataset>?location=EU&maximum_bytes_billed=1000000000
    bigquery://<project>/<dataset>?emulator_host=<host>:<port>

* ``<project>`` runs the query jobs (and is billed for them) and holds the data.
* ``<dataset>`` restricts ingestion to one dataset and is the default dataset of
  every query, so a bare ``Table`` resolves. Without it every dataset in the
  project is ingested (hidden datasets, whose ids start with ``_``, excepted).
* ``location`` is the job location (``US``, ``EU``, ``us-central1``, ...).
  Unset, BigQuery infers it from the tables a query reads.
* ``maximum_bytes_billed`` is set on every query job. A query that would bill
  more fails without being charged. Unset, the project's own default applies.

The URL carries no secret. Without ``emulator_host`` the client authenticates
with Application Default Credentials (``gcloud auth application-default
login``, ``GOOGLE_APPLICATION_CREDENTIALS``, or the metadata server). That is
the primary path.

With ``emulator_host`` the client talks plain HTTP to that emulator with
anonymous credentials (``client_options={"api_endpoint": ...}``), so the URL
alone decides where the connection goes. The library also reads the
process-wide ``BIGQUERY_EMULATOR_HOST`` and uses it for every client that is
not given an endpoint, which would silently send a production URL to an
emulator. So the connector refuses that variable unless the URL names the same
emulator.

Naming
------
The database is the project and the schema is the dataset, so a table is
``<project>.<dataset>.<table>`` -- BigQuery's own three-part name, which the
text-to-SQL prompts produce unchanged. BigQuery accepts a hyphenated project
unquoted at the start of a table path (``my-project.ds.t``) as well as every
backticked spelling, and sqlglot's ``bigquery`` dialect parses all of them, so
statements are sent exactly as written. :meth:`BigQueryDatabase.qualify` emits
```project`.`dataset`.`table```.

Read-only
---------
BigQuery has no read-only session, so :meth:`BigQueryDatabase.execute` refuses
anything that is not a single ``SELECT`` or ``WITH`` query before it is sent.

Metadata
--------
Everything comes from the tables API (``datasets.list``, ``tables.list``,
``tables.get``), which bills no query bytes and is unaffected by
``maximum_bytes_billed``; ``INFORMATION_SCHEMA`` is never queried. Columns are
the top-level fields; a ``STRUCT`` column is one column whose type spells out
its fields (``STRUCT<city STRING, zip STRING>``), and a repeated field is
reported as ``ARRAY<...>``. Primary and foreign keys are the declared
(unenforced) constraints in each table's ``tableConstraints``.

Example
-------
::

    CONNECTION_STRINGS=bigquery://my-project-123/sales
"""

from __future__ import annotations

import concurrent.futures
import logging
import math
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Iterable, Optional
from urllib.parse import parse_qs, urlparse

import pandas as pd

from auto_ontology.catalog.constants import TableTypes
from auto_ontology.connectors.base import SQLDatabase, StatementTimeout
from auto_ontology.connectors.spanner import _backticked, _tokenize

if TYPE_CHECKING:  # pragma: no cover - typing only
    from google.api_core.retry import Retry
    from google.cloud.bigquery import Client, QueryJobConfig, SchemaField, Table

logger = logging.getLogger(__name__)

#: The variable the client library reads to redirect clients to an emulator.
EMULATOR_ENV_VAR = "BIGQUERY_EMULATOR_HOST"

#: Seconds each HTTP request may take. For ``jobs.query`` it is also how long the
#: server holds the request open waiting for the query to finish; a longer
#: query is then polled, so this bounds a request, not a query.
REQUEST_TIMEOUT_S = 60.0

#: Seconds the client keeps retrying a failed request (connection refused,
#: 5xx, rate limits) before giving up. The library's own default is 10 minutes.
RETRY_BUDGET_S = 30.0

#: Seconds the client keeps re-running a query job that failed for a reason
#: BigQuery calls transient (``jobRateLimitExceeded``, ``jobBackendError``).
#: The library's own default is 40 minutes.
JOB_RETRY_BUDGET_S = 60.0

#: Tables described at once during introspection (one ``tables.get`` each).
DESCRIBE_WORKERS = 8

_ALLOWED_QUERY_PARAMETERS = ("emulator_host", "location", "maximum_bytes_billed")

# Permissive on purpose: these catch a malformed URL without second-guessing
# Google's id rules, which the server enforces anyway. ``.`` and ``:`` are
# allowed in a project for domain-scoped projects (``example.com:my-project``).
_PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:_-]*")
_DATASET_RE = re.compile(r"[A-Za-z0-9_]+")
_LOCATION_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_BYTES_RE = re.compile(r"[0-9]+")
_EMULATOR_HOST_RE = re.compile(
    r"[A-Za-z0-9.\-]+:[0-9]{1,5}|\[[0-9A-Fa-f:.]+\]:[0-9]{1,5}"
)
_MAX_INT64 = 2**63 - 1

_FORM = (
    "bigquery://<project>[/<dataset>][?emulator_host=<host>:<port>]"
    "[&location=<region>][&maximum_bytes_billed=<bytes>]"
)

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

# tables.list ``type`` -> catalog table type. External tables and snapshots are
# queryable relations with a schema of their own, so they are base tables here.
_TABLE_TYPES = {
    "TABLE": TableTypes.BASE_TABLE,
    "EXTERNAL": TableTypes.BASE_TABLE,
    "SNAPSHOT": TableTypes.BASE_TABLE,
    "VIEW": TableTypes.VIEW,
    "MATERIALIZED_VIEW": TableTypes.MATERIALIZED_VIEW,
}
# Only tables carry key constraints.
_CONSTRAINED_TYPES = frozenset({"TABLE"})

# The tables API reports legacy type names; prompts and generated SQL use
# GoogleSQL's, which is also what INFORMATION_SCHEMA.COLUMNS reports.
_STANDARD_TYPE_NAMES = {
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "BOOLEAN": "BOOL",
    "RECORD": "STRUCT",
}

_READ_KEYWORDS = frozenset({"SELECT", "WITH"})

# What ``GoogleAPICallError.message`` starts with when the client built it from
# an HTTP response: the method and full URL, which say nothing to a reader.
_HTTP_PREFIX = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE) \S+: ")


class BigQueryError(RuntimeError):
    """An error reported by BigQuery, or reaching it.

    Statement errors read ``<BigQuery reason>: <the server's message>`` (for
    example ``invalidQuery: Unrecognized name: foo at [1:8]``), and transport
    and credential failures start with "Could not connect to BigQuery", so
    :mod:`auto_ontology.connectors.db_errors` tells "the SQL was wrong" apart
    from "the database is unreachable" the same way it does for every engine.
    """


# ----------------------------------------------------------------------
# Connection string
# ----------------------------------------------------------------------


def _single(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1:
        raise ValueError(f"BigQuery URL parameter {name} may be given only once")
    return values[0]


def _check_emulator_host(value: str) -> str:
    if not _EMULATOR_HOST_RE.fullmatch(value):
        raise ValueError(
            f"Invalid BigQuery emulator_host {value!r}: expected host:port with "
            "no scheme, e.g. localhost:9050"
        )
    return value


def _check_location(value: str) -> str:
    if not _LOCATION_RE.fullmatch(value):
        raise ValueError(
            f"Invalid BigQuery location {value!r}: expected a location such as "
            "US, EU or us-central1"
        )
    return value


def _check_bytes(value: str) -> int:
    if not _BYTES_RE.fullmatch(value) or not 1 <= int(value) <= _MAX_INT64:
        raise ValueError(
            f"Invalid BigQuery maximum_bytes_billed {value!r}: expected a whole "
            "number of bytes, at least 1, e.g. 1000000000 for 1 GB"
        )
    return int(value)


def _parse_connection_string(connection_string: str) -> dict[str, Any]:
    """Parse ``bigquery://project[/dataset][?...]`` into its parts.

    Raises:
        ValueError: anything else -- naming the part that is wrong and the
            form that is accepted.
    """
    parsed = urlparse(connection_string)
    if parsed.scheme.lower() != "bigquery":
        raise ValueError(f"Not a BigQuery URL (expected {_FORM}): {connection_string}")
    if "@" in parsed.netloc:
        raise ValueError(
            "BigQuery connection strings carry no user or password; credentials "
            f"come from Application Default Credentials. Expected {_FORM}"
        )
    if parsed.fragment or parsed.params:
        raise ValueError(f"Unexpected '#' or ';' in BigQuery URL; expected {_FORM}")

    project = parsed.netloc
    if not project:
        raise ValueError(f"BigQuery URL has an empty project; expected {_FORM}")
    if not _PROJECT_RE.fullmatch(project):
        raise ValueError(
            f"Invalid BigQuery project id {project!r}: use letters, digits, '-', "
            f"'_', '.' and ':' (expected {_FORM})"
        )

    dataset: str | None = None
    if parsed.path not in ("", "/"):
        segments = parsed.path.split("/")[1:]
        if len(segments) != 1 or not segments[0]:
            raise ValueError(
                f"BigQuery URL may name at most one dataset after the project; "
                f"got path {parsed.path!r}. Expected {_FORM}"
            )
        dataset = segments[0]
        if not _DATASET_RE.fullmatch(dataset):
            raise ValueError(
                f"Invalid BigQuery dataset id {dataset!r}: use letters, digits "
                f"and '_' (expected {_FORM})"
            )

    query = parse_qs(parsed.query, keep_blank_values=True)
    unknown = sorted(set(query) - set(_ALLOWED_QUERY_PARAMETERS))
    if unknown:
        raise ValueError(
            f"Unknown BigQuery URL parameter(s) {unknown}; accepted: "
            f"{', '.join(_ALLOWED_QUERY_PARAMETERS)}"
        )
    emulator_host = _single(query, "emulator_host")
    location = _single(query, "location")
    maximum_bytes_billed = _single(query, "maximum_bytes_billed")

    return {
        "project": project,
        "dataset": dataset,
        "emulator_host": (
            None if emulator_host is None else _check_emulator_host(emulator_host)
        ),
        "location": None if location is None else _check_location(location),
        "maximum_bytes_billed": (
            None if maximum_bytes_billed is None else _check_bytes(maximum_bytes_billed)
        ),
    }


def format_connection_string(
    *,
    project: str,
    dataset: str | None = None,
    emulator_host: str | None = None,
    location: str | None = None,
    maximum_bytes_billed: int | None = None,
) -> str:
    """Build the URL :func:`_parse_connection_string` reads back into these parts.

    Used for connections added in the UI. Every character an accepted value may
    contain is URL-safe, so nothing is percent-encoded: a value that would need
    encoding is not valid and is refused here, naming the field, rather than
    turned into a URL that parses differently.

    Raises:
        ValueError: the project is empty, or a part is not valid.
    """
    if not project:
        raise ValueError("BigQuery project id is required")
    if not _PROJECT_RE.fullmatch(project):
        raise ValueError(
            f"Invalid BigQuery project id {project!r}: use letters, digits, '-', "
            "'_', '.' and ':'"
        )
    url = f"bigquery://{project}"
    if dataset is not None:
        if not _DATASET_RE.fullmatch(dataset):
            raise ValueError(
                f"Invalid BigQuery dataset id {dataset!r}: use letters, digits and '_'"
            )
        url += f"/{dataset}"
    parameters: list[str] = []
    if emulator_host is not None:
        parameters.append(f"emulator_host={_check_emulator_host(emulator_host)}")
    if location is not None:
        parameters.append(f"location={_check_location(location)}")
    if maximum_bytes_billed is not None:
        if isinstance(maximum_bytes_billed, bool) or not isinstance(
            maximum_bytes_billed, int
        ):
            raise ValueError(
                f"Invalid BigQuery maximum_bytes_billed {maximum_bytes_billed!r}: "
                "expected a whole number of bytes"
            )
        parameters.append(
            f"maximum_bytes_billed={_check_bytes(str(maximum_bytes_billed))}"
        )
    if parameters:
        url += "?" + "&".join(parameters)
    _parse_connection_string(url)
    return url


def _normalised_endpoint(value: str) -> str:
    """``http://host:port/`` and ``host:port`` name the same emulator."""
    value = value.strip().rstrip("/")
    for scheme in ("http://", "https://"):
        if value.lower().startswith(scheme):
            return value[len(scheme) :]
    return value


def _check_emulator_environment(emulator_host: str | None) -> None:
    """Refuse a ``BIGQUERY_EMULATOR_HOST`` the URL did not ask for.

    The library reads the variable in every ``Client()`` and uses it as the API
    base URL whenever the client is given no endpoint of its own, so with no
    ``emulator_host`` in the URL it would redirect a production connection. An
    endpoint given to the client wins over the variable, but a variable naming
    a different emulator still means two settings disagree, so that is refused
    too. An empty value counts as set.
    """
    env_host = os.environ.get(EMULATOR_ENV_VAR)
    if env_host is None:
        return
    if emulator_host is None:
        raise ValueError(
            f"{EMULATOR_ENV_VAR} is set to {env_host!r}, which would silently "
            "redirect this BigQuery connection to an emulator, but the URL has "
            f"no emulator_host. Unset {EMULATOR_ENV_VAR}, or add "
            f"?emulator_host={_normalised_endpoint(env_host)} to the URL to use "
            "that emulator."
        )
    if _normalised_endpoint(env_host) != emulator_host:
        raise ValueError(
            f"{EMULATOR_ENV_VAR} is {env_host!r} but the URL asks for "
            f"emulator_host={emulator_host!r}. Make them name the same emulator "
            "or unset the variable."
        )


# ----------------------------------------------------------------------
# Statements
# ----------------------------------------------------------------------


def check_read_only(sql: str) -> None:
    """Refuse *sql* unless it is one ``SELECT`` or ``WITH`` query.

    Leading comments and opening parentheses are allowed, and so are trailing
    semicolons. A ``;`` followed by anything else is a multi-statement script,
    which BigQuery would run statement by statement, so it is refused even when
    every statement is a query.

    Raises:
        BigQueryError: the statement is empty, is not a query, or is a script.
    """
    tokens = _tokenize(sql, None)
    position = 0
    while (
        position < len(tokens)
        and tokens[position].kind == "other"
        and tokens[position].text == "("
    ):
        position += 1
    if position == len(tokens):
        raise BigQueryError("Refusing to run an empty statement on BigQuery")
    first = tokens[position]
    if first.kind != "ident" or first.text.upper() not in _READ_KEYWORDS:
        word = first.text if first.kind == "ident" else sql[first.start : first.end]
        raise BigQueryError(
            "BigQuery connections are read-only: only a single SELECT or WITH "
            f"query is run, and this statement starts with {word!r}"
        )
    for index, token in enumerate(tokens):
        if token.kind == "other" and token.text == ";":
            rest = tokens[index + 1 :]
            if any(not (t.kind == "other" and t.text == ";") for t in rest):
                raise BigQueryError(
                    "BigQuery connections run a single query; this text holds "
                    "more than one statement. Send one SELECT or WITH query "
                    "without ';' between statements."
                )


def _type_name(field: "SchemaField") -> str:
    """Render a field's type as GoogleSQL writes it, element type included."""
    kind = str(field.field_type).upper()
    kind = _STANDARD_TYPE_NAMES.get(kind, kind)
    if kind == "STRUCT":
        inner = ", ".join(f"{sub.name} {_type_name(sub)}" for sub in field.fields)
        rendered = f"STRUCT<{inner}>"
    elif kind in ("NUMERIC", "BIGNUMERIC") and field.precision is not None:
        rendered = (
            f"{kind}({field.precision}, {field.scale})"
            if field.scale is not None
            else f"{kind}({field.precision})"
        )
    elif kind in ("STRING", "BYTES") and field.max_length is not None:
        rendered = f"{kind}({field.max_length})"
    elif kind == "RANGE" and field.range_element_type is not None:
        rendered = f"RANGE<{field.range_element_type.element_type}>"
    else:
        rendered = kind
    if str(field.mode).upper() == "REPEATED":
        return f"ARRAY<{rendered}>"
    return rendered


def _describe(error: BaseException) -> str:
    """Return ``reason: message`` for a Google API error, ``str(error)`` otherwise.

    ``str()`` of a ``GoogleAPICallError`` starts with the HTTP status, method
    and full request URL, and a failed query job's message gains a footer with
    the location and job id. Neither helps a reader or the SQL-repair prompt,
    which both receive this text. BigQuery's structured error (its ``reason``,
    such as ``invalidQuery`` or ``accessDenied``, and its message) says what
    happened; the original exception stays chained as ``__cause__``.
    """
    from google.api_core import exceptions as api

    if not isinstance(error, api.GoogleAPICallError):
        return str(error)
    errors = error.errors
    if errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
        message = errors[0].get("message")
        if reason and message:
            return f"{reason}: {message}"
    message = _HTTP_PREFIX.sub("", str(error.message))
    if error.code is not None:
        return f"HTTP {error.code}: {message}"
    return f"{type(error).__name__}: {message}"


def _reason(error: BaseException) -> str | None:
    errors = getattr(error, "errors", None)
    if errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
        return str(reason) if reason else None
    return None


def _rows_to_frame(
    rows: Iterable[Any], schema: Iterable["SchemaField"]
) -> pd.DataFrame:
    """Collect REST result rows into a DataFrame with the result's column names."""
    columns = [field.name for field in schema]
    return pd.DataFrame([list(row.values()) for row in rows], columns=columns)


# ----------------------------------------------------------------------
# Connector
# ----------------------------------------------------------------------


class BigQueryDatabase(SQLDatabase):
    """Concrete :class:`SQLDatabase` backed by Google BigQuery.

    Parameters
    ----------
    connection_string:
        ``bigquery://<project>[/<dataset>][?emulator_host=..&location=..&maximum_bytes_billed=..]``

    Construction only parses the URL; the client is created on the first
    request, so loading the registry never touches the network or the
    credentials.
    """

    supports_statement_timeout = True

    def __init__(self, connection_string: str) -> None:
        self._settings = _parse_connection_string(connection_string)
        _check_emulator_environment(self._settings["emulator_host"])
        self._project: str = self._settings["project"]
        self._dataset: str | None = self._settings["dataset"]
        emulator_host = self._settings["emulator_host"]
        self._endpoint = (
            f"http://{emulator_host}"
            if emulator_host is not None
            else "https://bigquery.googleapis.com"
        )
        self._scope = (
            f"{self._project}.{self._dataset}" if self._dataset else self._project
        )
        self._lock = threading.Lock()
        self._client: "Client | None" = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def dialect(self) -> str:
        """Return this engine's sqlglot dialect name (``bigquery``)."""
        return "bigquery"

    @property
    def database_name(self) -> str:
        """The project id: the first part of ``project.dataset.table``."""
        return self._project

    def qualify(self, schema: Optional[str], table: str) -> str:
        """Return ```project`.`dataset`.`table```.

        With no *schema* the URL's dataset is used; with neither, the bare
        ```table``` is returned, which resolves only against a default dataset.
        """
        dataset = schema or self._dataset
        if not dataset:
            return _backticked(table)
        return ".".join(_backticked(part) for part in (self._project, dataset, table))

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------

    def _open(self) -> "Client":
        """Return the client, building it on first use. No request is made here.

        The library is imported here, not at module import: the registry
        imports every connector when the backend starts, and a deployment with
        no BigQuery connection should not load it.
        """
        client = self._client
        if client is not None:
            return client
        with self._lock:
            if self._client is not None:
                return self._client
            from google.auth.credentials import AnonymousCredentials
            from google.auth.exceptions import DefaultCredentialsError
            from google.cloud import bigquery

            emulator_host = self._settings["emulator_host"]
            _check_emulator_environment(emulator_host)
            kwargs: dict[str, Any] = {"project": self._project}
            if self._settings["location"] is not None:
                kwargs["location"] = self._settings["location"]
            if emulator_host is not None:
                kwargs["credentials"] = AnonymousCredentials()
                kwargs["client_options"] = {"api_endpoint": self._endpoint}
            try:
                self._client = bigquery.Client(**kwargs)
            except DefaultCredentialsError as error:
                raise BigQueryError(
                    f"Could not connect to BigQuery project {self._project!r}: no "
                    "Google Cloud credentials were found. Run `gcloud auth "
                    "application-default login` or set "
                    f"GOOGLE_APPLICATION_CREDENTIALS ({error})"
                ) from error
            logger.info(
                "Opened BigQuery client for %s at %s", self._scope, self._endpoint
            )
            return self._client

    def _retry(self, budget_s: float) -> "Retry":
        from google.cloud.bigquery.retry import DEFAULT_RETRY

        return DEFAULT_RETRY.with_timeout(budget_s)

    def _job_retry(self, budget_s: float) -> "Retry | None":
        """Bounded re-runs of a transiently failed job; none on an emulator.

        The emulator reports every query error, a syntax error included, with
        the reason ``jobInternalError``, which the library treats as transient,
        so re-running there only delays the real error by the whole budget.
        """
        if self._settings["emulator_host"] is not None:
            return None
        from google.cloud.bigquery.retry import DEFAULT_JOB_RETRY

        return DEFAULT_JOB_RETRY.with_timeout(budget_s)

    def _job_config(self, timeout_s: float | None) -> "QueryJobConfig":
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(use_legacy_sql=False)
        if self._dataset is not None:
            config.default_dataset = f"{self._project}.{self._dataset}"
        if self._settings["maximum_bytes_billed"] is not None:
            config.maximum_bytes_billed = self._settings["maximum_bytes_billed"]
        if timeout_s is not None:
            # Server side: BigQuery cancels the job when this runs out.
            config.job_timeout_ms = max(1, math.ceil(timeout_s * 1000))
        return config

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def _translate(
        self,
        error: BaseException,
        timeout_s: float | None,
        started: float,
        *,
        subject: str | None = None,
    ) -> BaseException:
        """Map a client error onto the connector's error types.

        *subject* names what a metadata call was looking for, so a ``notFound``
        there says which part of the connection string is wrong; for a query
        it is ``None`` and a ``notFound`` is the statement's own error.
        """
        import requests
        from google.api_core import exceptions as api
        from google.auth import exceptions as auth

        if isinstance(error, BigQueryError):
            return error
        if timeout_s is not None and (
            isinstance(error, concurrent.futures.TimeoutError)
            or _reason(error) in ("timeout", "stopped")
            or (
                isinstance(error, api.RetryError)
                and isinstance(error.cause, requests.exceptions.Timeout)
            )
        ):
            return StatementTimeout.since(timeout_s, started)
        if isinstance(error, auth.RefreshError):
            return BigQueryError(
                f"Could not connect to BigQuery project {self._project!r}: the "
                "Google Cloud credentials could not be refreshed. Run `gcloud auth "
                "application-default login` again or check "
                f"GOOGLE_APPLICATION_CREDENTIALS ({error})"
            )
        if isinstance(error, api.RetryError):
            cause = error.cause
            if cause is None or not isinstance(cause, api.GoogleAPICallError):
                return BigQueryError(
                    f"Could not connect to BigQuery at {self._endpoint}: "
                    f"{error.message} ({cause})"
                )
            return BigQueryError(
                f"BigQuery kept failing after retrying ({error.message}): "
                f"{_describe(cause)}"
            )
        if isinstance(
            error, (requests.exceptions.RequestException, auth.TransportError)
        ):
            return BigQueryError(
                f"Could not connect to BigQuery at {self._endpoint}: {error}"
            )
        if not isinstance(error, api.GoogleAPICallError):
            return error
        reason = _reason(error)
        described = _describe(error)
        if reason == "bytesBilledLimitExceeded":
            return BigQueryError(
                f"{described} (the connection string sets maximum_bytes_billed="
                f"{self._settings['maximum_bytes_billed']}; raise or remove it to "
                "run this query)"
            )
        if reason == "accessDenied" or (
            reason is None and isinstance(error, api.Forbidden)
        ):
            return BigQueryError(
                f"BigQuery denied access in project {self._project!r}: {described}. "
                "The credentials need BigQuery Job User on the project and "
                "BigQuery Data Viewer on the datasets."
            )
        if subject is not None and isinstance(error, api.NotFound):
            return BigQueryError(
                f"BigQuery could not find {subject}; check the project and "
                f"dataset in the connection string: {described}"
            )
        return BigQueryError(described)

    def _call(self, subject: str, call: Any, *args: Any, **kwargs: Any) -> Any:
        """Run one metadata request with the bounded timeout and retry."""
        started = time.monotonic()
        try:
            return call(
                *args,
                retry=self._retry(RETRY_BUDGET_S),
                timeout=REQUEST_TIMEOUT_S,
                **kwargs,
            )
        except Exception as error:
            translated = self._translate(error, None, started, subject=subject)
            if translated is error:
                raise
            raise translated from error

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
        """Run one read-only query and return the rows as a DataFrame.

        Parameters
        ----------
        sql:
            A GoogleSQL ``SELECT`` or ``WITH`` query. Anything else is refused
            before it is sent (see :func:`check_read_only`).
        parameters:
            Not supported. BigQuery binds parameters by name or position with a
            declared type each; nothing in Auto Ontology passes parameters to a
            connector.
        timeout_s:
            Optional cap. Sent as the job's ``jobTimeoutMs`` (BigQuery cancels
            the job) and used as the client's wait for the result (the client
            cancels the job). Raises :class:`StatementTimeout` when it fires.

        Raises:
            ValueError: *parameters* was supplied.
            BigQueryError: the statement was refused, BigQuery rejected it, or
                BigQuery could not be reached.
            StatementTimeout: the statement outlived *timeout_s*.
        """
        if parameters:
            raise ValueError(
                "BigQuery binds parameters with declared types; positional "
                "parameters are not supported. Inline the values instead."
            )
        check_read_only(sql)
        client = self._open()
        request_timeout = REQUEST_TIMEOUT_S if timeout_s is None else timeout_s
        retry_budget = (
            RETRY_BUDGET_S if timeout_s is None else min(RETRY_BUDGET_S, timeout_s)
        )
        job_budget = (
            JOB_RETRY_BUDGET_S
            if timeout_s is None
            else min(JOB_RETRY_BUDGET_S, timeout_s)
        )
        started = time.monotonic()
        try:
            rows = client.query_and_wait(
                sql,
                job_config=self._job_config(timeout_s),
                api_timeout=request_timeout,
                wait_timeout=timeout_s,
                retry=self._retry(retry_budget),
                job_retry=self._job_retry(job_budget),
            )
            return _rows_to_frame(rows, rows.schema or [])
        except Exception as error:
            translated = self._translate(error, timeout_s, started)
            if translated is error:
                raise
            raise translated from error

    # ------------------------------------------------------------------
    # Schema introspection
    # ------------------------------------------------------------------

    def _datasets(self) -> list[str]:
        if self._dataset is not None:
            return [self._dataset]
        client = self._open()
        items = self._call(
            f"project {self._project!r}",
            lambda **kw: list(client.list_datasets(project=self._project, **kw)),
        )
        return sorted(item.dataset_id for item in items)

    def _listed(self) -> list[tuple[str, str, str]]:
        """``(dataset, table, type)`` for every relation in scope, sorted."""
        client = self._open()
        listed: list[tuple[str, str, str]] = []
        for dataset in self._datasets():
            reference = f"{self._project}.{dataset}"
            items = self._call(
                f"dataset {reference!r}",
                lambda ref=reference, **kw: list(client.list_tables(ref, **kw)),
            )
            for item in items:
                kind = str(item.table_type)
                if kind not in _TABLE_TYPES:
                    raise BigQueryError(
                        f"Unexpected BigQuery table type {kind!r} for "
                        f"{reference}.{item.table_id}; expected one of "
                        f"{sorted(_TABLE_TYPES)}"
                    )
                listed.append((dataset, item.table_id, kind))
        return sorted(listed)

    def _describe_tables(self, kinds: Iterable[str] | None = None) -> list["Table"]:
        """Full table resources (``tables.get``) for the relations in scope."""
        client = self._open()
        wanted = None if kinds is None else frozenset(kinds)
        references = [
            f"{self._project}.{dataset}.{table}"
            for dataset, table, kind in self._listed()
            if wanted is None or kind in wanted
        ]
        if not references:
            return []

        def fetch(reference: str) -> "Table":
            return self._call(f"table {reference!r}", client.get_table, reference)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(DESCRIBE_WORKERS, len(references)),
            thread_name_prefix="bigquery-describe",
        ) as pool:
            return list(pool.map(fetch, references))

    def get_tables(self) -> pd.DataFrame:
        rows = [
            [dataset, table, _TABLE_TYPES[kind]]
            for dataset, table, kind in self._listed()
        ]
        return pd.DataFrame(rows, columns=_TABLE_SCHEMA)

    def get_columns(self) -> pd.DataFrame:
        """One row per top-level column; ``STRUCT`` fields are in its type.

        A ``REQUIRED`` column is not nullable, and neither is a ``REPEATED``
        one: BigQuery stores a NULL array as an empty array.
        """
        rows = [
            [
                table.dataset_id,
                table.table_id,
                field.name,
                _type_name(field),
                str(field.mode).upper() == "NULLABLE",
                position,
            ]
            for table in self._describe_tables()
            for position, field in enumerate(table.schema, start=1)
        ]
        frame = pd.DataFrame(rows, columns=_COLUMN_SCHEMA)
        frame["is_nullable"] = frame["is_nullable"].astype(bool)
        frame["ordinal_position"] = frame["ordinal_position"].astype("int64")
        return frame

    def get_views(self) -> pd.DataFrame:
        rows = [
            [table.dataset_id, table.table_id, table.view_query]
            for table in self._describe_tables(["VIEW"])
        ]
        return pd.DataFrame(rows, columns=_VIEW_SCHEMA)

    def get_pks(self) -> pd.DataFrame:
        rows: list[list[Any]] = []
        for table in self._describe_tables(_CONSTRAINED_TYPES):
            constraints = table.table_constraints
            if constraints is None or constraints.primary_key is None:
                continue
            for position, column in enumerate(constraints.primary_key.columns, 1):
                rows.append([table.dataset_id, table.table_id, column, position])
        frame = pd.DataFrame(rows, columns=_PK_SCHEMA)
        frame["ordinal_position"] = frame["ordinal_position"].astype("int64")
        return frame

    def get_fks(self) -> pd.DataFrame:
        """Declared foreign keys, one row per column pair.

        A key whose referenced table is outside what this connection ingests
        (another project, or another dataset when the URL names one) cannot be
        resolved in the catalog, so it is left out with a warning.
        """
        datasets = set(self._datasets())
        rows: list[list[str]] = []
        outside: list[str] = []
        for table in self._describe_tables(_CONSTRAINED_TYPES):
            constraints = table.table_constraints
            if constraints is None or not constraints.foreign_keys:
                continue
            for key in constraints.foreign_keys:
                target = key.referenced_table
                if target.project != self._project or target.dataset_id not in datasets:
                    outside.append(
                        f"{table.dataset_id}.{table.table_id}.{key.name} -> "
                        f"{target.project}.{target.dataset_id}.{target.table_id}"
                    )
                    continue
                for pair in key.column_references:
                    rows.append(
                        [
                            table.dataset_id,
                            table.table_id,
                            pair.referencing_column,
                            target.dataset_id,
                            target.table_id,
                            pair.referenced_column,
                        ]
                    )
        if outside:
            logger.warning(
                "Leaving out BigQuery foreign key(s) %s in %s: the referenced "
                "table is outside what this connection ingests",
                outside,
                self._scope,
            )
        return pd.DataFrame(rows, columns=_FK_SCHEMA)

    def get_queries(self, hours: int = 24) -> pd.DataFrame:
        """Query history is not read, so this is always empty.

        ``INFORMATION_SCHEMA.JOBS`` has it, but reading it bills query bytes and
        needs project-level job-listing permission.
        """
        return pd.DataFrame(columns=["end_time", "query_text"])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> None:
        """Verify the endpoint, the credentials, the dataset and job permission."""
        client = self._open()
        if self._dataset is not None:
            self._call(f"dataset {self._scope!r}", client.get_dataset, self._scope)
        self.execute("SELECT 1")

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()
