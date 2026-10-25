# Bozeman Flow Report

Live USGS river flows for whitewater runs near Bozeman, Montana, sorted into
flow levels (low / medium / high) and an estimated rapid class. Free to host on
GitHub Pages, with hourly email alerts from GitHub Actions.

| River | Gauge | Run |
|---|---|---|
| Gallatin | [06043500](https://waterdata.usgs.gov/monitoring-location/USGS-06043500/) near Gallatin Gateway | Gallatin Canyon (Mad Mile, House Rock) |
| Gallatin | [06043120](https://waterdata.usgs.gov/monitoring-location/USGS-06043120/) above Deer Creek, near Big Sky | Upper canyon |
| Madison | [06041000](https://waterdata.usgs.gov/monitoring-location/USGS-06041000/) below Ennis Lake | Bear Trap Canyon (Kitchen Sink) |
| Yellowstone | [06191500](https://waterdata.usgs.gov/monitoring-location/USGS-06191500/) at Corwin Springs | Yankee Jim Canyon |
| Yellowstone | [06192500](https://waterdata.usgs.gov/monitoring-location/USGS-06192500/) near Livingston | Paradise Valley / town float |
| Stillwater | [06205000](https://waterdata.usgs.gov/monitoring-location/USGS-06205000/) near Absarokee | Lower Stillwater |
| Boulder | [06200000](https://waterdata.usgs.gov/monitoring-location/USGS-06200000/) at Big Timber | Speculator Creek to Chippy Park |

## How it works

```
                 every page load                         every hour
 browser ──────► api.waterdata.usgs.gov ◄────── GitHub Action (python -m riverwatch)
    │                (live readings)                     │
    │  if USGS doesn't answer                            ├─► data/latest.json   (fallback snapshot)
    └─────────────► data/latest.json                     ├─► data/alert_state.json
                                                         └─► email via SMTP when a threshold is crossed
 config/rivers.json  ← one file of gauges + flow bands, read by both the site and the Python job
```

- **Live on refresh.** The page calls the USGS Water Data API directly from the
  browser every time it loads (or when you hit Refresh). If that fails, it shows
  the hourly snapshot and says so.
- **New USGS API.** Uses `api.waterdata.usgs.gov`, not the legacy
  `waterservices.usgs.gov`, which USGS is retiring in early 2027.
- **No build step, no dependencies.** Plain HTML/CSS/JS; the charts are hand-drawn
  SVG. The Python package uses only the standard library.
- **Shareable views.** The URL keeps your selection, e.g.
  `?mode=compare&rivers=gallatin,madison,yellowstone` or `?mode=one&rivers=madison`.

## Put it on GitHub

1. Create a new repo and push this folder to it.
2. **Settings → Pages →** Source: *Deploy from a branch*, Branch: `main`, folder `/ (root)`.
   Your site will be at `https://<you>.github.io/<repo>/`.
3. **Actions tab →** enable workflows, then run **Refresh flows & send alerts**
   once by hand (tick *dry run* the first time) to create `data/latest.json`.

## Set up email alerts

Add these under **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Example | Notes |
|---|---|---|
| `SMTP_USERNAME` | `you@gmail.com` | |
| `SMTP_PASSWORD` | `abcd efgh ijkl mnop` | For Gmail, use an [App Password](https://myaccount.google.com/apppasswords) (needs 2-Step Verification), not your normal password. |
| `ALERT_TO` | `you@gmail.com, buddy@example.com` | Comma-separated. Kept secret so addresses aren't public. |
| `SMTP_HOST` | `smtp.gmail.com` | Optional; defaults to Gmail. |
| `SMTP_PORT` | `587` | Optional; `465` for SSL. |
| `ALERT_FROM` | `River Bot <you@gmail.com>` | Optional. |
| `USGS_API_KEY` | | Optional; free at [api.waterdata.usgs.gov/signup](https://api.waterdata.usgs.gov/signup/) for higher rate limits. |

Then edit `config/alerts.json`:

```json
{ "id": "gallatin-canyon-in", "site": "06043500", "above": 700, "below": 1500,
  "note": "Gallatin Canyon is in the sweet spot.", "notify_on_clear": true }
```

- `above` (inclusive) and/or `below` (exclusive), in cfs. Both together = a flow window.
- `to` overrides `default_to` per rule: literal addresses, or `"$SECRET_NAME"` to read
  a comma-separated list from another secret (add it to the workflow's `env:` block too).
- An alert emails **once when it starts being true**, not every hour. `hysteresis_pct`
  (default 5%) stops a flow hovering at the line from sending repeat emails.
  `notify_on_clear` also emails when it stops being true.
- One email per recipient per run, even if several rules fire.

## Tune the flow bands

Everything about levels and classes lives in `config/rivers.json`. Each gauge has six
bands (`too_low`, `low`, `medium`, `high`, `very_high`, `flood`), each with a `max` cfs, an
estimated `class` and a `note`. **The shipped numbers are starting estimates** pieced
together from American Whitewater, local outfitters and guidebook beta; adjust them to
what you and your crew see on the water. Add a river by adding an entry with its USGS
site number; the site and alerts pick it up automatically.

## Run locally

```bash
python -m http.server 8000          # then open http://localhost:8000
python -m riverwatch --dry-run      # fetch flows, print what alerts would send
python -m unittest discover tests   # tests (pytest works too)
```

Open the site through a server, not by double-clicking `index.html`; browsers block
loading `config/rivers.json` from `file://`.

## Notes

- Recent USGS data are **provisional** and can be revised. Gauges ice up in winter and
  may show no reading.
- GitHub may delay scheduled runs by several minutes at busy times.
- Rapid classes are estimates. Scout, boat with a crew, and make your own call.
