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
  - Live event stream with type filtering
  - Group activity display with member lists
  - Adjustable time window (1 minute to 24 hours)

---

## Requirements

- Python 3.11 or newer
- JS8Call running and configured to send UDP decodes
- A modern browser (Chrome, Firefox, Edge, Safari)
- A HamQTH account — free, optional, recommended for non-US callsign grid lookups

---

## Step 1 — Check your Python version

Open a terminal (or Command Prompt on Windows) and run:

```bash
python3 --version
```

You need `3.11` or higher. If you see `3.9` or `3.10`, upgrade. If the command is not found, install Python first.

**Linux (Debian/Ubuntu):**
```bash
sudo apt update && sudo apt install python3 python3-pip python3-venv
```

**macOS:**
```bash
brew install python3
```
If you do not have Homebrew: https://brew.sh

**Windows:**
Download the installer from https://www.python.org/downloads/
During install, check **"Add Python to PATH"** — this is important.
After installing, use `python` instead of `python3` in all commands below.

---

## Step 2 — Get the code

**Option A — git (recommended):**
```bash
git clone https://github.com/miltonics/js8-tracker.git
cd js8-tracker
```

**Option B — download ZIP:**
Click the green **Code** button on GitHub, choose **Download ZIP**, extract it, and open a terminal in that folder.

---

## Step 3 — Create a virtual environment

A virtual environment is an isolated Python installation for this project. It keeps JS8-tracker's dependencies separate from the rest of your system so nothing conflicts. You only do this once.

**Linux / macOS:**
```bash
python3 -m venv venv
```

**Windows:**
```
python -m venv venv
```

You should now see a `venv` folder inside the project directory.

---

## Step 4 — Activate the virtual environment

You need to do this **every time** you open a new terminal to run JS8-tracker.

**Linux / macOS:**
```bash
source venv/bin/activate
```

**Windows:**
```
venv\Scripts\activate
```

When activated, your terminal prompt will show `(venv)` at the start. That means it is working.

To deactivate (optional, when done):
```bash
deactivate
```

---

## Step 5 — Install dependencies

With the venv activated:

```bash
pip install -r requirements.txt
```

This installs FastAPI, uvicorn, requests, and pydantic. Only needs to be done once (or after updates to requirements.txt).

If you see `pip: command not found`, try `pip3` instead.

---

## Step 6 — Configure your callsign

Open `js8_tracker_backend.py` in any text editor. Near the top, find:

```python
MYCALL = "KE8SWO"
```

Change `KE8SWO` to your own callsign. Save the file. That is the only required change.

---

## Step 7 — HamQTH credentials (optional but recommended)

Grid lookups use HamQTH first, then fall back to callook.info automatically. callook.info requires no account and covers US callsigns. For non-US callsigns, HamQTH is needed.

Register free at https://www.hamqth.com if you do not have an account.

**Linux / macOS** — create the credentials file:
```bash
mkdir -p ~/.config/js8_gt_bridge
nano ~/.config/js8_gt_bridge/hamqth.json
```

**Windows** — create the file at:
```
C:\Users\YOUR_USERNAME\.config\js8_gt_bridge\hamqth.json
```

Contents (replace with your actual credentials):
```json
{
  "user": "YOUR_HAMQTH_USERNAME",
  "password": "YOUR_HAMQTH_PASSWORD"
}
```

Alternatively, set environment variables:

**Linux / macOS:**
```bash
export HAMQTH_USER=your_username
export HAMQTH_PASS=your_password
```

**Windows:**
```
set HAMQTH_USER=your_username
set HAMQTH_PASS=your_password
```

---

## Step 8 — Configure JS8Call

In JS8Call: **File -> Settings -> Reporting**

- Check **"UDP Server"** to enable it
- Set host to `127.0.0.1`
- Set port to `2237`
- Click OK and restart JS8Call if prompted

**If you were using GridTracker:** GridTracker listens on the same UDP port. You cannot run both at the same time. Stop GridTracker before starting JS8-tracker.

---

## Step 9 — Run JS8-tracker

Start JS8Call first. Then in your terminal:

**Linux / macOS:**
```bash
cd js8-tracker
source venv/bin/activate
python3 js8_tracker_backend.py
```

**Windows:**
```
cd js8-tracker
venv\Scripts\activate
python js8_tracker_backend.py
```

You should see output like:
```
[js8-tracker] self-check passed
[js8-tracker] HamQTH credentials loaded for user: YOUR_CALL
[js8-tracker] own grid: XX99 (from HamQTH)
[js8-tracker] build tag: js8-tracker-0.3.1
[js8-tracker] starting HTTP on 127.0.0.1:5000
[js8-tracker] UDP listening on 127.0.0.1:2237
```

Open your browser to: **http://127.0.0.1:5000**

Stations and events appear as JS8Call decodes traffic. To stop: press `Ctrl+C`.

---

## Daily use

Every time you want to run JS8-tracker:

1. Start JS8Call
2. Open a terminal
3. `cd js8-tracker`
4. `source venv/bin/activate` (Linux/macOS) or `venv\Scripts\activate` (Windows)
5. `python3 js8_tracker_backend.py`
6. Open browser to http://127.0.0.1:5000

The database (`js8_tracker.db`) keeps history between sessions. Delete it to start fresh.

---

## UI overview

| Element | Description |
|---|---|
| Station list (left) | All heard stations, color-coded by hearing layer |
| Group strip | Active JS8 groups; click to select and highlight members on map |
| Map (center) | Station dots with animated directional connection lines |
| Connection strip (bottom of map) | Top connections by weight |
| Event stream (right) | Live decoded traffic; click a row to highlight stations on map |
| Layer toggles (header) | Show/hide direct, reported, and inferred stations |
| Window slider (header) | Time window from 1 minute to 24 hours, or All |
| Fit button (map) | Fit map view to show all known stations |

### Station colors

| Color | Meaning |
|---|---|
| Green | Directly decoded by your station |
| Amber | Reported (another station reported hearing this call) |
| Purple | Inferred (weak or ambiguous parse) |
| Cyan star | Your own station |

### Grid status in station list

| Label | Meaning |
|---|---|
| `EM96 TX` | Grid transmitted in the JS8 message |
| `EM96 LKP` | Grid from HamQTH or callook lookup |
| `grid pending 4s` | Lookup in progress; timer shows elapsed seconds |
| `no grid` | Lookup returned nothing for this callsign |

---

## Troubleshooting

**"No module named fastapi" or similar error**
The venv is not activated, or dependencies were not installed.
Run `source venv/bin/activate` then `pip install -r requirements.txt`.

**"Address already in use" on port 5000**
Another process is using port 5000. Either stop it, or change `HTTP_PORT = 5000` near the top of `js8_tracker_backend.py` to another port such as `5001`, then open `http://127.0.0.1:5001`.

**"Address already in use" on port 2237**
GridTracker or another listener is still running. Find it:
- Linux: `ss -ulnp | grep 2237`
- macOS: `lsof -iUDP:2237`
- Windows: `netstat -ano | findstr 2237`

**UI shows "Waiting for stations" and nothing appears**
- Check the terminal for lines like `[js8-tracker] decode text=...`
- If none appear, JS8Call is not sending UDP. Re-check Step 8.
- If decodes appear in terminal but not in the browser, open browser developer tools (F12 -> Console) and look for errors.

**"python3: command not found" on Windows**
Use `python` instead of `python3`. If that also fails, Python is not installed or not on your PATH. Re-run the installer and check "Add Python to PATH".

**Stations show "grid pending" for a long time**
- Open `http://127.0.0.1:5000/api/status` and check `hamqth_configured` and `lookup_errors`.
- callook.info covers US callsigns automatically with no account.
- Non-US callsigns without HamQTH will stay pending until they transmit a grid in a message.

**Self-check fails on startup**
This is a bug. Please open an issue on GitHub with the full terminal output.

---

## File structure

```
js8-tracker/
├── js8_tracker_backend.py   # Python backend — edit MYCALL here
├── js8_tracker_ui.html      # Browser UI — served by the backend
├── requirements.txt         # Python dependencies
├── README.md                # This file
└── js8_tracker.db           # SQLite database (created on first run, not in git)
```

---

## Versioning

Versions follow `0.phase.feature.patch`:

- `0` — pre-release / beta
- `phase` — major development phase
- `feature` — significant addition within a phase
- `patch` — bug fix or small correction

`1.0.0` will mark the first stable non-beta release.

---

## Known limitations

- callook.info covers US callsigns only. Non-US stations without HamQTH stay pending until they transmit a grid.
- The database grows indefinitely. Delete `js8_tracker.db` to start fresh. Automatic pruning is planned.
- Only one process can bind UDP port 2237 at a time.
- Windows support is functional but less tested than Linux.

---

## License

MIT — do what you like, attribution appreciated.
