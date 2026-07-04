"""Tesla VIN decoding: model, model year and factory.

A Tesla VIN is 17 characters, e.g. LRW3F7EK3RC309372:
  1-3   WMI / factory region (LRW = Giga Shanghai, 5YJ = Fremont, ...)
  4     model line (3 = Model 3, Y = Model Y, S, X)
  10    model year letter (R = 2024, S = 2025, ...)
  11    assembly plant (C = Shanghai, F = Fremont, A = Austin, B = Berlin)
"""
from __future__ import annotations

MODEL_BY_CODE = {"3": "Model 3", "Y": "Model Y", "S": "Model S", "X": "Model X"}

# Standard VIN model-year letters (I, O, Q, U, Z are never used).
YEAR_BY_CODE = {
    "A": 2010, "B": 2011, "C": 2012, "D": 2013, "E": 2014, "F": 2015,
    "G": 2016, "H": 2017, "J": 2018, "K": 2019, "L": 2020, "M": 2021,
    "N": 2022, "P": 2023, "R": 2024, "S": 2025, "T": 2026, "V": 2027,
    "W": 2028, "X": 2029, "Y": 2030,
    "1": 2031, "2": 2032, "3": 2033, "4": 2034, "5": 2035, "6": 2036,
    "7": 2037, "8": 2038, "9": 2039,
}

PLANT_BY_CODE = {
    "F": "Fremont", "A": "Austin", "B": "Berlin", "C": "Shanghai",
    "N": "Nevada", "R": "Fremont",
}


def decode(vin: str) -> dict:
    """Best-effort {model, year, plant} from a Tesla VIN; {} if not decodable."""
    vin = (vin or "").strip().upper()
    if len(vin) != 17 or vin.startswith(("DEMO", "IMPORT", "LINKED")):
        return {}
    return {
        "model": MODEL_BY_CODE.get(vin[3]),
        "year": YEAR_BY_CODE.get(vin[9]),
        "plant": PLANT_BY_CODE.get(vin[10]),
    }
