"""Sample API responses in the shape the real APIs return, used to test parsing offline."""

# 2026-10-20 00:00, 00:15, 00:30, 00:45 UTC
T0 = 1792454400
QUARTERS = [T0, T0 + 900, T0 + 1800, T0 + 2700]

ENERGY_CHARTS_PRICE = {
    "license_info": "CC BY 4.0",
    "unix_seconds": QUARTERS,
    "price": [80.0, 90.0, 100.0, 110.0],
    "unit": "EUR / MWh",
    "deprecated": False,
}

ENERGY_CHARTS_POWER = {
    "unix_seconds": QUARTERS[:2],
    "production_types": [
        {"name": "Wind onshore", "data": [6000.0, 6000.0]},
        {"name": "Fossil gas", "data": [4000.0, 4000.0]},
        {"name": "Hydro pumped storage consumption", "data": [-500.0, -500.0]},
        {"name": "Load", "data": [50000.0, 50000.0]},
        {"name": "Renewable share of generation", "data": [60.0, 60.0]},
        {"name": "Solar", "data": [None, 0.0]},
    ],
    "deprecated": False,
}

AWATTAR_MARKETDATA = {
    "object": "list",
    "data": [
        {"start_timestamp": T0 * 1000, "end_timestamp": (T0 + 3600) * 1000, "marketprice": 95.5, "unit": "Eur/MWh"},
        {"start_timestamp": (T0 + 3600) * 1000, "end_timestamp": (T0 + 7200) * 1000, "marketprice": 70.0, "unit": "Eur/MWh"},
    ],
    "url": "/at/v1/marketdata",
}

# Open-Meteo previous-runs response (timeformat=unixtime), two hours, one null to show gaps.
OPEN_METEO_PREVIOUS_RUNS = {
    "latitude": 54.5,
    "longitude": 8.9,
    "hourly_units": {"time": "unixtime", "wind_speed_100m_previous_day2": "m/s"},
    "hourly": {
        "time": [T0, T0 + 3600, T0 + 7200],
        "wind_speed_100m_previous_day2": [9.5, 11.0, None],
        "shortwave_radiation_previous_day2": [0.0, 0.0, None],
        "temperature_2m_previous_day2": [8.1, 7.9, None],
    },
}
