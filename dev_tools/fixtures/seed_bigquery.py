# SPDX-License-Identifier: Apache-2.0

"""Create (or delete) a small music-and-concerts dataset in BigQuery.

Used by the opt-in live tests in
``auto_ontology/connectors/tests/test_bigquery_live.py`` and for trying the
BigQuery connector by hand. It is written for a real Google Cloud project,
authenticated with Application Default Credentials; with ``--emulator-host`` it
talks to a BigQuery emulator instead, with anonymous credentials.

Cost: the dataset holds a few kilobytes. Seeding runs about a dozen DDL and DML
jobs (DDL is free; each ``INSERT`` bills BigQuery's 10 MB minimum), well within
the free tier.

Safety: the dataset is created with the label ``created_by=auto_ontology_seed``.
The script replaces or deletes a dataset only when it carries that label, so it
refuses to touch any dataset it did not create.

What the dataset covers (GoogleSQL):

* ``Singers`` -- ``DATE``, ``BOOL``, ``ARRAY<STRING>``, ``JSON`` and
  ``STRUCT`` columns, ``NOT NULL`` columns, primary key.
* ``Albums`` -- two-column primary key, ``NUMERIC``; foreign key to ``Singers``.
* ``Venues`` and ``Concerts`` -- ``Concerts`` has a three-column primary key,
  a ``TIMESTAMP`` and foreign keys to ``Venues`` and ``Singers``.
* ``TicketSales`` -- a three-column foreign key to ``Concerts``.
* ``SingerRevenue`` -- a view joining ``Singers`` and ``Concerts``.

Keys are declared ``NOT ENFORCED``, the only kind BigQuery has. The emulator
does not support ``FOREIGN KEY`` at all, so with ``--emulator-host`` the
foreign keys are not declared, and the script says so.

Usage::

    gcloud auth application-default login
    uv run python -m dev_tools.fixtures.seed_bigquery --project my-project
    uv run python -m dev_tools.fixtures.seed_bigquery --project my-project \\
        --dataset auto_ontology_music --location EU
    uv run python -m dev_tools.fixtures.seed_bigquery --project my-project --delete

    # Emulator
    uv run python -m dev_tools.fixtures.seed_bigquery --project auto-ontology-test \\
        --emulator-host localhost:9050

The matching connection string is printed at the end, e.g.
``bigquery://my-project/auto_ontology_music``.
"""

from __future__ import annotations

import argparse
import logging
import re
from typing import TYPE_CHECKING, Any

from auto_ontology.connectors.bigquery import (
    _check_emulator_environment,
    format_connection_string,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from google.cloud.bigquery import Client

logger = logging.getLogger("dev_tools.fixtures.seed_bigquery")

DEFAULT_DATASET = "auto_ontology_music"

#: The label that marks a dataset as this script's to replace or delete.
LABEL_KEY = "created_by"
LABEL_VALUE = "auto_ontology_seed"

_DATASET_RE = re.compile(r"[A-Za-z0-9_]+")
_RETRY_BUDGET_S = 60.0
_REQUEST_TIMEOUT_S = 60.0


def _tables(dataset: str) -> list[str]:
    """``CREATE TABLE`` and ``CREATE VIEW`` statements, in dependency order."""
    d = dataset
    return [
        f"""CREATE TABLE {d}.Singers (
            SingerId INT64 NOT NULL,
            FirstName STRING,
            LastName STRING NOT NULL,
            BirthDate DATE,
            IsActive BOOL NOT NULL,
            Genres ARRAY<STRING>,
            Profile JSON,
            Address STRUCT<city STRING, country STRING>,
            PRIMARY KEY (SingerId) NOT ENFORCED
        )""",
        f"""CREATE TABLE {d}.Albums (
            SingerId INT64 NOT NULL,
            AlbumId INT64 NOT NULL,
            AlbumTitle STRING NOT NULL,
            ReleaseDate DATE,
            Price NUMERIC,
            PRIMARY KEY (SingerId, AlbumId) NOT ENFORCED
        )""",
        f"""CREATE TABLE {d}.Venues (
            VenueId INT64 NOT NULL,
            VenueName STRING NOT NULL,
            City STRING NOT NULL,
            Capacity INT64,
            PRIMARY KEY (VenueId) NOT ENFORCED
        )""",
        f"""CREATE TABLE {d}.Concerts (
            VenueId INT64 NOT NULL,
            SingerId INT64 NOT NULL,
            ConcertDate DATE NOT NULL,
            StartTime TIMESTAMP NOT NULL,
            TicketRevenue NUMERIC,
            PRIMARY KEY (VenueId, SingerId, ConcertDate) NOT ENFORCED
        )""",
        f"""CREATE TABLE {d}.TicketSales (
            SaleId INT64 NOT NULL,
            VenueId INT64 NOT NULL,
            SingerId INT64 NOT NULL,
            ConcertDate DATE NOT NULL,
            Quantity INT64 NOT NULL,
            Amount NUMERIC NOT NULL,
            SoldAt TIMESTAMP NOT NULL,
            PRIMARY KEY (SaleId) NOT ENFORCED
        )""",
        f"""CREATE VIEW {d}.SingerRevenue AS
            SELECT Singers.SingerId, Singers.LastName,
                   SUM(Concerts.TicketRevenue) AS TotalRevenue
            FROM {d}.Singers AS Singers
            JOIN {d}.Concerts AS Concerts ON Concerts.SingerId = Singers.SingerId
            GROUP BY Singers.SingerId, Singers.LastName""",
    ]


def _foreign_keys(dataset: str) -> list[str]:
    d = dataset
    return [
        f"""ALTER TABLE {d}.Albums ADD CONSTRAINT FK_Albums_Singers
            FOREIGN KEY (SingerId) REFERENCES {d}.Singers (SingerId) NOT ENFORCED""",
        f"""ALTER TABLE {d}.Concerts ADD CONSTRAINT FK_Concerts_Venues
            FOREIGN KEY (VenueId) REFERENCES {d}.Venues (VenueId) NOT ENFORCED""",
        f"""ALTER TABLE {d}.Concerts ADD CONSTRAINT FK_Concerts_Singers
            FOREIGN KEY (SingerId) REFERENCES {d}.Singers (SingerId) NOT ENFORCED""",
        f"""ALTER TABLE {d}.TicketSales ADD CONSTRAINT FK_TicketSales_Concerts
            FOREIGN KEY (VenueId, SingerId, ConcertDate)
            REFERENCES {d}.Concerts (VenueId, SingerId, ConcertDate) NOT ENFORCED""",
    ]


def _rows(dataset: str) -> list[str]:
    """One ``INSERT`` per table, so seeding bills as few minimums as possible."""
    d = dataset
    return [
        f"""INSERT INTO {d}.Singers
            (SingerId, FirstName, LastName, BirthDate, IsActive, Genres, Profile, Address)
        VALUES
            (1, 'Marc', 'Richards', DATE '1970-09-03', TRUE, ['rock', 'blues'],
             JSON '{{"label": "Northside", "agent": {{"name": "Ann"}}}}',
             STRUCT('Seattle' AS city, 'US' AS country)),
            (2, 'Catalina', 'Smith', DATE '1990-08-17', TRUE, ['pop'],
             JSON '{{"label": "Brightline"}}',
             STRUCT('Portland' AS city, 'US' AS country)),
            (3, NULL, 'Trentor', DATE '1991-10-02', FALSE, ARRAY<STRING>[], NULL,
             NULL),
            (4, 'Lea', 'Martin', NULL, TRUE, ['jazz', 'soul'],
             JSON '{{"label": "Northside", "awards": 3}}',
             STRUCT('Lyon' AS city, 'FR' AS country))""",
        f"""INSERT INTO {d}.Albums (SingerId, AlbumId, AlbumTitle, ReleaseDate, Price)
        VALUES
            (1, 1, 'Total Junk', DATE '2001-05-01', NUMERIC '9.99'),
            (1, 2, 'Go, Go, Go', DATE '2004-03-12', NUMERIC '12.50'),
            (2, 1, 'Green', DATE '2015-06-30', NUMERIC '14.00'),
            (2, 2, 'Forever Hold Your Peace', NULL, NULL),
            (3, 1, 'Terrified', DATE '2010-01-15', NUMERIC '7.25'),
            (4, 1, 'Blue Hours', DATE '2020-11-20', NUMERIC '15.99')""",
        f"""INSERT INTO {d}.Venues (VenueId, VenueName, City, Capacity)
        VALUES
            (1, 'Harbor Hall', 'Seattle', 2500),
            (2, 'The Lantern', 'Portland', 800),
            (3, 'Riverside Arena', 'Austin', NULL)""",
        f"""INSERT INTO {d}.Concerts
            (VenueId, SingerId, ConcertDate, StartTime, TicketRevenue)
        VALUES
            (1, 1, DATE '2024-04-05', TIMESTAMP '2024-04-05 20:00:00+00',
             NUMERIC '125000.00'),
            (1, 2, DATE '2024-05-10', TIMESTAMP '2024-05-10 19:30:00+00',
             NUMERIC '98000.50'),
            (2, 2, DATE '2024-06-01', TIMESTAMP '2024-06-01 21:00:00+00',
             NUMERIC '40500.00'),
            (2, 3, DATE '2024-07-19', TIMESTAMP '2024-07-19 20:00:00+00', NULL),
            (3, 4, DATE '2024-08-23', TIMESTAMP '2024-08-23 18:00:00+00',
             NUMERIC '210000.75')""",
        f"""INSERT INTO {d}.TicketSales
            (SaleId, VenueId, SingerId, ConcertDate, Quantity, Amount, SoldAt)
        VALUES
            (1, 1, 1, DATE '2024-04-05', 2, NUMERIC '180.00',
             TIMESTAMP '2024-03-01 10:15:00+00'),
            (2, 1, 1, DATE '2024-04-05', 4, NUMERIC '360.00',
             TIMESTAMP '2024-03-02 12:00:00+00'),
            (3, 1, 2, DATE '2024-05-10', 1, NUMERIC '95.50',
             TIMESTAMP '2024-04-20 09:45:00+00'),
            (4, 2, 2, DATE '2024-06-01', 3, NUMERIC '150.00',
             TIMESTAMP '2024-05-15 18:30:00+00'),
            (5, 3, 4, DATE '2024-08-23', 6, NUMERIC '720.00',
             TIMESTAMP '2024-07-01 08:00:00+00'),
            (6, 3, 4, DATE '2024-08-23', 2, NUMERIC '240.00',
             TIMESTAMP '2024-07-03 14:20:00+00')""",
    ]


class SeedRefused(RuntimeError):
    """The dataset exists and does not carry this script's label."""


def _client(project: str, emulator_host: str | None, location: str | None) -> "Client":
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    _check_emulator_environment(emulator_host)
    kwargs: dict[str, Any] = {"project": project}
    if location is not None:
        kwargs["location"] = location
    if emulator_host is not None:
        kwargs["credentials"] = AnonymousCredentials()
        kwargs["client_options"] = {"api_endpoint": f"http://{emulator_host}"}
    return bigquery.Client(**kwargs)


def _retry() -> Any:
    from google.cloud.bigquery.retry import DEFAULT_RETRY

    return DEFAULT_RETRY.with_timeout(_RETRY_BUDGET_S)


def _owned(client: "Client", reference: str) -> bool | None:
    """``None`` when the dataset does not exist, else whether it is ours.

    Raises:
        SeedRefused: the dataset exists without this script's label.
    """
    from google.api_core.exceptions import NotFound

    try:
        dataset = client.get_dataset(
            reference, retry=_retry(), timeout=_REQUEST_TIMEOUT_S
        )
    except NotFound:
        return None
    if (dataset.labels or {}).get(LABEL_KEY) != LABEL_VALUE:
        raise SeedRefused(
            f"Dataset {reference} exists but does not carry the label "
            f"{LABEL_KEY}={LABEL_VALUE}, so this script did not create it. "
            "Refusing to replace or delete it; pass another --dataset."
        )
    return True


def _check_dataset(dataset: str) -> None:
    if not _DATASET_RE.fullmatch(dataset):
        raise ValueError(f"Invalid dataset id {dataset!r}: use letters, digits and '_'")


def delete(
    project: str,
    dataset: str = DEFAULT_DATASET,
    *,
    emulator_host: str | None = None,
    location: str | None = None,
) -> bool:
    """Delete the dataset if this script created it. Return whether it existed.

    Raises:
        SeedRefused: the dataset exists without this script's label.
    """
    _check_dataset(dataset)
    client = _client(project, emulator_host, location)
    try:
        reference = f"{project}.{dataset}"
        if _owned(client, reference) is None:
            logger.info("Dataset %s does not exist; nothing to delete", reference)
            return False
        client.delete_dataset(
            reference,
            delete_contents=True,
            retry=_retry(),
            timeout=_REQUEST_TIMEOUT_S,
        )
        logger.info("Deleted dataset %s", reference)
        return True
    finally:
        client.close()


def seed(
    project: str,
    dataset: str = DEFAULT_DATASET,
    *,
    emulator_host: str | None = None,
    location: str | None = None,
) -> str:
    """Create (or recreate) the dataset and return its connection string.

    Raises:
        SeedRefused: the dataset exists without this script's label.
        ValueError: ``BIGQUERY_EMULATOR_HOST`` disagrees with *emulator_host*,
            or an id is invalid.
    """
    from google.cloud import bigquery

    _check_dataset(dataset)
    url = format_connection_string(
        project=project,
        dataset=dataset,
        emulator_host=emulator_host,
        location=location,
    )
    client = _client(project, emulator_host, location)
    try:
        reference = f"{project}.{dataset}"
        if _owned(client, reference):
            logger.info("Replacing dataset %s, created by an earlier run", reference)
            client.delete_dataset(
                reference,
                delete_contents=True,
                retry=_retry(),
                timeout=_REQUEST_TIMEOUT_S,
            )
        resource = bigquery.Dataset(reference)
        resource.labels = {LABEL_KEY: LABEL_VALUE}
        resource.description = (
            "Auto Ontology test fixture (dev_tools/fixtures/seed_bigquery.py). "
            "Safe to delete."
        )
        if location is not None:
            resource.location = location
        created = client.create_dataset(
            resource, retry=_retry(), timeout=_REQUEST_TIMEOUT_S
        )
        logger.info("Created dataset %s in %s", reference, created.location)

        statements = _tables(dataset)
        if emulator_host is None:
            statements += _foreign_keys(dataset)
        else:
            logger.warning(
                "Emulator: not declaring %d foreign keys; the emulator rejects "
                "FOREIGN KEY constraints",
                len(_foreign_keys(dataset)),
            )
        statements += _rows(dataset)
        # The emulator labels every query error as a transient job error, so
        # job re-runs there would only delay a real failure.
        job_retry = None
        if emulator_host is None:
            from google.cloud.bigquery.retry import DEFAULT_JOB_RETRY

            job_retry = DEFAULT_JOB_RETRY.with_timeout(_RETRY_BUDGET_S)
        for statement in statements:
            client.query_and_wait(
                statement,
                api_timeout=_REQUEST_TIMEOUT_S,
                retry=_retry(),
                job_retry=job_retry,
            )
            logger.info("Ran %s", " ".join(statement.split())[:60])
    finally:
        client.close()
    return url


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True, help="Google Cloud project id")
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"Dataset id to create or delete (default {DEFAULT_DATASET})",
    )
    parser.add_argument(
        "--emulator-host",
        default=None,
        help="Use a BigQuery emulator at host:port instead of Google Cloud",
    )
    parser.add_argument(
        "--location",
        default=None,
        help="Dataset and job location, e.g. US, EU or us-central1",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the dataset (only if this script created it)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.delete:
        delete(
            args.project,
            args.dataset,
            emulator_host=args.emulator_host,
            location=args.location,
        )
        return
    print(
        seed(
            args.project,
            args.dataset,
            emulator_host=args.emulator_host,
            location=args.location,
        )
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    main()
