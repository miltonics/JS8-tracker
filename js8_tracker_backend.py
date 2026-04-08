from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional, Literal

import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

# ============================================================
# JS8-tracker backend
#
# User config: set MYCALL near the top of this file.
#
# Features:
# - UDP listener for JS8Call WSJT-X decode stream
# - Event classifier (heartbeat, SNR, directed, group, hearing, etc.)
# - SQLite storage: events, stations, connections, groups
# - HamQTH + callook.info grid lookup with background worker
# - Automatic DB pruning (configurable retention windows)
# - FastAPI REST endpoints + serves browser UI at /
# - CORS enabled for local network access
#
# Versioning: 0.phase.feature.patch — 1.0.0 = first stable release
# ============================================================

UDP_HOST = "127.0.0.1"
UDP_PORT = 2237
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 5000
FORWARD_UDP_ENABLED = False
FORWARD_UDP_HOST = "127.0.0.1"
FORWARD_UDP_PORT = 2240
DB_PATH = "js8_tracker_phase2.db"
BUILD_TAG = "js8-tracker-0.3.4"

# Database pruning — old rows are deleted automatically in the background
DB_PRUNE_EVENTS_DAYS      = 7   # keep events for this many days
DB_PRUNE_CONNECTIONS_DAYS = 7   # prune connections not seen for this long
DB_PRUNE_STATIONS_DAYS    = 30  # prune stations not heard for this long
DB_PRUNE_INTERVAL_HOURS   = 1   # how often to run pruning

# HamQTH — same credential file the old bridge used
HAMQTH_CRED_FILE = Path.home() / ".config" / "js8_gt_bridge" / "hamqth.json"
HAMQTH_LOOKUP_TIMEOUT = 6          # seconds per HTTP request
HAMQTH_CACHE_TTL = 7 * 24 * 3600  # 7 days in seconds
HAMQTH_FAIL_TTL  = 3600            # suppress re-lookup of unknown calls for 1 hour

# ── USER CONFIG ───────────────────────────────────────────
# Set your callsign here. This is the only line most users need to change.
MYCALL = "KE8SWO"

# WSJT-X / JS8 UDP constants
MAGIC = 0xADBCCBDA
TYPE_HEARTBEAT = 0
TYPE_STATUS = 1
TYPE_DECODE = 2
TYPE_CLEAR = 3
TYPE_REPLY = 4
TYPE_QSO_LOGGED = 5
TYPE_CLOSE = 6
TYPE_REPLAY = 7
TYPE_HALT_TX = 8
TYPE_FREE_TEXT = 9
TYPE_WSPR_DECODE = 10
TYPE_LOCATION = 11
TYPE_LOGGED_ADIF = 12
TYPE_HIGHLIGHT_CALLSIGN = 13
TYPE_SWITCH_CONFIG = 14
TYPE_CONFIGURE = 15

PACKET_TYPE_NAMES = {
    TYPE_HEARTBEAT: "heartbeat",
    TYPE_STATUS: "status",
    TYPE_DECODE: "decode",
    TYPE_CLEAR: "clear",
    TYPE_REPLY: "reply",
    TYPE_QSO_LOGGED: "qso_logged",
    TYPE_CLOSE: "close",
    TYPE_REPLAY: "replay",
    TYPE_HALT_TX: "halt_tx",
    TYPE_FREE_TEXT: "free_text",
    TYPE_WSPR_DECODE: "wspr_decode",
    TYPE_LOCATION: "location",
    TYPE_LOGGED_ADIF: "logged_adif",
    TYPE_HIGHLIGHT_CALLSIGN: "highlight_callsign",
    TYPE_SWITCH_CONFIG: "switch_config",
    TYPE_CONFIGURE: "configure",
}

CONF_NONE = "none"
CONF_LOW = "low"
CONF_MED = "medium"
CONF_HIGH = "high"

GROUP_TOKEN_RE = re.compile(r"@([A-Z0-9_]+)", re.I)
GRID_RE = re.compile(r"\b([A-R]{2}\d{2}(?:[A-X]{2})?)\b", re.I)
NONCALL_WORDS = {
    "CQ", "QRZ", "DE", "HB", "HEARTBEAT", "SNR", "ACK", "ACK?", "INFO", "INFO?",
    "GRID", "GRID?", "STATUS", "MSG", "MSGS", "MSGS?", "QUERY", "QUERY?",
    "NO", "YES", "HEARING", "HEARING?", "ALLCALL", "APRSIS", "TO", "FROM",
    "CALL", "CALL?", "QSO", "73", "RR", "RRR", "FB", "GE", "GM", "GA",
    "GN", "TU", "PSE", "AGN", "SRI", "TEST", "DX", "QRP", "OM", "YL",
    "ANY", "LUC", "RELAY", "Q", "SNR?"
}

# Grid source priority — lower index = higher priority
GRID_SOURCE_PRIORITY = ["transmitted", "reported", "lookup", "inferred", "unknown"]

# Base connection weights by event type
CONNECTION_BASE_WEIGHTS: dict[str, float] = {
    "heartbeat_direct":  1.0,
    "activity_direct":   1.0,
    "directed_message":  0.9,
    "directed_ack":      0.9,
    "directed_query":    0.9,
    "directed_info":     0.9,
    "directed_grid":     0.9,
    "directed_status":   0.9,
    "directed_relay":    0.9,
    "directed_hearing":        0.9,  # explicit confirmed hearing
    "directed_hearing_query":  0.7,
    "directed_call":     0.8,
    "heartbeat_report":  0.8,
    "snr_report":        0.8,
    "broadcast_group":   0.7,
    "activity_inferred": 0.3,
    "broadcast_cq":      0.5,
    "unknown":           0.2,
}
WEIGHT_INCREMENT = 0.05   # per repeat
WEIGHT_CAP = 1.0


# ============================================================
# Enums + dataclasses
# ============================================================

class EventType(str, Enum):
    HEARTBEAT_DIRECT = "heartbeat_direct"
    HEARTBEAT_REPORT = "heartbeat_report"
    SNR_REPORT = "snr_report"
    DIRECTED_MESSAGE = "directed_message"
    DIRECTED_ACK = "directed_ack"
    DIRECTED_QUERY = "directed_query"
    DIRECTED_INFO = "directed_info"
    DIRECTED_GRID = "directed_grid"
    DIRECTED_STATUS = "directed_status"
    DIRECTED_RELAY  = "directed_relay"
    DIRECTED_HEARING       = "directed_hearing"
    DIRECTED_HEARING_QUERY = "directed_hearing_query"
    DIRECTED_CALL           = "directed_call"
    BROADCAST_GROUP = "broadcast_group"
    BROADCAST_CQ = "broadcast_cq"
    ACTIVITY_DIRECT = "activity_direct"
    ACTIVITY_INFERRED = "activity_inferred"
    UNKNOWN = "unknown"


HearingLayer = Literal["direct", "reported", "inferred"]
Confidence = Literal["none", "low", "medium", "high"]
GridSource = Literal["transmitted", "reported", "lookup", "inferred", "unknown"]


@dataclass
class ParsedEvent:
    id: str
    timestamp: str
    raw_text: str
    event_type: str
    source_station: Optional[str]
    target_station: Optional[str]
    group_name: Optional[str]
    snr: Optional[int]
    hearing_layer: HearingLayer
    confidence: Confidence
    grid_in_text: Optional[str]
    parser_note: Optional[str]


@dataclass
class PacketParseResult:
    text: Optional[str]
    packet_type: Optional[int]
    packet_type_name: Optional[str]
    parse_error: bool


# ============================================================
# Pydantic response models
# ============================================================

class StatusResponse(BaseModel):
    app_name: str
    build_tag: str
    udp_host: str
    udp_port: int
    http_host: str
    http_port: int
    started_at: str
    total_events: int
    high_confidence: int
    medium_confidence: int
    low_confidence: int
    dropped_lines: int
    ignored_packets: int
    decode_packets: int
    non_decode_packets: int
    parse_errors: int
    empty_decodes: int
    last_event_at: Optional[str]
    last_decode_text: Optional[str]
    last_packet_type: Optional[str]
    lookup_hits: int
    lookup_misses: int
    lookup_errors: int
    lookup_queue_depth: int
    hamqth_configured: bool
    my_call: str
    my_grid: Optional[str]
    last_prune_at: Optional[str]
    last_prune_deleted: int


class EventResponse(BaseModel):
    id: str
    timestamp: str
    raw_text: str
    event_type: str
    source_station: Optional[str]
    target_station: Optional[str]
    group_name: Optional[str]
    snr: Optional[int]
    hearing_layer: str
    confidence: str
    grid_in_text: Optional[str]
    parser_note: Optional[str]


class GridRecordResponse(BaseModel):
    grid: str
    source: str
    observed_at: str
    confidence: str


class StationResponse(BaseModel):
    callsign: str
    best_grid: Optional[str]
    best_grid_source: str
    last_heard_at: Optional[str]
    last_snr: Optional[int]
    snr_min: Optional[int]
    snr_max: Optional[int]
    hearing_layer: str
    confidence: str
    event_count: int
    active_groups: list[str]
    is_local: bool
    grid_status: str


class ConnectionResponse(BaseModel):
    source: str
    target: str
    target_type: str
    connection_type: str
    first_seen_at: str
    last_seen_at: str
    count: int
    last_snr: Optional[int]
    weight: float
    hearing_layer: str
    confidence: str


class GroupResponse(BaseModel):
    name: str
    last_activity_at: Optional[str]
    active_station_count: int
    event_count: int
    confidence: str
    members: list[str]  # callsigns active in this group within window


# ============================================================
# App + shared state
# ============================================================

app = FastAPI(title="JS8-tracker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STARTED_AT = datetime.now(timezone.utc).isoformat()
STATUS_LOCK = threading.Lock()
STATUS: dict = {
    "total_events": 0,
    "high_confidence": 0,
    "medium_confidence": 0,
    "low_confidence": 0,
    "dropped_lines": 0,
    "ignored_packets": 0,
    "decode_packets": 0,
    "non_decode_packets": 0,
    "parse_errors": 0,
    "empty_decodes": 0,
    "last_event_at": None,
    "last_decode_text": None,
    "last_packet_type": None,
    "lookup_hits": 0,
    "lookup_misses": 0,
    "lookup_errors": 0,
    "lookup_queue_depth": 0,
    "my_call": "",
    "my_grid": None,
    "last_prune_at": None,
    "last_prune_deleted": 0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# HamQTH credential loader  (same file as old bridge)
# ============================================================

def load_hamqth_credentials() -> tuple[str, str]:
    """Load from env vars first, then ~/.config/js8_gt_bridge/hamqth.json."""
    env_user = os.environ.get("HAMQTH_USER", "").strip()
    env_pass = os.environ.get("HAMQTH_PASS", "").strip()
    if env_user and env_pass:
        return env_user, env_pass

    if HAMQTH_CRED_FILE.exists():
        try:
            data = json.loads(HAMQTH_CRED_FILE.read_text())
            user = str(data.get("user", "")).strip()
            password = str(data.get("password", "")).strip()
            if user and password:
                return user, password
        except Exception as exc:
            print(f"[js8-tracker] WARNING: could not read HamQTH creds: {exc}", flush=True)

    return "", ""


# ============================================================
# HamQTH XML API client  (ported from old bridge v4)
# ============================================================

class HamQTHClient:
    def __init__(self, user: str, password: str) -> None:
        self.user = user
        self.password = password
        self.session_id: Optional[str] = None
        self.session_expires_at: float = 0.0
        self.http = requests.Session()

    def _ensure_session(self) -> None:
        if not self.user or not self.password:
            raise RuntimeError("HamQTH credentials not configured")
        now = time.time()
        if self.session_id and now < self.session_expires_at - 30:
            return

        r = self.http.get(
            "https://www.hamqth.com/xml.php",
            params={"u": self.user, "p": self.password},
            timeout=HAMQTH_LOOKUP_TIMEOUT,
        )
        r.raise_for_status()
        root = ET.fromstring(r.text)

        sid = None
        err = None
        for el in root.iter():
            tag = el.tag.lower()
            if tag.endswith("session_id"):
                sid = (el.text or "").strip()
            if tag.endswith("error"):
                err = (el.text or "").strip()

        if err:
            raise RuntimeError(f"HamQTH login error: {err}")
        if not sid:
            raise RuntimeError("HamQTH login failed: no session_id returned")

        self.session_id = sid
        self.session_expires_at = now + 3600

    def lookup_grid(self, callsign: str) -> Optional[str]:
        self._ensure_session()
        r = self.http.get(
            "https://www.hamqth.com/xml.php",
            params={"id": self.session_id, "callsign": callsign},
            timeout=HAMQTH_LOOKUP_TIMEOUT,
        )
        r.raise_for_status()
        root = ET.fromstring(r.text)

        grid = None
        err = None
        for el in root.iter():
            tag = el.tag.lower()
            if tag.endswith("grid"):
                grid = (el.text or "").strip()
            if tag.endswith("error"):
                err = (el.text or "").strip()

        if err:
            return None
        if grid and GRID_RE.match(grid):
            return grid.upper()
        return None


# ============================================================
# callook.info fallback lookup (no auth required)
# ============================================================

def callook_lookup_grid(callsign: str) -> Optional[str]:
    """
    Free fallback lookup via callook.info JSON API.
    No authentication needed. US callsigns only.
    Returns grid square or None.
    """
    try:
        r = requests.get(
            f"https://callook.info/{callsign}/json",
            timeout=HAMQTH_LOOKUP_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("status") != "VALID":
            return None
        grid = (data.get("location") or {}).get("gridsquare", "").strip()
        if grid and GRID_RE.match(grid):
            return grid.upper()
    except Exception:
        pass
    return None


# ============================================================
# Grid cache  (thread-safe, TTL-based)
# ============================================================

class GridCache:
    """
    Stores successful lookups for HAMQTH_CACHE_TTL seconds.
    Stores failed lookups (None) for HAMQTH_FAIL_TTL seconds
    so we don't hammer HamQTH for calls that aren't in the database.
    """
    def __init__(self) -> None:
        self._data: dict[str, tuple[Optional[str], float]] = {}
        self._lock = threading.Lock()

    def get(self, callsign: str) -> tuple[bool, Optional[str]]:
        """Returns (found_in_cache, grid_or_None)."""
        now = time.time()
        key = callsign.upper()
        with self._lock:
            if key in self._data:
                grid, ts = self._data[key]
                ttl = HAMQTH_CACHE_TTL if grid else HAMQTH_FAIL_TTL
                if now - ts < ttl:
                    return True, grid
                del self._data[key]
        return False, None

    def put(self, callsign: str, grid: Optional[str]) -> None:
        with self._lock:
            self._data[callsign.upper()] = (grid, time.time())


# ============================================================
# Background lookup worker
# ============================================================

_lookup_queue: queue.Queue[str] = queue.Queue(maxsize=500)
_grid_cache = GridCache()
_hamqth_client: Optional[HamQTHClient] = None


def _apply_lookup_grid(callsign: str, grid: str, timestamp: str) -> None:
    """Write a lookup-sourced grid into stations + station_grids."""
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("SELECT best_grid, best_grid_source FROM stations WHERE callsign = ?", (callsign,))
        row = cur.fetchone()
        if row is None:
            con.close()
            return  # station disappeared; skip

        current_grid, current_source = row
        current_source = current_source or "unknown"

        # Only write if lookup beats or equals current source
        if current_grid is None or grid_source_beats("lookup", current_source):
            cur.execute("""
                UPDATE stations SET best_grid = ?, best_grid_source = ?, grid_status = 'found'
                WHERE callsign = ?
            """, (grid, "lookup", callsign))

        # Always record in station_grids
        cur.execute("""
            INSERT INTO station_grids (callsign, grid, source, observed_at, confidence)
            VALUES (?, ?, 'lookup', ?, 'medium')
            ON CONFLICT(callsign, grid, source) DO UPDATE SET
                observed_at = excluded.observed_at
        """, (callsign, grid, timestamp))

        con.commit()
        with STATUS_LOCK:
            STATUS["lookup_hits"] += 1
    finally:
        con.close()


def _lookup_worker() -> None:
    """Background thread: drains the lookup queue, calls HamQTH then callook fallback."""
    while True:
        callsign = _lookup_queue.get()
        try:
            # Check cache first — may already be resolved by a prior event
            found, grid = _grid_cache.get(callsign)
            if found:
                if grid:
                    _apply_lookup_grid(callsign, grid, utc_now())
                continue

            # Try HamQTH first
            grid = None
            try:
                if _hamqth_client:
                    grid = _hamqth_client.lookup_grid(callsign)
                    if grid:
                        print(f"[js8-tracker] lookup {callsign} -> {grid} (HamQTH)", flush=True)
            except Exception as exc:
                print(f"[js8-tracker] HamQTH error for {callsign}: {exc}", flush=True)

            # Fallback to callook.info if HamQTH returned nothing
            if not grid:
                try:
                    grid = callook_lookup_grid(callsign)
                    if grid:
                        print(f"[js8-tracker] lookup {callsign} -> {grid} (callook)", flush=True)
                except Exception as exc:
                    print(f"[js8-tracker] callook error for {callsign}: {exc}", flush=True)

            # Store result and update DB
            _grid_cache.put(callsign, grid)
            if grid:
                _apply_lookup_grid(callsign, grid, utc_now())
            else:
                print(f"[js8-tracker] lookup {callsign} -> not found", flush=True)
                with STATUS_LOCK:
                    STATUS["lookup_misses"] += 1
                con2 = sqlite3.connect(DB_PATH)
                try:
                    con2.execute(
                        "UPDATE stations SET grid_status = 'not_found' "
                        "WHERE callsign = ? AND best_grid IS NULL",
                        (callsign,)
                    )
                    con2.commit()
                finally:
                    con2.close()

        except Exception as exc:
            print(f"[js8-tracker] lookup worker unexpected error: {exc}", flush=True)
        finally:
            _lookup_queue.task_done()

        time.sleep(0.1)


def _register_local_station() -> None:
    """Insert own callsign into stations table as local station on startup."""
    now = utc_now()
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("SELECT callsign FROM stations WHERE callsign = ?", (MYCALL,))
        if cur.fetchone() is None:
            cur.execute("""
                INSERT INTO stations
                    (callsign, best_grid, best_grid_source, last_heard_at,
                     snr_min, snr_max, last_snr, hearing_layer, confidence,
                     event_count, active_groups, is_local, grid_status)
                VALUES (?, NULL, 'unknown', ?, NULL, NULL, NULL, 'direct', 'high', 0, '', 1, 'pending')
            """, (MYCALL, now))
        else:
            cur.execute("UPDATE stations SET is_local = 1 WHERE callsign = ?", (MYCALL,))
        con.commit()
        print(f"[js8-tracker] registered local station: {MYCALL}", flush=True)
    finally:
        con.close()


def enqueue_lookup(callsign: str) -> None:
    """Queue a callsign for grid lookup if not already cached and queue not full."""
    found, _ = _grid_cache.get(callsign)
    if found:
        return
    try:
        _lookup_queue.put_nowait(callsign)
    except queue.Full:
        pass  # drop silently if queue is full


# ============================================================
# Database init
# ============================================================

def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()

        # ---- Phase 1 table (unchanged) ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source_station TEXT,
                target_station TEXT,
                group_name TEXT,
                snr INTEGER,
                hearing_layer TEXT NOT NULL,
                confidence TEXT NOT NULL,
                grid_in_text TEXT,
                parser_note TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC)")

        # ---- Phase 2: stations ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stations (
                callsign TEXT PRIMARY KEY,
                best_grid TEXT,
                best_grid_source TEXT NOT NULL DEFAULT 'unknown',
                last_heard_at TEXT,
                last_snr INTEGER,
                snr_min INTEGER,
                snr_max INTEGER,
                hearing_layer TEXT NOT NULL DEFAULT 'inferred',
                confidence TEXT NOT NULL DEFAULT 'low',
                event_count INTEGER NOT NULL DEFAULT 0,
                active_groups TEXT NOT NULL DEFAULT '',
                is_local INTEGER NOT NULL DEFAULT 0,
                grid_status TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        # Migration: add grid_status if upgrading from older schema
        try:
            cur.execute("ALTER TABLE stations ADD COLUMN grid_status TEXT NOT NULL DEFAULT 'pending'")
        except Exception:
            pass  # column already exists

        # ---- Phase 2: station_grids ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS station_grids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                callsign TEXT NOT NULL,
                grid TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                confidence TEXT NOT NULL,
                UNIQUE(callsign, grid, source)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_grids_callsign ON station_grids(callsign)")

        # ---- Phase 2: connections ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT 'station',
                connection_type TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                last_snr INTEGER,
                weight REAL NOT NULL DEFAULT 0.5,
                hearing_layer TEXT NOT NULL DEFAULT 'inferred',
                confidence TEXT NOT NULL DEFAULT 'low',
                UNIQUE(source, target, connection_type)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conn_source ON connections(source)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conn_target ON connections(target)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conn_last_seen ON connections(last_seen_at DESC)")

        # ---- Phase 2: groups ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                name TEXT PRIMARY KEY,
                last_activity_at TEXT,
                active_station_count INTEGER NOT NULL DEFAULT 0,
                event_count INTEGER NOT NULL DEFAULT 0,
                confidence TEXT NOT NULL DEFAULT 'low'
            )
        """)

        con.commit()
    finally:
        con.close()


# ============================================================
# Database pruning
# ============================================================

def prune_db() -> int:
    """Delete rows older than the configured retention windows.

    Returns the total number of rows deleted across all tables.
    """
    now = datetime.now(timezone.utc)
    event_cutoff      = (now - timedelta(days=DB_PRUNE_EVENTS_DAYS)).isoformat()
    conn_cutoff       = (now - timedelta(days=DB_PRUNE_CONNECTIONS_DAYS)).isoformat()
    station_cutoff    = (now - timedelta(days=DB_PRUNE_STATIONS_DAYS)).isoformat()

    total_deleted = 0
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()

        cur.execute("DELETE FROM events WHERE timestamp < ?", (event_cutoff,))
        total_deleted += cur.rowcount

        cur.execute("DELETE FROM connections WHERE last_seen_at < ?", (conn_cutoff,))
        total_deleted += cur.rowcount

        cur.execute(
            "DELETE FROM groups WHERE last_activity_at < ?",
            (conn_cutoff,),
        )
        total_deleted += cur.rowcount

        # Prune stations that haven't been heard recently (never prune own station)
        cur.execute(
            "DELETE FROM stations WHERE last_heard_at < ? AND is_local = 0",
            (station_cutoff,),
        )
        total_deleted += cur.rowcount

        # Cascade: remove grids for stations that no longer exist
        cur.execute(
            "DELETE FROM station_grids WHERE callsign NOT IN (SELECT callsign FROM stations)"
        )
        total_deleted += cur.rowcount

        con.commit()
    finally:
        con.close()

    return total_deleted


def _prune_worker() -> None:
    """Background thread: run prune_db() at startup and then every DB_PRUNE_INTERVAL_HOURS."""
    # Brief delay so startup messages print first
    time.sleep(5)
    while True:
        try:
            deleted = prune_db()
            ts = utc_now()
            with STATUS_LOCK:
                STATUS["last_prune_at"] = ts
                STATUS["last_prune_deleted"] = deleted
            if deleted:
                print(f"[js8-tracker] prune: removed {deleted} old rows", flush=True)
        except Exception as exc:
            print(f"[js8-tracker] prune error: {exc}", flush=True)
        time.sleep(DB_PRUNE_INTERVAL_HOURS * 3600)


# ============================================================
# Grid priority helper
# ============================================================

def grid_source_rank(source: str) -> int:
    """Lower rank = higher priority. Unknown/missing = worst."""
    try:
        return GRID_SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(GRID_SOURCE_PRIORITY)


def grid_source_beats(new_source: str, current_source: str) -> bool:
    """Return True if new_source is strictly better than current_source."""
    return grid_source_rank(new_source) < grid_source_rank(current_source)


# ============================================================
# Event type → connection type mapping
# ============================================================

EVENT_TO_CONNECTION_TYPE: dict[str, str] = {
    "heartbeat_direct":  "heard",
    "heartbeat_report":  "reported_heard",
    "snr_report":        "reported_heard",
    "directed_message":  "directed",
    "directed_ack":      "ack",
    "directed_query":    "query",
    "directed_info":     "info",
    "directed_grid":     "grid",
    "directed_status":   "status",
    "directed_relay":    "directed",
    "directed_hearing":        "reported_heard",
    "directed_hearing_query":  "query",
    "directed_call":     "directed",
    "broadcast_group":   "broadcast",
    "broadcast_cq":      "broadcast",
    "activity_direct":   "heard",
    "activity_inferred": "inferred",
    "unknown":           "inferred",
}


# ============================================================
# Upsert helpers
# ============================================================

def upsert_station(con: sqlite3.Connection, callsign: str, event: ParsedEvent, role: str) -> None:
    """
    role: "source" | "target" — determines which hearing_layer/confidence applies.
    For the source station the hearing is always 'direct' for A:B messages
    (A is the one transmitting); for the target it matches event.hearing_layer.
    """
    now = event.timestamp
    snr = event.snr if role == "target" else None

    # Determine hearing layer for this station in this event
    station_layer = "direct" if role == "source" else event.hearing_layer

    cur = con.cursor()
    cur.execute("SELECT * FROM stations WHERE callsign = ?", (callsign,))
    row = cur.fetchone()

    if row is None:
        # New station
        active_groups = event.group_name if (event.group_name and role == "source") else ""
        initial_grid_status = "found" if event.grid_in_text else "pending"
        cur.execute("""
            INSERT INTO stations
                (callsign, best_grid, best_grid_source, last_heard_at,
                 last_snr, snr_min, snr_max, hearing_layer, confidence,
                 event_count, active_groups, is_local, grid_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, ?)
        """, (
            callsign,
            event.grid_in_text,
            "transmitted" if event.grid_in_text else "unknown",
            now,
            snr, snr, snr,
            station_layer,
            event.confidence,
            active_groups,
            initial_grid_status,
        ))
    else:
        # Existing station — selective update
        cols = ["callsign", "best_grid", "best_grid_source", "last_heard_at",
                "last_snr", "snr_min", "snr_max", "hearing_layer", "confidence",
                "event_count", "active_groups", "is_local"]
        r = dict(zip(cols, row))

        new_best_grid = r["best_grid"]
        new_best_grid_source = r["best_grid_source"] or "unknown"

        # Upgrade best_grid if we have a better source
        if event.grid_in_text:
            incoming_source = "transmitted"  # grid found in the decoded text = transmitted
            if new_best_grid is None or grid_source_beats(incoming_source, new_best_grid_source):
                new_best_grid = event.grid_in_text
                new_best_grid_source = incoming_source

        # SNR tracking (only meaningful for the target station)
        new_last_snr = r["last_snr"]
        new_snr_min = r["snr_min"]
        new_snr_max = r["snr_max"]
        if snr is not None:
            new_last_snr = snr
            new_snr_min = min(snr, r["snr_min"]) if r["snr_min"] is not None else snr
            new_snr_max = max(snr, r["snr_max"]) if r["snr_max"] is not None else snr

        # Hearing layer: upgrade toward more direct
        existing_rank = grid_source_rank(r["hearing_layer"])  # reuse priority for layers
        # Actually define layer priority inline
        layer_priority = {"direct": 0, "reported": 1, "inferred": 2}
        new_layer = r["hearing_layer"]
        if layer_priority.get(station_layer, 99) < layer_priority.get(r["hearing_layer"], 99):
            new_layer = station_layer

        # Confidence: keep best seen
        conf_priority = {"high": 0, "medium": 1, "low": 2, "none": 3}
        new_conf = r["confidence"]
        if conf_priority.get(event.confidence, 99) < conf_priority.get(r["confidence"], 99):
            new_conf = event.confidence

        # active_groups: accumulate
        existing_groups = set(g for g in r["active_groups"].split(",") if g)
        if event.group_name and role == "source":
            existing_groups.add(event.group_name)
        new_groups = ",".join(sorted(existing_groups))

        new_grid_status = "found" if new_best_grid else r.get("grid_status", "pending")
        cur.execute("""
            UPDATE stations SET
                best_grid = ?,
                best_grid_source = ?,
                last_heard_at = ?,
                last_snr = ?,
                snr_min = ?,
                snr_max = ?,
                hearing_layer = ?,
                confidence = ?,
                event_count = event_count + 1,
                active_groups = ?,
                grid_status = ?
            WHERE callsign = ?
        """, (
            new_best_grid, new_best_grid_source, now,
            new_last_snr, new_snr_min, new_snr_max,
            new_layer, new_conf, new_groups, new_grid_status, callsign,
        ))

    # Always record the grid in station_grids if present
    if event.grid_in_text:
        cur.execute("""
            INSERT INTO station_grids (callsign, grid, source, observed_at, confidence)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(callsign, grid, source) DO UPDATE SET
                observed_at = excluded.observed_at,
                confidence = excluded.confidence
        """, (callsign, event.grid_in_text, "transmitted", now, event.confidence))


def upsert_connection(con: sqlite3.Connection, event: ParsedEvent) -> None:
    """
    Create or update the aggregated connection for this event.
    Only called when both source and target are known, or source + group.
    """
    source = event.source_station
    target = event.target_station or event.group_name
    if not source or not target:
        return

    target_type = "group" if event.group_name and target == event.group_name else "station"
    connection_type = EVENT_TO_CONNECTION_TYPE.get(event.event_type, "inferred")
    base_weight = CONNECTION_BASE_WEIGHTS.get(event.event_type, 0.3)
    now = event.timestamp

    cur = con.cursor()
    cur.execute("""
        SELECT id, count, weight FROM connections
        WHERE source = ? AND target = ? AND connection_type = ?
    """, (source, target, connection_type))
    row = cur.fetchone()

    if row is None:
        cur.execute("""
            INSERT INTO connections
                (source, target, target_type, connection_type,
                 first_seen_at, last_seen_at, count, last_snr, weight,
                 hearing_layer, confidence)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """, (
            source, target, target_type, connection_type,
            now, now, event.snr, base_weight,
            event.hearing_layer, event.confidence,
        ))
    else:
        row_id, count, current_weight = row
        new_count = count + 1
        new_weight = min(WEIGHT_CAP, current_weight + WEIGHT_INCREMENT)
        # Low confidence caps weight below medium-confidence baseline
        if event.confidence == "low":
            new_weight = min(new_weight, 0.5)
        cur.execute("""
            UPDATE connections SET
                last_seen_at = ?,
                count = ?,
                last_snr = COALESCE(?, last_snr),
                weight = ?,
                hearing_layer = ?,
                confidence = ?
            WHERE id = ?
        """, (now, new_count, event.snr, new_weight,
              event.hearing_layer, event.confidence, row_id))


def upsert_group(con: sqlite3.Connection, event: ParsedEvent) -> None:
    """Update the groups table when a broadcast_group event is seen."""
    if not event.group_name:
        return
    name = event.group_name
    now = event.timestamp
    cur = con.cursor()
    cur.execute("SELECT event_count FROM groups WHERE name = ?", (name,))
    row = cur.fetchone()
    if row is None:
        cur.execute("""
            INSERT INTO groups (name, last_activity_at, active_station_count, event_count, confidence)
            VALUES (?, ?, 1, 1, ?)
        """, (name, now, event.confidence))
    else:
        cur.execute("""
            UPDATE groups SET
                last_activity_at = ?,
                event_count = event_count + 1,
                confidence = ?
            WHERE name = ?
        """, (now, event.confidence, name))
        # Recount distinct active stations seen in last 90 min for this group
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        cur.execute("""
            SELECT COUNT(DISTINCT source_station) FROM events
            WHERE group_name = ? AND timestamp >= ?
        """, (name, cutoff))
        cnt_row = cur.fetchone()
        if cnt_row:
            cur.execute("UPDATE groups SET active_station_count = ? WHERE name = ?",
                        (cnt_row[0], name))


# ============================================================
# Store event + trigger Phase 2 upserts
# ============================================================

def store_event(event: ParsedEvent) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("""
            INSERT INTO events (
                id, timestamp, raw_text, event_type, source_station, target_station,
                group_name, snr, hearing_layer, confidence, grid_in_text, parser_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.id, event.timestamp, event.raw_text, event.event_type,
            event.source_station, event.target_station, event.group_name,
            event.snr, event.hearing_layer, event.confidence,
            event.grid_in_text, event.parser_note,
        ))

        # Phase 2: upsert station records
        if event.source_station:
            upsert_station(con, event.source_station, event, "source")
        if event.target_station:
            upsert_station(con, event.target_station, event, "target")

        # Phase 2: upsert connection
        if (event.source_station and
                (event.target_station or event.group_name) and
                event.event_type != EventType.UNKNOWN.value):
            upsert_connection(con, event)

        # HEARING extras: create reported_heard connections for each extra station
        if (event.event_type == EventType.DIRECTED_HEARING.value and
                event.parser_note and event.parser_note.startswith("also heard:")):
            extras = event.parser_note.replace("also heard:", "").strip().split()
            for extra_call in extras:
                extra_call = extra_call.upper()
                # Upsert the extra station as reported
                upsert_station(con, extra_call, event, "target")
                # Create a synthetic connection: source heard extra_call
                from dataclasses import replace as dc_replace
                extra_event = ParsedEvent(
                    id=event.id + f"_extra_{extra_call}",
                    timestamp=event.timestamp,
                    raw_text=event.raw_text,
                    event_type=EventType.DIRECTED_HEARING.value,
                    source_station=event.source_station,
                    target_station=extra_call,
                    group_name=None,
                    snr=None,
                    hearing_layer="reported",
                    confidence="medium",
                    grid_in_text=None,
                    parser_note=None,
                )
                upsert_connection(con, extra_event)

        # Phase 2: upsert group
        if event.group_name:
            upsert_group(con, event)

        con.commit()
    finally:
        con.close()

    # Enqueue grid lookups for any station without a transmitted grid
    if event.source_station and not event.grid_in_text:
        enqueue_lookup(event.source_station)
    if event.target_station and not event.grid_in_text:
        enqueue_lookup(event.target_station)

    with STATUS_LOCK:
        STATUS["total_events"] += 1
        if event.confidence == CONF_HIGH:
            STATUS["high_confidence"] += 1
        elif event.confidence == CONF_MED:
            STATUS["medium_confidence"] += 1
        elif event.confidence == CONF_LOW:
            STATUS["low_confidence"] += 1
        STATUS["last_event_at"] = event.timestamp


def note_drop() -> None:
    with STATUS_LOCK:
        STATUS["dropped_lines"] += 1


def note_packet(parse_result: PacketParseResult) -> None:
    with STATUS_LOCK:
        if parse_result.packet_type_name:
            STATUS["last_packet_type"] = parse_result.packet_type_name
        if parse_result.parse_error:
            STATUS["parse_errors"] += 1
            STATUS["ignored_packets"] += 1
            return
        if parse_result.packet_type == TYPE_DECODE:
            STATUS["decode_packets"] += 1
            STATUS["last_decode_text"] = parse_result.text
            if not parse_result.text:
                STATUS["empty_decodes"] += 1
        else:
            STATUS["non_decode_packets"] += 1
            STATUS["ignored_packets"] += 1


# ============================================================
# WSJT-X packet parsing (unchanged from Phase 1)
# ============================================================

def unpack_u32(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from(">I", buf, off)[0], off + 4


def unpack_i32(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from(">i", buf, off)[0], off + 4


def unpack_bool(buf: bytes, off: int) -> tuple[bool, int]:
    return (struct.unpack_from(">B", buf, off)[0] != 0), off + 1


def unpack_double(buf: bytes, off: int) -> tuple[float, int]:
    return struct.unpack_from(">d", buf, off)[0], off + 8


def unpack_qbytearray_utf8(buf: bytes, off: int) -> tuple[str, int]:
    n, off = unpack_u32(buf, off)
    if n == 0xFFFFFFFF:
        return "", off
    if off + n > len(buf):
        raise ValueError("qbytearray length exceeds packet size")
    s = buf[off:off + n].decode("utf-8", errors="replace")
    return s, off + n


def unpack_qtime(buf: bytes, off: int) -> tuple[int, int]:
    ms, off = unpack_u32(buf, off)
    return ms, off


def parse_wsjtx_packet(packet: bytes) -> PacketParseResult:
    if len(packet) < 12:
        return PacketParseResult(text=None, packet_type=None,
                                 packet_type_name=None, parse_error=True)
    try:
        magic, off = unpack_u32(packet, 0)
        if magic != MAGIC:
            return PacketParseResult(text=None, packet_type=None,
                                     packet_type_name=None, parse_error=False)
        _schema, off = unpack_u32(packet, off)
        packet_type, off = unpack_u32(packet, off)
        packet_type_name = PACKET_TYPE_NAMES.get(packet_type, f"type_{packet_type}")
        _wsjtx_id, off = unpack_qbytearray_utf8(packet, off)

        if packet_type != TYPE_DECODE:
            return PacketParseResult(text=None, packet_type=packet_type,
                                     packet_type_name=packet_type_name, parse_error=False)

        _new, off = unpack_bool(packet, off)
        _time_ms, off = unpack_qtime(packet, off)
        _snr, off = unpack_i32(packet, off)
        _dt, off = unpack_double(packet, off)
        _df, off = unpack_u32(packet, off)
        _mode, off = unpack_qbytearray_utf8(packet, off)
        text, off = unpack_qbytearray_utf8(packet, off)
        text = text.strip()
        return PacketParseResult(text=text or None, packet_type=packet_type,
                                 packet_type_name=packet_type_name, parse_error=False)
    except Exception as exc:
        print(f"[js8-tracker] packet parse error: {exc}", flush=True)
        return PacketParseResult(text=None, packet_type=None,
                                 packet_type_name=None, parse_error=True)


# ============================================================
# Text parsing / event classifier (unchanged from Phase 1)
# ============================================================

def normalize_call(call: str) -> str:
    return call.split("/")[0].upper().strip(" ,:;")


def looks_like_callsign(token: str) -> bool:
    t = token.upper().strip(" ,:;")
    if not t or t.startswith("@"):
        return False
    if t in NONCALL_WORDS:
        return False
    if GRID_RE.fullmatch(t):
        return False
    if not re.search(r"[A-Z]", t):
        return False
    if not re.search(r"\d", t):
        return False
    if len(t) < 3 or len(t) > 15:
        return False
    return True


def first_callsign(tokens: list[str]) -> Optional[str]:
    for token in tokens:
        if looks_like_callsign(token):
            return normalize_call(token)
    return None


def parse_snr(text: str) -> Optional[int]:
    match = re.search(r"\bSNR\s*([+\-]?\d+)\b", text.upper())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_group(text: str) -> Optional[str]:
    match = GROUP_TOKEN_RE.search(text.upper())
    if not match:
        return None
    return "@" + match.group(1).upper()


def classify_line(raw_text: str) -> Optional[ParsedEvent]:
    text = raw_text.strip().upper()
    if not text:
        return None
    if re.fullmatch(r"[A-Z0-9/]+\s*:\s*", text):
        return None

    group_name = parse_group(text)
    snr = parse_snr(text)
    grid_in_text = None
    grid_match = GRID_RE.search(text)
    if grid_match:
        grid_in_text = grid_match.group(1).upper()

    source_station: Optional[str] = None
    target_station: Optional[str] = None
    parser_note: Optional[str] = None
    event_type = EventType.UNKNOWN.value
    hearing_layer: HearingLayer = "inferred"
    confidence: Confidence = "low"

    if ":" in text:
        left, right = text.split(":", 1)
        left_tokens = left.strip().split()
        right_tokens = right.strip().split()
        source_station = first_callsign(left_tokens)
        target_station = first_callsign(right_tokens)

        # Helper: check for a keyword token in the right side of A: B ...
        right_text = right.strip()
        def has_token(*tokens):
            for tok in tokens:
                if re.search(r"\b" + re.escape(tok) + r"\b", right_text):
                    return True
            return False

        if group_name and source_station:
            event_type = EventType.BROADCAST_GROUP.value
            hearing_layer = "reported"
            confidence = "high"
        elif "HEARTBEAT" in text and source_station and target_station:
            event_type = EventType.HEARTBEAT_REPORT.value
            hearing_layer = "reported"
            confidence = "high"
        elif re.search(r"\bSNR\b", text) and source_station and target_station:
            event_type = EventType.SNR_REPORT.value
            hearing_layer = "reported"
            confidence = "high" if snr is not None else "medium"
        elif has_token("MSG", "MSGS", "MSGS?") and source_station and target_station:
            event_type = EventType.DIRECTED_MESSAGE.value
            hearing_layer = "reported"
            confidence = "medium"
        elif has_token("ACK", "ACK?", "RR", "RRR", "NO", "YES", "73") and source_station and target_station:
            event_type = EventType.DIRECTED_ACK.value
            hearing_layer = "reported"
            confidence = "medium"
        elif has_token("QUERY", "QUERY?") and source_station and target_station:
            event_type = EventType.DIRECTED_QUERY.value
            hearing_layer = "reported"
            confidence = "medium"
        elif has_token("INFO", "INFO?") and source_station and target_station:
            event_type = EventType.DIRECTED_INFO.value
            hearing_layer = "reported"
            confidence = "medium"
        elif has_token("GRID", "GRID?") and source_station and target_station:
            event_type = EventType.DIRECTED_GRID.value
            hearing_layer = "reported"
            confidence = "medium"
        elif has_token("STATUS", "STATUS?") and source_station and target_station:
            event_type = EventType.DIRECTED_STATUS.value
            hearing_layer = "reported"
            confidence = "medium"
        elif re.search(r"\bHEARING\?", right_text) and source_station and target_station:
            # Query form: "can you hear me?"
            event_type = EventType.DIRECTED_HEARING_QUERY.value
            hearing_layer = "reported"
            confidence = "medium"
        elif re.search(r"\bHEARING\b(?!\?)", right_text) and source_station and target_station:
            # Statement form: "I can hear you" — explicit confirmed reception
            event_type = EventType.DIRECTED_HEARING.value
            hearing_layer = "reported"
            confidence = "high"
            # Parse any additional callsigns after HEARING as also-heard stations
            # e.g. "A: B HEARING C D E" means A heard B, C, D, and E
            hearing_extras = [
                t for t in right_tokens
                if looks_like_callsign(t) and t != target_station
            ]
            if hearing_extras:
                parser_note = f"also heard: {' '.join(hearing_extras)}"
        elif has_token("RELAY") and source_station and target_station:
            event_type = EventType.DIRECTED_RELAY.value
            hearing_layer = "reported"
            confidence = "medium"
        elif source_station and target_station:
            # Bare "A: B" = right side has only callsign(s), no keyword payload
            non_call_tokens = [t for t in right_tokens if not looks_like_callsign(t)]
            if not non_call_tokens:
                event_type = EventType.DIRECTED_CALL.value
                hearing_layer = "reported"
                confidence = "medium"
                parser_note = "bare directed call"
            else:
                # Has unrecognized content — still directed, low confidence
                event_type = EventType.DIRECTED_MESSAGE.value
                hearing_layer = "reported"
                confidence = "low"
                parser_note = "unrecognized marker, classified as directed message"
        elif source_station and not target_station and right_tokens:
            # A: <no callsign> content — activity from source
            event_type = EventType.ACTIVITY_INFERRED.value
            hearing_layer = "inferred"
            confidence = "low"
            parser_note = "source only, no target callsign found"
    else:
        tokens = text.split()
        target_station = first_callsign(tokens)
        if group_name and target_station:
            event_type = EventType.BROADCAST_GROUP.value
            hearing_layer = "direct"
            confidence = "medium"
        elif "CQ" in text and target_station:
            event_type = EventType.BROADCAST_CQ.value
            hearing_layer = "direct"
            confidence = "medium"
        elif "HEARTBEAT" in text and target_station:
            event_type = EventType.HEARTBEAT_DIRECT.value
            hearing_layer = "direct"
            confidence = "high"
        elif target_station and snr is not None:
            event_type = EventType.ACTIVITY_DIRECT.value
            hearing_layer = "direct"
            confidence = "medium"
        elif target_station:
            event_type = EventType.ACTIVITY_INFERRED.value
            hearing_layer = "inferred"
            confidence = "low"
            parser_note = "single callsign from ambiguous direct text"

    if event_type == EventType.UNKNOWN.value and not target_station and not source_station and not group_name:
        return None

    return ParsedEvent(
        id=str(uuid.uuid4()),
        timestamp=utc_now(),
        raw_text=raw_text,
        event_type=event_type,
        source_station=source_station,
        target_station=target_station,
        group_name=group_name,
        snr=snr,
        hearing_layer=hearing_layer,
        confidence=confidence,
        grid_in_text=grid_in_text,
        parser_note=parser_note,
    )


# ============================================================
# CLI event formatter
# ============================================================

def format_cli_event(event: ParsedEvent) -> str:
    parts = [f"[{event.timestamp}] [js8-tracker]"]
    parts.append(f"class={event.event_type}")
    if event.source_station:
        parts.append(f"src={event.source_station}")
    if event.target_station:
        parts.append(f"dst={event.target_station}")
    if event.group_name:
        parts.append(f"group={event.group_name}")
    if event.snr is not None:
        parts.append(f"snr={event.snr:+d}")
    parts.append(f"layer={event.hearing_layer}")
    parts.append(f"conf={event.confidence}")
    if event.grid_in_text:
        parts.append(f"grid={event.grid_in_text}")
    if event.parser_note:
        parts.append(f"note={event.parser_note}")
    parts.append(f"text={event.raw_text}")
    return " ".join(parts)


# ============================================================
# UDP forwarding (optional)
# ============================================================

def maybe_forward_udp(packet: bytes) -> None:
    if not FORWARD_UDP_ENABLED:
        return
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(packet, (FORWARD_UDP_HOST, FORWARD_UDP_PORT))
    finally:
        sock.close()


# ============================================================
# UDP listener
# ============================================================

def udp_listener() -> None:
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_HOST, UDP_PORT))
    print(f"[js8-tracker] UDP listening on {UDP_HOST}:{UDP_PORT}", flush=True)

    while True:
        packet, _addr = sock.recvfrom(65535)
        maybe_forward_udp(packet)

        parse_result = parse_wsjtx_packet(packet)
        note_packet(parse_result)

        if not parse_result.text:
            continue

        print(f"[js8-tracker] decode text={parse_result.text!r}", flush=True)

        event = classify_line(parse_result.text)
        if event is None:
            note_drop()
            continue

        store_event(event)
        print(format_cli_event(event), flush=True)


# ============================================================
# API routes — Phase 1 (unchanged)
# ============================================================

@app.get("/api/status", response_model=StatusResponse)
def api_status() -> StatusResponse:
    with STATUS_LOCK:
        snapshot = dict(STATUS)
    snapshot["lookup_queue_depth"] = _lookup_queue.qsize()
    return StatusResponse(
        app_name="JS8-tracker",
        build_tag=BUILD_TAG,
        udp_host=UDP_HOST,
        udp_port=UDP_PORT,
        http_host=HTTP_HOST,
        http_port=HTTP_PORT,
        started_at=STARTED_AT,
        total_events=snapshot["total_events"],
        high_confidence=snapshot["high_confidence"],
        medium_confidence=snapshot["medium_confidence"],
        low_confidence=snapshot["low_confidence"],
        dropped_lines=snapshot["dropped_lines"],
        ignored_packets=snapshot["ignored_packets"],
        decode_packets=snapshot["decode_packets"],
        non_decode_packets=snapshot["non_decode_packets"],
        parse_errors=snapshot["parse_errors"],
        empty_decodes=snapshot["empty_decodes"],
        last_event_at=snapshot["last_event_at"],
        last_decode_text=snapshot["last_decode_text"],
        last_packet_type=snapshot["last_packet_type"],
        lookup_hits=snapshot["lookup_hits"],
        lookup_misses=snapshot["lookup_misses"],
        lookup_errors=snapshot["lookup_errors"],
        lookup_queue_depth=snapshot["lookup_queue_depth"],
        hamqth_configured=(_hamqth_client is not None),
        my_call=snapshot["my_call"] or MYCALL,
        my_grid=snapshot["my_grid"],
        last_prune_at=snapshot["last_prune_at"],
        last_prune_deleted=snapshot["last_prune_deleted"],
    )


@app.get("/api/events", response_model=list[EventResponse])
def api_events(
    limit: int = Query(default=100, ge=1, le=1000),
    confidence: Optional[str] = Query(default=None),
    minutes: Optional[int] = Query(default=None),
    types: Optional[str] = Query(default=None),
) -> list[EventResponse]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        clauses: list[str] = []
        params: list = []

        if confidence:
            allowed = [c.strip().lower() for c in confidence.split(",") if c.strip()]
            placeholders = ",".join("?" for _ in allowed)
            clauses.append(f"confidence IN ({placeholders})")
            params.extend(allowed)

        if minutes:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            clauses.append("timestamp >= ?")
            params.append(cutoff)

        if types:
            allowed_types = [t.strip().lower() for t in types.split(",") if t.strip()]
            placeholders = ",".join("?" for _ in allowed_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(allowed_types)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cur.execute(f"SELECT * FROM events {where} ORDER BY timestamp DESC LIMIT ?", params)
        rows = cur.fetchall()
    finally:
        con.close()
    return [EventResponse(**dict(row)) for row in rows]


# ============================================================
# API routes — Phase 2 (new)
# ============================================================

@app.get("/api/stations", response_model=list[StationResponse])
def api_stations(
    confidence: Optional[str] = Query(default=None),
    hearing: Optional[str] = Query(default=None),
    active_only: bool = Query(default=False),
    minutes: Optional[int] = Query(default=None),
) -> list[StationResponse]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        clauses: list[str] = []
        params: list = []

        if confidence:
            allowed = [c.strip().lower() for c in confidence.split(",") if c.strip()]
            placeholders = ",".join("?" for _ in allowed)
            clauses.append(f"confidence IN ({placeholders})")
            params.extend(allowed)

        if hearing:
            allowed = [h.strip().lower() for h in hearing.split(",") if h.strip()]
            placeholders = ",".join("?" for _ in allowed)
            clauses.append(f"hearing_layer IN ({placeholders})")
            params.extend(allowed)

        if active_only or minutes:
            mins = minutes or 30
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=mins)).isoformat()
            clauses.append("last_heard_at >= ?")
            params.append(cutoff)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur.execute(f"SELECT * FROM stations {where} ORDER BY last_heard_at DESC", params)
        rows = cur.fetchall()
    finally:
        con.close()

    result = []
    for row in rows:
        d = dict(row)
        active_groups = [g for g in d.get("active_groups", "").split(",") if g]
        result.append(StationResponse(
            callsign=d["callsign"],
            best_grid=d["best_grid"],
            best_grid_source=d["best_grid_source"] or "unknown",
            last_heard_at=d["last_heard_at"],
            last_snr=d["last_snr"],
            snr_min=d["snr_min"],
            snr_max=d["snr_max"],
            hearing_layer=d["hearing_layer"],
            confidence=d["confidence"],
            event_count=d["event_count"],
            active_groups=active_groups,
            is_local=bool(d["is_local"]),
            grid_status=d.get("grid_status") or "pending",
        ))
    return result


@app.get("/api/connections", response_model=list[ConnectionResponse])
def api_connections(
    confidence: Optional[str] = Query(default=None),
    types: Optional[str] = Query(default=None),
    minutes: Optional[int] = Query(default=None),
    min_weight: Optional[float] = Query(default=None),
) -> list[ConnectionResponse]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        clauses: list[str] = []
        params: list = []

        if confidence:
            allowed = [c.strip().lower() for c in confidence.split(",") if c.strip()]
            placeholders = ",".join("?" for _ in allowed)
            clauses.append(f"confidence IN ({placeholders})")
            params.extend(allowed)

        if types:
            allowed_types = [t.strip().lower() for t in types.split(",") if t.strip()]
            placeholders = ",".join("?" for _ in allowed_types)
            clauses.append(f"connection_type IN ({placeholders})")
            params.extend(allowed_types)

        if minutes:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            clauses.append("last_seen_at >= ?")
            params.append(cutoff)

        if min_weight is not None:
            clauses.append("weight >= ?")
            params.append(min_weight)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur.execute(
            f"SELECT * FROM connections {where} ORDER BY last_seen_at DESC",
            params,
        )
        rows = cur.fetchall()
    finally:
        con.close()

    return [ConnectionResponse(**dict(row)) for row in rows]


@app.get("/api/groups", response_model=list[GroupResponse])
def api_groups(
    minutes: Optional[int] = Query(default=None),
) -> list[GroupResponse]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        if minutes:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            cur.execute(
                "SELECT * FROM groups WHERE last_activity_at >= ? ORDER BY last_activity_at DESC",
                (cutoff,),
            )
        else:
            cur.execute("SELECT * FROM groups ORDER BY last_activity_at DESC")
        rows = cur.fetchall()

        # Fetch members for each group
        result = []
        member_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes or 90)).isoformat()
        for row in rows:
            d = dict(row)
            cur.execute("""
                SELECT DISTINCT source_station FROM events
                WHERE group_name = ?
                  AND source_station IS NOT NULL
                  AND timestamp >= ?
                ORDER BY timestamp DESC
            """, (d["name"], member_cutoff))
            members = [r[0] for r in cur.fetchall()]
            result.append(GroupResponse(
                name=d["name"],
                last_activity_at=d["last_activity_at"],
                active_station_count=d["active_station_count"],
                event_count=d["event_count"],
                confidence=d["confidence"],
                members=members,
            ))
    finally:
        con.close()
    return result


@app.get("/api/groups/{name}/events", response_model=list[EventResponse])
def api_group_events(
    name: str,
    limit: int = Query(default=50, ge=1, le=500),
    minutes: Optional[int] = Query(default=None),
) -> list[EventResponse]:
    """Recent events for a specific group."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        clauses = ["group_name = ?"]
        params: list = [name]
        if minutes:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            clauses.append("timestamp >= ?")
            params.append(cutoff)
        where = "WHERE " + " AND ".join(clauses)
        params.append(limit)
        cur.execute(
            f"SELECT * FROM events {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [EventResponse(**dict(row)) for row in rows]


@app.get("/api/stations/{callsign}/detail")
def api_station_detail(
    callsign: str,
    minutes: Optional[int] = Query(default=None),
) -> dict:
    """Full detail for a single station: events, connections, all grids."""
    callsign = callsign.upper()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()

        # Station record
        cur.execute("SELECT * FROM stations WHERE callsign = ?", (callsign,))
        row = cur.fetchone()
        if row is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Station not found")
        station = dict(row)
        station["active_groups"] = [g for g in (station.get("active_groups") or "").split(",") if g]

        # All known grids
        cur.execute("""
            SELECT grid, source, observed_at, confidence
            FROM station_grids WHERE callsign = ?
            ORDER BY observed_at DESC
        """, (callsign,))
        grids = [dict(r) for r in cur.fetchall()]

        # Recent events involving this station
        cutoff_clause = ""
        params_evt: list = [callsign, callsign]
        if minutes:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            cutoff_clause = "AND timestamp >= ?"
            params_evt.append(cutoff)
        cur.execute(f"""
            SELECT * FROM events
            WHERE (source_station = ? OR target_station = ?)
            {cutoff_clause}
            ORDER BY timestamp DESC
            LIMIT 100
        """, params_evt)
        events = [dict(r) for r in cur.fetchall()]

        # Connections involving this station
        cur.execute("""
            SELECT * FROM connections
            WHERE source = ? OR target = ?
            ORDER BY last_seen_at DESC
        """, (callsign, callsign))
        connections = [dict(r) for r in cur.fetchall()]

        # SNR history (last 50 SNR readings where this station is target)
        cur.execute("""
            SELECT timestamp, snr FROM events
            WHERE target_station = ? AND snr IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 50
        """, (callsign,))
        snr_history = [dict(r) for r in cur.fetchall()]

    finally:
        con.close()

    return {
        "station": station,
        "grids": grids,
        "events": events,
        "connections": connections,
        "snr_history": list(reversed(snr_history)),
    }


@app.get("/")
def root():
    return FileResponse("js8_tracker_ui.html")


# ============================================================
# Self-check (Phase 1 cases + Phase 2 DB round-trip)
# ============================================================

def build_sample_decode_packet(text: str) -> bytes:
    def pack_u32(x: int) -> bytes:
        return struct.pack(">I", x)

    def pack_i32(x: int) -> bytes:
        return struct.pack(">i", x)

    def pack_bool(b: bool) -> bytes:
        return struct.pack(">B", 1 if b else 0)

    def pack_double(x: float) -> bytes:
        return struct.pack(">d", x)

    def pack_qbytearray(s: str) -> bytes:
        b = s.encode("utf-8")
        return pack_u32(len(b)) + b

    out = bytearray()
    out += pack_u32(MAGIC)
    out += pack_u32(2)
    out += pack_u32(TYPE_DECODE)
    out += pack_qbytearray("JS8Call")
    out += pack_bool(True)
    out += pack_u32(0)
    out += pack_i32(0)
    out += pack_double(0.0)
    out += pack_u32(0)
    out += pack_qbytearray("JS8")
    out += pack_qbytearray(text)
    out += pack_bool(False)
    out += pack_bool(False)
    return bytes(out)


def run_parser_selfcheck() -> None:
    # Phase 1 classifier checks
    samples = [
        "KM4JRD: KC1WDO HEARTBEAT SNR +10",
        "KN4YAV: KF0DCV SNR +05",
        "NC4BD: K4KUS MSG",
        "K4FMM: @HB HEARTBEAT EM96",
        "N3CHX/P1 HEARTBEAT SNR -07",
        "KC7OU: NC4BD SNR -02",
        "KM4JRD: ND7M HEARTBEAT SNR -18",
        "K0EMP:",
    ]
    results = [classify_line(s) for s in samples]
    assert results[0] and results[0].event_type == EventType.HEARTBEAT_REPORT.value
    assert results[1] and results[1].event_type == EventType.SNR_REPORT.value
    assert results[2] and results[2].event_type in (EventType.DIRECTED_MESSAGE.value, EventType.DIRECTED_ACK.value)
    assert results[3] and results[3].event_type == EventType.BROADCAST_GROUP.value
    assert results[4] and results[4].event_type == EventType.HEARTBEAT_DIRECT.value
    assert results[5] and results[5].source_station == "KC7OU" and results[5].target_station == "NC4BD"
    assert results[6] and results[6].source_station == "KM4JRD" and results[6].target_station == "ND7M"
    assert results[7] is None

    # HEARING classification checks
    h1 = classify_line("W4CAT: KE8SWO HEARING")
    assert h1 and h1.event_type == EventType.DIRECTED_HEARING.value, f"Expected directed_hearing, got {h1.event_type if h1 else None}"
    assert h1.confidence == "high", f"HEARING should be high confidence, got {h1.confidence}"

    h2 = classify_line("VE3ICH: KC1NNR HEARING?")
    assert h2 and h2.event_type == EventType.DIRECTED_HEARING_QUERY.value, f"Expected directed_hearing_query, got {h2.event_type if h2 else None}"

    h3 = classify_line("W4CAT: KE8SWO HEARING KU4B W0MQD")
    assert h3 and h3.event_type == EventType.DIRECTED_HEARING.value
    assert h3.parser_note and "KU4B" in h3.parser_note, f"Expected extras in note, got {h3.parser_note}"

    pkt = build_sample_decode_packet("KM4JRD: KC1WDO HEARTBEAT SNR +10")
    parsed = parse_wsjtx_packet(pkt)
    assert parsed.text == "KM4JRD: KC1WDO HEARTBEAT SNR +10"
    assert parsed.packet_type == TYPE_DECODE
    assert parsed.parse_error is False

    # Phase 2: store events and verify DB round-trip
    for s in samples[:-1]:  # skip the hard-drop line
        event = classify_line(s)
        if event:
            store_event(event)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()

        # stations should have been created
        cur.execute("SELECT COUNT(*) FROM stations")
        assert cur.fetchone()[0] > 0, "No stations were stored"

        # KM4JRD should appear as a source station
        cur.execute("SELECT * FROM stations WHERE callsign = 'KM4JRD'")
        row = cur.fetchone()
        assert row is not None, "KM4JRD not found in stations"
        assert row["event_count"] >= 1

        # There should be at least one connection
        cur.execute("SELECT COUNT(*) FROM connections")
        assert cur.fetchone()[0] > 0, "No connections were stored"

        # @HB group should exist
        cur.execute("SELECT * FROM groups WHERE name = '@HB'")
        row = cur.fetchone()
        assert row is not None, "@HB group not found"

        # Grid EM96 from the @HB broadcast should be in station_grids for K4FMM
        cur.execute("SELECT * FROM station_grids WHERE callsign = 'K4FMM' AND grid = 'EM96'")
        row = cur.fetchone()
        assert row is not None, "K4FMM EM96 grid not stored"

        # Connection weight for a repeated event should be higher than base
        # Store the same event again to trigger increment
        event2 = classify_line("KM4JRD: KC1WDO HEARTBEAT SNR +10")
        if event2:
            store_event(event2)
        cur.execute("""
            SELECT weight FROM connections
            WHERE source = 'KM4JRD' AND target = 'KC1WDO'
        """)
        row = cur.fetchone()
        assert row is not None, "KM4JRD->KC1WDO connection not found"
        assert row["weight"] > 0.8, f"Weight did not increment: {row['weight']}"

    finally:
        con.close()

    print("[js8-tracker] self-check passed", flush=True)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    init_db()
    run_parser_selfcheck()

    # Initialize HamQTH lookup
    hamqth_user, hamqth_pass = load_hamqth_credentials()
    if hamqth_user and hamqth_pass:
        _hamqth_client = HamQTHClient(hamqth_user, hamqth_pass)
        print(f"[js8-tracker] HamQTH credentials loaded for user: {hamqth_user}", flush=True)
    else:
        print(f"[js8-tracker] WARNING: HamQTH credentials not found.", flush=True)
        print(f"[js8-tracker]   Expected: {HAMQTH_CRED_FILE}", flush=True)
        print(f"[js8-tracker]   Or set env vars HAMQTH_USER / HAMQTH_PASS", flush=True)
        print(f"[js8-tracker]   Lookup disabled — grids from transmitted text only.", flush=True)

    lookup_thread = threading.Thread(target=_lookup_worker, daemon=True, name="lookup-worker")
    lookup_thread.start()

    prune_thread = threading.Thread(target=_prune_worker, daemon=True, name="prune-worker")
    prune_thread.start()

    # Register own station in DB and look up grid on startup
    with STATUS_LOCK:
        STATUS["my_call"] = MYCALL
    print(f"[js8-tracker] local callsign: {MYCALL}", flush=True)
    _register_local_station()
    if _hamqth_client:
        try:
            my_grid = _hamqth_client.lookup_grid(MYCALL)
            if my_grid:
                with STATUS_LOCK:
                    STATUS["my_grid"] = my_grid
                con_local = sqlite3.connect(DB_PATH)
                try:
                    con_local.execute(
                        "UPDATE stations SET best_grid = ?, best_grid_source = 'lookup', grid_status = 'found' WHERE callsign = ?",
                        (my_grid, MYCALL)
                    )
                    con_local.commit()
                finally:
                    con_local.close()
                print(f"[js8-tracker] own grid: {my_grid} (from HamQTH)", flush=True)
            else:
                print(f"[js8-tracker] own grid: not found in HamQTH", flush=True)
        except Exception as exc:
            print(f"[js8-tracker] own grid lookup failed: {exc}", flush=True)

    print(f"[js8-tracker] build tag: {BUILD_TAG}", flush=True)
    print(f"[js8-tracker] starting HTTP on {HTTP_HOST}:{HTTP_PORT}", flush=True)
    print(f"[js8-tracker] UDP forward enabled: {FORWARD_UDP_ENABLED}", flush=True)

    listener_thread = threading.Thread(target=udp_listener, daemon=True)
    listener_thread.start()

    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, timeout_graceful_shutdown=1)
