"""Run with:  python -m unittest discover tests   (or pytest)"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from riverwatch import alerts, config, snapshot
from riverwatch.usgs import USGSClient

CFG = config.load()


def feature(site, pcode, value, time):
    return {"type": "Feature", "properties": {
        "monitoring_location_id": f"USGS-{site}", "parameter_code": pcode,
        "value": str(value), "time": time, "approval_status": "Provisional", "qualifier": None}}


class FakeClient(USGSClient):
    """Serves canned pages; page 1 links to page 2 to exercise pagination."""

    def __init__(self, pages):
        super().__init__(api_key="test")
        self.pages, self.urls = pages, []

    def _get_json(self, url):
        self.urls.append(url)
        return self.pages[len(self.urls) - 1]


class TestConfig(unittest.TestCase):
    def test_every_band_set_is_valid(self):
        self.assertGreaterEqual(len(CFG.sites), 7)

    def test_classify_edges(self):
        self.assertEqual(CFG.classify("06043500", 299)["level"], "very_low")
        self.assertEqual(CFG.classify("06043500", 300)["level"], "low")      # max is exclusive
        self.assertEqual(CFG.classify("06043500", 1000)["general"], "Medium")
        self.assertEqual(CFG.classify("06043500", 9000)["level"], "flood")
        self.assertIsNone(CFG.classify("06043500", None))

    def test_validate_rejects_unsorted_bands(self):
        raw = json.loads(config.RIVERS_PATH.read_text())
        raw["rivers"][0]["sites"][0]["bands"][1]["max"] = 10
        with self.assertRaises(ValueError):
            config.validate(raw)


class TestUSGS(unittest.TestCase):
    def test_latest_follows_pagination_and_keeps_newest(self):
        pages = [
            {"features": [feature("06043500", "00060", "512", "2026-10-25T17:45:00+00:00")],
             "links": [{"rel": "next", "href": "https://example/page2"}]},
            {"features": [feature("06043500", "00060", "-999999", "2026-10-25T16:00:00Z"),
                          feature("06043500", "00065", "2.41", "2026-10-25T17:45:00Z")],
             "links": []},
        ]
        client = FakeClient(pages)
        out = client.latest(["06043500"])
        self.assertEqual(out["06043500"]["00060"]["value"], 512.0)
        self.assertEqual(out["06043500"]["00065"]["value"], 2.41)
        self.assertIn("monitoring_location_id=USGS-06043500", client.urls[0])
        self.assertEqual(client.urls[1], "https://example/page2")

    def test_series_drops_sentinels_and_sorts(self):
        client = FakeClient([{"features": [
            feature("06041000", "00060", "1500", "2026-10-25T12:00:00Z"),
            feature("06041000", "00060", "1400", "2026-10-25T11:00:00Z"),
            feature("06041000", "00060", "-999999", "2026-10-25T13:00:00Z"),
        ], "links": []}])
        pts = client.series(["06041000"])["06041000"]
        self.assertEqual([v for _, v in pts], [1400.0, 1500.0])


class TestSnapshot(unittest.TestCase):
    def series(self, start, end, hours=30):
        t0 = datetime(2026, 10, 24, tzinfo=timezone.utc)
        n = hours * 4
        return [((t0 + timedelta(minutes=15 * i)).isoformat(), start + (end - start) * i / (n - 1))
                for i in range(n)]

    def test_trend_directions(self):
        self.assertEqual(snapshot.trend_24h(self.series(500, 700))["direction"], "rising")
        self.assertEqual(snapshot.trend_24h(self.series(700, 500))["direction"], "falling")
        self.assertEqual(snapshot.trend_24h(self.series(500, 505))["direction"], "steady")

    def test_downsample_hourly(self):
        self.assertEqual(len(snapshot.downsample_hourly(self.series(1, 2, hours=10))), 10)

    def test_build_handles_missing_sites(self):
        snap = snapshot.build(CFG, {"06043500": {"00060": {"value": 900.0, "time": "t"}}}, {})
        self.assertEqual(snap["sites"]["06043500"]["classification"]["level"], "medium")
        self.assertIsNone(snap["sites"]["06041000"]["cfs"])


class TestAlerts(unittest.TestCase):
    RULES = {"default_to": ["$ALERT_TO"], "hysteresis_pct": 5, "rules": [
        {"id": "window", "site": "06043500", "above": 700, "below": 1500, "notify_on_clear": True},
        {"id": "low", "site": "06205000", "below": 500, "to": ["paddler@example.com"]},
    ]}
    ENV = {"ALERT_TO": "a@example.com, b@example.com"}

    def run_at(self, state, gallatin, stillwater=800):
        snap = {"sites": {"06043500": {"cfs": gallatin}, "06205000": {"cfs": stillwater}}}
        return alerts.evaluate(CFG, self.RULES, snap, state, env=self.ENV)

    def test_fires_once_then_hysteresis_then_clears(self):
        state = {}
        first = self.run_at(state, 800)
        self.assertEqual([(n.rule_id, n.kind) for n in first], [("window", "triggered")])
        self.assertEqual(first[0].recipients, ["a@example.com", "b@example.com"])
        self.assertEqual(self.run_at(state, 900), [])     # still in window: no repeat email
        self.assertEqual(self.run_at(state, 680), [])     # dipped 3% under 700: hysteresis holds
        cleared = self.run_at(state, 600)
        self.assertEqual([(n.rule_id, n.kind) for n in cleared], [("window", "cleared")])

    def test_below_rule_and_literal_recipient(self):
        notes = self.run_at({}, 100, stillwater=450)
        low = [n for n in notes if n.rule_id == "low"][0]
        self.assertEqual(low.recipients, ["paddler@example.com"])

    def test_offline_gauge_keeps_state(self):
        state = {}
        self.run_at(state, 800)
        self.assertEqual(self.run_at(state, None), [])
        self.assertTrue(state["rules"]["window"]["active"])

    def test_dry_run_composes_one_email_per_recipient(self):
        notes = self.run_at({}, 800, stillwater=450)
        self.assertEqual(alerts.send(notes, self.RULES, dry_run=True), 3)

    def test_shipped_alerts_config_references_real_gauges(self):
        shipped = json.loads(config.ALERTS_PATH.read_text())
        for rule in shipped["rules"]:
            self.assertIn(rule["site"], CFG.sites, rule["id"])


if __name__ == "__main__":
    unittest.main()
