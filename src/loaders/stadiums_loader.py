"""
Claude created dataset
Seed NFL stadiums - static reference data, not fetched from any API.
"""

import logging
import sys
from typing import Any

from config.config import configure_logging, load_env
from src.loaders.loader_helper import sql_batch_call

logger = logging.getLogger(__name__)

STADIUMS: list[dict[str, Any]] = [
    {
        "name": "Levi's Stadium",
        "city": "Santa Clara",
        "state": "CA",
        "country": "USA",
        "latitude": 37.4032,
        "longitude": -121.9694,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "AT&T Stadium",
        "city": "Arlington",
        "state": "TX",
        "country": "USA",
        "latitude": 32.7473,
        "longitude": -97.0945,
        "surface_type": "Artificial Turf",
        "roof_type": "Retractable",
    },
    {
        "name": "Acrisure Stadium",
        "city": "Pittsburgh",
        "state": "PA",
        "country": "USA",
        "latitude": 40.4468,
        "longitude": -80.0158,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Allegiant Stadium",
        "city": "Las Vegas",
        "state": "NV",
        "country": "USA",
        "latitude": 36.0909,
        "longitude": -115.1833,
        "surface_type": "Grass",
        "roof_type": "Dome",
    },
    {
        "name": "Arrowhead Stadium",
        "city": "Kansas City",
        "state": "MO",
        "country": "USA",
        "latitude": 39.0489,
        "longitude": -94.4839,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Bank of America Stadium",
        "city": "Charlotte",
        "state": "NC",
        "country": "USA",
        "latitude": 35.2258,
        "longitude": -80.8528,
        "surface_type": "Artificial Turf",
        "roof_type": "Open",
    },
    {
        "name": "Caesars Superdome",
        "city": "New Orleans",
        "state": "LA",
        "country": "USA",
        "latitude": 29.9511,
        "longitude": -90.0812,
        "surface_type": "Artificial Turf",
        "roof_type": "Dome",
    },
    {
        "name": "Empower Field at Mile High",
        "city": "Denver",
        "state": "CO",
        "country": "USA",
        "latitude": 39.7439,
        "longitude": -105.0201,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "EverBank Stadium",
        "city": "Jacksonville",
        "state": "FL",
        "country": "USA",
        "latitude": 30.3239,
        "longitude": -81.6373,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Ford Field",
        "city": "Detroit",
        "state": "MI",
        "country": "USA",
        "latitude": 42.3400,
        "longitude": -83.0456,
        "surface_type": "Artificial Turf",
        "roof_type": "Dome",
    },
    {
        "name": "Gillette Stadium",
        "city": "Foxborough",
        "state": "MA",
        "country": "USA",
        "latitude": 42.0909,
        "longitude": -71.2643,
        "surface_type": "Artificial Turf",
        "roof_type": "Open",
    },
    {
        "name": "Hard Rock Stadium",
        "city": "Miami Gardens",
        "state": "FL",
        "country": "USA",
        "latitude": 25.9580,
        "longitude": -80.2389,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Highmark Stadium",
        "city": "Orchard Park",
        "state": "NY",
        "country": "USA",
        "latitude": 42.7745,
        "longitude": -78.7968,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Huntington Bank Field",
        "city": "Cleveland",
        "state": "OH",
        "country": "USA",
        "latitude": 41.5061,
        "longitude": -81.6995,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Lambeau Field",
        "city": "Green Bay",
        "state": "WI",
        "country": "USA",
        "latitude": 44.5013,
        "longitude": -88.0622,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Lincoln Financial Field",
        "city": "Philadelphia",
        "state": "PA",
        "country": "USA",
        "latitude": 39.9008,
        "longitude": -75.1675,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Lucas Oil Stadium",
        "city": "Indianapolis",
        "state": "IN",
        "country": "USA",
        "latitude": 39.7601,
        "longitude": -86.1639,
        "surface_type": "Artificial Turf",
        "roof_type": "Retractable",
    },
    {
        "name": "Lumen Field",
        "city": "Seattle",
        "state": "WA",
        "country": "USA",
        "latitude": 47.5952,
        "longitude": -122.3316,
        "surface_type": "Artificial Turf",
        "roof_type": "Open",
    },
    {
        "name": "M&T Bank Stadium",
        "city": "Baltimore",
        "state": "MD",
        "country": "USA",
        "latitude": 39.2780,
        "longitude": -76.6227,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Mercedes-Benz Stadium",
        "city": "Atlanta",
        "state": "GA",
        "country": "USA",
        "latitude": 33.7554,
        "longitude": -84.4008,
        "surface_type": "Artificial Turf",
        "roof_type": "Retractable",
    },
    {
        # Shared by the Giants and Jets.
        "name": "MetLife Stadium",
        "city": "East Rutherford",
        "state": "NJ",
        "country": "USA",
        "latitude": 40.8135,
        "longitude": -74.0745,
        "surface_type": "Artificial Turf",
        "roof_type": "Open",
    },
    {
        "name": "Nissan Stadium",
        "city": "Nashville",
        "state": "TN",
        "country": "USA",
        "latitude": 36.1665,
        "longitude": -86.7713,
        "surface_type": "Artificial Turf",
        "roof_type": "Open",
    },
    {
        "name": "Northwest Stadium",
        "city": "Landover",
        "state": "MD",
        "country": "USA",
        "latitude": 38.9078,
        "longitude": -76.8645,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Paycor Stadium",
        "city": "Cincinnati",
        "state": "OH",
        "country": "USA",
        "latitude": 39.0954,
        "longitude": -84.5160,
        "surface_type": "Artificial Turf",
        "roof_type": "Open",
    },
    {
        "name": "Raymond James Stadium",
        "city": "Tampa",
        "state": "FL",
        "country": "USA",
        "latitude": 27.9759,
        "longitude": -82.5033,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "NRG Stadium",
        "city": "Houston",
        "state": "TX",
        "country": "USA",
        "latitude": 29.6847,
        "longitude": -95.4107,
        "surface_type": "Artificial Turf",
        "roof_type": "Retractable",
    },
    {
        "name": "SoFi Stadium",
        "city": "Inglewood",
        "state": "CA",
        "country": "USA",
        "latitude": 33.9535,
        "longitude": -118.3392,
        "surface_type": "Artificial Turf",
        "roof_type": "Dome",  # fixed translucent canopy, but open sides
    },
    {
        "name": "Soldier Field",
        "city": "Chicago",
        "state": "IL",
        "country": "USA",
        "latitude": 41.8623,
        "longitude": -87.6167,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "State Farm Stadium",
        "city": "Glendale",
        "state": "AZ",
        "country": "USA",
        "latitude": 33.5276,
        "longitude": -112.2626,
        "surface_type": "Grass",
        "roof_type": "Retractable",
    },
    {
        "name": "U.S. Bank Stadium",
        "city": "Minneapolis",
        "state": "MN",
        "country": "USA",
        "latitude": 44.9735,
        "longitude": -93.2575,
        "surface_type": "Artificial Turf",
        "roof_type": "Dome",
    },
]

INTERNATIONAL_VENUES: list[dict[str, Any]] = [
    {
        "name": "Estadio Banorte",
        "city": "Mexico City",
        "state": None,
        "country": "Mexico",
        "latitude": 19.3029,
        "longitude": -99.1505,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        # Sports IO's own venue name for this one is "FC Bayern Munich
        # Stadium" - sports_io_loader.py's VENUE_NAME_CORRECTIONS maps that
        # onto this row's real name instead.
        "name": "Allianz Arena",
        "city": "Munich",
        "state": None,
        "country": "Germany",
        "latitude": 48.2188,
        "longitude": 11.6247,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Maracanã Stadium",
        "city": "Rio de Janeiro",
        "state": None,
        "country": "Brazil",
        "latitude": -22.9121,
        "longitude": -43.2302,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Melbourne Cricket Ground",
        "city": "Melbourne",
        "state": None,
        "country": "Australia",
        "latitude": -37.8200,
        "longitude": 144.9834,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Santiago Bernabéu",
        "city": "Madrid",
        "state": None,
        "country": "Spain",
        "latitude": 40.4531,
        "longitude": -3.6883,
        "surface_type": "Grass",
        "roof_type": "Retractable",
    },
    {
        "name": "Stade de France",
        "city": "Saint-Denis",
        "state": None,
        "country": "France",
        "latitude": 48.9244,
        "longitude": 2.3601,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
    {
        "name": "Tottenham Hotspur Stadium",
        "city": "London",
        "state": None,
        "country": "England",
        "latitude": 51.6043,
        "longitude": -0.0665,
        "surface_type": "Artificial Turf",  # NFL-specific turf tray over the retractable grass pitch
        "roof_type": "Open",
    },
    {
        "name": "Wembley Stadium",
        "city": "London",
        "state": None,
        "country": "England",
        "latitude": 51.5560,
        "longitude": -0.2795,
        "surface_type": "Grass",
        "roof_type": "Open",
    },
]

_UPSERT_STADIUM_SQL = """
INSERT INTO stadiums (name, city, state, country, latitude, longitude, surface_type, roof_type)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(name) DO UPDATE SET
    city = excluded.city,
    state = excluded.state,
    country = excluded.country,
    latitude = excluded.latitude,
    longitude = excluded.longitude,
    surface_type = excluded.surface_type,
    roof_type = excluded.roof_type
"""


def load_stadiums(env: str = "local") -> None:
    """seed/update the static stadiums list"""
    if not load_env(env):
        sys.exit(1)

    statements: list[tuple[str, list[Any] | None]] = [
        (
            _UPSERT_STADIUM_SQL,
            [
                s["name"],
                s["city"],
                s["state"],
                s["country"],
                s["latitude"],
                s["longitude"],
                s["surface_type"],
                s["roof_type"],
            ],
        )
        for s in STADIUMS + INTERNATIONAL_VENUES
    ]
    sql_batch_call(statements)
    logger.info("Upserted %d stadiums into D1 (%s)", len(statements), env)


def main(env: str = "local") -> None:
    """load stadium details"""
    load_stadiums(env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
