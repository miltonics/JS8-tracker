# JS8-tracker

A local situational awareness tool for JS8Call. Listens to the JS8Call UDP decode stream, classifies traffic, looks up station grids, and serves a live browser UI showing who is on the air, who is connected to whom, and where they are on a map.

**This is not a logging tool or award tracker.** It is a real-time operator display — think SDR waterfall, not logbook.

---

## What it does

- Receives decoded JS8 messages from JS8Call over UDP
- Classifies each decode (heartbeat, SNR report, directed message, group broadcast, etc.)
- Looks up station grid squares via HamQTH and callook.info
- Stores station state, connections, and events in a local SQLite database
- Serves a browser UI at `http://127.0.0.1:5000` with:
  - Station list with hearing layer, grid, SNR, and age
  - Leaflet map with station dots and animated directional connection lines
  - Live event stream with filtering
  - Active group display
  - Adjustable time window (1 minute to 24 hours)

---

## Requirements

- Python 3.11 or newer
- JS8Call running and configured to output UDP decodes (default port 2237)
- A HamQTH account (free) — optional but recommended for grid lookups
- A modern browser

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/miltonics/js8-tracker.git
cd js8-tracker

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Open `js8_tracker_backend.py` in any text editor. Find this line near the top:

```python
MYCALL = "KE8SWO"
```

Change it to your own callsign. That is the only required change.

### HamQTH credentials (optional but recommended)

Grid lookups use HamQTH first, then fall back to callook.info automatically. callook.info requires no account and covers US callsigns. For non-US stations, HamQTH is needed.

Create the credentials file:

```bash
mkdir -p ~/.config/js8_gt_bridge
nano ~/.config/js8_gt_bridge/hamqth.json
```

Contents:

```json
{
  "user": "YOUR_HAMQTH_USERNAME",
  "password": "YOUR_HAMQTH_PASSWORD"
}
```

Alternatively, set environment variables:

```bash
export HAMQTH_USER=your_username
export HAMQTH_PASS=your_password
```

---

## Running

```bash
# Make sure JS8Call is running first, then:
cd js8-tracker
source venv/bin/activate
python js8_tracker_backend.py
```

Open your browser to: **http://127.0.0.1:5000**

The UI is served directly by the backend — no separate web server needed.

To stop: press `Ctrl+C`. The port is released immediately.

---

## JS8Call UDP setup

JS8Call must be configured to send UDP decodes to the port this tool listens on.

In JS8Call: `File → Settings → Reporting`

- Enable "UDP Server"
- Set host to `127.0.0.1`
- Set port to `2237`

If you were previously using GridTracker, it was likely consuming the same port. You cannot run both at the same time on the same port. Stop GridTracker before starting JS8-tracker, or configure JS8Call to forward to a different port.

---

## UI overview

| Element | Description |
|---|---|
| Station list (left) | All heard stations, color-coded by hearing layer |
| Map (center) | Station dots; animated lines show connections with direction of transmission |
| Event stream (right) | Live decoded traffic, filterable by type |
| Connection strip (bottom of map) | Top connections by weight |
| Layer toggles (header) | Show/hide direct, reported, and inferred stations |
| Window slider (header) | Time window from 1 minute to 24 hours, or All |
| ⊞ button (map) | Fit map to show all known stations |

### Station colors

| Color | Meaning |
|---|---|
| Green | Directly decoded (your station heard this callsign) |
| Amber | Reported (another station reported hearing this callsign) |
| Purple | Inferred (weak or ambiguous parse) |

### Grid status

| Label | Meaning |
|---|---|
| `EM96 TX` | Grid transmitted in the message |
| `EM96 LKP` | Grid from HamQTH or callook lookup |
| `grid pending… 4s` | Lookup in progress, timer shows elapsed seconds |
| `no grid` | Lookup returned nothing |

---

## File structure

```
js8-tracker/
├── js8_tracker_backend.py   # Python backend — edit MYCALL here
├── js8_tracker_ui.html      # Browser UI — served by the backend
├── requirements.txt         # Python dependencies
├── README.md                # This file
└── js8_tracker.db           # SQLite database (created on first run)
```

---

## Versioning

Versions follow `0.phase.feature.patch`:

- `0` — pre-release / beta
- `phase` — major phase of development (1=parser, 2=state, 3=UI, etc.)
- `feature` — significant addition within a phase
- `patch` — bug fix or small correction

`1.0.0` will mark the first stable release.

---

## Known limitations

- callook.info only covers US callsigns. Non-US stations without HamQTH credentials will stay "grid pending" until they transmit a grid themselves.
- The database is not pruned automatically. Run `python js8_tracker_backend.py --prune` (not yet implemented) or delete `js8_tracker.db` to start fresh.
- Only one process can bind UDP port 2237. Stop any other listeners (GridTracker, old bridge scripts) before starting.

---

## License

MIT — do what you like, attribution appreciated.
