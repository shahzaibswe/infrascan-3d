"""Deterministic demo data. Every row created here is flagged is_sample so the UI can label it."""
from __future__ import annotations

from . import repo

SAMPLE_ASSETS = [
    {
        "name": "Raiwind Road Corridor, km 42",
        "asset_type": "road_network",
        "region": "Punjab, Pakistan",
        "latitude": 31.5204,
        "longitude": 74.3587,
        "material": "asphalt concrete",
        "installed_year": 2011,
        "last_inspection": "2025-03-14",
        "notes": "Sample asset shipped with the prototype. Not a real survey record.",
    },
    {
        "name": "Chak 22 Water Board Transmission Main",
        "asset_type": "pipeline",
        "region": "Punjab, Pakistan",
        "latitude": 30.99,
        "longitude": 73.5,
        "material": "ductile iron, cement-lined",
        "installed_year": 1996,
        "last_inspection": "2024-11-02",
        "notes": "Sample asset shipped with the prototype. Not a real survey record.",
    },
    {
        "name": "L-138 Transmission Tower 47",
        "asset_type": "power_grid",
        "region": "Sindh, Pakistan",
        "latitude": 25.4,
        "longitude": 68.37,
        "material": "galvanised steel lattice",
        "installed_year": 2004,
        "last_inspection": "2025-06-21",
        "notes": "Sample asset shipped with the prototype. Not a real survey record.",
    },
]


def ensure_seed_data() -> int:
    return repo.seed_sample_assets(SAMPLE_ASSETS)
