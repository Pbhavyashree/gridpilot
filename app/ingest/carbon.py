"""Estimate grid carbon intensity (g CO2-eq per kWh) from the generation mix.

carbon_intensity = sum(MW of each fuel * emission factor of that fuel) / total MW

The factors below are APPROXIMATE life-cycle values. Most are the IPCC AR5 (2014,
WG III, Annex III) median values; lignite, oil, waste and "other" are rough
placeholders. Before you publish results, check each factor against a source you
cite in the README (for example IPCC AR5 Annex III, or the German Umweltbundesamt),
and always call the output an ESTIMATE.
"""

import pandas as pd

# Matched against the lower-cased fuel name, first match wins, so specific keys come first.
EMISSION_FACTORS_G_PER_KWH: list[tuple[str, float]] = [
    ("lignite", 1100.0),  # placeholder: verify (brown coal is dirtier than hard coal)
    ("brown coal", 1100.0),
    ("hard coal", 820.0),  # IPCC AR5 median, coal
    ("coal-derived gas", 820.0),  # treated like coal
    ("coal", 820.0),
    ("gas", 490.0),  # IPCC AR5 median, gas combined cycle
    ("oil", 650.0),  # placeholder: verify
    ("biomass", 230.0),  # IPCC AR5 median
    ("waste", 300.0),  # placeholder: verify
    ("geothermal", 38.0),  # IPCC AR5 median
    ("solar", 48.0),  # IPCC AR5 median, utility PV
    ("wind offshore", 12.0),  # IPCC AR5 median
    ("wind", 11.0),  # IPCC AR5 median, onshore
    ("hydro", 24.0),  # IPCC AR5 median
    ("nuclear", 12.0),  # IPCC AR5 median
    ("other", 500.0),  # placeholder: unknown mix
]


def factor_for(fuel: str) -> float | None:
    name = fuel.lower()
    for keyword, factor in EMISSION_FACTORS_G_PER_KWH:
        if keyword in name:
            return factor
    return None


def compute_carbon_intensity(generation: pd.DataFrame) -> pd.DataFrame:
    """Input: long format with ts, fuel, mw. Output: ts, g_per_kwh.

    Fuels with no known factor are left out of both the numerator and the denominator,
    so an unexpected new series name can't silently drag the average towards zero.
    """
    if generation.empty:
        return pd.DataFrame(columns=["ts", "g_per_kwh"])

    df = generation.copy()
    df["factor"] = df["fuel"].map(factor_for)
    df = df.dropna(subset=["factor"])
    df["weighted"] = df["mw"] * df["factor"]

    grouped = df.groupby("ts").agg(weighted=("weighted", "sum"), total_mw=("mw", "sum"))
    grouped = grouped[grouped["total_mw"] > 0]
    grouped["g_per_kwh"] = grouped["weighted"] / grouped["total_mw"]
    return grouped["g_per_kwh"].reset_index()
