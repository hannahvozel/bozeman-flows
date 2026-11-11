"""Minimal client for the modern USGS Water Data API.

Docs: https://api.waterdata.usgs.gov/
The legacy waterservices.usgs.gov API is being decommissioned in early 2027,
so this talks only to api.waterdata.usgs.gov.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

BASE_URL = os.environ.get("USGS_API_BASE", "https://api.waterdata.usgs.gov/ogcapi/v1")
DISCHARGE = "00060"   # cubic feet per second
GAGE_HEIGHT = "00065"  # feet
USER_AGENT = "bozeman-flows/1.0 (+https://github.com/hannahvozel/bozeman-flows)"

"""Create new class that inherits from RuntimeError."""
class USGSError(RuntimeError):
    pass


def parse_time(raw: str | None) -> datetime:
    """Parse USGS ISO-8601 times (with Z or an offset) into aware UTC datetimes."""
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_value(raw: Any) -> float | None:
    """USGS sends values as strings; sentinel negatives (e.g. -999999) mean no data."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if value < 0 else value


"""Easy to integrate API key if needed in the future, don't have to now."""
class USGSClient:
    def __init__(self, api_key: str | None = None, base_url: str = BASE_URL,
                 timeout: float = 30.0, retries: int = 3):
        self.api_key = api_key if api_key is not None else os.environ.get("USGS_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries

    # -- low level -----------------------------------------------------------
    def _get_json(self, url: str) -> dict[str, Any]:
        headers = {"Accept": "application/geo+json, application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        last_err: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                last_err = err
                # 429 = rate limited, 5xx = transient. Anything else won't fix itself.
                if err.code != 429 and err.code < 500:
                    raise USGSError(f"USGS API returned HTTP {err.code} for {url}") from err
            except (urllib.error.URLError, TimeoutError) as err:
                last_err = err
            time.sleep(2 ** attempt)
        raise USGSError(f"USGS API request failed after {self.retries} tries: {last_err}")

    def _items(self, collection: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield features from a collection, following rel=next pagination links."""
        query = {"f": "json", "skipGeometry": "true", **params}
        url = f"{self.base_url}/collections/{collection}/items?{urllib.parse.urlencode(query)}"
        while url:
            page = self._get_json(url)
            yield from page.get("features", [])
            url = next((l["href"] for l in page.get("links", []) if l.get("rel") == "next"), None)

    # -- public --------------------------------------------------------------
    def latest(self, usgs_ids: Iterable[str],
               parameter_codes: Iterable[str] = (DISCHARGE, GAGE_HEIGHT)) -> dict[str, dict[str, Any]]:
        """Most recent reading per site and parameter.

        Returns {"06043500": {"00060": {"value": 512.0, "time": "...", "approval": "Provisional"}, ...}}
        """
        ids = [f"USGS-{i}" for i in usgs_ids]
        out: dict[str, dict[str, Any]] = {}
        for feat in self._items("latest-continuous", {
            "monitoring_location_id": ",".join(ids),
            "parameter_code": ",".join(parameter_codes),
            "limit": 1000,
        }):
            p = feat.get("properties", {})
            site = p.get("monitoring_location_id", "").removeprefix("USGS-")
            reading = {
                "value": _parse_value(p.get("value")),
                "time": p.get("time"),
                "approval": p.get("approval_status"),
                "qualifier": p.get("qualifier"),
            }
            prev = out.setdefault(site, {}).get(p.get("parameter_code"))
            # A site can have more than one time series per parameter; keep the newest.
            if prev is None or parse_time(reading["time"]) > parse_time(prev["time"]):
                out[site][p.get("parameter_code")] = reading
        return out

    def series(self, usgs_ids: Iterable[str], period: str = "P7D",
               parameter_code: str = DISCHARGE) -> dict[str, list[tuple[str, float]]]:
        """Recent 15-minute readings, sorted by time: {"06043500": [(iso_time, cfs), ...]}."""
        ids = [f"USGS-{i}" for i in usgs_ids]
        out: dict[str, list[tuple[str, float]]] = {}
        for feat in self._items("continuous", {
            "monitoring_location_id": ",".join(ids),
            "parameter_code": parameter_code,
            "time": period,
            "properties": "monitoring_location_id,time,value",
            "limit": 10000,
        }):
            p = feat.get("properties", {})
            value = _parse_value(p.get("value"))
            if value is None or not p.get("time"):
                continue
            site = p.get("monitoring_location_id", "").removeprefix("USGS-")
            out.setdefault(site, []).append((p["time"], value))
        for points in out.values():
            points.sort(key=lambda pt: parse_time(pt[0]))
        return out
