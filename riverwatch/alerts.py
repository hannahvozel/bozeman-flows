"""Threshold alerts: decide which rules just tripped, then email the right people.

Rules live in config/alerts.json. An alert fires once when its condition
*becomes* true (not on every run while it stays true), and hysteresis keeps a
flow hovering right at the threshold from sending a flurry of emails.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .config import RiverConfig


@dataclass
class Notification:
    rule_id: str
    usgs: str
    site_label: str
    run: str
    cfs: float
    kind: str  # "triggered" or "cleared"
    condition: str
    level: str
    rapid_class: str
    note: str = ""
    recipients: list[str] = field(default_factory=list)


def describe(rule: dict[str, Any]) -> str:
    lo, hi = rule.get("above"), rule.get("below")
    if lo is not None and hi is not None:
        return f"between {lo:,.0f} and {hi:,.0f} cfs"
    if lo is not None:
        return f"at or above {lo:,.0f} cfs"
    return f"below {hi:,.0f} cfs"


def condition_met(rule: dict[str, Any], cfs: float, was_active: bool, hysteresis_pct: float) -> bool:
    """`above` is inclusive, `below` is exclusive. Both together make a flow window.

    While a rule is active its bounds are relaxed by `hysteresis_pct`, so it only
    clears once the flow has moved meaningfully past the threshold.
    """
    lo, hi = rule.get("above"), rule.get("below")
    h = rule.get("hysteresis_pct", hysteresis_pct) / 100.0
    if was_active:
        lo = lo * (1 - h) if lo is not None else None
        hi = hi * (1 + h) if hi is not None else None
    return (lo is None or cfs >= lo) and (hi is None or cfs < hi)


def expand_recipients(entries: list[str], env: dict[str, str] | None = None) -> list[str]:
    """Literal addresses pass through; "$NAME" reads a comma-separated list from env (GitHub secrets)."""
    env = os.environ if env is None else env
    out: list[str] = []
    for entry in entries:
        if entry.startswith("$"):
            value = env.get(entry[1:], "")
            if not value:
                print(f"warning: recipient variable {entry} is not set; skipping", file=sys.stderr)
            out.extend(a.strip() for a in value.split(",") if a.strip())
        else:
            out.append(entry.strip())
    return list(dict.fromkeys(out))  # de-dupe, keep order


def evaluate(cfg: RiverConfig, alerts_cfg: dict[str, Any], snapshot: dict[str, Any],
             state: dict[str, Any], env: dict[str, str] | None = None) -> list[Notification]:
    """Compare current flows with each rule, update `state` in place, return notifications."""
    hysteresis = alerts_cfg.get("hysteresis_pct", 5)
    default_to = alerts_cfg.get("default_to", [])
    rule_state = state.setdefault("rules", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    notes: list[Notification] = []

    for rule in alerts_cfg.get("rules", []):
        rid, usgs = rule["id"], rule["site"]
        if usgs not in cfg.sites:
            print(f"warning: rule {rid} references unknown gauge {usgs}", file=sys.stderr)
            continue
        if rule.get("above") is None and rule.get("below") is None:
            print(f"warning: rule {rid} needs 'above' and/or 'below'", file=sys.stderr)
            continue
        cfs = (snapshot["sites"].get(usgs) or {}).get("cfs")
        if cfs is None:
            continue  # gauge offline or iced up: leave state alone
        prev = rule_state.get(rid, {"active": False})
        active = condition_met(rule, cfs, prev["active"], hysteresis)

        kind = None
        if active and not prev["active"]:
            kind = "triggered"
        elif not active and prev["active"] and rule.get("notify_on_clear", False):
            kind = "cleared"

        rule_state[rid] = {
            "active": active,
            "since": now if active != prev["active"] else prev.get("since", now),
            "last_cfs": cfs,
        }
        if kind:
            site = cfg.sites[usgs]
            c = cfg.classify(usgs, cfs) or {}
            notes.append(Notification(
                rule_id=rid, usgs=usgs, site_label=site.label, run=site.run, cfs=cfs, kind=kind,
                condition=describe(rule), level=c.get("label", "?"), rapid_class=c.get("class", "?"),
                note=rule.get("note", ""),
                recipients=expand_recipients(rule.get("to", default_to), env),
            ))
    return notes


# -- email -------------------------------------------------------------------

def compose(recipient: str, notes: list[Notification], sender: str, site_url: str,
            subject_prefix: str) -> EmailMessage:
    if len(notes) == 1:
        n = notes[0]
        verb = "is now" if n.kind == "triggered" else "is no longer"
        subject = f"{subject_prefix} {n.site_label} {verb} {n.condition}"
    else:
        subject = f"{subject_prefix} {len(notes)} river alerts"

    lines = []
    for n in notes:
        verb = "is now" if n.kind == "triggered" else "is no longer"
        lines.append(
            f"{n.site_label}: {n.cfs:,.0f} cfs, {verb} {n.condition}.\n"
            f"  Run: {n.run}\n"
            f"  Level: {n.level}  |  Est. rapid class: {n.rapid_class}\n"
            + (f"  Note: {n.note}\n" if n.note else "")
            + f"  USGS gauge: https://waterdata.usgs.gov/monitoring-location/USGS-{n.usgs}/\n"
        )
    footer = (
        (f"\nLive flows: {site_url}\n" if site_url else "")
        + "\nUSGS data are provisional. Rapid classes are estimates; scout and use your own judgment.\n"
    )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content("\n".join(lines) + footer)
    return msg


def send(notes: list[Notification], alerts_cfg: dict[str, Any], dry_run: bool = False) -> int:
    """Group notifications per recipient and send one email each. Returns emails sent."""
    by_recipient: dict[str, list[Notification]] = {}
    for n in notes:
        for r in n.recipients:
            by_recipient.setdefault(r, []).append(n)
    if not by_recipient:
        return 0

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("ALERT_FROM") or user or "river-alerts@localhost"
    site_url = os.environ.get("SITE_URL", "")
    prefix = alerts_cfg.get("subject_prefix", "[River Alert]")
    messages = [compose(r, ns, sender, site_url, prefix) for r, ns in by_recipient.items()]

    if dry_run:
        for m in messages:
            print("=" * 60, f"\nTo: {m['To']}\nSubject: {m['Subject']}\n\n{m.get_content()}")
        return len(messages)
    if not (user and password):
        raise RuntimeError("SMTP_USERNAME and SMTP_PASSWORD must be set to send email")

    context = ssl.create_default_context()
    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=context)
    with server:
        server.login(user, password)
        for m in messages:
            server.send_message(m)
    return len(messages)


def load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default
