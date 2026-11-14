"""Build the data/latest.json snapshot the website falls back to if live USGS calls fail."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import RiverConfig
from .usgs import DISCHARGE, GAGE_HEIGHT, parse_time

STEADY_PCT = 5.0  # +/- change over 24h that still counts as "steady"


def downsample_hourly(points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Keep the last reading in each clock hour. 15-min data -> ~4x smaller file."""
    by_hour: dict[datetime, tuple[str, float]] = {}
    for t, v in points:
        dt = parse_time(t)
        by_hour[dt.replace(minute=0, second=0, microsecond=0)] = (dt.isoformat().replace("+00:00", "Z"), v)
    return [by_hour[k] for k in sorted(by_hour)]


def trend_24h(points: list[tuple[str, float]], hours: float = 24) -> dict[str, Any] | None:
    """Compare the latest reading with the reading closest to `hours` earlier."""
    if len(points) < 2:
        return None
    latest_t, latest_v = parse_time(points[-1][0]), points[-1][1]
    target = latest_t - timedelta(hours=hours)
    past_t, past_v = min(((parse_time(t), v) for t, v in points), key=lambda tv: abs(tv[0] - target))
    if abs(past_t - target) > timedelta(hours=3) or past_v <= 0:
        return None
    pct = (latest_v - past_v) / past_v * 100
    direction = "steady" if abs(pct) < STEADY_PCT else ("rising" if pct > 0 else "falling")
    return {"from": past_v, "pct": round(pct, 1), "direction": direction}


def build(cfg: RiverConfig, latest: dict[str, dict[str, Any]],
          series: dict[str, list[tuple[str, float]]]) -> dict[str, Any]:
    sites: dict[str, Any] = {}
    for usgs in cfg.sites:
        reading = latest.get(usgs, {})
        q = reading.get(DISCHARGE) or {}
        gh = reading.get(GAGE_HEIGHT) or {}
        pts = series.get(usgs, [])
        cfs = q.get("value")
        sites[usgs] = {
            "cfs": cfs,
            "time": q.get("time"),
            "approval": q.get("approval"),
            "classification": cfg.classify(usgs, cfs),
            "trend_24h": trend_24h(pts),
            "series": downsample_hourly(pts),
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "USGS Water Data API (api.waterdata.usgs.gov), provisional data",
        "sites": sites,
    }
