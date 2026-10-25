"""Command line entry point.

    python -m riverwatch               # fetch, write snapshot, evaluate + send alerts
    python -m riverwatch --dry-run     # print emails instead of sending
    python -m riverwatch --no-alerts   # just refresh data/latest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import alerts, config, snapshot
from .usgs import USGSClient, USGSError

ROOT = config.REPO_ROOT


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="riverwatch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", type=Path, default=ROOT / "data" / "latest.json")
    ap.add_argument("--state", type=Path, default=ROOT / "data" / "alert_state.json")
    ap.add_argument("--alerts", type=Path, default=config.ALERTS_PATH)
    ap.add_argument("--period", default="P7D", help="history window for charts (ISO-8601 duration)")
    ap.add_argument("--dry-run", action="store_true", help="print alert emails, don't send or save state")
    ap.add_argument("--no-alerts", action="store_true", help="skip alert evaluation")
    args = ap.parse_args(argv)

    cfg = config.load()
    client = USGSClient()
    ids = list(cfg.sites)
    try:
        latest = client.latest(ids)
        series = client.series(ids, period=args.period)
    except USGSError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    snap = snapshot.build(cfg, latest, series)
    args.snapshot.parent.mkdir(parents=True, exist_ok=True)
    args.snapshot.write_text(json.dumps(snap, separators=(",", ":")))

    for usgs, s in snap["sites"].items():
        site = cfg.sites[usgs]
        c = s["classification"] or {}
        cfs = f"{s['cfs']:,.0f} cfs" if s["cfs"] is not None else "no data"
        print(f"{site.label:<48} {cfs:>11}  {c.get('label', '-'):<9} class {c.get('class', '-')}")

    if args.no_alerts:
        return 0
    alerts_cfg = alerts.load_json(args.alerts, {"rules": []})
    state = alerts.load_json(args.state, {"rules": {}})
    notes = alerts.evaluate(cfg, alerts_cfg, snap, state)
    for n in notes:
        print(f"alert {n.kind}: {n.rule_id} ({n.site_label} {n.cfs:,.0f} cfs) -> {len(n.recipients)} recipient(s)")
    sent = alerts.send(notes, alerts_cfg, dry_run=args.dry_run)
    print(f"{len(notes)} alert(s), {sent} email(s) {'previewed' if args.dry_run else 'sent'}")
    if not args.dry_run:
        args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
