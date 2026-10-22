import pytest

from app.ingest import awattar, energy_charts
from app.ingest.carbon import compute_carbon_intensity, factor_for
from tests.fixtures import AWATTAR_MARKETDATA, ENERGY_CHARTS_POWER, ENERGY_CHARTS_PRICE


def test_energy_charts_prices_parse_to_utc():
    df = energy_charts.parse_prices(ENERGY_CHARTS_PRICE)
    assert list(df.columns) == ["ts", "eur_mwh"]
    assert len(df) == 4
    assert str(df["ts"].iloc[0]) == "2026-10-20 00:00:00+00:00"


def test_generation_skips_load_shares_and_storage_charging():
    df = energy_charts.parse_generation(ENERGY_CHARTS_POWER)
    assert set(df["fuel"]) == {"Wind onshore", "Fossil gas", "Solar"}
    assert (df["mw"] >= 0).all()
    # The None solar value is dropped, the 0.0 one is kept.
    assert len(df[df["fuel"] == "Solar"]) == 1


def test_awattar_prices_parse():
    df = awattar.parse_prices(AWATTAR_MARKETDATA)
    assert df["eur_mwh"].tolist() == [95.5, 70.0]
    assert str(df["ts"].iloc[1]) == "2026-10-20 01:00:00+00:00"


@pytest.mark.parametrize(
    "fuel, expected",
    [
        ("Fossil brown coal / lignite", 1100.0),
        ("Fossil hard coal", 820.0),
        ("Fossil gas", 490.0),
        ("Wind offshore", 12.0),
        ("Wind onshore", 11.0),
        ("Hydro Run-of-River", 24.0),
        ("Something new", None),
    ],
)
def test_emission_factor_lookup(fuel, expected):
    assert factor_for(fuel) == expected


def test_carbon_intensity_is_generation_weighted():
    df = energy_charts.parse_generation(ENERGY_CHARTS_POWER)
    carbon = compute_carbon_intensity(df)
    # First quarter: 6000 MW wind @11 + 4000 MW gas @490, solar missing -> 10000 MW total
    expected_first = (6000 * 11 + 4000 * 490) / 10000
    assert carbon["g_per_kwh"].iloc[0] == pytest.approx(expected_first)
    # Second quarter adds 0 MW solar, which changes nothing.
    assert carbon["g_per_kwh"].iloc[1] == pytest.approx(expected_first)
