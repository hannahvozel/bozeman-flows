"""Load config/rivers.json and classify flows into levels and rapid classes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RIVERS_PATH = REPO_ROOT / "config" / "rivers.json"
ALERTS_PATH = REPO_ROOT / "config" / "alerts.json"


@dataclass(frozen=True)
class Site:
    usgs: str
    name: str
    run: str
    river_id: str
    river_name: str
    bands: tuple[dict[str, Any], ...]

    @property
    def monitoring_location_id(self) -> str:
        return f"USGS-{self.usgs}"

    @property
    def label(self) -> str:
        return f"{self.river_name} {self.name}"


@dataclass(frozen=True)
class RiverConfig:
    levels: dict[str, dict[str, str]]
    rivers: list[dict[str, Any]]
    sites: dict[str, Site]

    def classify(self, usgs: str, cfs: float | None) -> dict[str, Any] | None:
        """Return the band a flow falls in, with level label, general level and class."""
        if cfs is None:
            return None
        site = self.sites[usgs]
        for band in site.bands:
            if band["max"] is None or cfs < band["max"]:
                level = self.levels[band["level"]]
                return {
                    "level": band["level"],
                    "label": level["label"],
                    "general": level["general"],
                    "class": band["class"],
                    "note": band.get("note", ""),
                }
        raise ValueError(f"No band matched {cfs} cfs at {usgs}; last band must have max=null")


def validate(raw: dict[str, Any]) -> None:
    """Catch config mistakes early (bands out of order, unknown levels, duplicate gauges)."""
    seen: set[str] = set()
    for river in raw["rivers"]:
        for site in river["sites"]:
            usgs = site["usgs"]
            if usgs in seen:
                raise ValueError(f"Gauge {usgs} is listed twice")
            seen.add(usgs)
            bands = site["bands"]
            if not bands or bands[-1]["max"] is not None:
                raise ValueError(f"{usgs}: last band must have max = null")
            prev = float("-inf")
            for band in bands:
                if band["level"] not in raw["levels"]:
                    raise ValueError(f"{usgs}: unknown level {band['level']!r}")
                if band["max"] is not None:
                    if band["max"] <= prev:
                        raise ValueError(f"{usgs}: band maxima must increase")
                    prev = band["max"]


def load(path: Path = RIVERS_PATH) -> RiverConfig:
    raw = json.loads(Path(path).read_text())
    validate(raw)
    sites: dict[str, Site] = {}
    for river in raw["rivers"]:
        for s in river["sites"]:
            sites[s["usgs"]] = Site(
                usgs=s["usgs"],
                name=s["name"],
                run=s["run"],
                river_id=river["id"],
                river_name=river["name"],
                bands=tuple(s["bands"]),
            )
    return RiverConfig(levels=raw["levels"], rivers=raw["rivers"], sites=sites)
