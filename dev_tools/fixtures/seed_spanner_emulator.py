# SPDX-License-Identifier: Apache-2.0

"""Seed a Cloud Spanner emulator with a small music-and-concerts database.

Used by the opt-in emulator tests in
``auto_ontology/connectors/tests/test_spanner_emulator.py`` and for trying the
Spanner connector by hand. It only ever talks to an emulator: the client is
built with anonymous credentials and an explicit endpoint, so it cannot reach a
real Cloud Spanner project.

The emulator keeps its state in memory, so run this again after the emulator
restarts. Re-running is safe: the instance is created when missing, and the
database is dropped and created again from the definitions below, so every run
ends in the same state.

What the schema covers (GoogleSQL dialect):

* ``Singers`` -- parent table; nullable, ``ARRAY``, ``JSON``, ``DATE`` and
  ``BOOL`` columns.
* ``Albums`` -- interleaved in ``Singers``; ``NUMERIC`` column.
* ``Venues`` and ``Concerts`` -- ``Concerts`` declares foreign keys to
  ``Venues`` and ``Singers``; ``TIMESTAMP`` and ``NUMERIC`` ticket revenue.
* ``TicketSales`` -- a two-column foreign key to ``Concerts``.
* ``sales.Promotions`` -- a table in a named schema, with a foreign key back to
  ``Venues`` in the default schema.
* ``SingerRevenue`` -- a view joining ``Singers`` and ``Concerts``.

Usage::

    docker run -d -p 9010:9010 -p 9020:9020 gcr.io/cloud-spanner-emulator/emulator
    uv run python -m dev_tools.fixtures.seed_spanner_emulator
    uv run python -m dev_tools.fixtures.seed_spanner_emulator \\
        --host localhost:9010 --project auto-ontology --instance auto-ontology \\
        --database music

Each option falls back to an environment variable, then to a default:

=============  ==========================  ===============
Option         Environment variable        Default
=============  ==========================  ===============
``--host``     ``SPANNER_EMULATOR_HOST``   ``localhost:9010``
``--project``  ``SPANNER_PROJECT``         ``auto-ontology``
``--instance`` ``SPANNER_INSTANCE``        ``auto-ontology``
``--database`` ``SPANNER_DATABASE``        ``music``
=============  ==========================  ===============

The matching connection string is printed at the end, e.g.
``spanner://auto-ontology/auto-ontology/music?emulator_host=localhost:9010``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
from decimal import Decimal
from typing import Any

logger = logging.getLogger("dev_tools.fixtures.seed_spanner_emulator")

DEFAULT_HOST = "localhost:9010"
DEFAULT_PROJECT = "auto-ontology"
DEFAULT_INSTANCE = "auto-ontology"
DEFAULT_DATABASE = "music"

# The emulator accepts this configuration name for any project.
_EMULATOR_CONFIG = "emulator-config"
_OPERATION_TIMEOUT_S = 120

DDL: list[str] = [
    """CREATE TABLE Singers (
        SingerId INT64 NOT NULL,
        FirstName STRING(100),
        LastName STRING(100) NOT NULL,
        BirthDate DATE,
        IsActive BOOL NOT NULL,
        Genres ARRAY<STRING(50)>,
        Profile JSON,
    ) PRIMARY KEY (SingerId)""",
    """CREATE TABLE Albums (
        SingerId INT64 NOT NULL,
        AlbumId INT64 NOT NULL,
        AlbumTitle STRING(200) NOT NULL,
        ReleaseDate DATE,
        Price NUMERIC,
    ) PRIMARY KEY (SingerId, AlbumId),
      INTERLEAVE IN PARENT Singers ON DELETE CASCADE""",
    """CREATE TABLE Venues (
        VenueId INT64 NOT NULL,
        VenueName STRING(200) NOT NULL,
        City STRING(100) NOT NULL,
        Capacity INT64,
    ) PRIMARY KEY (VenueId)""",
    """CREATE TABLE Concerts (
        VenueId INT64 NOT NULL,
        SingerId INT64 NOT NULL,
        ConcertDate DATE NOT NULL,
        StartTime TIMESTAMP NOT NULL,
        TicketRevenue NUMERIC,
        CONSTRAINT FK_Concerts_Venues FOREIGN KEY (VenueId)
            REFERENCES Venues (VenueId),
        CONSTRAINT FK_Concerts_Singers FOREIGN KEY (SingerId)
            REFERENCES Singers (SingerId),
    ) PRIMARY KEY (VenueId, SingerId, ConcertDate)""",
    """CREATE TABLE TicketSales (
        SaleId INT64 NOT NULL,
        VenueId INT64 NOT NULL,
        SingerId INT64 NOT NULL,
        ConcertDate DATE NOT NULL,
        Quantity INT64 NOT NULL,
        Amount NUMERIC NOT NULL,
        SoldAt TIMESTAMP NOT NULL,
        CONSTRAINT FK_TicketSales_Concerts FOREIGN KEY (VenueId, SingerId, ConcertDate)
            REFERENCES Concerts (VenueId, SingerId, ConcertDate),
    ) PRIMARY KEY (SaleId)""",
    "CREATE SCHEMA sales",
    """CREATE TABLE sales.Promotions (
        PromotionId INT64 NOT NULL,
        VenueId INT64 NOT NULL,
        Code STRING(20) NOT NULL,
        Discount NUMERIC,
        CONSTRAINT FK_Promotions_Venues FOREIGN KEY (VenueId)
            REFERENCES Venues (VenueId),
    ) PRIMARY KEY (PromotionId)""",
    """CREATE VIEW SingerRevenue SQL SECURITY INVOKER AS
        SELECT Singers.SingerId, Singers.LastName,
               SUM(Concerts.TicketRevenue) AS TotalRevenue
        FROM Singers
        JOIN Concerts ON Concerts.SingerId = Singers.SingerId
        GROUP BY Singers.SingerId, Singers.LastName""",
]


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)


def _rows() -> list[tuple[str, tuple[str, ...], list[tuple[Any, ...]]]]:
    """Return ``(table, columns, rows)`` in an order the foreign keys accept."""
    from google.cloud.spanner_v1.data_types import JsonObject

    return [
        (
            "Singers",
            (
                "SingerId",
                "FirstName",
                "LastName",
                "BirthDate",
                "IsActive",
                "Genres",
                "Profile",
            ),
            [
                (
                    1,
                    "Marc",
                    "Richards",
                    dt.date(1970, 9, 3),
                    True,
                    ["rock", "blues"],
                    JsonObject({"label": "Northside", "agent": {"name": "Ann"}}),
                ),
                (
                    2,
                    "Catalina",
                    "Smith",
                    dt.date(1990, 8, 17),
                    True,
                    ["pop"],
                    JsonObject({"label": "Brightline"}),
                ),
                (3, None, "Trentor", dt.date(1991, 10, 2), False, None, None),
                (
                    4,
                    "Lea",
                    "Martin",
                    None,
                    True,
                    ["jazz", "soul"],
                    JsonObject({"label": "Northside", "awards": 3}),
                ),
            ],
        ),
        (
            "Albums",
            ("SingerId", "AlbumId", "AlbumTitle", "ReleaseDate", "Price"),
            [
                (1, 1, "Total Junk", dt.date(2001, 5, 1), Decimal("9.99")),
                (1, 2, "Go, Go, Go", dt.date(2004, 3, 12), Decimal("12.50")),
                (2, 1, "Green", dt.date(2015, 6, 30), Decimal("14.00")),
                (2, 2, "Forever Hold Your Peace", None, None),
                (3, 1, "Terrified", dt.date(2010, 1, 15), Decimal("7.25")),
                (4, 1, "Blue Hours", dt.date(2020, 11, 20), Decimal("15.99")),
            ],
        ),
        (
            "Venues",
            ("VenueId", "VenueName", "City", "Capacity"),
            [
                (1, "Harbor Hall", "Seattle", 2500),
                (2, "The Lantern", "Portland", 800),
                (3, "Riverside Arena", "Austin", None),
            ],
        ),
        (
            "Concerts",
            ("VenueId", "SingerId", "ConcertDate", "StartTime", "TicketRevenue"),
            [
                (
                    1,
                    1,
                    dt.date(2024, 4, 5),
                    _ts("2024-04-05T20:00:00"),
                    Decimal("125000.00"),
                ),
                (
                    1,
                    2,
                    dt.date(2024, 5, 10),
                    _ts("2024-05-10T19:30:00"),
                    Decimal("98000.50"),
                ),
                (
                    2,
                    2,
                    dt.date(2024, 6, 1),
                    _ts("2024-06-01T21:00:00"),
                    Decimal("40500.00"),
                ),
                (2, 3, dt.date(2024, 7, 19), _ts("2024-07-19T20:00:00"), None),
                (
                    3,
                    4,
                    dt.date(2024, 8, 23),
                    _ts("2024-08-23T18:00:00"),
                    Decimal("210000.75"),
                ),
            ],
        ),
        (
            "TicketSales",
            (
                "SaleId",
                "VenueId",
                "SingerId",
                "ConcertDate",
                "Quantity",
                "Amount",
                "SoldAt",
            ),
            [
                (
                    1,
                    1,
                    1,
                    dt.date(2024, 4, 5),
                    2,
                    Decimal("180.00"),
                    _ts("2024-03-01T10:15:00"),
                ),
                (
                    2,
                    1,
                    1,
                    dt.date(2024, 4, 5),
                    4,
                    Decimal("360.00"),
                    _ts("2024-03-02T12:00:00"),
                ),
                (
                    3,
                    1,
                    2,
                    dt.date(2024, 5, 10),
                    1,
                    Decimal("95.50"),
                    _ts("2024-04-20T09:45:00"),
                ),
                (
                    4,
                    2,
                    2,
                    dt.date(2024, 6, 1),
                    3,
                    Decimal("150.00"),
                    _ts("2024-05-15T18:30:00"),
                ),
                (
                    5,
                    3,
                    4,
                    dt.date(2024, 8, 23),
                    6,
                    Decimal("720.00"),
                    _ts("2024-07-01T08:00:00"),
                ),
                (
                    6,
                    3,
                    4,
                    dt.date(2024, 8, 23),
                    2,
                    Decimal("240.00"),
                    _ts("2024-07-03T14:20:00"),
                ),
            ],
        ),
        (
            "sales.Promotions",
            ("PromotionId", "VenueId", "Code", "Discount"),
            [
                (1, 1, "SPRING24", Decimal("0.10")),
                (2, 3, "AUSTIN15", Decimal("0.15")),
            ],
        ),
    ]


def connection_string(host: str, project: str, instance: str, database: str) -> str:
    """Return the connector URL for the seeded database."""
    return f"spanner://{project}/{instance}/{database}?emulator_host={host}"


def seed(
    host: str = DEFAULT_HOST,
    project: str = DEFAULT_PROJECT,
    instance: str = DEFAULT_INSTANCE,
    database: str = DEFAULT_DATABASE,
) -> str:
    """Create (or recreate) the fixture database and return its connection string.

    Raises:
        ValueError: ``SPANNER_EMULATOR_HOST`` is set to a different host. The
            client library always prefers that variable, so seeding would go
            somewhere other than *host*.
    """
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import spanner

    env_host = os.environ.get("SPANNER_EMULATOR_HOST")
    if env_host is not None and env_host != host:
        raise ValueError(
            f"SPANNER_EMULATOR_HOST is set to {env_host!r} but the host to seed is "
            f"{host!r}; the client library would use the environment variable. "
            "Unset it or pass the same host."
        )

    client = spanner.Client(
        project=project,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": host},
        disable_builtin_metrics=True,
    )
    spanner_instance = client.instance(
        instance,
        configuration_name=f"projects/{project}/instanceConfigs/{_EMULATOR_CONFIG}",
        display_name=instance,
        node_count=1,
    )
    if not spanner_instance.exists():
        logger.info("Creating instance %s/%s", project, instance)
        spanner_instance.create().result(_OPERATION_TIMEOUT_S)

    spanner_database = spanner_instance.database(database)
    if spanner_database.exists():
        logger.info("Dropping existing database %s", database)
        spanner_database.drop()
    spanner_database = spanner_instance.database(database, ddl_statements=DDL)
    logger.info("Creating database %s", database)
    spanner_database.create().result(_OPERATION_TIMEOUT_S)

    for table, columns, rows in _rows():
        with spanner_database.batch() as batch:
            batch.insert(table=table, columns=columns, values=rows)
        logger.info("Inserted %d rows into %s", len(rows), table)

    return connection_string(host, project, instance, database)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--host",
        default=os.environ.get("SPANNER_EMULATOR_HOST") or DEFAULT_HOST,
        help="Emulator gRPC endpoint as host:port (env SPANNER_EMULATOR_HOST)",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("SPANNER_PROJECT") or DEFAULT_PROJECT,
        help="Project id (env SPANNER_PROJECT)",
    )
    parser.add_argument(
        "--instance",
        default=os.environ.get("SPANNER_INSTANCE") or DEFAULT_INSTANCE,
        help="Instance id (env SPANNER_INSTANCE)",
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("SPANNER_DATABASE") or DEFAULT_DATABASE,
        help="Database id (env SPANNER_DATABASE)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    url = seed(args.host, args.project, args.instance, args.database)
    print(url)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    main()
