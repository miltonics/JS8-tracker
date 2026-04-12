# JS8-tracker

A local situational awareness tool for JS8Call. Listens to the JS8Call decode stream, classifies traffic, looks up station grids, and serves a live browser UI showing who is on the air, who is connected to whom, and where they are on a map.

**This is not a logging tool or award tracker.** It is a real-time operator display — think SDR waterfall, not logbook.

---

## What it does

- Receives decoded JS8 messages from JS8Call over UDP (port 2237 — WSJT-X format)
- Connects to the JS8Call JSON API (port 2239) for richer data: spot grids, your own TX, band/frequency tracking
- Classifies each decode: heartbeat, SNR report, directed message, group broadcast, HEARING, relay, etc.
- Looks up station grid squares via HamQTH → callook.info → offline FCC database (fallback chain)
- Reassembles fragmented long messages before classification
- Stores station state, connections, groups, and events in a local SQLite database
- Automatically prunes old data (configurable retention)
- Serves a browser UI at `http://127.0.0.1:5000` with:
  - Station list with hearing layer, grid source, SNR, age, and band
  - Leaflet map with station dots, your own position, and animated directional connection lines
  - Live event stream with type filtering and click-to-highlight
  - Group activity display with member lists and map highlighting
  - Station detail panel with SNR sparkline, connection history, and grid records
  - Band filter with auto-follow (switches automatically when you QSY)
  - Adjustable time window
  - Drag-to-resize panels, collapsible panels, two-row header
  - Settings panel with font size, compact mode, connection fade

---

## Requirements

- Python 3.11 or newer
- JS8Call running and configured to send UDP decodes
- A modern browser (Chrome, Firefox, Edge, Safari)
- A HamQTH account — free, optional, recommended for non-US callsign grid lookups

---

## Step 1 — Check your Python version

Open a terminal and run:

```bash
python3 --version
```

You need `3.11` or higher.

**Linux (Debian/Ubuntu):**
```bash
sudo apt update && sudo apt install python3 python3-pip python3-venv
```

**macOS:**
```bash
brew install python3
```

**Windows:**
Download from https://www.python.org/downloads/ and check **"Add Python to PATH"** during install.
Use `python` instead of `python3` in all commands below.

---

## Step 2 — Get the code

**Option A — git:**
```bash
git clone https://github.com/miltonics/JS8-tracker.git
cd JS8-tracker
```

**Option B — download ZIP:**
Click the green **Code** button on GitHub, choose **Download ZIP**, extract it, open a terminal in that folder.

---

## Step 3 — Create a virtual environment

A virtual environment keeps JS8-tracker's dependencies separate from your system Python. You only do this once.

**Linux / macOS:**
```bash
python3 -m venv venv
```

**Windows:**
```
python -m venv venv
```

---

## Step 4 — Activate the virtual environment

**Every time** you open a new terminal to run JS8-tracker:

**Linux / macOS:**
```bash
source venv/bin/activate
```

**Windows:**
```
venv\Scripts\activate
```

Your prompt will show `(venv)` when activated. To deactivate: `deactivate`

---

## Step 5 — Install dependencies

With the venv activated:

```bash
pip install -r requirements.txt
```

---

## Step 6 — Configure your callsign

Open `js8_tracker_backend.py` in any text editor. Near the top:

```python
MYCALL = "KE8SWO"
```

Change it to your callsign. That is the only required change.

---

## Step 7 — HamQTH credentials (optional but recommended)

Grid lookups use HamQTH first, then callook.info (US, no account), then the offline FCC database. For non-US callsigns, HamQTH is the primary source.

Register free at https://www.hamqth.com

**Linux / macOS:**
```bash
mkdir -p ~/.config/js8_gt_bridge
nano ~/.config/js8_gt_bridge/hamqth.json
```

**Windows:** Create `C:\Users\YOUR_USERNAME\.config\js8_gt_bridge\hamqth.json`

Contents:
```json
{
  "user": "YOUR_HAMQTH_USERNAME",
  "password": "YOUR_HAMQTH_PASSWORD"
}
```

Or use environment variables: `HAMQTH_USER` and `HAMQTH_PASS`

---

## Step 8 — Offline FCC database (optional, US callsigns)

For offline grid lookups without internet (covers all licensed US amateurs):

```bash
python3 setup_fcc_db.py
```

This downloads ~35 MB from the FCC, imports it, and creates `fcc_offline.db`. Takes about 60-90 seconds. The FCC updates this data weekly — re-run with `--update` to refresh.

```bash
python3 setup_fcc_db.py --check    # show stats
python3 setup_fcc_db.py --update   # re-download fresh data
```

---

## Step 9 — Configure JS8Call

In JS8Call: **File → Settings → Reporting**

- Enable **UDP Server**, host `127.0.0.1`, port `2237`

In JS8Call: **File → Settings → (API section)**

- Enable **UDP Server API**, host `127.0.0.1`, port `2239`
- Check **Enable UDP Server API**

**If you were using GridTracker:** it listens on port 2237. You cannot run both at the same time. Stop GridTracker before starting JS8-tracker.

---

## Step 10 — Run

**Linux / macOS:**
```bash
cd JS8-tracker
source venv/bin/activate
python3 js8_tracker_backend.py
```

**Windows:**
```
cd JS8-tracker
venv\Scripts\activate
python js8_tracker_backend.py
```

Expected startup output:
```
[js8-tracker] self-check passed
[js8-tracker] HamQTH credentials loaded for user: YOUR_CALL
[js8-tracker] FCC offline database: fcc_offline.db
[js8-tracker] own grid: XX99 (from HamQTH)
[js8-tracker] JS8Call API listening on 127.0.0.1:2239
[js8-tracker] UDP listening on 127.0.0.1:2237
```

Open browser to: **http://127.0.0.1:5000**

To stop: `Ctrl+C`

---

## Daily use

1. Start JS8Call
2. Open a terminal → `cd JS8-tracker` → `source venv/bin/activate`
3. `python3 js8_tracker_backend.py`
4. Open browser to http://127.0.0.1:5000

The database keeps history between sessions and prunes automatically.

---

## UI overview

### Header
| Element | Description |
|---|---|
| Row 1 | Logo, stats, layer toggles, window, band filter, settings ⚙, collapse ▲ |
| Row 2 | Panel toggles: Stations / Map / Events + Compact button |
| ▲ / ▼ | Collapse/expand header row 2 |
| ⚙ | Settings: font size, compact mode, connection fade |

### Panels
| Panel | Description |
|---|---|
| Stations (left) | All heard stations with grid, SNR, age, band badge |
| Map (center) | Station dots, animated directional connection lines, fit-all button |
| Events (right) | Live decoded traffic; click a row to highlight stations on map |

Panels can be shown/hidden using Row 2 toggle buttons. Drag the handles between panels to resize. Settings and panel layout persist across sessions.

### Station colors
| Color | Meaning |
|---|---|
| Green | Directly decoded |
| Amber | Reported (another station reported hearing this call) |
| Purple | Inferred |
| Cyan ★ | Your own station |

### Grid status
| Label | Meaning |
|---|---|
| `EM96 TX` | Transmitted in JS8 message |
| `EM96 LKP` | From HamQTH, callook, or FCC offline lookup |
| `grid pending 4s` | Lookup in progress |
| `no grid` | Not found in any source |

### Map lines
Animated dashed lines show connections. Direction of animation = direction of transmission. Color matches hearing layer. Lines fade after 5 minutes (configurable in settings).

### Station detail panel
Click any station (in list, on map, or in group member list) to open a detail panel showing: grid history, SNR sparkline, connections, and recent events. Click again or ✕ to close and restore the previous map view.

### Group panel
Click a group pill (e.g. `@HB`, `@MAGNET`) to highlight all members on the map and fly to fit them. Click again or ✕ to restore the previous map view.

---

## Troubleshooting

**"No module named fastapi"**
The venv is not activated. Run `source venv/bin/activate` then `pip install -r requirements.txt`.

**Port 5000 already in use**
```bash
fuser -k 5000/tcp
```
Or change `HTTP_PORT` in the backend file.

**Port 2237 already in use**
GridTracker or another listener is running. Find it:
- Linux: `ss -ulnp | grep 2237`
- macOS: `lsof -iUDP:2237`
- Windows: `netstat -ano | findstr 2237`

**UI shows nothing / waiting for stations**
- Check terminal for `[js8-tracker] decode text=...` lines
- If none appear, JS8Call UDP is not configured (Step 9)
- If decodes appear but not in browser, press F12 → Console for errors

**Stations show "grid pending" for a long time**
- Check `/api/status` for `hamqth_configured` and `lookup_errors`
- US callsigns: run `python3 setup_fcc_db.py` for offline fallback
- Non-US without HamQTH: stays pending until station transmits a grid

---

## File structure

```
JS8-tracker/
├── js8_tracker_backend.py   # Python backend — edit MYCALL here
├── js8_tracker_ui.html      # Browser UI — served at http://127.0.0.1:5000
├── setup_fcc_db.py          # One-time FCC offline database setup
├── requirements.txt         # Python dependencies
├── setup/
│   └── js8tracker.service   # systemd user service template
├── README.md                # This file
└── fcc_offline.db           # FCC database (created by setup_fcc_db.py, not in git)
```

---

## Automatic startup (Linux — systemd)

To start JS8-tracker automatically at login:

```bash
# Edit paths in setup/js8tracker.service to match your system
mkdir -p ~/.config/systemd/user
cp setup/js8tracker.service ~/.config/systemd/user/
systemctl --user enable js8tracker
systemctl --user start js8tracker
```

Check status: `systemctl --user status js8tracker`
View logs: `journalctl --user -u js8tracker -f`

---

## Configuration

All tunable constants are near the top of `js8_tracker_backend.py`:

| Setting | Default | Description |
|---|---|---|
| `MYCALL` | `KE8SWO` | Your callsign — **change this** |
| `HTTP_PORT` | `5000` | Web UI port |
| `UDP_PORT` | `2237` | JS8Call WSJT-X UDP port |
| `JS8CALL_API_PORT` | `2239` | JS8Call JSON API port |
| `DB_PRUNE_EVENTS_DAYS` | `7` | Keep events for this many days |
| `DB_PRUNE_STATIONS_DAYS` | `30` | Keep stations for this many days |
| `DB_PRUNE_INTERVAL_HOURS` | `1` | How often to prune |
| `HAMQTH_CACHE_TTL` | `7 days` | Grid lookup cache lifetime |

---

## Versioning

Versions follow `0.phase.feature.patch`:
- `0` — pre-release / beta
- `phase` — major development phase
- `feature` — significant addition
- `patch` — bug fix

`1.0.0` = first stable non-beta release.

---

## Known limitations

- callook.info covers US callsigns only. Non-US stations without HamQTH or a transmitted grid stay pending.
- The JS8Call API (port 2239) must have "Enable UDP Server API" checked in JS8Call settings.
- Only one process can bind UDP port 2237 at a time — stop GridTracker before running.
- Windows support is functional but less tested than Linux.
- Long JS8Call messages (e.g. HEARING with many callsigns) are reassembled from fragments — the full station list may not always be recovered depending on how JS8Call splits the packet.

---

## License

MIT — do what you like, attribution appreciated.
