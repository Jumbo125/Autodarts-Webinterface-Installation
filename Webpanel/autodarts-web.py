#!/usr/bin/env python3
import os
import json
import re
import socket
import subprocess
import shutil
import shlex
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.error
from pathlib import Path
import time  # für weichen Dongle-Reset
import uuid
from datetime import datetime
import threading

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    render_template,
    send_file,
    send_from_directory,
    jsonify,
    session,
    flash,
    Response,
    stream_with_context,)


app = Flask(__name__)
app.secret_key = os.environ.get('AUTODARTS_WEB_SECRET', 'autodarts-web-admin')

# === KONFIGURATION ===

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

AUTODARTS_DATA_DIR = os.environ.get("AUTODARTS_DATA_DIR", "/home/peter/autodarts-data")
DATA_DIR = Path(AUTODARTS_DATA_DIR).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
HELP_PDF_FILENAME = "Autodarts_install_manual.pdf"
CAM_CONFIG_PATH = "/var/lib/autodarts/cam-config.json"
CAMERA_MODE_SESSION = uuid.uuid4().hex

# ---- Live-Journal (Admin) ----
# Streams a limited set of systemd units to the browser (admin-only).
# We redact obvious secrets (password/token) before sending.
ALLOWED_JOURNAL_UNITS = {"darts-caller.service", "darts-wled.service"}

def redact_journal_line(line: str) -> str:
    if not line:
        return line
    # Hide CLI secrets we commonly pass to darts-caller (e.g. -P password, -U email)
    line = re.sub(r"(?i)(\s-[Pp]\s+)(\S+)", r"\1***", line)
    line = re.sub(r"(?i)(\s-[Uu]\s+)(\S+)", r"\1***", line)
    # Generic patterns
    line = re.sub(r"(?i)(\bpassword\b\s*[:=]\s*)(\S+)", r"\1***", line)
    line = re.sub(r"(?i)(\btoken\b\s*[:=]\s*)(\S+)", r"\1***", line)
    return line


MAX_CAMERAS = 8
MAX_VIDEO_INDEX = 50
STREAM_BASE_PORT = 8081

WIFI_CONNECTION_NAME = "Autodarts-Net"
AUTODARTS_SERVICE = "autodarts.service"

# WLAN-Client (USB-Dongle)
WIFI_INTERFACE = "wlan0"

# Access-Point (Onboard-WLAN)
AP_CONNECTION_NAME = "Autodarts-AP"
AP_INTERFACE = "wlan_ap"

# --- LED / darts-caller / darts-wled ---
DARTS_CALLER_START_CUSTOM = "/var/lib/autodarts/extensions/darts-caller/start-custom.sh"
DARTS_CALLER_DIR = "/var/lib/autodarts/extensions/darts-caller"

DARTS_WLED_DIR = "/var/lib/autodarts/extensions/darts-wled"
DARTS_WLED_SERVICE = "darts-wled.service"

# darts-wled Config (Startscript mit Mapping/Presets) – wir ändern hier NUR die -WEPS Zeile
DARTS_WLED_START_CUSTOM = "/var/lib/autodarts/config/darts-wled/start-custom.sh"

# Multi-WLED (bis zu 3 Targets)
WLED_CONFIG_PATH = "/var/lib/autodarts/wled-targets.json"

# Legacy (alte Single-Variante)
WLED_MDNS_NAME = "Dart-Led.local"
WLED_FLAG_PATH = "/var/lib/autodarts/wled-enabled.json"

# WLED Reachability Cache (damit die Seite schnell lädt)
WLED_STATUS_CACHE: dict[str, tuple[float, dict]] = {}
WLED_STATUS_CACHE_TTL_SEC = 3.0

# --- ADMIN / DOKU ---
ADMIN_GPIO_IMAGE = "/home/peter/autodarts-data/GPIO_Setup.jpeg"

# --- Webpanel Settings (damit man Kleinigkeiten ändern kann, ohne am Script zu schrauben) ---
SETTINGS_PATH = "/var/lib/autodarts/webpanel-settings.json"
AUTODARTS_UPDATE_LOG = "/var/log/autodarts_update.log"
AUTODARTS_UPDATE_STATE = "/var/lib/autodarts/autodarts-update-state.json"
AUTODARTS_UPDATE_CHECK = "/var/lib/autodarts/autodarts-update-check.json"
AUTOUPDATE_SERVICE = "autodartsupdater.service"

# --- Autodarts Versionen (EINFACH pflegen) ---
# Du änderst nur diese EINE Liste (Reihenfolge = Dropdown):
#   "aktuell"  -> installiert immer die neueste Version (wie "latest")
#   "zuletzt"  -> Rollback auf die zuletzt installierte Version (merkt sich das Panel automatisch)
#   "1.0.4"    -> fixe Version (SemVer)
#
# Beispiel (so wie du es beschrieben hast):
#   ["aktuell", "zuletzt", "1.0.4"]
# Wenn später 1.0.6 stabil ist:
#   ["aktuell", "zuletzt", "1.0.6", "1.0.4"]
AUTODARTS_VERSION_MENU = ["aktuell", "zuletzt", "1.0.4", "1.0.5"]

# Datei, in die das Panel automatisch die "zuletzt"-Version schreibt (musst du NICHT anfassen)
AUTODARTS_LAST_VERSION_FILE = "/var/lib/autodarts/autodarts-last-version.json"

_AUTODARTS_LATEST_CACHE = {"ts": 0.0, "ver": None}
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-(?:beta|alpha)\.\d+)?$")

def _menu_token(raw: str) -> str:
    s = (raw or "").strip()
    low = s.lower()
    if low in {"aktuell", "aktuellste", "neueste", "neuste", "latest"}:
        return "__LATEST__"
    if low in {"zuletzt", "rollback", "vorige", "previous", "last"}:
        return "__LAST__"
    return s.lstrip("v").strip()

def autodarts_stable_from_menu() -> str | None:
    """Erste feste SemVer in AUTODARTS_VERSION_MENU gilt als 'stabil'."""
    for x in AUTODARTS_VERSION_MENU:
        tok = _menu_token(str(x))
        if _SEMVER_RE.match(tok):
            return tok
    return None

def autodarts_last_version() -> str | None:
    try:
        p = Path(AUTODARTS_LAST_VERSION_FILE)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8", errors="ignore") or "{}")
        v = str(data.get("last") or "").strip().lstrip("v")
        return v if _SEMVER_RE.match(v) else None
    except Exception:
        return None

def autodarts_set_last_version(v: str) -> None:
    try:
        v = (v or "").strip().lstrip("v")
        if not _SEMVER_RE.match(v):
            return
        Path(AUTODARTS_LAST_VERSION_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(AUTODARTS_LAST_VERSION_FILE).write_text(json.dumps({"last": v}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def autodarts_latest_cached(ttl_s: float = 60.0) -> str | None:
    """Online 'aktuellste' Version (kurz gecached)."""
    try:
        now = time.time()
        ts = float(_AUTODARTS_LATEST_CACHE.get("ts") or 0.0)
        if now - ts < ttl_s:
            return _AUTODARTS_LATEST_CACHE.get("ver")
        ver = fetch_latest_autodarts_version()
        _AUTODARTS_LATEST_CACHE["ts"] = now
        _AUTODARTS_LATEST_CACHE["ver"] = ver
        return ver
    except Exception:
        return None

def build_autodarts_versions_dropdown() -> list[dict]:
    """Dropdown-Optionen aus AUTODARTS_VERSION_MENU (kein Freitext)."""
    stable = autodarts_stable_from_menu()
    last = autodarts_last_version()
    latest = autodarts_latest_cached()

    choices: list[dict] = []
    for x in AUTODARTS_VERSION_MENU:
        tok = _menu_token(str(x))
        if tok == "__LATEST__":
            label = f"Aktuellste (online: {latest})" if latest else "Aktuellste (online: unbekannt)"
            choices.append({"value": "__LATEST__", "label": label})
        elif tok == "__LAST__":
            label = f"Zuletzt (Rollback: {last})" if last else "Zuletzt (Rollback: noch nicht verfügbar)"
            choices.append({"value": "__LAST__", "label": label})
        else:
            if not _SEMVER_RE.match(tok):
                continue
            if stable and tok == stable:
                choices.append({"value": tok, "label": f"Stabil ({tok})"})
            else:
                choices.append({"value": tok, "label": tok})

    # Doppelte raus (falls jemand z.B. 'aktuell' zweimal rein schreibt)
    seen: set[str] = set()
    out: list[dict] = []
    for o in choices:
        v = str(o.get("value") or "")
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(o)
    return out

PINGTEST_STATE_DIR = "/var/lib/autodarts/pingtests"

# --- Webpanel (diese Weboberfläche) Update ---
WEBPANEL_SERVICE = "autodarts-web.service"
WEBPANEL_UPDATE_SCRIPT = "/usr/local/bin/autodarts-webpanel-update.sh"
WEBPANEL_UPDATE_LOG = "/var/log/autodarts_webpanel_update.log"
WEBPANEL_UPDATE_STATE = "/var/lib/autodarts/webpanel-update-state.json"
WEBPANEL_UPDATE_CHECK = "/var/lib/autodarts/webpanel-update-check.json"
STATE_DIR = os.path.dirname(WEBPANEL_UPDATE_STATE)
UVC_BACKUP_ROOT = os.path.join(STATE_DIR, "uvc-backup")

# --- System (apt) Update + Reboot ---
OS_UPDATE_LOG = "/var/log/autodarts_os_update.log"
OS_UPDATE_STATE = "/var/lib/autodarts/os-update-state.json"


# --- Firewall (UFW) ---
# Status wird NICHT bei jedem Seitenaufruf geprüft, sondern nur auf Button-Klick ("Status aktualisieren")
UFW_LOG = "/var/log/autodarts_ufw.log"
UFW_STATE = "/var/lib/autodarts/ufw-state.json"

# Ports die freigegeben werden sollen (idempotent: "ufw allow" kann mehrfach laufen)
UFW_PORT_RULES = [
    "22/tcp",     # SSH
    "3180/tcp",   # Autodarts
    "8079/tcp",   # Dartcaller?
    "3181/tcp",   # Extension
    "80/tcp",     # Webpanel / HTTP
    "21324/udp",  # WLED / Darts
    "5568/udp",
    "6454/udp",
    "4048/udp",
]


# --- Extensions Update (darts-caller / darts-wled) ---
EXTENSIONS_UPDATE_SCRIPT = "/usr/local/bin/autodarts-extensions-update.sh"
EXTENSIONS_UPDATE_LOG = "/var/log/autodarts_extensions_update.log"
EXTENSIONS_UPDATE_STATE = "/var/lib/autodarts/extensions-update-state.json"
EXTENSIONS_UPDATE_LAST = "/var/lib/autodarts/extensions-update-last.json"


# lokale Version (wird durch autodarts-webpanel-update.sh nach /var/lib/autodarts/webpanel-version.txt geschrieben)
WEBPANEL_VERSION_FILE = os.environ.get("WEBPANEL_VERSION_FILE", "/var/lib/autodarts/webpanel-version.txt")

# Wenn du die Version lieber direkt im Script pflegen willst: hier eintragen.
# Leer lassen ("") um wieder die Version aus WEBPANEL_VERSION_FILE / version.txt zu lesen.
WEBPANEL_HARDCODED_VERSION = "1.48"


# Remote (GitHub Raw) – kann per ENV überschrieben werden
WEBPANEL_RAW_BASE = os.environ.get(
    "WEBPANEL_RAW_BASE",
    "https://raw.githubusercontent.com/jumbo1250/Autodarts-Webinterface-Installation/main/latest",
)
WEBPANEL_VERSION_URL = os.environ.get("WEBPANEL_VERSION_URL", WEBPANEL_RAW_BASE + "/version.txt")

# Fallback falls keine lokale Version-Datei existiert
WEBPANEL_UI_FALLBACK_VERSION = "1.20"


DEFAULT_SETTINGS = {
    "admin_password": "1234",
    "ap_ssid_choices": [f"Autodartsinstall{i}" for i in range(1, 11)],
    # Wenn leer/fehlt, versuchen wir es automatisch über systemd/Dateipfade zu finden
    "autodarts_update_cmd": "",

    # --- Autodarts Version-Pinning (für Dropdown / "stabile Version") ---
    # Wenn leer, zeigt die UI nur den "stabile Version"-Button (falls gesetzt) bzw. keinen Dropdown.
    # Beispiel: ["0.16.0", "0.16.1", "0.16.2"]
    "autodarts_versions": [],

    # Optional: Version, die der Button "Auf stabile Version wechseln" installieren soll.
    # Beispiel: "0.16.0"
    "autodarts_stable_version": "",

    # Standard: Auto-Update soll AUS sein (wir deaktivieren den Service einmalig beim ersten Start nach Webpanel-Update).
    # Wer das nicht möchte, kann hier True setzen.
    "autoupdate_default_enabled": False,
}



def load_settings() -> dict:
    cfg = {}
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
    except Exception:
        cfg = {}

    merged = DEFAULT_SETTINGS.copy()
    for k in merged.keys():
        if k in cfg:
            merged[k] = cfg.get(k)

    # Normalize
    pw = str(merged.get("admin_password", "1234") or "1234").strip()
    merged["admin_password"] = pw if pw else "1234"

    choices = merged.get("ap_ssid_choices")
    if not isinstance(choices, list) or not choices:
        choices = DEFAULT_SETTINGS["ap_ssid_choices"]
    # unique + max length 32
    uniq = []
    for x in choices:
        s = str(x).strip()
        if not s or len(s) > 32:
            continue
        if s not in uniq:
            uniq.append(s)
    merged["ap_ssid_choices"] = uniq or DEFAULT_SETTINGS["ap_ssid_choices"]

    merged["autodarts_update_cmd"] = str(merged.get("autodarts_update_cmd") or "").strip()

    # --- Autodarts Versionsliste / stabile Version (für UI) ---
    def _sanitize_version_str(v: str) -> str:
        v = (v or "").strip()
        v = v.lstrip("v").strip()
        low = v.lower()

        # Zulassen: 0.16.0 oder 0.16.0-beta.1 oder Sonderwerte (Dropdown)
        # "latest" wird in der UI als "Aktuellste" angezeigt (damit es jeder versteht)
        if low in ("beta",):
            return "Beta"
        if low in ("latest", "aktuellste", "neueste", "neuste"):
            return "Aktuellste"
        if re.match(r"^\d+\.\d+\.\d+(?:-(?:beta|alpha)\.\d+)?$", v):
            return v
        return ""

    versions = merged.get("autodarts_versions")
    if not isinstance(versions, list):
        versions = []
    vclean = []
    for x in versions:
        s = _sanitize_version_str(str(x))
        if s and s not in vclean:
            vclean.append(s)
    merged["autodarts_versions"] = vclean

    stable = _sanitize_version_str(str(merged.get("autodarts_stable_version") or ""))
    merged["autodarts_stable_version"] = stable

    merged["autoupdate_default_enabled"] = bool(merged.get("autoupdate_default_enabled", False))

    return merged


# --- Settings (werden automatisch neu geladen, wenn sich webpanel-settings.json ändert) ---
SETTINGS = load_settings()
ADMIN_PASSWORD = SETTINGS.get("admin_password", "admin")
AP_SSID_CHOICES = SETTINGS.get("ap_ssid_choices", [])

_SETTINGS_MTIME = None

def _settings_mtime():
    try:
        return os.stat(SETTINGS_PATH).st_mtime
    except Exception:
        return None

def refresh_settings_if_needed(force: bool = False) -> None:
    global SETTINGS, ADMIN_PASSWORD, AP_SSID_CHOICES, _SETTINGS_MTIME
    mt = _settings_mtime()
    if force or (mt != _SETTINGS_MTIME):
        SETTINGS = load_settings()
        ADMIN_PASSWORD = SETTINGS.get("admin_password", ADMIN_PASSWORD)
        AP_SSID_CHOICES = SETTINGS.get("ap_ssid_choices", AP_SSID_CHOICES)
        _SETTINGS_MTIME = mt

def get_autodarts_versions_choices() -> list[dict]:
    """Liste der erlaubten Versionen für das Dropdown.

    Quelle ist AUTODARTS_VERSION_MENU (oben im Script). Kein Freitext.
    """
    return build_autodarts_versions_dropdown()



@app.before_request
def _autoload_settings():
    refresh_settings_if_needed()

AUTODARTS_VERSION_CACHE = {"ts": 0.0, "v": None}
AUTODARTS_VERSION_CACHE_TTL_SEC = 10.0

# === leichte Caches für Statusdaten (reduziert subprocess-Last) ===
INDEX_STATS_CACHE = {'ts': 0.0, 'data': None}
INDEX_STATS_TTL_SEC = 2.0  # Startseite: Statuswerte max. alle 2s neu holen

WIFI_SIGNAL_CACHE = {'ts': 0.0, 'v': None}
WIFI_SIGNAL_CACHE_TTL_SEC = 5.0  # Signalstärke nur auf Knopfdruck, kurz cachen


PI_MONITOR_SCRIPT = "/usr/local/bin/pi_monitor_test.sh"
PI_MONITOR_CSV = "/var/log/pi_monitor_test.csv"
PI_MONITOR_README = "/var/log/pi_monitor_test_README.txt"

PI_MONITOR_STATE = "/tmp/autodarts_pi_monitor_state.json"
PI_MONITOR_PIDFILE = "/tmp/autodarts_pi_monitor.pid"
PI_MONITOR_OUTLOG = "/var/log/pi_monitor_test.out"

EXTENSIONS_DIR = "/var/lib/autodarts/extensions"
USR_LOCAL_BIN_DIR = "/usr/local/bin"

# =====================


# ---------------- WLAN / AP ----------------

def get_wifi_status():
    """
    Liefert (ssid, ip) für den WLAN-Dongle (WIFI_INTERFACE) oder (None, None),
    wenn er nicht mit einem WLAN verbunden ist.

    ssid = echte WLAN-SSID (Name des Routers), nicht nur der Verbindungsname.
    """
    ssid = None
    ip = None
    dev = WIFI_INTERFACE
    conn_name = None

    # 1) herausfinden, ob das Interface verbunden ist und wie die Connection heißt
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.strip().split(":", 2)
                if len(parts) >= 3 and parts[0] == dev:
                    state = parts[1]
                    conn_name = parts[2] or None
                    if state != "connected":
                        conn_name = None
                    break
    except Exception:
        conn_name = None

    if not conn_name:
        return None, None

    # 2) SSID aus der Connection auslesen
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", conn_name],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("802-11-wireless.ssid:"):
                    val = line.split(":", 1)[1].strip()
                    if val:
                        ssid = val
                    break
    except Exception:
        pass

    # 3) IPv4-Adresse auslesen
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", dev],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("IP4.ADDRESS"):
                    ip_addr = line.split(":", 1)[1].strip()
                    if ip_addr:
                        ip = ip_addr.split("/", 1)[0]
                    break
    except Exception:
        pass

    return ssid, ip

def get_lan_status():
    """
    Liefert die IPv4-Adresse von eth0 oder None,
    wenn keine aktive LAN-Verbindung vorhanden ist.
    """
    ip = None

    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,STATE", "device"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode != 0:
            return None

        eth_connected = False
        for line in result.stdout.splitlines():
            parts = line.strip().split(":", 1)
            if len(parts) >= 2 and parts[0] == "eth0":
                if parts[1] == "connected":
                    eth_connected = True
                break

        if not eth_connected:
            return None
    except Exception:
        return None

    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", "eth0"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("IP4.ADDRESS"):
                    ip_addr = line.split(":", 1)[1].strip()
                    if ip_addr:
                        ip = ip_addr.split("/", 1)[0]
                    break
    except Exception:
        pass

    return ip

def _get_default_route_interface() -> str | None:
    """Return interface used for the default route (best proxy for "home network" interface)."""
    try:
        r = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
        if r.returncode != 0:
            return None
        # example: "1.1.1.1 via 192.168.1.1 dev wlan0 src 192.168.1.50 uid 1000"
        m = re.search(r"\bdev\s+(\S+)", r.stdout or "")
        if not m:
            return None
        dev = m.group(1).strip()
        if dev == AP_INTERFACE:
            return None
        return dev
    except Exception:
        return None


def _get_connected_wifi_interface(prefer: str | None = None) -> str | None:
    """
    Best-effort: return a connected WiFi interface (via nmcli), excluding the AP interface/profile.
    Preference order:
      1) 'prefer' if connected
      2) interface connected to WIFI_CONNECTION_NAME (e.g. Autodarts-Net)
      3) first connected WiFi excluding AP
    """
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if r.returncode != 0:
            return None

        connected: list[tuple[str, str]] = []
        for line in (r.stdout or "").splitlines():
            parts = line.split(":", 3)
            if len(parts) != 4:
                continue
            dev, typ, state, conn = [p.strip() for p in parts]
            if typ != "wifi" or state != "connected":
                continue
            # exclude AP interface / AP connection profile
            if dev == AP_INTERFACE or conn == AP_CONNECTION_NAME:
                continue
            connected.append((dev, conn))

        if not connected:
            return None

        if prefer:
            for dev, _conn in connected:
                if dev == prefer:
                    return dev

        for dev, conn in connected:
            if WIFI_CONNECTION_NAME and conn == WIFI_CONNECTION_NAME:
                return dev

        return connected[0][0]
    except Exception:
        return None

def _wifi_signal_from_proc(iface: str) -> int | None:
    """
    Very lightweight signal read via /proc/net/wireless.
    Returns percent (0..100) or None if iface not present / not wireless.
    """
    try:
        with open("/proc/net/wireless", "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        for line in lines:
            line = line.strip()
            if not line.startswith(iface + ":"):
                continue
            # Example columns: iface: status link level noise ...
            # link quality is usually 0..70 in column 2
            parts = line.split()
            if len(parts) < 3:
                return None
            # parts[1] is status, parts[2] is link quality (e.g. "70.")
            q_str = parts[2].strip().rstrip(".")
            q = float(q_str)
            # Convert 0..70 to 0..100
            pct = int(round((q / 70.0) * 100.0))
            if pct < 0:
                pct = 0
            if pct > 100:
                pct = 100
            return pct
    except Exception:
        return None


def _wifi_signal_from_iw(iface: str) -> int | None:
    """Get signal strength as percent using 'iw dev <iface> link' (no scan)."""
    try:
        r = subprocess.run(
            ["iw", "dev", iface, "link"],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
        if r.returncode != 0:
            return None
        out = (r.stdout or "").strip()
        if not out or "Not connected" in out:
            return None
        m = re.search(r"signal:\s*(-?\d+)\s*dBm", out)
        if not m:
            return None
        dbm = int(m.group(1))
        # Map -90..-30 dBm roughly to 0..100%
        pct = int(round((dbm + 90) * 100 / 60))
        if pct < 0:
            pct = 0
        if pct > 100:
            pct = 100
        return pct
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _wifi_signal_from_nmcli(iface: str) -> int | None:
    """Fallback: nmcli scan-less signal strength (0..100)."""
    try:
        r = subprocess.run(
            ["nmcli", "-t", "--rescan", "no", "-f", "IN-USE,SIGNAL,SSID", "device", "wifi", "list", "ifname", iface],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if r.returncode != 0:
            return None
        for line in (r.stdout or "").splitlines():
            # Format usually: *:70:MyWifi
            parts = line.split(":", 2)
            if len(parts) >= 2 and parts[0].strip() in ("*", "yes", "Yes", "YES"):
                try:
                    return int(parts[1].strip())
                except Exception:
                    return None
    except Exception:
        return None
    return None


def get_wifi_signal_percent() -> int | None:
    """
    Liefert die Signalstärke (0-100) **für die Heimnetz-Verbindung** (nicht den AP).

    Ressourcenschonend:
      1) /proc/net/wireless (kein Scan)
      2) iw dev <iface> link (kein Scan)
      3) nmcli --rescan no (Fallback)
    """
    # Ziel: Heimnetz-Interface finden (wlan0), AP (wlan_ap) ignorieren
    home_iface = None

    # 1) Wenn WIFI_INTERFACE gesetzt ist (z.B. wlan0), das bevorzugen
    if WIFI_INTERFACE and WIFI_INTERFACE != AP_INTERFACE:
        home_iface = WIFI_INTERFACE

    # 2) Falls das nicht klappt: Interface mit Default-Route (Internet)
    if not home_iface:
        home_iface = _get_default_route_interface()

    # 3) Fallback: nmcli connected wifi (AP ausgeschlossen)
    if not home_iface:
        home_iface = _get_connected_wifi_interface(prefer=WIFI_INTERFACE if WIFI_INTERFACE else None)

    candidates: list[str] = []
    if home_iface:
        candidates.append(home_iface)

    # Safety fallback: wenn oben nix gefunden, aber WIFI_INTERFACE existiert
    if WIFI_INTERFACE and WIFI_INTERFACE not in candidates and WIFI_INTERFACE != AP_INTERFACE:
        candidates.append(WIFI_INTERFACE)

    for dev in candidates:
        if not dev or dev == AP_INTERFACE:
            continue

        # 1) /proc/net/wireless
        sig = _wifi_signal_from_proc(dev)
        if sig is not None:
            return sig

        # 2) iw link
        sig = _wifi_signal_from_iw(dev)
        if sig is not None:
            return sig

        # 3) nmcli fallback
        sig = _wifi_signal_from_nmcli(dev)
        if sig is not None:
            return sig

    return None


def wifi_dongle_present() -> bool:
    """Prüft, ob der WLAN-USB-Dongle (WIFI_INTERFACE) als WiFi-Device beim NetworkManager sichtbar ist."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode != 0:
            return False

        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2:
                dev, dev_type = parts[0], parts[1]
                if dev == WIFI_INTERFACE and dev_type == "wifi":
                    return True
        return False
    except Exception:
        return False


def interpret_nmcli_error(stdout: str, stderr: str):
    """
    Macht aus der nmcli-Ausgabe eine verständliche Meldung + kurzen Debug-Hinweis.
    """
    text = (stderr or stdout or "").strip()
    short = text.splitlines()[0] if text else ""
    lower = text.lower()

    user_msg = "Verbindung konnte nicht hergestellt werden."

    if "no device" in lower or "unknown device" in lower:
        user_msg = "WLAN-USB-Stick wird nicht richtig erkannt."
    elif "no wifi device" in lower:
        user_msg = "Kein gültiges WLAN-Gerät gefunden (WLAN-Stick fehlt oder wird nicht richtig erkannt)."
    elif "no network with ssid" in lower or "wifi network could not be found" in lower or "ssid not found" in lower:
        user_msg = "Der eingegebene WLAN-Name (SSID) wurde nicht gefunden. Bitte Schreibweise und Abstand zum Router prüfen."
    elif "wrong password" in lower or "secrets were required, but not provided" in lower or "invalid passphrase" in lower:
        user_msg = "Das WLAN-Passwort scheint nicht zu stimmen. Bitte erneut eingeben."
    elif "no suitable device found" in lower or "profile is not compatible with device" in lower:
        user_msg = "Das WLAN-Profil passt nicht zum Gerät (z.B. falsches Interface wie eth0 statt WLAN-Stick)."
    elif "activation failed" in lower:
        user_msg = "Der Router hat die Verbindung abgelehnt oder es gibt ein Problem mit den WLAN-Einstellungen."

    debug_msg = f" (Details für Profis: {short})" if short else ""
    return user_msg + debug_msg


def soft_reset_wifi_dongle():
    """
    Resetet nur den WLAN-Client (USB-Dongle, WIFI_INTERFACE),
    ohne den Access-Point (AP_INTERFACE) zu beeinflussen.
    """
    try:
        subprocess.run(
            ["nmcli", "device", "disconnect", WIFI_INTERFACE],
            capture_output=True,
            text=True,
        )
    except Exception:
        pass

    time.sleep(3)


def get_ap_ssid():
    """Liefert die aktuelle SSID des Access-Points (AP_CONNECTION_NAME) oder None."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", AP_CONNECTION_NAME],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode != 0:
            return None

        for line in result.stdout.splitlines():
            if line.startswith("802-11-wireless.ssid:"):
                val = line.split(":", 1)[1].strip()
                if val:
                    return val
    except Exception:
        pass
    return None


# ---------------- System / Stats ----------------

def is_autodarts_active() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", AUTODARTS_SERVICE],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def get_system_stats():
    """CPU-Last (grob), RAM und Temperatur."""
    cpu_pct = None
    mem_used = None
    mem_total = None
    temp_c = None

    # CPU
    try:
        with open("/proc/loadavg", "r") as f:
            load1 = float(f.read().split()[0])
        cores = multiprocessing.cpu_count()
        cpu_pct = round(min(100.0, (load1 / cores) * 100.0), 1)
    except Exception:
        pass

    # RAM
    try:
        with open("/proc/meminfo", "r") as f:
            data = f.read().splitlines()
        kv = {}
        for line in data:
            parts = line.split(":")
            if len(parts) == 2:
                key = parts[0].strip()
                val = parts[1].strip().split()[0]
                kv[key] = int(val)
        mem_total_kb = kv.get("MemTotal")
        mem_avail_kb = kv.get("MemAvailable")
        if mem_total_kb and mem_avail_kb:
            mem_used_kb = mem_total_kb - mem_avail_kb
            mem_total = round(mem_total_kb / 1024 / 1024, 2)
            mem_used = round(mem_used_kb / 1024 / 1024, 2)
    except Exception:
        pass

    # Temperatur
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if out.returncode == 0:
            s = out.stdout.strip()
            if "temp=" in s and "'C" in s:
                val = s.split("temp=")[1].split("'C")[0]
                temp_c = float(val)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    if temp_c is None:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                temp_c = int(f.read().strip()) / 1000.0
        except Exception:
            pass

    return cpu_pct, mem_used, mem_total, temp_c

def get_index_stats_cached():
    """
    Sammel-Status für die Startseite (mit kurzem Cache), damit nicht bei jedem Reload
    viele externe Tools gestartet werden (systemctl/nmcli/vcgencmd).
    """
    now = time.time()
    try:
        if INDEX_STATS_CACHE.get('data') and (now - float(INDEX_STATS_CACHE.get('ts', 0.0))) < INDEX_STATS_TTL_SEC:
            return INDEX_STATS_CACHE['data']
    except Exception:
        pass

    ssid, ip = get_wifi_status()
    lan_ip = get_lan_status()

    autodarts_active = is_autodarts_active()
    autodarts_version = get_autodarts_version()
    cpu_pct, mem_used, mem_total, temp_c = get_system_stats()
    current_ap_ssid = get_ap_ssid()
    ping_uplink_iface = get_ping_uplink_interface()
    net_ok = bool(ping_uplink_iface)
    ping_uplink_label = ping_iface_label(ping_uplink_iface) if ping_uplink_iface else ""

    wifi_ok = bool(ssid and ip)
    lan_ok = bool(lan_ip)

    # Dongle nur dann problematisch, wenn weder WLAN noch LAN aktiv ist
    dongle_ok = lan_ok or wifi_ok or wifi_dongle_present()

    data = (
        ssid, ip, lan_ip,
        autodarts_active, autodarts_version,
        cpu_pct, mem_used, mem_total, temp_c,
        wifi_ok, lan_ok, dongle_ok,
        net_ok, ping_uplink_label,
        current_ap_ssid,
    )
    try:
        INDEX_STATS_CACHE['ts'] = now
        INDEX_STATS_CACHE['data'] = data
    except Exception:
        pass
    return data



# ---------------- Notes / Cam config ----------------


def load_cam_config():
    try:
        with open(CAM_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cam_config(config: dict):
    os.makedirs(os.path.dirname(CAM_CONFIG_PATH), exist_ok=True)
    with open(CAM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)



def _camera_mode_runtime_active(cfg: dict | None, autodarts_active: bool | None = None) -> bool:
    cfg = cfg or {}
    if not bool(cfg.get("camera_mode", False)):
        return False
    if str(cfg.get("camera_mode_session") or "") != CAMERA_MODE_SESSION:
        return False
    if autodarts_active is True:
        return False
    return True


def _set_camera_mode_state(cfg: dict, enabled: bool):
    cfg["camera_mode"] = bool(enabled)
    if enabled:
        cfg["camera_mode_session"] = CAMERA_MODE_SESSION
    else:
        cfg["camera_mode"] = False
        cfg.pop("camera_mode_session", None)



# --- V4L2 Probe Helpers (robustere Kamera-Auswahl & bessere Fehlermeldungen) ---
V4L2CTL_TIMEOUT = 1.5

def _v4l2ctl(args: list[str], timeout: float = V4L2CTL_TIMEOUT):
    """Run v4l2-ctl with timeout; returns CompletedProcess or None."""
    try:
        return subprocess.run(
            ["v4l2-ctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

def probe_v4l2_device(dev: str) -> dict:
    """Probe device for pixel formats and discrete resolutions.

    Returns:
      {
        ok: bool,
        formats: set[str],
        resolutions: dict[str, list[tuple[int,int]]],  # fmt -> [(w,h), ...]
        error: str|None,
        raw: str
      }
    """
    r = _v4l2ctl(["-d", dev, "--list-formats-ext"])
    if not r:
        return {"ok": False, "formats": set(), "resolutions": {}, "error": "v4l2-ctl nicht verfügbar oder Timeout.", "raw": ""}

    raw = (r.stdout or "") + ("\n" + (r.stderr or "") if r.stderr else "")
    if r.returncode != 0:
        # Häufig: Permission denied / Not a capture device / busy
        err = (r.stderr or r.stdout or "").strip() or f"v4l2-ctl returncode {r.returncode}"
        return {"ok": False, "formats": set(), "resolutions": {}, "error": err, "raw": raw}

    fmt = None
    formats: set[str] = set()
    resolutions: dict[str, list[tuple[int, int]]] = {}

    re_fmt = re.compile(r"(?:Pixel\s+Format:\s+\'([A-Z0-9]+)\'|\[\d+\]:\s+\'([A-Z0-9]+)\')")
    re_size = re.compile(r"Size:\s+Discrete\s+(\d+)x(\d+)")
    for line in (r.stdout or "").splitlines():
        m = re_fmt.search(line)
        if m:
            fmt = m.group(1) or m.group(2)
            formats.add(fmt)
            resolutions.setdefault(fmt, [])
            continue
        m = re_size.search(line)
        if m and fmt:
            w, h = int(m.group(1)), int(m.group(2))
            if (w, h) not in resolutions[fmt]:
                resolutions[fmt].append((w, h))

    return {"ok": True, "formats": formats, "resolutions": resolutions, "error": None, "raw": raw}

def _best_resolution_for_formats(resolutions: dict[str, list[tuple[int,int]]], preferred_formats: list[str]) -> tuple[str|None, str|None]:
    """Pick best (format, WxH) based on preferences and available discrete sizes."""
    # Prefer common sizes if present, otherwise max area.
    preferred_sizes = [(1920,1080),(1600,1200),(1280,720),(1024,768),(800,600),(640,480)]
    for fmt in preferred_formats:
        sizes = resolutions.get(fmt) or []
        if not sizes:
            continue
        for wh in preferred_sizes:
            if wh in sizes:
                return fmt, f"{wh[0]}x{wh[1]}"
        # fallback: largest area
        w,h = max(sizes, key=lambda x: x[0]*x[1])
        return fmt, f"{w}x{h}"
    return None, None

def _pick_best_video_device(video_devs: list[str]) -> str | None:
    """Pick best /dev/videoX from a physical camera group.

    Preference:
      - device that supports MJPG (best for mjpg_streamer)
      - else device that supports YUYV
      - else first device that at least responds to v4l2-ctl
    """
    best = None
    best_score = -1
    for dev in video_devs:
        p = probe_v4l2_device(dev)
        if not p.get("ok"):
            # still might be usable; but score low
            score = 0
        else:
            fmts = p.get("formats", set())
            if "MJPG" in fmts:
                score = 3
            elif "YUYV" in fmts:
                score = 2
            elif fmts:
                score = 1
            else:
                score = 0
        if score > best_score:
            best_score = score
            best = dev
    return best

def _v4l2_device_info(dev: str) -> dict:
    """Return basic v4l2 device info using 'v4l2-ctl -D'."""
    r = _v4l2ctl(["-d", dev, "-D"], timeout=0.9)
    if not r or r.returncode != 0:
        return {}
    info = {}
    for line in (r.stdout or "").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        v = v.strip()
        if k in ("driver name", "card type", "bus info"):
            info[k] = v
    return info


def _is_probably_camera_device(dev: str) -> bool:
    """Heuristic to avoid false positives like codec/ISP/decoder nodes."""
    info = _v4l2_device_info(dev)
    card = (info.get("card type") or "").lower()
    bus = (info.get("bus info") or "").lower()

    bad = ["codec", "isp", "rpivid", "v4l2loopback", "loopback", "virtual", "m2m", "mem2mem", "decoder", "encoder"]
    if any(b in card for b in bad) or any(b in bus for b in bad):
        return False

    # Must have at least one format we can stream
    p = probe_v4l2_device(dev)
    if not p.get("ok"):
        return False
    fmts = p.get("formats", set()) or set()
    if not fmts:
        return False

    # Prefer USB cameras
    if "usb" in bus:
        return True

    # CSI / sensor-style names
    good = ["camera", "webcam", "uvc", "unicam", "csi", "ov", "imx", "ar", "gc", "s5k"]
    if any(g in card for g in good) or any(g in bus for g in good):
        return True

    # Unknown: be conservative to prevent ghost-devices when no camera is attached
    return False



def detect_cameras(desired_count: int):
    """
    Erkennt Kameras möglichst zuverlässig.

    1) Wenn verfügbar, nutzt es 'v4l2-ctl --list-devices', um physische USB-Kameras zu gruppieren
       und pro Kamera genau EIN /dev/videoX zu wählen (verhindert Doppel-/Meta-Devices).
    2) Fallback: einfache /dev/video0..N Suche.
    """
    # 1) Versuch: v4l2-ctl verwenden
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            lines = result.stdout.splitlines()

            groups = []  # Liste von (name, [video-devices])
            current_name = None
            current_videos = []

            for line in lines:
                if line.strip() == "":
                    continue

                if not line.startswith("	") and not line.startswith(" "):
                    # Neue Gerätegruppe beginnt
                    if current_name and current_videos:
                        groups.append((current_name, current_videos))
                    current_name = line.strip().rstrip(":")
                    current_videos = []
                else:
                    # Eingrückte Zeile -> Device
                    path = line.strip()
                    if path.startswith("/dev/video"):
                        current_videos.append(path)

            # letzte Gruppe übernehmen
            if current_name and current_videos:
                groups.append((current_name, current_videos))

            # Gruppen filtern: echte Kameras bevorzugen, System/Meta-Geräte eher raus
            def _looks_like_camera(name: str) -> bool:
                n = (name or "").lower()

                # harte Ausschlüsse: typische System/Codec/ISP/Decoder-Geräte
                bad = ["codec", "isp", "rpivid", "v4l2loopback", "loopback", "virtual", "m2m", "mem2mem", "decoder", "encoder"]
                if any(b in n for b in bad):
                    return False

                # USB-Kameras erkennt man meist direkt
                if "usb" in n or "uvc" in n or "webcam" in n or "camera" in n:
                    return True

                # CSI/Sensoren (ov9732, imx219, ...)
                sensor = ["ov", "imx", "ar", "gc", "s5k", "unicam", "csi"]
                if any(s in n for s in sensor):
                    return True

                # Default: konservativ, sonst findet man ohne Kamera gerne 'Geistergeräte'
                return False

            cam_groups = [g for g in groups if _looks_like_camera(g[0])]

            devices = []
            for name, videos in cam_groups:
                videos_sorted = sorted(videos)
                # Einige Kameras liefern mehrere /dev/videoX Nodes (Meta/H264/etc.).
                # Wir wählen nach Möglichkeit den Node, der MJPG (oder YUYV) anbietet.
                dev = _pick_best_video_device(videos_sorted) or videos_sorted[0]
                if _is_probably_camera_device(dev):
                    devices.append(dev)
                if len(devices) >= desired_count:
                    break

            if devices:
                return devices
    except FileNotFoundError:
        # v4l2-ctl nicht vorhanden -> Fallback
        pass
    except Exception as e:
        print(f"[autodarts-web] Warnung detect_cameras mit v4l2-ctl: {e}")

    # 2) Fallback: einfache /dev/video0..N-Suche
    found = []
    for idx in range(MAX_VIDEO_INDEX):
        dev = f"/dev/video{idx}"
        if os.path.exists(dev) and _is_probably_camera_device(dev):
            found.append(dev)
            if len(found) >= desired_count:
                break
    return found


def _camera_symlink_map() -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {"by-id": {}, "by-path": {}}
    for kind, base in (("by-id", "/dev/v4l/by-id"), ("by-path", "/dev/v4l/by-path")):
        if not os.path.isdir(base):
            continue
        try:
            for name in sorted(os.listdir(base)):
                full = os.path.join(base, name)
                try:
                    real = os.path.realpath(full)
                except Exception:
                    continue
                if not real.startswith("/dev/video"):
                    continue
                out[kind].setdefault(real, []).append(full)
        except Exception:
            continue
    return out


def _camera_aliases_for_device(dev: str, symlink_map: dict[str, dict[str, list[str]]] | None = None) -> tuple[list[str], list[str]]:
    symlink_map = symlink_map or _camera_symlink_map()
    by_id = list(symlink_map.get("by-id", {}).get(dev, []))
    by_path = list(symlink_map.get("by-path", {}).get(dev, []))
    return by_id, by_path


def _sanitize_camera_key(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "camera"


def _camera_stable_id(group_name: str, dev: str, symlink_map: dict[str, dict[str, list[str]]] | None = None) -> str:
    symlink_map = symlink_map or _camera_symlink_map()
    by_id, by_path = _camera_aliases_for_device(dev, symlink_map)
    if by_id:
        return f"byid:{os.path.basename(by_id[0])}"
    if by_path:
        return f"bypath:{os.path.basename(by_path[0])}"

    info = _v4l2_device_info(dev)
    bus = (info.get("bus info") or "").strip()
    card = (info.get("card type") or group_name or dev).strip()
    if bus:
        return f"bus:{_sanitize_camera_key(bus)}:{_sanitize_camera_key(card)}"
    return f"dev:{_sanitize_camera_key(group_name or dev)}"


def _camera_label(group_name: str, dev: str, symlink_map: dict[str, dict[str, list[str]]] | None = None) -> str:
    symlink_map = symlink_map or _camera_symlink_map()
    info = _v4l2_device_info(dev)
    base = (group_name or info.get("card type") or dev or "Kamera").strip().rstrip(":")
    by_id, by_path = _camera_aliases_for_device(dev, symlink_map)

    extra = ""
    if by_id:
        extra = os.path.basename(by_id[0]).replace("-video-index0", "")
    elif by_path:
        extra = os.path.basename(by_path[0]).replace("-video-index0", "")
    elif info.get("bus info"):
        extra = str(info.get("bus info") or "").strip()

    if extra and extra not in base:
        return f"{base} [{extra}]"
    return base


def _looks_like_camera_group(name: str) -> bool:
    n = (name or "").lower()
    bad = ["codec", "isp", "rpivid", "v4l2loopback", "loopback", "virtual", "m2m", "mem2mem", "decoder", "encoder"]
    if any(b in n for b in bad):
        return False
    if "usb" in n or "uvc" in n or "webcam" in n or "camera" in n:
        return True
    sensor = ["ov", "imx", "ar", "gc", "s5k", "unicam", "csi"]
    if any(s in n for s in sensor):
        return True
    return False


def detect_camera_inventory(limit: int = MAX_CAMERAS) -> list[dict]:
    limit = max(0, min(MAX_CAMERAS, int(limit)))
    symlink_map = _camera_symlink_map()
    cameras: list[dict] = []

    try:
        result = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True)
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            groups: list[tuple[str, list[str]]] = []
            current_name = None
            current_videos: list[str] = []

            for line in lines:
                if line.strip() == "":
                    continue
                if not line.startswith("	") and not line.startswith(" "):
                    if current_name and current_videos:
                        groups.append((current_name, current_videos))
                    current_name = line.strip().rstrip(":")
                    current_videos = []
                else:
                    dev_path = line.strip()
                    if dev_path.startswith("/dev/video"):
                        current_videos.append(dev_path)

            if current_name and current_videos:
                groups.append((current_name, current_videos))

            for name, videos in groups:
                if not _looks_like_camera_group(name):
                    continue
                videos_sorted = sorted(videos)
                preferred_dev = _pick_best_video_device(videos_sorted) or videos_sorted[0]
                if not _is_probably_camera_device(preferred_dev):
                    continue
                by_id, by_path = _camera_aliases_for_device(preferred_dev, symlink_map)
                info = _v4l2_device_info(preferred_dev)
                cameras.append({
                    "id": _camera_stable_id(name, preferred_dev, symlink_map),
                    "label": _camera_label(name, preferred_dev, symlink_map),
                    "group_name": name,
                    "preferred_dev": preferred_dev,
                    "video_devs": videos_sorted,
                    "bus_info": info.get("bus info", ""),
                    "card_type": info.get("card type", ""),
                    "by_id": by_id[0] if by_id else "",
                    "by_path": by_path[0] if by_path else "",
                })
                if limit and len(cameras) >= limit:
                    break
            if cameras:
                return cameras
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[autodarts-web] Warnung detect_camera_inventory mit v4l2-ctl: {e}")

    for idx in range(MAX_VIDEO_INDEX):
        dev = f"/dev/video{idx}"
        if not os.path.exists(dev) or not _is_probably_camera_device(dev):
            continue
        by_id, by_path = _camera_aliases_for_device(dev, symlink_map)
        info = _v4l2_device_info(dev)
        name = info.get("card type") or dev
        cameras.append({
            "id": _camera_stable_id(name, dev, symlink_map),
            "label": _camera_label(name, dev, symlink_map),
            "group_name": name,
            "preferred_dev": dev,
            "video_devs": [dev],
            "bus_info": info.get("bus info", ""),
            "card_type": info.get("card type", ""),
            "by_id": by_id[0] if by_id else "",
            "by_path": by_path[0] if by_path else "",
        })
        if limit and len(cameras) >= limit:
            break
    return cameras


def _legacy_inventory_from_devices(devices: list[str]) -> list[dict]:
    symlink_map = _camera_symlink_map()
    out: list[dict] = []
    for dev in devices or []:
        if not isinstance(dev, str) or not dev.strip():
            continue
        dev = dev.strip()
        info = _v4l2_device_info(dev)
        name = info.get("card type") or dev
        by_id, by_path = _camera_aliases_for_device(dev, symlink_map)
        out.append({
            "id": _camera_stable_id(name, dev, symlink_map),
            "label": _camera_label(name, dev, symlink_map),
            "group_name": name,
            "preferred_dev": dev,
            "video_devs": [dev],
            "bus_info": info.get("bus info", ""),
            "card_type": info.get("card type", ""),
            "by_id": by_id[0] if by_id else "",
            "by_path": by_path[0] if by_path else "",
        })
    return out


def _normalize_camera_slots(camera_inventory: list[dict], stored_slots) -> list[dict]:
    cams = [c for c in (camera_inventory or []) if isinstance(c, dict) and str(c.get("id") or "").strip()]
    by_id = {str(c.get("id")): c for c in cams}
    camera_ids = [str(c.get("id")) for c in cams]
    raw_slots = stored_slots if isinstance(stored_slots, list) else []

    slots: list[dict] = []
    used: set[str] = set()
    for idx in range(len(cams)):
        chosen = ""
        if idx < len(raw_slots):
            raw = raw_slots[idx]
            if isinstance(raw, dict):
                candidate = str(raw.get("camera_id") or "").strip()
            else:
                candidate = str(raw or "").strip()
            if candidate in by_id and candidate not in used:
                chosen = candidate

        if not chosen:
            for candidate in camera_ids:
                if candidate not in used:
                    chosen = candidate
                    break

        if chosen:
            used.add(chosen)
        cam = by_id.get(chosen, {})
        slots.append({
            "slot": idx + 1,
            "camera_id": chosen,
            "label": cam.get("label", ""),
            "device": cam.get("preferred_dev", ""),
        })
    return slots


def _devices_for_slots(camera_inventory: list[dict], camera_slots: list[dict]) -> list[str]:
    by_id = {str(c.get("id")): c for c in (camera_inventory or []) if isinstance(c, dict)}
    out: list[str] = []
    for slot in camera_slots or []:
        cam = by_id.get(str((slot or {}).get("camera_id") or ""))
        if cam and cam.get("preferred_dev"):
            out.append(str(cam.get("preferred_dev")))
    return out

# ---------------- systemd helpers ----------------

def service_exists(service_name: str) -> bool:
    candidates = [
        f"/etc/systemd/system/{service_name}",
        f"/lib/systemd/system/{service_name}",
        f"/usr/lib/systemd/system/{service_name}",
    ]
    return any(os.path.exists(p) for p in candidates)


# systemctl helper (verhindert Hänger durch blockierende systemctl-Aufrufe)
SYSTEMCTL_CHECK_TIMEOUT = 2.0
SYSTEMCTL_ACTION_TIMEOUT = 20.0

def _run_systemctl(args: list[str], timeout: float):
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

def service_is_active(service_name: str) -> bool:
    r = _run_systemctl(["is-active", service_name], timeout=SYSTEMCTL_CHECK_TIMEOUT)
    return bool(r and r.stdout.strip() == "active")

def _systemd_execstart_path(service_name: str) -> str | None:
    """Versucht den ExecStart-Pfad aus systemd herauszulesen (z.B. /home/peter/.local/bin/autodarts)."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "ExecStart", service_name],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if out.returncode != 0:
            return None
        line = (out.stdout or "").strip()
        # Beispiele:
        # ExecStart=/home/peter/.local/bin/autodarts
        # ExecStart={ path=/home/peter/.local/bin/autodarts ; argv[]=/home/peter/.local/bin/autodarts ; ... }
        m = re.search(r"/[^\s;]+", line)
        if m:
            p = m.group(0).strip()
            return p if os.path.exists(p) else p  # exist check optional
    except Exception:
        pass
    return None


def get_autodarts_binary_path() -> str | None:
    """Findet das Autodarts Binary möglichst robust."""
    # 1) aus systemd
    p = _systemd_execstart_path(AUTODARTS_SERVICE)
    if p:
        return p

    # 2) typische Orte (User/Root)
    candidates = [
        os.path.expanduser("~/.local/bin/autodarts"),
        "/home/peter/.local/bin/autodarts",
        "/home/pi/.local/bin/autodarts",
        "/root/.local/bin/autodarts",
        "/usr/local/bin/autodarts",
        "/usr/bin/autodarts",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def get_autodarts_version() -> str | None:
    """Liest die installierte Autodarts-Version (über autodarts --version)."""
    # Cache (damit die Startseite nicht träge wird)
    try:
        now = time.time()
        if (now - float(AUTODARTS_VERSION_CACHE.get("ts", 0.0))) < AUTODARTS_VERSION_CACHE_TTL_SEC:
            v = AUTODARTS_VERSION_CACHE.get("v")
            if v:
                return str(v)
    except Exception:
        pass
    bin_path = get_autodarts_binary_path()
    if not bin_path:
        return None
    try:
        r = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=1.5)
        if r.returncode != 0:
            # fallback: manche Tools nutzen -V
            r = subprocess.run([bin_path, "-V"], capture_output=True, text=True, timeout=1.5)
        out = (r.stdout or r.stderr or "").strip()
        m = re.search(r"(\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?)", out)
        ver = m.group(1) if m else (out.splitlines()[0] if out else None)
        try:
            AUTODARTS_VERSION_CACHE["ts"] = time.time()
            AUTODARTS_VERSION_CACHE["v"] = ver
        except Exception:
            pass
        return ver
    except Exception:
        return None


def _get_autodarts_updater_path() -> str | None:
    """Versucht updater.sh zu finden (wird vom offiziellen Installer angelegt)."""
    # Neben dem autodarts binary
    bin_path = get_autodarts_binary_path()
    if bin_path:
        d = os.path.dirname(bin_path)
        cand = os.path.join(d, "updater.sh")
        if os.path.exists(cand):
            return cand

    # typische Orte
    candidates = [
        os.path.expanduser("~/.local/bin/updater.sh"),
        "/home/peter/.local/bin/updater.sh",
        "/home/pi/.local/bin/updater.sh",
        "/root/.local/bin/updater.sh",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def load_update_state() -> dict:
    try:
        with open(AUTODARTS_UPDATE_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_update_state(state: dict):
    try:
        os.makedirs(os.path.dirname(AUTODARTS_UPDATE_STATE), exist_ok=True)
        with open(AUTODARTS_UPDATE_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def start_autodarts_update_background(
    cmd_override: str | None = None,
    requested_version: str | None = None,
    purpose: str | None = None,
    disable_autoupdate_after: bool = False,
) -> tuple[bool, str]:
    """Startet ein Autodarts-Update/Install im Hintergrund und loggt nach AUTODARTS_UPDATE_LOG.

    cmd_override: wenn gesetzt, wird genau dieses Kommando in `bash -lc` ausgeführt.
    requested_version/purpose: nur Info für Status/Log.
    disable_autoupdate_after: am Ende best-effort Auto-Updater deaktivieren (Default soll AUS bleiben).
    """
    # Läuft schon?
    state = load_update_state()
    pid = state.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
            return False, "Update läuft bereits."
        except Exception:
            pass  # PID tot -> weiter

    # Command bestimmen
    cmd = (cmd_override or "").strip()
    if not cmd:
        cmd = (SETTINGS.get("autodarts_update_cmd") or "").strip()

    updater = _get_autodarts_updater_path()
    if not cmd:
        if updater:
            cmd = updater
        else:
            # Fallback auf offiziellen Installer (holt auch updater.sh neu)
            cmd = "bash <(curl -sL get.autodarts.io)"

    if disable_autoupdate_after:
        # Danach: Auto-Updater sicherheitshalber deaktivieren (egal ob Service existiert)
        cmd += f"; (sudo -n systemctl disable --now {AUTOUPDATE_SERVICE} >/dev/null 2>&1 || systemctl disable --now {AUTOUPDATE_SERVICE} >/dev/null 2>&1 || true)"

    # Log-File öffnen
    try:
        os.makedirs(os.path.dirname(AUTODARTS_UPDATE_LOG), exist_ok=True)
    except Exception:
        pass

    try:
        logf = open(AUTODARTS_UPDATE_LOG, "a", encoding="utf-8")
        logf.write("\n\n===== Autodarts Job gestartet: %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        if purpose:
            logf.write(f"Purpose: {purpose}\n")
        if requested_version:
            logf.write(f"Requested Version: {requested_version}\n")
        logf.write(f"CMD: {cmd}\n")
        logf.flush()

        # in bash ausführen (für process substitution)
        # WICHTIG: Wenn das Webpanel als root läuft, soll der Autodarts-Installer/Updater
        # unter dem User 'peter' laufen (sonst landet Binary+Config unter /root).
        popen_cmd = ["bash", "-lc", cmd]
        if os.geteuid() == 0:
            if shutil.which("sudo"):
                popen_cmd = ["sudo", "-u", "peter", "-H", "bash", "-lc", cmd]
            elif shutil.which("runuser"):
                popen_cmd = ["runuser", "-l", "peter", "-c", "bash -lc " + shlex.quote(cmd)]
            elif shutil.which("su"):
                popen_cmd = ["su", "-", "peter", "-c", "bash -lc " + shlex.quote(cmd)]
        p = subprocess.Popen(
            popen_cmd,
            stdout=logf,
            stderr=logf,
            close_fds=True,
        )

        save_update_state({
            "pid": p.pid,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cmd": cmd,
            "purpose": purpose or "",
            "requested_version": requested_version or "",
        })
        return True, "Job gestartet."
    except Exception as e:
        return False, f"Job konnte nicht gestartet werden: {e}"

def get_webpanel_version() -> str | None:
    """Liest die installierte Webpanel-Version (lokale version.txt)."""
    # 1) Hardcoded im Script (einfach zu pflegen)
    try:
        if (WEBPANEL_HARDCODED_VERSION or "").strip():
            v = (WEBPANEL_HARDCODED_VERSION or "").strip().lstrip("v").strip()
            return v or WEBPANEL_UI_FALLBACK_VERSION
    except Exception:
        pass

    # 2) Aus Datei (/var/lib/... oder version.txt)
    candidates = [
        WEBPANEL_VERSION_FILE,
        str(Path(BASE_DIR) / "version.txt"),
    ]
    for p in candidates:
        try:
            if p and os.path.exists(p):
                v = Path(p).read_text(encoding="utf-8", errors="ignore").strip()
                v = v.lstrip("v").strip()
                if v:
                    return v
        except Exception:
            continue
    return WEBPANEL_UI_FALLBACK_VERSION


def fetch_latest_webpanel_version(timeout_s: float = 2.0) -> str | None:
    """Liest die aktuelle Webpanel-Version aus GitHub (raw version.txt)."""
    try:
        req = urllib.request.Request(WEBPANEL_VERSION_URL, headers={"User-Agent": "AutodartsPanel"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            v = (r.read().decode("utf-8", errors="ignore") or "").strip()
        v = v.lstrip("v").strip()
        return v or None
    except Exception:
        return None


def load_webpanel_update_check() -> dict:
    try:
        with open(WEBPANEL_UPDATE_CHECK, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_webpanel_update_check(d: dict):
    try:
        os.makedirs(os.path.dirname(WEBPANEL_UPDATE_CHECK), exist_ok=True)
        with open(WEBPANEL_UPDATE_CHECK, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def load_webpanel_update_state() -> dict:
    try:
        with open(WEBPANEL_UPDATE_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_webpanel_update_state(state: dict):
    try:
        os.makedirs(os.path.dirname(WEBPANEL_UPDATE_STATE), exist_ok=True)
        with open(WEBPANEL_UPDATE_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def get_uvc_backup_info() -> dict:
    try:
        kernel = os.uname().release
    except Exception:
        kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=1.0).stdout.strip() or "unknown"

    backup_dir = os.path.join(UVC_BACKUP_ROOT, kernel)
    marker_path = os.path.join(STATE_DIR, f"once-uvc-hack-{kernel}.done")
    moddir = f"/lib/modules/{kernel}/kernel/drivers/media/usb/uvc"

    backup_ko = os.path.exists(os.path.join(backup_dir, "uvcvideo.ko"))
    backup_koxz = os.path.exists(os.path.join(backup_dir, "uvcvideo.ko.xz"))
    marker_exists = os.path.exists(marker_path)

    return {
        "kernel": kernel,
        "backup_dir": backup_dir,
        "backup_exists": bool(backup_ko or backup_koxz),
        "backup_ko": bool(backup_ko),
        "backup_koxz": bool(backup_koxz),
        "marker_exists": bool(marker_exists),
        "marker_path": marker_path,
        "moddir": moddir,
        "install_safe": bool((backup_ko or backup_koxz) or not marker_exists),
    }


def start_webpanel_update_background(mode: str = "update", allow_self_update: bool = True) -> tuple[bool, str]:
    """Startet das Webpanel-Update oder UVC-Spezialjobs außerhalb des Service-CGroups."""
    if not os.path.exists(WEBPANEL_UPDATE_SCRIPT):
        return False, f"Update-Script nicht gefunden: {WEBPANEL_UPDATE_SCRIPT}"

    mode = (mode or "update").strip() or "update"
    lock_path = "/var/lib/autodarts/webpanel-update.lock"

    def _unit_is_active(unit: str) -> bool:
        try:
            r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
            return r.returncode == 0
        except Exception:
            return False

    try:
        if os.path.exists(lock_path):
            try:
                with open(lock_path, "r", encoding="utf-8") as f:
                    info = json.load(f) or {}
            except Exception:
                info = {}

            unit = str(info.get("unit") or "").strip()
            ts = int(info.get("ts") or 0)

            if unit and _unit_is_active(unit):
                return False, "Webpanel-Job läuft bereits."
            if ts and (time.time() - ts) > 1800:
                try:
                    os.remove(lock_path)
                except Exception:
                    return False, "Webpanel-Job läuft bereits (Lock konnte nicht entfernt werden)."
            else:
                return False, "Webpanel-Job läuft bereits."
    except Exception:
        pass

    state = load_webpanel_update_state()
    state["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["finished"] = None
    state["success"] = None
    state["error"] = None
    state["mode"] = mode
    save_webpanel_update_state(state)

    unit_suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", mode).strip("-") or "update"
    unit_name = f"autodarts-webpanel-{unit_suffix}-{int(time.time())}"

    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "unit": unit_name, "mode": mode}, f)
    except Exception:
        pass

    remote_updater_url = WEBPANEL_RAW_BASE + "/autodarts-webpanel-update.sh"
    mode_arg = shlex.quote(mode)

    self_update_cmd = ""
    if allow_self_update and mode == "update":
        self_update_cmd = (
            "tmp=$(mktemp); "
            f"if curl -fsSL --retry 2 --connect-timeout 5 --max-time 30 {shlex.quote(remote_updater_url)} -o \"$tmp\"; then "
            "sed -i 's/\r$//' \"$tmp\" || true; "
            "sed -i '1s/^\xEF\xBB\xBF//' \"$tmp\" || true; "
            f"sudo -n install -m 755 \"$tmp\" {shlex.quote(WEBPANEL_UPDATE_SCRIPT)}; "
            "fi; "
            "rm -f \"$tmp\" || true; "
        )

    wrapper_cmd = (
        "set -euo pipefail; "
        f"lock={shlex.quote(lock_path)}; "
        f"{self_update_cmd}"
        "rc=0; "
        f"sudo -n {shlex.quote(WEBPANEL_UPDATE_SCRIPT)} {mode_arg} >> {shlex.quote(WEBPANEL_UPDATE_LOG)} 2>&1 || rc=$?; "
        'rm -f "$lock" || true; '
        "exit $rc"
    )

    try:
        res = subprocess.run(
            [
                "systemd-run",
                "--unit", unit_name,
                "--collect",
                "/bin/bash", "-lc", wrapper_cmd,
            ],
            capture_output=True,
            text=True,
        )

        if res.returncode == 0:
            state["unit"] = unit_name
            state["method"] = "systemd-run"
            state["mode"] = mode
            save_webpanel_update_state(state)
            return True, ""

        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except Exception:
            pass

        fallback = (
            "nohup /bin/bash -lc "
            + shlex.quote(
                f"{self_update_cmd}"
                f"sudo -n {WEBPANEL_UPDATE_SCRIPT} {mode_arg} >> {WEBPANEL_UPDATE_LOG} 2>&1"
            )
            + " &"
        )
        res2 = subprocess.run(["bash", "-lc", fallback], capture_output=True, text=True)
        if res2.returncode == 0:
            state["unit"] = None
            state["method"] = "nohup-fallback"
            state["mode"] = mode
            state["error"] = (res.stderr or res.stdout or "").strip()[:300]
            save_webpanel_update_state(state)
            return True, state.get("error", "")

        state["unit"] = None
        state["method"] = "failed"
        state["mode"] = mode
        state["error"] = (res.stderr or res.stdout or "").strip()[:300]
        save_webpanel_update_state(state)
        return False, state["error"]

    except Exception as e:
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except Exception:
            pass
        state["unit"] = None
        state["method"] = "exception"
        state["mode"] = mode
        state["error"] = str(e)[:300]
        save_webpanel_update_state(state)
        return False, state["error"]

def get_webpanel_version() -> str | None:
    """Liest die installierte Webpanel-Version (lokale version.txt)."""
    # 1) Hardcoded im Script (einfach zu pflegen)
    try:
        if (WEBPANEL_HARDCODED_VERSION or "").strip():
            v = (WEBPANEL_HARDCODED_VERSION or "").strip().lstrip("v").strip()
            return v or WEBPANEL_UI_FALLBACK_VERSION
    except Exception:
        pass

    # 2) Aus Datei (/var/lib/... oder version.txt)
    candidates = [
        WEBPANEL_VERSION_FILE,
        str(Path(BASE_DIR) / "version.txt"),
    ]
    for p in candidates:
        try:
            if p and os.path.exists(p):
                v = Path(p).read_text(encoding="utf-8", errors="ignore").strip()
                v = v.lstrip("v").strip()
                if v:
                    return v
        except Exception:
            continue
    return WEBPANEL_UI_FALLBACK_VERSION


def fetch_latest_webpanel_version(timeout_s: float = 2.0) -> str | None:
    """Liest die aktuelle Webpanel-Version aus GitHub (raw version.txt)."""
    try:
        req = urllib.request.Request(WEBPANEL_VERSION_URL, headers={"User-Agent": "AutodartsPanel"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            v = (r.read().decode("utf-8", errors="ignore") or "").strip()
        v = v.lstrip("v").strip()
        return v or None
    except Exception:
        return None


def load_webpanel_update_check() -> dict:
    try:
        with open(WEBPANEL_UPDATE_CHECK, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_webpanel_update_check(d: dict):
    try:
        os.makedirs(os.path.dirname(WEBPANEL_UPDATE_CHECK), exist_ok=True)
        with open(WEBPANEL_UPDATE_CHECK, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def load_webpanel_update_state() -> dict:
    try:
        with open(WEBPANEL_UPDATE_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_webpanel_update_state(state: dict):
    try:
        os.makedirs(os.path.dirname(WEBPANEL_UPDATE_STATE), exist_ok=True)
        with open(WEBPANEL_UPDATE_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass





# ---------------- System Update (apt + reboot) ----------------

def load_os_update_state() -> dict:
    try:
        with open(OS_UPDATE_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_os_update_state(state: dict):
    try:
        os.makedirs(os.path.dirname(OS_UPDATE_STATE), exist_ok=True)
        with open(OS_UPDATE_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def load_ufw_state() -> dict:
    try:
        with open(UFW_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_ufw_state(state: dict):
    try:
        os.makedirs(os.path.dirname(UFW_STATE), exist_ok=True)
        with open(UFW_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def ufw_is_installed() -> bool:
    # sehr billig (kein Prozess): reicht um Install-Button sinnvoll anzuzeigen
    return bool(shutil.which("ufw"))


def _run_root(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ufw_refresh_state() -> dict:
    """
    Liest UFW Status (active/inactive) aus. Wird nur auf Button-Klick aufgerufen.
    """
    st = load_ufw_state() or {}
    st["checked_ts"] = time.time()
    st["checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["installed"] = ufw_is_installed()

    if not st["installed"]:
        st["enabled"] = False
        st["status"] = "not_installed"
        st["raw"] = ""
        save_ufw_state(st)
        return st

    try:
        r = _run_root(["ufw", "status"], timeout=6.0)
        raw = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        st["raw"] = raw.strip()
        if "Status: active" in raw:
            st["enabled"] = True
            st["status"] = "active"
        elif "Status: inactive" in raw:
            st["enabled"] = False
            st["status"] = "inactive"
        else:
            st["enabled"] = None
            st["status"] = "unknown"
            st["error"] = (r.stderr or "").strip() or None
    except Exception as e:
        st["enabled"] = None
        st["status"] = "error"
        st["error"] = str(e)

    save_ufw_state(st)
    return st


def ufw_apply_port_rules() -> tuple[bool, str]:
    """
    Setzt die erlaubten Ports idempotent (mehrfach ausführen ist ok).
    """
    if not ufw_is_installed():
        return False, "UFW ist nicht installiert."

    logs = []
    ok = True
    for rule in UFW_PORT_RULES:
        try:
            r = _run_root(["ufw", "allow", rule], timeout=10.0)
            if r.returncode != 0:
                ok = False
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            if out:
                logs.append(out)
            if err:
                logs.append(err)
        except Exception as e:
            ok = False
            logs.append(str(e))

    msg = "Ports angewendet." if ok else "Ports angewendet, aber es gab Fehler."
    return ok, msg + ("\n" + "\n".join(logs[-10:]) if logs else "")


def ufw_set_enabled(enable: bool) -> tuple[bool, str]:
    if not ufw_is_installed():
        return False, "UFW ist nicht installiert."

    if enable:
        ok_ports, msg_ports = ufw_apply_port_rules()
        try:
            r = _run_root(["ufw", "--force", "enable"], timeout=10.0)
            ok = (r.returncode == 0) and ok_ports
            msg = "UFW aktiviert." if ok else "UFW Aktivierung fehlgeschlagen."
            extra = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            extra = extra.strip()
            if msg_ports:
                extra = (msg_ports + "\n" + extra).strip()
            return ok, (msg + ("\n" + extra if extra else ""))
        except Exception as e:
            return False, f"UFW Aktivierung fehlgeschlagen: {e}"
    else:
        try:
            r = _run_root(["ufw", "disable"], timeout=10.0)
            ok = (r.returncode == 0)
            extra = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            extra = extra.strip()
            return ok, ("UFW deaktiviert." + ("\n" + extra if extra else ""))
        except Exception as e:
            return False, f"UFW deaktivieren fehlgeschlagen: {e}"


def start_ufw_install_background() -> tuple[bool, str]:
    """
    Installiert UFW + setzt Ports + aktiviert UFW im Hintergrund via systemd-run.
    Damit blockiert die Weboberfläche nicht.
    """
    unit_name = f"autodarts-ufw-install-{int(time.time())}"
    st = load_ufw_state() or {}
    st["started"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["unit"] = unit_name
    st["status"] = "installing"
    save_ufw_state(st)

    rules = " ".join([f"ufw allow {r} || true;" for r in UFW_PORT_RULES])
    cmdline = (
        "set -e; "
        "echo '[ufw] apt-get update'; apt-get update -y; "
        "echo '[ufw] install'; apt-get install -y ufw; "
        f"echo '[ufw] rules'; {rules} "
        "echo '[ufw] enable'; ufw --force enable; "
        "echo '[ufw] done'; ufw status;"
    )
    # Logfile
    cmdline = f"{{ {cmdline} ; }} >> '{UFW_LOG}' 2>&1"

    try:
        cmd = [
            "systemd-run",
            "--unit", unit_name,
            "--no-block",
            "--collect",
            "bash", "-lc", cmdline,
        ]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True, "UFW Installation gestartet (läuft im Hintergrund)."
        st["status"] = "install_failed"
        st["error"] = (res.stderr or res.stdout or "").strip() or "systemd-run fehlgeschlagen."
        save_ufw_state(st)
        return False, st["error"]
    except Exception as e:
        st["status"] = "install_error"
        st["error"] = str(e)
        save_ufw_state(st)
        return False, str(e)


def start_os_update_background() -> tuple[bool, str]:
    """Startet 'apt update + apt upgrade' im Hintergrund und rebootet danach IMMER.

    Läuft via `systemd-run` in einem eigenen transienten Unit (damit es einen Webpanel-Restart überlebt).
    """

    state = load_os_update_state()
    state["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state.pop("error", None)

    unit_name = f"autodarts-os-update-{int(time.time())}"

    # Non-interactive apt (damit es nicht hängen bleibt bei config prompts)
    cmdline = (
        f"exec >>{OS_UPDATE_LOG} 2>&1; "
        f"echo '===== OS Update START {time.strftime('%Y-%m-%d %H:%M:%S')} ====='; "
        f"apt-get update || echo 'apt-get update FAILED (continue)'; "
        f"DEBIAN_FRONTEND=noninteractive "
        f"apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade "
        f"|| echo 'apt-get upgrade FAILED (continue)'; "
        f"echo '===== OS Update DONE -> reboot ====='; "
        f"sync; /sbin/reboot"
    )

    try:
        cmd = [
            "sudo", "-n",
            "systemd-run",
            "--unit", unit_name,
            "--no-block",
            "--collect",
            "bash", "-lc", cmdline,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)

        if res.returncode == 0:
            state["unit"] = unit_name
            state["method"] = "systemd-run"
            save_os_update_state(state)
            return True, "System-Update gestartet – der Pi rebootet danach automatisch."

        state["unit"] = None
        state["method"] = "systemd-run-failed"
        state["error"] = (res.stderr or res.stdout or "").strip()[:300]
        save_os_update_state(state)
        return False, f"System-Update konnte nicht gestartet werden: {state['error']}"

    except Exception as e:
        state["unit"] = None
        state["method"] = "exception"
        state["error"] = str(e)[:300]
        save_os_update_state(state)
        return False, state["error"]

def service_enable_now(service_name: str):
    _run_systemctl(["enable", "--now", service_name], timeout=SYSTEMCTL_ACTION_TIMEOUT)

def service_disable_now(service_name: str):
    _run_systemctl(["disable", "--now", service_name], timeout=SYSTEMCTL_ACTION_TIMEOUT)

def service_restart(service_name: str):
    _run_systemctl(["restart", service_name], timeout=SYSTEMCTL_ACTION_TIMEOUT)

def service_is_enabled(service_name: str) -> bool:
    r = _run_systemctl(["is-enabled", service_name], timeout=SYSTEMCTL_CHECK_TIMEOUT)
    return bool(r and r.stdout.strip() == "enabled")

def autodarts_autoupdate_is_enabled() -> bool | None:
    """True/False wenn Service existiert, sonst None."""
    if not service_exists(AUTOUPDATE_SERVICE):
        return None
    return service_is_enabled(AUTOUPDATE_SERVICE)

def autodarts_set_autoupdate(enabled: bool) -> tuple[bool, str]:
    """Enable/disable autodarts auto-updater service.

    Wenn der Service nicht existiert:
      - disable => OK (bereits aus)
      - enable  => Fehler (Service fehlt)
    """
    if not service_exists(AUTOUPDATE_SERVICE):
        if not enabled:
            return True, "Auto-Update ist bereits deaktiviert (Service fehlt)."
        return False, f"{AUTOUPDATE_SERVICE} nicht gefunden."
    try:
        cmd = ["systemctl", ("enable" if enabled else "disable"), "--now", AUTOUPDATE_SERVICE]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True, ("Auto-Update aktiviert." if enabled else "Auto-Update deaktiviert.")
        err = (r.stderr or r.stdout or "").strip()
        short = (err.splitlines()[0] if err else "systemctl fehlgeschlagen.")
        return False, short
    except Exception as e:
        return False, f"Auto-Update konnte nicht geändert werden: {e}"



# ---------------- Extensions Update (darts-caller / darts-wled) ----------------

def load_extensions_update_state() -> dict:
    try:
        with open(EXTENSIONS_UPDATE_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_extensions_update_state(state: dict):
    try:
        os.makedirs(os.path.dirname(EXTENSIONS_UPDATE_STATE), exist_ok=True)
        with open(EXTENSIONS_UPDATE_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def load_extensions_update_last() -> dict:
    try:
        with open(EXTENSIONS_UPDATE_LAST, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


EXTENSIONS_UPDATE_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
set -euo pipefail

# --- Einstellungen ---
CALLER_DIR="/var/lib/autodarts/extensions/darts-caller"
WLED_DIR="/var/lib/autodarts/extensions/darts-wled"

CALLER_START="${CALLER_DIR}/start-custom.sh"
WLED_WRAPPER="${WLED_DIR}/start-custom.sh"
WLED_CONFIG="/var/lib/autodarts/config/darts-wled/start-custom.sh"

BACKUP_ROOT="/var/lib/autodarts/config/backups"
TS="$(date +'%Y%m%d-%H%M%S')"
BK="${BACKUP_ROOT}/extensions-update-${TS}"

LOG_FILE="/var/log/autodarts_extensions_update.log"
RESULT_JSON="/var/lib/autodarts/extensions-update-last.json"

FORCE="${FORCE:-0}"
TARGET="${1:-all}"   # all | caller | wled

# --- Helpers ---
log() { echo "[$(date +'%F %T')] $*"; }

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$RESULT_JSON")"
exec >>"$LOG_FILE" 2>&1

# Lock gegen Doppelklick
LOCK="/run/autodarts-extensions-update.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -n 9 || { log "Update läuft bereits (lock: $LOCK)."; exit 0; }
fi

CALLER_STATUS="SKIPPED"
WLED_STATUS="SKIPPED"
ERRORS=""

write_result() {
  cat > "$RESULT_JSON" <<JSON
{
  "ts": "$(date +'%F %T')",
  "target": "$TARGET",
  "caller": "$CALLER_STATUS",
  "wled": "$WLED_STATUS",
  "backup": "$BK",
  "force": "$FORCE",
  "errors": "$(echo "$ERRORS" | tr '\n' ' ' | sed 's/"/\\"/g')"
}
JSON
  chmod 666 "$RESULT_JSON" 2>/dev/null || true
}
trap write_result EXIT

exists_unitfile() {
  systemctl list-unit-files --no-legend 2>/dev/null | awk '{print $1}' | grep -qx "$1"
}
is_active() { systemctl is-active --quiet "$1"; }

stop_if_exists() {
  local svc="$1"
  if exists_unitfile "$svc"; then
    if is_active "$svc"; then
      log "Stoppe $svc ..."
      systemctl stop "$svc" || true
      echo "1"
    else
      echo "0"
    fi
  else
    echo "0"
  fi
}

restart_if_exists() {
  local svc="$1"
  if exists_unitfile "$svc"; then
    log "Starte/Restarte $svc ..."
    systemctl restart "$svc" || systemctl start "$svc" || true
  else
    log "Service nicht gefunden: $svc (ok, übersprungen)"
  fi
}

ensure_wrapper() {
  if [[ -d "$WLED_DIR" ]]; then
    log "Stelle sicher: WLED Wrapper zeigt auf Config-Startscript ..."
    cat > "$WLED_WRAPPER" <<EOF
#!/usr/bin/env bash
exec "$WLED_CONFIG"
EOF
    chmod +x "$WLED_WRAPPER" || true
  fi
  if [[ -f "$WLED_CONFIG" ]]; then
    chmod +x "$WLED_CONFIG" || true
  fi
}

backup_file_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    log "Backup: $src -> $dst"
  else
    log "Backup übersprungen (nicht gefunden): $src"
  fi
}

restore_file_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    log "Restore: $src -> $dst"
  else
    log "Restore übersprungen (Backup fehlt): $src"
  fi
}

repo_update_status() {
  # prints: CHANGED / UNCHANGED / SKIPPED
  local dir="$1"
  local label="$2"

  if [[ ! -d "$dir/.git" ]]; then
    log "$label: Kein Git-Repo: $dir (SKIPPED)"
    echo "SKIPPED"
    return 0
  fi

  git config --global --add safe.directory "$dir" >/dev/null 2>&1 || true

  log "$label: Update Repo: $dir"
  pushd "$dir" >/dev/null || { echo "SKIPPED"; return 0; }

  local before after upstream
  before="$(git rev-parse HEAD 2>/dev/null || echo "")"
  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "")"

  git fetch --all --prune || true

  if [[ -n "$upstream" ]]; then
    git reset --hard "@{u}" || true
  else
    git pull --rebase --autostash || true
  fi

  after="$(git rev-parse HEAD 2>/dev/null || echo "")"
  popd >/dev/null || true

  if [[ -n "$before" && -n "$after" && "$before" != "$after" ]]; then
    log "$label: CHANGED ($before -> $after)"
    echo "CHANGED"
  else
    log "$label: UNCHANGED ($after)"
    echo "UNCHANGED"
  fi
}

venv_refresh_install_if_needed() {
  local dir="$1"
  local label="$2"
  local changed="$3"
  local requirements="$dir/requirements.txt"

  [[ -d "$dir" ]] || return 0

  if [[ "$FORCE" == "1" ]]; then
    log "$label: FORCE=1 -> pip/venv wird ausgeführt."
  elif [[ "$changed" != "CHANGED" && -d "$dir/.venv" ]]; then
    log "$label: Keine Repo-Änderung und .venv vorhanden -> pip übersprungen."
    return 0
  fi

  log "$label: Python venv/requirements: $dir"
  pushd "$dir" >/dev/null || return 1

  export PIP_DISABLE_PIP_VERSION_CHECK=1
  export PIP_NO_INPUT=1

  if [[ ! -d ".venv" ]]; then
    if ! python3 -m venv .venv; then
      ERRORS="${ERRORS}\n${label}: python3 -m venv fehlgeschlagen (python3-venv installiert?)"
      popd >/dev/null || true
      return 0
    fi
  fi

  # shellcheck disable=SC1091
  source .venv/bin/activate

  python3 -m pip install -U pip setuptools wheel || true

  if [[ -f "$requirements" ]]; then
    if python3 -m pip install -r "$requirements" --upgrade; then
      log "$label: requirements OK"
    else
      log "$label: requirements FAIL -> Retry ohne pyinstaller/pyinstaller-hooks-contrib"
      grep -v -E '^pyinstaller(==|$)|^pyinstaller-hooks-contrib(==|$)' "$requirements" > /tmp/req-no-pyinstaller.txt
      if ! python3 -m pip install -r /tmp/req-no-pyinstaller.txt --upgrade; then
        ERRORS="${ERRORS}\n${label}: pip install requirements fehlgeschlagen"
      fi
    fi
  else
    log "$label: Keine requirements.txt gefunden (übersprungen)"
  fi

  deactivate || true
  popd >/dev/null || true
}

# ---- MAIN ----
log "===== Extensions Update START (target=${TARGET}) ====="

if [[ "$(id -u)" -ne 0 ]]; then
  log "Bitte mit sudo ausführen."
  exit 1
fi

mkdir -p "$BK"
log "Backup-Ordner: $BK"
log "FORCE=$FORCE"

DO_CALLER=0
DO_WLED=0
case "$TARGET" in
  all) DO_CALLER=1; DO_WLED=1;;
  caller) DO_CALLER=1;;
  wled) DO_WLED=1;;
  *) log "Unbekannter TARGET: $TARGET -> all"; DO_CALLER=1; DO_WLED=1;;
esac

CALLER_WAS_ACTIVE="0"
WLED_WAS_ACTIVE="0"

if [[ "$DO_CALLER" == "1" ]]; then
  CALLER_WAS_ACTIVE="$(stop_if_exists darts-caller.service)"
  backup_file_if_exists "$CALLER_START" "${BK}/darts-caller/start-custom.sh"
fi

if [[ "$DO_WLED" == "1" ]]; then
  WLED_WAS_ACTIVE="$(stop_if_exists darts-wled.service)"
  backup_file_if_exists "$WLED_WRAPPER" "${BK}/darts-wled/start-custom.wrapper.sh"
  backup_file_if_exists "$WLED_CONFIG"  "${BK}/darts-wled/start-custom.config.sh"
fi

# Repos updaten
if [[ "$DO_CALLER" == "1" ]]; then
  CALLER_STATUS="$(repo_update_status "$CALLER_DIR" "CALLER")"
fi
if [[ "$DO_WLED" == "1" ]]; then
  WLED_STATUS="$(repo_update_status "$WLED_DIR" "WLED")"
fi

# Restore start-custom
if [[ "$DO_CALLER" == "1" ]]; then
  restore_file_if_exists "${BK}/darts-caller/start-custom.sh" "$CALLER_START"
fi
if [[ "$DO_WLED" == "1" ]]; then
  restore_file_if_exists "${BK}/darts-wled/start-custom.wrapper.sh" "$WLED_WRAPPER"
  restore_file_if_exists "${BK}/darts-wled/start-custom.config.sh" "$WLED_CONFIG"
  ensure_wrapper
fi

# venv + requirements nur wenn nötig
if [[ "$DO_CALLER" == "1" ]]; then
  venv_refresh_install_if_needed "$CALLER_DIR" "CALLER" "$CALLER_STATUS" || true
fi
if [[ "$DO_WLED" == "1" ]]; then
  venv_refresh_install_if_needed "$WLED_DIR" "WLED" "$WLED_STATUS" || true
fi

# Services restart nur wenn vorher aktiv UND relevant geändert (oder FORCE)
if [[ "$DO_CALLER" == "1" && "$CALLER_WAS_ACTIVE" == "1" ]]; then
  if [[ "$FORCE" == "1" || "$CALLER_STATUS" == "CHANGED" ]]; then
    restart_if_exists darts-caller.service
  else
    log "CALLER: war aktiv, aber UNCHANGED -> kein Restart."
  fi
fi

if [[ "$DO_WLED" == "1" && "$WLED_WAS_ACTIVE" == "1" ]]; then
  if [[ "$FORCE" == "1" || "$WLED_STATUS" == "CHANGED" ]]; then
    restart_if_exists darts-wled.service
  else
    log "WLED: war aktiv, aber UNCHANGED -> kein Restart."
  fi
fi

systemctl daemon-reload || true

log "===== SUMMARY ====="
log "CALLER: $CALLER_STATUS"
log "WLED:   $WLED_STATUS"
if [[ -n "$ERRORS" ]]; then
  log "WARN/ERRORS: $ERRORS"
fi
log "Backup: $BK"
log "===== Extensions Update DONE ====="
"""

def start_extensions_update_background(target: str = "all") -> tuple[bool, str]:
    """Startet das Extensions-Update (darts-caller + darts-wled) im Hintergrund via systemd-run.

    Button-Name im UI: "WLED UPDATE" (historisch), aktualisiert aber beide Extensions nach Bedarf.

    WICHTIG: Es wird **keine** neue Script-Version aus dem Webpanel hineingeschrieben.
    Es wird die lokal installierte Datei EXTENSIONS_UPDATE_SCRIPT ausgeführt (die du z.B.
    über dein Webpanel-Update-Script aus GitHub aktualisieren lässt).

    Das Script schreibt:
      - Log:  EXTENSIONS_UPDATE_LOG
      - Last: EXTENSIONS_UPDATE_LAST (JSON: caller/wled CHANGED/UNCHANGED)
    """
    # nur erlaubte Targets akzeptieren
    if target not in ("all", "caller", "wled"):
        target = "all"

    # State aktualisieren
    state = load_extensions_update_state()
    state["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["target"] = target
    state.pop("error", None)

    unit_name = f"autodarts-wled-update-{int(time.time())}"

    # Script muss lokal vorhanden sein
    if not os.path.isfile(EXTENSIONS_UPDATE_SCRIPT):
        state["unit"] = None
        state["method"] = "missing-script"
        state["error"] = (
            f"{EXTENSIONS_UPDATE_SCRIPT} fehlt. "
            "Bitte Webpanel-Update ausführen oder das Script manuell installieren."
        )
        save_extensions_update_state(state)
        return False, state["error"]

    # Im Root-Context: CRLF entfernen (falls per Download reingekommen), ausführbar machen, dann starten
    cmdline = "\n".join([
        "set -e",
        f"sed -i 's/\\r$//' {EXTENSIONS_UPDATE_SCRIPT} || true",
        f"chmod +x {EXTENSIONS_UPDATE_SCRIPT} || true",
        f"{EXTENSIONS_UPDATE_SCRIPT} {target}",
        "",
    ])

    try:
        cmd = [
            "sudo", "-n",
            "systemd-run",
            "--unit", unit_name,
            "--no-block",
            "--collect",
            "bash", "-lc", cmdline,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)

        if res.returncode == 0:
            state["unit"] = unit_name
            state["method"] = "systemd-run"
            save_extensions_update_state(state)
            return True, "WLED UPDATE gestartet (Caller/WLED werden nur aktualisiert, wenn nötig)."

        state["unit"] = None
        state["method"] = "systemd-run-failed"
        state["error"] = (res.stderr or res.stdout or "").strip()[:300]
        save_extensions_update_state(state)
        return False, state["error"] or "systemd-run fehlgeschlagen."

    except Exception as e:
        state["unit"] = None
        state["method"] = "exception"
        state["error"] = str(e)[:300]
        save_extensions_update_state(state)
        return False, state["error"]
def service_enable_now(service_name: str):
    _run_systemctl(["enable", "--now", service_name], timeout=SYSTEMCTL_ACTION_TIMEOUT)

def service_disable_now(service_name: str):
    _run_systemctl(["disable", "--now", service_name], timeout=SYSTEMCTL_ACTION_TIMEOUT)

def service_restart(service_name: str):
    _run_systemctl(["restart", service_name], timeout=SYSTEMCTL_ACTION_TIMEOUT)

def service_is_enabled(service_name: str) -> bool:
    r = _run_systemctl(["is-enabled", service_name], timeout=SYSTEMCTL_CHECK_TIMEOUT)
    return bool(r and r.stdout.strip() == "enabled")

def autodarts_autoupdate_is_enabled() -> bool | None:
    """True/False wenn Service existiert, sonst None."""
    if not service_exists(AUTOUPDATE_SERVICE):
        return None
    return service_is_enabled(AUTOUPDATE_SERVICE)

def autodarts_set_autoupdate(enabled: bool) -> tuple[bool, str]:
    """Enable/disable autodarts auto-updater service.

    Wenn der Service nicht existiert:
      - disable => OK (bereits aus)
      - enable  => Fehler (Service fehlt)
    """
    if not service_exists(AUTOUPDATE_SERVICE):
        if not enabled:
            return True, "Auto-Update ist bereits deaktiviert (Service fehlt)."
        return False, f"{AUTOUPDATE_SERVICE} nicht gefunden."
    try:
        cmd = ["systemctl", ("enable" if enabled else "disable"), "--now", AUTOUPDATE_SERVICE]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True, ("Auto-Update aktiviert." if enabled else "Auto-Update deaktiviert.")
        err = (r.stderr or r.stdout or "").strip()
        short = (err.splitlines()[0] if err else "systemctl fehlgeschlagen.")
        return False, short
    except Exception as e:
        return False, f"Auto-Update konnte nicht geändert werden: {e}"

# ---------------- Auto-Update Default (soll standardmäßig AUS sein) ----------------

AUTOUPDATE_DEFAULT_MARKER = str(DATA_DIR / "autoupdate-default-applied.json")
_AUTOUPDATE_DEFAULT_RAN = False
_AUTOUPDATE_DEFAULT_LOCK = threading.Lock()

def ensure_autoupdate_default_once() -> str | None:
    """Deaktiviert den Autodarts Auto-Updater einmalig (Default = AUS).

    Hintergrund:
    Der offizielle Installer aktiviert Auto-Update standardmäßig. Wenn ein Release gerade
    Probleme macht, ist das unangenehm. Daher schalten wir den Service *einmalig* beim
    ersten Start nach Webpanel-Update aus (User kann danach wieder einschalten).
    """
    global _AUTOUPDATE_DEFAULT_RAN
    if _AUTOUPDATE_DEFAULT_RAN:
        return None
    with _AUTOUPDATE_DEFAULT_LOCK:
        if _AUTOUPDATE_DEFAULT_RAN:
            return None
        _AUTOUPDATE_DEFAULT_RAN = True

    # schon erledigt?
    try:
        if os.path.exists(AUTOUPDATE_DEFAULT_MARKER):
            return None
    except Exception:
        # Wenn wir nicht mal prüfen können, lieber nichts kaputtmachen.
        return None

    desired_default = bool(SETTINGS.get("autoupdate_default_enabled", False))
    cur = autodarts_autoupdate_is_enabled()

    changed = False
    msg = None

    if desired_default:
        # User möchte Default-AN (wir ändern nichts, markieren nur)
        pass
    else:
        # Default-AUS
        if cur is True:
            ok, _m = autodarts_set_autoupdate(False)
            changed = bool(ok)
            if changed:
                msg = "Auto-Update wurde standardmäßig deaktiviert (kann bei Bedarf wieder eingeschaltet werden)."

    # Marker schreiben (damit es wirklich nur einmal passiert)
    # Wenn wir deaktivieren wollten, das aber fehlschlägt, schreiben wir keinen Marker,
    # damit es beim nächsten Start erneut versucht wird.
    if desired_default or cur is not True or changed:
        try:
            os.makedirs(os.path.dirname(AUTOUPDATE_DEFAULT_MARKER), exist_ok=True)
        except Exception:
            pass
        try:
            with open(AUTOUPDATE_DEFAULT_MARKER, "w", encoding="utf-8") as f:
                json.dump({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "desired_default_enabled": desired_default,
                    "cur_before": cur,
                    "changed": changed,
                }, f, indent=2)
        except Exception:
            pass

    return msg




# ---------------- Update-Check (nur bei Klick) ----------------

def load_update_check() -> dict:
    try:
        with open(AUTODARTS_UPDATE_CHECK, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_update_check(d: dict):
    os.makedirs(os.path.dirname(AUTODARTS_UPDATE_CHECK), exist_ok=True)
    with open(AUTODARTS_UPDATE_CHECK, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)

def _get_platform_arch_for_autodarts() -> tuple[str, str]:
    # Plattform ist im Installer 'linux'
    platform = "linux"
    arch = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
    if arch in ("x86_64", "amd64"):
        arch = "amd64"
    elif arch in ("aarch64", "arm64"):
        arch = "arm64"
    elif arch == "armv7l":
        arch = "armv7l"
    return platform, arch

def _get_updater_channel() -> str:
    # Versuche CHANNEL aus updater.sh zu lesen (latest/beta)
    updater = _get_autodarts_updater_path()
    if updater:
        try:
            for line in Path(updater).read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("CHANNEL="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        except Exception:
            pass
    return "latest"

def fetch_latest_autodarts_version(channel: str | None = None, timeout_s: float = 2.5) -> str | None:
    try:
        platform, arch = _get_platform_arch_for_autodarts()
        ch = (channel or _get_updater_channel()).strip() or "latest"
        # Installer nutzt:
        # latest: detection/latest/<platform>/<arch>/RELEASES.json
        # beta:   detection/beta/<platform>/<arch>/RELEASES.json
        url = f"https://get.autodarts.io/detection/{ch}/{platform}/{arch}/RELEASES.json"
        req = urllib.request.Request(url, headers={"User-Agent": "AutodartsPanel"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore") or "{}")
        cv = str(data.get("currentVersion", "")).strip()
        if cv.startswith("v"):
            cv = cv[1:]
        return cv or None
    except Exception:
        return None


# ---------------- Verbindungstest (Ping) ----------------

PING_JOBS: dict[str, dict] = {}

def get_default_gateway() -> str | None:
    # Default-Route -> Gateway IP
    try:
        r = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        # Beispiel: 'default via 192.168.178.1 dev wlan0 ...'
        m = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)", r.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def get_ping_uplink_interface() -> str | None:
    """
    Für den Verbindungstest nur echte Uplinks erlauben:
    - eth0 (Kabel)
    - WIFI_INTERFACE (z.B. wlan0)
    Access Point Interface (wlan_ap) ist bereits ausgeschlossen.
    """
    iface = _get_default_route_interface()
    if not iface:
        return None
    if iface == "eth0":
        return iface
    if iface == WIFI_INTERFACE:
        return iface
    return None


def get_default_gateway_for_interface(iface: str) -> str | None:
    try:
        r = subprocess.run(["ip", "route", "show", "default", "dev", iface], capture_output=True, text=True, timeout=1.2)
        if r.returncode != 0:
            return None
        m = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)", r.stdout or "")
        return m.group(1) if m else None
    except Exception:
        return None


def ping_iface_label(iface: str) -> str:
    if iface == "eth0":
        return "Verbindungstest über Kabel (eth0)"
    if iface == WIFI_INTERFACE:
        return f"Verbindungstest über WLAN ({iface})"
    return f"Verbindungstest über {iface}"

def _ping_worker(job_id: str, target: str, count: int):
    job = PING_JOBS.get(job_id)
    if not job:
        return
    times = []
    received = 0
    try:
        p = subprocess.Popen(
            ["ping", "-n", "-c", str(count), *(["-I", str(job.get("iface"))] if job and job.get("iface") else []), target],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
        job["pid"] = p.pid
        for line in p.stdout or []:
            # icmp_seq=1 time=12.3 ms
            m = re.search(r"icmp_seq=(\d+).*time=([0-9\.]+)\s*ms", line)
            if m:
                seq = int(m.group(1))
                t = float(m.group(2))
                times.append(t)
                received = len(times)
                job["progress"] = max(job.get("progress", 0), seq)
                job["received"] = received
        p.wait()
        # Summary parse (optional)
        # '30 packets transmitted, 30 received, 0% packet loss, time ...'
        # We prefer our measured times for min/avg/max.
    except Exception as e:
        job["error"] = str(e)

    if times:
        job["min_ms"] = round(min(times), 2)
        job["max_ms"] = round(max(times), 2)
        job["avg_ms"] = round(sum(times) / len(times), 2)
    job["done"] = True

def start_ping_test(count: int = 30) -> tuple[bool, str, str | None]:
    iface = get_ping_uplink_interface()
    if not iface:
        return False, "Kein Gateway gefunden (nur Access Point oder nicht verbunden?).", None

    gw = get_default_gateway_for_interface(iface)
    if not gw:
        return False, f"Kein Gateway auf {iface} gefunden (nicht verbunden?).", None

    job_id = uuid.uuid4().hex[:10]
    PING_JOBS[job_id] = {
        "target": gw,
        "iface": iface,
        "iface_label": ping_iface_label(iface),
        "count": int(count),
        "started": time.time(),
        "progress": 0,
        "received": 0,
        "done": False,
        "min_ms": None,
        "max_ms": None,
        "avg_ms": None,
        "error": None,
        "pid": None,
    }
    th = threading.Thread(target=_ping_worker, args=(job_id, gw, int(count)), daemon=True)
    th.start()
    return True, "Ping gestartet.", job_id




# ---------------- WLED reachability ----------------

def is_wled_reachable(ip_or_host: str, timeout_sec: float = 1.2) -> bool:
    url = f"http://{ip_or_host}/json/info"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AutodartsPanel"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as r:
            status = getattr(r, "status", 200)
            return 200 <= status < 300
    except (urllib.error.URLError, TimeoutError, Exception):
        return False



# ---------------- Host / HTTP Reachability (schnell + gecached) ----------------

_DNS_CACHE: dict[str, tuple[float, str | None]] = {}
_HTTP_CACHE: dict[tuple[str, float], tuple[float, bool, str | None]] = {}
_DNS_TTL_SEC = 60.0
_HTTP_TTL_SEC = 4.0

def resolve_host_to_ip_fast(host: str, timeout_s: float = 0.6) -> str | None:
    """
    Schnelle, robuste Namensauflösung (wichtig bei .local/mDNS):
    - IP bleibt IP
    - .local bevorzugt via avahi-resolve-host-name (mit Timeout)
    - sonst via getent (mit Timeout)
    - KEIN socket.gethostbyname (kann sonst lange blockieren)
    """
    host = (host or "").strip()
    if not host:
        return None

    # Wenn schon IP
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass

    # .local -> avahi
    if host.endswith(".local") and shutil.which("avahi-resolve-host-name"):
        try:
            r = subprocess.run(
                ["avahi-resolve-host-name", "-4", host],
                capture_output=True,
                text=True,
                timeout=max(0.2, timeout_s),
            )
            if r.returncode == 0 and r.stdout.strip():
                parts = r.stdout.strip().split()
                if len(parts) >= 2:
                    return parts[1].strip()
        except Exception:
            pass

    # Fallback -> getent
    if shutil.which("getent"):
        try:
            r = subprocess.run(
                ["getent", "hosts", host],
                capture_output=True,
                text=True,
                timeout=max(0.2, timeout_s),
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().split()[0].strip()
        except Exception:
            pass

    return None


def resolve_host_to_ip(host: str) -> str | None:
    # Wrapper für bestehende Aufrufe (mit Cache)
    host = (host or "").strip()
    if not host:
        return None

    now = time.time()
    cached = _DNS_CACHE.get(host)
    if cached and (now - cached[0]) < _DNS_TTL_SEC:
        return cached[1]

    ip = resolve_host_to_ip_fast(host, timeout_s=0.6)
    _DNS_CACHE[host] = (now, ip)
    return ip


def is_http_reachable(host: str, timeout_s: float = 0.6) -> tuple[bool, str | None]:
    """
    Prüft, ob WLED unter http://<host>/json/info erreichbar ist.
    Gibt (ok, ip) zurück. ip kann None sein (z.B. wenn DNS nicht auflösbar war).
    """
    host = (host or "").strip()
    if not host:
        return False, None

    # Cache pro Host+Timeout (damit schnelle Reloads nicht 3x DNS/HTTP machen)
    key = (host, float(timeout_s))
    now = time.time()
    c = _HTTP_CACHE.get(key)
    if c and (now - c[0]) < _HTTP_TTL_SEC:
        return c[1], c[2]

    ip = resolve_host_to_ip(host)
    if not ip:
        _HTTP_CACHE[key] = (now, False, None)
        return False, None
    target = ip

    ok = False
    try:
        url = f"http://{target}/json/info"
        req = urllib.request.Request(url, headers={"User-Agent": "AutodartsPanel"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            status = getattr(r, "status", 200)
            data = r.read(64)  # reicht als Lebenszeichen
            ok = (200 <= status < 300) and bool(data)
    except Exception:
        ok = False

    _HTTP_CACHE[key] = (now, ok, ip)
    return ok, ip



# ---------------- WLED persistent config (Multi, migriert Legacy) ----------------

def load_wled_config() -> dict:
    """
    Multi-WLED Konfiguration laden.
    Format:
      {
        "master_enabled": true/false,
        "targets": [
          {"label":"Dart LED1","host":"Dart-Led1.local","enabled":true},
          {"label":"Dart LED2","host":"Dart-Led2.local","enabled":false},
          {"label":"Dart LED3","host":"Dart-Led3.local","enabled":false}
        ]
      }

    Migration:
      - Wenn WLED_CONFIG_PATH fehlt, aber WLED_FLAG_PATH existiert, wird von der alten Single-Variante migriert.
    """
    default_cfg = {
        "master_enabled": True,
        "targets": [
            {"label": "Dart LED1", "host": "Dart-Led1.local", "enabled": True},
            {"label": "Dart LED2", "host": "Dart-Led2.local", "enabled": False},
            {"label": "Dart LED3", "host": "Dart-Led3.local", "enabled": False},
        ],
    }

    # Neu vorhanden?
    try:
        with open(WLED_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except FileNotFoundError:
        cfg = None
    except Exception:
        cfg = None

    # Migration von legacy
    if cfg is None:
        legacy_enabled = True
        try:
            with open(WLED_FLAG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f) or {}
                legacy_enabled = bool(d.get("enabled", True))
        except Exception:
            legacy_enabled = True

        cfg = default_cfg
        cfg["master_enabled"] = legacy_enabled
        cfg["targets"][0]["enabled"] = legacy_enabled
        cfg["targets"][0]["host"] = WLED_MDNS_NAME  # alte Single-Default

        save_wled_config(cfg)
        return cfg

    # Normalisieren / Defaults
    master_enabled = bool(cfg.get("master_enabled", True))
    targets = cfg.get("targets", [])
    if not isinstance(targets, list):
        targets = []

    # Ensure exactly 3 targets
    norm_targets = []
    for i in range(3):
        base = default_cfg["targets"][i].copy()
        if i < len(targets) and isinstance(targets[i], dict):
            base["label"] = str(targets[i].get("label", base["label"]))[:40]
            base["host"] = str(targets[i].get("host", base["host"])).strip()
            base["enabled"] = bool(targets[i].get("enabled", base["enabled"]))
        norm_targets.append(base)

    return {"master_enabled": master_enabled, "targets": norm_targets}


def save_wled_config(cfg: dict):
    os.makedirs(os.path.dirname(WLED_CONFIG_PATH), exist_ok=True)
    with open(WLED_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_enabled_wled_hosts(cfg: dict) -> list[str]:
    hosts: list[str] = []
    for t in cfg.get("targets", []):
        if not isinstance(t, dict):
            continue
        if bool(t.get("enabled", False)):
            h = str(t.get("host", "")).strip()
            if h:
                hosts.append(h)
    return hosts


def update_darts_wled_start_custom_weps(hosts: list[str]) -> tuple[bool, str]:
    """
    Schreibt NUR die -WEPS Zeile in /var/lib/autodarts/config/darts-wled/start-custom.sh um.
    Mapping/Presets bleiben unberührt.
    """
    if not os.path.exists(DARTS_WLED_START_CUSTOM):
        return False, f"start-custom.sh nicht gefunden: {DARTS_WLED_START_CUSTOM}"

    try:
        with open(DARTS_WLED_START_CUSTOM, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return False, f"start-custom.sh konnte nicht gelesen werden: {e}"

    new_lines = []
    replaced = False
    weps_re = re.compile(r'^\s*-WEPS\b')

    for line in lines:
        if (not replaced) and weps_re.match(line):
            indent = line.split("-WEPS", 1)[0]
            has_backslash = line.rstrip().endswith("\\")
            args = " ".join([f'"{h}"' for h in hosts]) if hosts else f'"{WLED_MDNS_NAME}"'
            new_line = f"{indent}-WEPS {args} "
            new_line += "\\\n" if has_backslash else "\n"
            new_lines.append(new_line)
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        return False, "Keine -WEPS Zeile in start-custom.sh gefunden (unerwartetes Format)."

    # Backup einmalig
    try:
        bak = DARTS_WLED_START_CUSTOM + ".bak"
        if not os.path.exists(bak):
            with open(bak, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except Exception:
        pass

    try:
        with open(DARTS_WLED_START_CUSTOM, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        return False, f"start-custom.sh konnte nicht geschrieben werden: {e}"

    return True, "start-custom.sh (-WEPS) aktualisiert."


# ---------------- WLED legacy flag compatibility ----------------



# ---------------- WLED Presets load/save ----------------

WLED_PRESET_FIXED_TYPES = {
    "-IDE": {"typeId": "player1", "label": "Spieler 1 / Player 1", "duration": False},
    "-IDE2": {"typeId": "player2", "label": "Spieler 2 / Player 2", "duration": False},
    "-IDE3": {"typeId": "player3", "label": "Spieler 3 / Player 3", "duration": False},
    "-IDE4": {"typeId": "player4", "label": "Spieler 4 / Player 4", "duration": False},
    "-IDE5": {"typeId": "player5", "label": "Spieler 5 / Player 5", "duration": False},
    "-IDE6": {"typeId": "player6", "label": "Spieler 6 / Player 6", "duration": False},
    "-G": {"typeId": "leg", "label": "Leg gewonnen / Game won", "duration": True},
    "-M": {"typeId": "match", "label": "Match gewonnen / Match won", "duration": True},
    "-TOE": {"typeId": "checkout", "label": "Checkout / Takeout", "duration": True},
    "-DSBULL": {"typeId": "bull", "label": "Doppelbull / Bull", "duration": True},
    "-S0": {"typeId": "score0", "label": "Score 0 / Score 0", "duration": True},
}


def _wled_presets_default_weps_text() -> str:
    return f'"{WLED_MDNS_NAME}"'


def _wled_presets_strip_line(line: str) -> str:
    s = (line or "").rstrip("\n")
    if s.rstrip().endswith("\\"):
        s = s.rstrip()
        s = s[:-1].rstrip()
    return s.strip()


def _wled_presets_read_file() -> list[str]:
    with open(DARTS_WLED_START_CUSTOM, "r", encoding="utf-8") as f:
        return f.readlines()


def _wled_presets_find_weps_index(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if re.match(r'^\s*-WEPS\b', line or ""):
            return idx
    return -1


def _wled_presets_extract_weps_text(lines: list[str], weps_idx: int) -> str:
    if weps_idx < 0 or weps_idx >= len(lines):
        return _wled_presets_default_weps_text()
    cleaned = _wled_presets_strip_line(lines[weps_idx])
    m = re.match(r'^-WEPS\s+(.*)$', cleaned)
    if not m:
        return _wled_presets_default_weps_text()
    value = (m.group(1) or "").strip()
    return value or _wled_presets_default_weps_text()


def _wled_presets_block_end(lines: list[str], start_idx: int) -> int:
    idx = start_idx
    while idx < len(lines):
        stripped = (lines[idx] or "").strip()
        if stripped == "" or stripped.startswith("#"):
            idx += 1
            continue
        if re.match(r'^-[A-Za-z0-9]+\b', stripped):
            idx += 1
            continue
        break
    return idx


def _wled_presets_row_from_line(line: str) -> dict | None:
    cleaned = _wled_presets_strip_line(line)
    if not cleaned or cleaned.startswith("#"):
        return None

    m = re.match(r'^(-A\d+)\s+(\d+)-(\d+)\s+"ps\|(\d+)(?:\|([^\"]+))?"$', cleaned)
    if m:
        return {
            "kind": "score_range",
            "typeId": "score_range",
            "label": "Score-Bereich / Score range",
            "arg": m.group(1),
            "duration": True,
            "seconds": (m.group(5) or "").strip(),
            "score": 180,
            "from": int(m.group(2)),
            "to": int(m.group(3)),
        }

    m = re.match(r'^(-S(\d+))\s+"ps\|(\d+)(?:\|([^\"]+))?"$', cleaned)
    if m:
        score_val = int(m.group(2))
        if score_val == 0:
            meta = WLED_PRESET_FIXED_TYPES["-S0"]
            return {
                "kind": "fixed",
                "typeId": meta["typeId"],
                "label": meta["label"],
                "arg": "-S0",
                "duration": True,
                "seconds": (m.group(4) or "").strip(),
                "score": 0,
                "from": 0,
                "to": 60,
            }
        return {
            "kind": "score_exact",
            "typeId": "score_exact",
            "label": "Exakter Score / Exact score",
            "arg": m.group(1),
            "duration": True,
            "seconds": (m.group(4) or "").strip(),
            "score": score_val,
            "from": 0,
            "to": 60,
        }

    m = re.match(r'^(-[A-Z0-9]+)\s+"ps\|(\d+)(?:\|([^\"]+))?"$', cleaned)
    if m:
        arg = m.group(1)
        sec = (m.group(3) or "").strip()
        meta = WLED_PRESET_FIXED_TYPES.get(arg)
        if meta:
            return {
                "kind": "fixed",
                "typeId": meta["typeId"],
                "label": meta["label"],
                "arg": arg,
                "duration": bool(meta.get("duration", False)),
                "seconds": sec,
                "score": 180,
                "from": 0,
                "to": 60,
            }
        return {
            "kind": "unknown",
            "typeId": "unknown",
            "label": f"Unbekannt / {arg}",
            "arg": arg,
            "duration": bool(sec),
            "seconds": sec,
            "score": 180,
            "from": 0,
            "to": 60,
        }

    return {
        "kind": "unknown",
        "typeId": "unknown",
        "label": f"Unbekannt / {cleaned}",
        "arg": cleaned.split()[0] if cleaned.startswith("-") else "-?",
        "duration": False,
        "seconds": "",
        "score": 180,
        "from": 0,
        "to": 60,
        "raw": cleaned,
    }


def load_wled_presets_state() -> tuple[bool, str, list[dict], str]:
    if not os.path.exists(DARTS_WLED_START_CUSTOM):
        return False, f"start-custom.sh nicht gefunden: {DARTS_WLED_START_CUSTOM}", [], _wled_presets_default_weps_text()

    try:
        lines = _wled_presets_read_file()
    except Exception as e:
        return False, f"start-custom.sh konnte nicht gelesen werden: {e}", [], _wled_presets_default_weps_text()

    weps_idx = _wled_presets_find_weps_index(lines)
    if weps_idx < 0:
        return False, "Keine -WEPS Zeile gefunden.", [], _wled_presets_default_weps_text()

    weps_text = _wled_presets_extract_weps_text(lines, weps_idx)
    block_end = _wled_presets_block_end(lines, weps_idx + 1)
    rows: list[dict] = []
    for line in lines[weps_idx + 1:block_end]:
        row = _wled_presets_row_from_line(line)
        if row:
            rows.append(row)

    return True, "Aktuelle Einstellungen geladen.", rows, weps_text


def _wled_presets_int(value, default: int, min_val: int = 0, max_val: int = 999) -> int:
    try:
        num = int(str(value).strip())
    except Exception:
        num = default
    return max(min_val, min(max_val, num))


def _wled_presets_normalize_row(row: dict) -> dict:
    kind = str((row or {}).get("kind") or "fixed").strip()
    type_id = str((row or {}).get("typeId") or "").strip()
    arg = str((row or {}).get("arg") or "").strip()
    label = str((row or {}).get("label") or "").strip()
    seconds = str((row or {}).get("seconds") or "").strip()

    if kind == "score_exact":
        score = _wled_presets_int((row or {}).get("score"), 180, 0, 180)
        return {
            "kind": "score_exact",
            "typeId": "score_exact",
            "label": label or "Exakter Score / Exact score",
            "arg": f"-S{score}",
            "duration": True,
            "seconds": seconds,
            "score": score,
            "from": 0,
            "to": 60,
        }

    if kind == "score_range":
        from_val = _wled_presets_int((row or {}).get("from"), 0, 0, 180)
        to_val = _wled_presets_int((row or {}).get("to"), 60, 0, 180)
        return {
            "kind": "score_range",
            "typeId": "score_range",
            "label": label or "Score-Bereich / Score range",
            "arg": "",
            "duration": True,
            "seconds": seconds,
            "score": 180,
            "from": from_val,
            "to": to_val,
        }

    if kind == "unknown":
        return {
            "kind": "unknown",
            "typeId": "unknown",
            "label": label or (f"Unbekannt / {arg}" if arg else "Unbekannt"),
            "arg": arg or "-?",
            "duration": bool(seconds),
            "seconds": seconds,
            "score": 180,
            "from": 0,
            "to": 60,
        }

    meta = WLED_PRESET_FIXED_TYPES.get(arg)
    if not meta and type_id:
        for fixed_arg, fixed_meta in WLED_PRESET_FIXED_TYPES.items():
            if fixed_meta.get("typeId") == type_id:
                arg = fixed_arg
                meta = fixed_meta
                break
    if not meta:
        meta = {"typeId": type_id or "fixed", "label": label or arg or "Eintrag", "duration": bool(seconds)}

    return {
        "kind": "fixed",
        "typeId": meta.get("typeId") or type_id or "fixed",
        "label": label or meta.get("label") or arg or "Eintrag",
        "arg": arg,
        "duration": bool(meta.get("duration", False)),
        "seconds": seconds,
        "score": 0 if arg == "-S0" else 180,
        "from": 0,
        "to": 60,
    }


def _wled_presets_line_for_row(row: dict, preset_index: int, area_index: int | None) -> str:
    kind = row.get("kind")
    seconds = str(row.get("seconds") or "").strip()
    sec_part = f"|{seconds}" if seconds else ""

    if kind == "score_exact":
        score = _wled_presets_int(row.get("score"), 180, 0, 180)
        return f'  -S{score} "ps|{preset_index}{sec_part}"'

    if kind == "score_range":
        from_val = _wled_presets_int(row.get("from"), 0, 0, 180)
        to_val = _wled_presets_int(row.get("to"), 60, 0, 180)
        return f'  -A{area_index} {from_val}-{to_val} "ps|{preset_index}{sec_part}"'

    arg = str(row.get("arg") or "").strip() or "-?"
    spacing = "   " if len(arg) < 4 else "  "
    if bool(row.get("duration", False)) or bool(seconds):
        return f'  {arg}{spacing}"ps|{preset_index}{sec_part}"'
    return f'  {arg}{spacing}"ps|{preset_index}"'


def save_wled_presets_state(rows_payload) -> tuple[bool, str, list[dict], str]:
    if not os.path.exists(DARTS_WLED_START_CUSTOM):
        return False, f"start-custom.sh nicht gefunden: {DARTS_WLED_START_CUSTOM}", [], _wled_presets_default_weps_text()

    try:
        lines = _wled_presets_read_file()
    except Exception as e:
        return False, f"start-custom.sh konnte nicht gelesen werden: {e}", [], _wled_presets_default_weps_text()

    weps_idx = _wled_presets_find_weps_index(lines)
    if weps_idx < 0:
        return False, "Keine -WEPS Zeile gefunden.", [], _wled_presets_default_weps_text()

    weps_text = _wled_presets_extract_weps_text(lines, weps_idx)
    rows_in = rows_payload if isinstance(rows_payload, list) else []
    rows = [_wled_presets_normalize_row(r if isinstance(r, dict) else {}) for r in rows_in]

    area_counter = 0
    rendered_lines: list[str] = []
    for idx, row in enumerate(rows, start=1):
        area_idx = None
        if row.get("kind") == "score_range":
            area_counter += 1
            area_idx = area_counter
        rendered_lines.append(_wled_presets_line_for_row(row, idx, area_idx))

    block_end = _wled_presets_block_end(lines, weps_idx + 1)
    prefix = list(lines[:weps_idx + 1])
    suffix = list(lines[block_end:])

    weps_line = prefix[-1].rstrip("\n")
    if rendered_lines:
        if not weps_line.rstrip().endswith("\\"):
            weps_line = weps_line.rstrip() + " \\"
        prefix[-1] = weps_line + "\n"
    else:
        if weps_line.rstrip().endswith("\\"):
            weps_line = weps_line.rstrip()
            weps_line = weps_line[:-1].rstrip()
        prefix[-1] = weps_line + "\n"

    body: list[str] = []
    for i, rendered in enumerate(rendered_lines):
        if i < len(rendered_lines) - 1:
            body.append(rendered + " \\\n")
        else:
            body.append(rendered + "\n")

    new_lines = prefix + body + suffix

    try:
        bak = DARTS_WLED_START_CUSTOM + ".bak"
        if not os.path.exists(bak):
            with open(bak, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except Exception:
        pass

    try:
        with open(DARTS_WLED_START_CUSTOM, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        return False, f"start-custom.sh konnte nicht geschrieben werden: {e}", rows, weps_text

    restarted = False
    try:
        if service_exists(DARTS_WLED_SERVICE) and service_is_active(DARTS_WLED_SERVICE):
            service_restart(DARTS_WLED_SERVICE)
            restarted = True
    except Exception:
        restarted = False

    msg = "Preset-Einstellungen gespeichert."
    if restarted:
        msg += " darts-wled.service wurde neu gestartet."
    return True, msg, rows, weps_text

def load_wled_flag() -> bool:
    # Rückwärtskompatibel: master_enabled
    try:
        cfg = load_wled_config()
        return bool(cfg.get("master_enabled", True))
    except Exception:
        return True


def save_wled_flag(enabled: bool):
    # 1) in Multi-Config spiegeln
    cfg = load_wled_config()
    cfg["master_enabled"] = bool(enabled)
    # wenn master aus, lassen wir Targets wie sie sind (nur master verhindert Start)
    save_wled_config(cfg)

    # 2) Legacy-Flag weiterhin schreiben (falls andere Teile es noch lesen)
    try:
        os.makedirs(os.path.dirname(WLED_FLAG_PATH), exist_ok=True)
        with open(WLED_FLAG_PATH, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(enabled)}, f, indent=2)
    except Exception:
        pass


# ---------------- Misc helpers ----------------

def tail_file(path: str, n: int = 20, max_chars: int = 6000) -> str:
    """Liest die letzten N Zeilen einer Datei, ohne die komplette Datei einzulesen."""
    try:
        if not os.path.exists(path):
            return ""

        # Wir lesen von hinten in Blöcken, bis wir genug Zeilen haben.
        block_size = 4096
        data = b""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()

            while pos > 0 and data.count(b"\n") <= (n + 2) and len(data) < (max_chars * 6):
                step = min(block_size, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data

        text = data.decode("utf-8", errors="replace")
        out = "\n".join(text.splitlines()[-n:])
        if len(out) > max_chars:
            out = out[-max_chars:]
        return out.strip()
    except Exception:
        return ""

# ---------------- darts-caller start-custom.sh read/write ----------------

# === Pi monitor test: status/lock helpers (leichtgewichtig, damit man nicht mehrfach startet) ===
def _pid_cmdline_contains(pid: int, needle: str) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", "ignore").replace("\x00", " ")
        return needle in cmd
    except Exception:
        return False


def _is_pi_monitor_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    # sicherstellen, dass es wirklich unser Script ist (pid reuse vermeiden)
    return _pid_cmdline_contains(pid, "pi_monitor_test.sh")


def _read_pi_monitor_state() -> dict:
    state: dict = {}
    try:
        with open(PI_MONITOR_STATE, "r", encoding="utf-8") as f:
            state = json.load(f) or {}
    except Exception:
        state = {}
    pid = None
    try:
        with open(PI_MONITOR_PIDFILE, "r", encoding="utf-8") as f:
            pid = int((f.read() or "").strip() or "0")
    except Exception:
        pid = None

    running = _is_pi_monitor_running(pid)
    now = time.time()
    started_ts = float(state.get("started_ts") or 0)
    ends_ts = float(state.get("ends_ts") or 0)
    interval_s = int(state.get("interval_s") or 0)
    duration_min = int(state.get("duration_min") or 0)
    remaining_sec = max(0, int(ends_ts - now)) if ends_ts else 0

    if not running:
        # stale files aufräumen, wenn Prozess wirklich weg ist
        try:
            if os.path.exists(PI_MONITOR_PIDFILE):
                os.remove(PI_MONITOR_PIDFILE)
        except Exception:
            pass
        try:
            if os.path.exists(PI_MONITOR_STATE):
                os.remove(PI_MONITOR_STATE)
        except Exception:
            pass

    return {
        "running": bool(running),
        "pid": int(pid) if (pid and running) else None,
        "started_ts": started_ts if started_ts else None,
        "ends_ts": ends_ts if ends_ts else None,
        "interval_s": interval_s if interval_s else None,
        "duration_min": duration_min if duration_min else None,
        "remaining_sec": remaining_sec if running else 0,
    }


def get_pi_monitor_status() -> dict:
    st = _read_pi_monitor_state()
    if not st.get("running"):
        return {"running": False, "msg": "Nicht aktiv."}

    # menschenlesbare Infos
    try:
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["started_ts"]))
    except Exception:
        started = ""
    try:
        ends = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["ends_ts"]))
    except Exception:
        ends = ""
    return {
        "running": True,
        "msg": f"Läuft (PID {st.get('pid')}). Start: {started} · Ende: {ends}",
        **st,
    }


def start_pi_monitor(interval_s: int, duration_min: int) -> dict:
    # Guard: nicht mehrfach starten
    st = _read_pi_monitor_state()
    if st.get("running"):
        return {"ok": False, "running": True, "msg": "Pi Monitor läuft bereits – bitte warten bis er fertig ist."}

    if interval_s < 1 or interval_s > 3600:
        return {"ok": False, "running": False, "msg": "Intervall ungültig."}
    if duration_min < 1 or duration_min > 24 * 60:
        return {"ok": False, "running": False, "msg": "Dauer ungültig."}

    if not os.path.exists(PI_MONITOR_SCRIPT):
        return {"ok": False, "running": False, "msg": f"Script nicht gefunden: {PI_MONITOR_SCRIPT}"}

    # Command bauen (wenn nicht root → sudo -n, damit es nicht hängen kann)
    cmd = [PI_MONITOR_SCRIPT, str(interval_s), str(duration_min)]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd

    # Output mitschreiben (hilft bei Debug, kost kaum was)
    try:
        out = open(PI_MONITOR_OUTLOG, "a", encoding="utf-8")
    except Exception:
        out = open(os.devnull, "w", encoding="utf-8")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
    except Exception as e:
        try:
            out.close()
        except Exception:
            pass
        return {"ok": False, "running": False, "msg": f"Start fehlgeschlagen: {e}"}

    now = time.time()
    ends = now + (duration_min * 60)
    state = {
        "started_ts": now,
        "ends_ts": ends,
        "interval_s": interval_s,
        "duration_min": duration_min,
    }
    try:
        with open(PI_MONITOR_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f)
        with open(PI_MONITOR_PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except Exception:
        pass

    return {"ok": True, "running": True, "msg": "Pi Monitor gestartet.", **get_pi_monitor_status()}


def stop_pi_monitor() -> dict:
    st = _read_pi_monitor_state()
    if not st.get("running"):
        return {"ok": True, "running": False, "msg": "Nicht aktiv."}
    pid = st.get("pid")
    try:
        os.kill(int(pid), 15)  # SIGTERM
    except Exception as e:
        return {"ok": False, "running": True, "msg": f"Konnte nicht stoppen: {e}"}
    return {"ok": True, "running": False, "msg": "Stop gesendet."}

def _read_var_from_line(line: str) -> str:
    if "=" not in line:
        return ""
    val = line.split("=", 1)[1].strip()
    if val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    if val.startswith("'") and val.endswith("'"):
        return val[1:-1]
    return val


def read_darts_caller_credentials():
    """
    Liest NUR diese Variablen:
      autodarts_email
      autodarts_password
      autodarts_board_id
    """
    email = ""
    password = ""
    board_id = ""

    if not os.path.exists(DARTS_CALLER_START_CUSTOM):
        return email, password, board_id, False, ""

    try:
        with open(DARTS_CALLER_START_CUSTOM, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return email, password, board_id, True, f"Fehler beim Lesen von start-custom.sh: {e}"

    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("autodarts_email"):
            email = _read_var_from_line(s)
        elif s.startswith("autodarts_password"):
            password = _read_var_from_line(s)
        elif s.startswith("autodarts_board_id"):
            board_id = _read_var_from_line(s)

    return email, password, board_id, True, ""


def _set_var_line(lines, key, value):
    pattern = re.compile(rf'^(\s*{re.escape(key)}\s*=\s*).*$')
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = pattern.match(line)
        if m:
            prefix = m.group(1)
            safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines[i] = f'{prefix}"{safe}"\n'
            return True
    return False


def write_darts_caller_credentials_strict(path, email, password, board_id):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    ok1 = _set_var_line(lines, "autodarts_email", email) if email is not None else True
    ok2 = _set_var_line(lines, "autodarts_password", password) if password is not None else True
    ok3 = _set_var_line(lines, "autodarts_board_id", board_id) if board_id is not None else True

    if not (ok1 and ok2 and ok3):
        raise RuntimeError("start-custom.sh: benötigte Variablenzeilen nicht gefunden – es wurde NICHT geschrieben.")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def write_darts_caller_credentials(email, password, board_id):
    return write_darts_caller_credentials_strict(
        DARTS_CALLER_START_CUSTOM,
        email=email,
        password=password,
        board_id=board_id,
    )


# ---------------- ROUTES ----------------

@app.route("/help", methods=["GET"])
def help_page():
    """Handbuch als PDF (inline) aus AUTODARTS_DATA_DIR ausliefern."""
    pdf_path = DATA_DIR / HELP_PDF_FILENAME
    if not pdf_path.exists():
        return (
            "<html><body><h1>Handbuch nicht gefunden</h1>"
            "<p>Die Datei <code>Autodarts_install_manual.pdf</code> wurde nicht gefunden.</p>"
            "</body></html>",
            404,
        )

    resp = send_from_directory(str(DATA_DIR), HELP_PDF_FILENAME)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'inline; filename="{HELP_PDF_FILENAME}"'
    return resp


@app.route("/", methods=["GET"])
def index():
    # Auto-Update soll standardmäßig AUS sein (einmalige Umstellung)
    ensure_msg = ensure_autoupdate_default_once()

    (
        ssid, ip, lan_ip,
        autodarts_active, autodarts_version,
        cpu_pct, mem_used, mem_total, temp_c,
        wifi_ok, lan_ok, dongle_ok,
        net_ok, ping_uplink_label,
        current_ap_ssid,
    ) = get_index_stats_cached()
    wifi_signal = None  # Signalstärke wird nur auf Knopfdruck geladen
    ad_restarted = request.args.get("ad_restarted") == "1"
    wifi_conn_name, wifi_autoconnect_enabled = get_active_wifi_autoconnect_state()



    cam_config = load_cam_config()
    camera_mode = _camera_mode_runtime_active(cam_config, autodarts_active=autodarts_active)
    cam_info_message = ""

    # Nach Service-Neustart oder bei inkonsistentem Zustand (Autodarts läuft bereits)
    # soll der Kamera-Modus sicher AUS sein und nichts blockieren.
    if bool(cam_config.get("camera_mode", False)) and not camera_mode:
        subprocess.run(["pkill", "-f", "mjpg_streamer"], capture_output=True, text=True)
        _set_camera_mode_state(cam_config, False)
        save_cam_config(cam_config)

    cam_inventory = []
    cam_slots = []
    cam_devices = []
    cam_count_found = 0

    if camera_mode:
        cam_inventory = cam_config.get("camera_inventory", [])
        if not isinstance(cam_inventory, list):
            cam_inventory = []

        if not cam_inventory:
            legacy_devices = cam_config.get("devices", [])
            if isinstance(legacy_devices, list) and legacy_devices:
                cam_inventory = _legacy_inventory_from_devices(legacy_devices)

        cam_slots = _normalize_camera_slots(cam_inventory, cam_config.get("camera_slots"))
        cam_devices = _devices_for_slots(cam_inventory, cam_slots)
        cam_count_found = len(cam_inventory)

        dirty_cam_cfg = False
        if cam_config.get("camera_inventory") != cam_inventory:
            cam_config["camera_inventory"] = cam_inventory
            dirty_cam_cfg = True
        if cam_config.get("camera_slots") != cam_slots:
            cam_config["camera_slots"] = cam_slots
            dirty_cam_cfg = True
        if cam_config.get("devices") != cam_devices:
            cam_config["devices"] = cam_devices
            dirty_cam_cfg = True
        if cam_config.get("desired_cams") != cam_count_found:
            cam_config["desired_cams"] = cam_count_found
            dirty_cam_cfg = True
        if dirty_cam_cfg:
            save_cam_config(cam_config)

        if cam_count_found == 0:
            cam_info_message = "Es wurden noch keine Kameras gesucht."
    host = request.host.split(":", 1)[0]
    darts_url = f"http://{host}:3180"

    cam_indices = list(range(1, len(cam_slots) + 1))

    # LED panel info
    caller_email, caller_password, caller_board_id, caller_exists, caller_err = read_darts_caller_credentials()
    caller_installed = os.path.exists(DARTS_CALLER_DIR)
    wled_installed = os.path.exists(DARTS_WLED_DIR)
    # WLED / LED-Bänder (User-UI: nur Ein/Aus)
    wled_cfg = load_wled_config()

    wled_master_enabled = bool(wled_cfg.get("master_enabled", True))

    admin_unlocked = bool(session.get('admin_unlocked', False))
    adminerr = request.args.get('adminerr')
    adminmsg = (request.args.get('adminmsg') or '').strip()
    adminok = (request.args.get('adminok') == '1')

    msg = request.args.get('msg', '') or (ensure_msg or '')
    open_adver = (request.args.get('open_adver') == '1')

    autoupdate_enabled = autodarts_autoupdate_is_enabled()
    update_check = load_update_check()
    webpanel_version = get_webpanel_version()
    webpanel_check = load_webpanel_update_check() if admin_unlocked else {}
    webpanel_update_available = bool(webpanel_check.get('installed') and webpanel_check.get('latest') and webpanel_check.get('installed') != webpanel_check.get('latest'))
    webpanel_state = load_webpanel_update_state() if admin_unlocked else {}
    webpanel_log_tail = tail_file(WEBPANEL_UPDATE_LOG, n=25, max_chars=3500) if admin_unlocked else ""
    uvc_backup_info = get_uvc_backup_info() if admin_unlocked else {}

    extensions_state = load_extensions_update_state() if admin_unlocked else {}
    extensions_last = load_extensions_update_last() if admin_unlocked else {}
    extensions_log_tail = tail_file(EXTENSIONS_UPDATE_LOG, n=25, max_chars=3500) if admin_unlocked else ""
    update_available = bool(update_check.get('installed') and update_check.get('latest') and update_check.get('installed') != update_check.get('latest'))

    update_state = load_update_state() if admin_unlocked else {}
    update_log_tail = tail_file(AUTODARTS_UPDATE_LOG, n=25, max_chars=3500) if admin_unlocked else ""

    os_update_state = load_os_update_state() if admin_unlocked else {}
    os_update_log_tail = tail_file(OS_UPDATE_LOG, n=25, max_chars=3500) if admin_unlocked else ""

    ufw_installed = ufw_is_installed()
    ufw_state = load_ufw_state() if admin_unlocked else {}

    wled_targets = wled_cfg.get("targets", []) or []
    while len(wled_targets) < 3:
        wled_targets.append({"label": f"Dart LED{len(wled_targets)+1}", "host": "", "enabled": False})
    wled_targets = wled_targets[:3]

    wled_bands = []
    wled_hosts = []
    for i, t in enumerate(wled_targets, start=1):
        wled_bands.append({"slot": i, "enabled": bool(t.get("enabled", False))})
        wled_hosts.append(str(t.get("host", "")).strip())
    wled_service_exists = service_exists(DARTS_WLED_SERVICE)
    wled_service_active = service_is_active(DARTS_WLED_SERVICE) if wled_service_exists else False

    creds_ok = bool(caller_email and caller_board_id)

    ledcheck = request.args.get("ledcheck", "")
    ledmsg = request.args.get("ledmsg", "")

    # Buttons
    can_save_creds = caller_exists
    can_check = caller_installed and caller_exists and bool(caller_email and caller_board_id)

    # Admin / Doku
    admin_gpio_exists = os.path.exists(ADMIN_GPIO_IMAGE)
    pi_csv_tail = tail_file(PI_MONITOR_CSV, n=20)
    pi_csv_exists = os.path.exists(PI_MONITOR_CSV)
    pi_mon_status = get_pi_monitor_status()
    pi_readme_exists = os.path.exists(PI_MONITOR_README)

    return render_template(
        "index.html",
        darts_url=darts_url,
        max_cams=MAX_CAMERAS,
        cam_inventory=cam_inventory,
        cam_slots=cam_slots,
        cam_devices=cam_devices,
        cam_count_found=cam_count_found,
        cam_info_message=cam_info_message,
        base_port=STREAM_BASE_PORT,
        cpu_pct=cpu_pct,
        mem_used=mem_used,
        mem_total=mem_total,
        temp_c=temp_c,
        cam_indices=cam_indices,
        caller_email=caller_email,
        caller_board_id=caller_board_id,
        caller_exists=caller_exists,
        caller_err=caller_err,
        ledcheck=ledcheck,
        ledmsg=ledmsg,
        can_save_creds=can_save_creds,
        can_check=can_check,
        wled_installed=wled_installed,
        wled_bands=wled_bands,
        wled_hosts=wled_hosts,
        wled_master_enabled=wled_master_enabled,
        admin_unlocked=admin_unlocked,
        os_update_state=os_update_state,
        os_update_log_tail=os_update_log_tail,
        ufw_installed=ufw_installed,
        ufw_state=ufw_state,
        adminerr=adminerr,
        adminmsg=adminmsg,
        adminok=adminok,
        update_state=update_state,
        update_log_tail=update_log_tail,
        wled_service_exists=wled_service_exists,
        wled_service_active=wled_service_active,
        admin_gpio_exists=admin_gpio_exists,
        pi_csv_tail=pi_csv_tail,
        pi_mon_status=pi_mon_status,
        pi_csv_exists=pi_csv_exists,
        pi_readme_exists=pi_readme_exists,
        pi_monitor_script=PI_MONITOR_SCRIPT,
        pi_monitor_csv=PI_MONITOR_CSV,
        pi_monitor_readme=PI_MONITOR_README,
        pi_monitor_outlog=PI_MONITOR_OUTLOG,
        usr_local_bin_dir=USR_LOCAL_BIN_DIR,
        autodarts_data_dir=AUTODARTS_DATA_DIR,
        extensions_dir=EXTENSIONS_DIR,
        autodarts_active=autodarts_active,
        autodarts_version=autodarts_version,
        camera_mode=camera_mode,
        autodarts_notice=ad_restarted,
        wifi_ok=wifi_ok,
        dongle_ok=dongle_ok,
        net_ok=net_ok,
        ping_uplink_label=ping_uplink_label,
        current_ap_ssid=current_ap_ssid,
        ssid=ssid,
        ip=ip,
        wifi_signal=wifi_signal,
        wifi_conn_name=wifi_conn_name,
        wifi_autoconnect_enabled=wifi_autoconnect_enabled,
        msg=msg,
        autoupdate_enabled=autoupdate_enabled,
        update_check=update_check,
        update_available=update_available,
        wifi_interface=WIFI_INTERFACE,
        webpanel_version=webpanel_version,
        webpanel_check=webpanel_check,
        webpanel_update_available=webpanel_update_available,
        webpanel_state=webpanel_state,
        webpanel_log_tail=webpanel_log_tail,
        uvc_backup_info=uvc_backup_info,
        autodarts_versions_choices=get_autodarts_versions_choices(),
        autodarts_stable_version=autodarts_stable_from_menu(),
        autodarts_latest_online=autodarts_latest_cached(),
        autodarts_last_version=autodarts_last_version(),
        settings_path=SETTINGS_PATH,
        extensions_state=extensions_state,
        extensions_last=extensions_last,
        extensions_log_tail=extensions_log_tail,
        lan_ok=lan_ok,
        lan_ip=lan_ip,
    )


@app.route("/led/save", methods=["POST"])
def led_save():
    ad_email = request.form.get("ad_email", "").strip()
    ad_password = request.form.get("ad_password", "").strip()
    ad_board = request.form.get("ad_board", "").strip()

    cur_email, cur_pw, cur_board, exists, err = read_darts_caller_credentials()
    if not exists:
        return redirect(url_for("index", ledcheck="bad", ledmsg="start-custom.sh nicht gefunden."))

    # leer lassen => unverändert
    if not ad_email:
        ad_email = cur_email
    if not ad_password:
        ad_password = cur_pw
    if not ad_board:
        ad_board = cur_board

    try:
        write_darts_caller_credentials(ad_email, ad_password, ad_board)
        return redirect(url_for("index", ledcheck="ok", ledmsg="Gespeichert (start-custom.sh aktualisiert)."))
    except Exception as e:
        return redirect(url_for("index", ledcheck="bad", ledmsg=f"Speichern fehlgeschlagen: {e}"))


@app.route("/led/check", methods=["POST"])
def led_check():
    email, pw, bid, exists, err = read_darts_caller_credentials()
    if not exists:
        return redirect(url_for("index", ledcheck="bad", ledmsg="start-custom.sh nicht gefunden."))
    if err:
        return redirect(url_for("index", ledcheck="bad", ledmsg=err))
    if not email or not pw or not bid:
        return redirect(url_for("index", ledcheck="bad", ledmsg="Bitte Account/Passwort/Board-ID setzen. (2FA muss AUS sein)"))
    return redirect(url_for("index", ledcheck="ok", ledmsg="Daten vorhanden. Hinweis: 2FA muss AUS sein."))




@app.route("/wled-presets", methods=["GET"])
def wled_presets():
    return render_template("wled_presets.html")


def _wled_json_get(ip_or_host: str, path: str = "/json/state", timeout_s: float = 1.2):
    url = f"http://{ip_or_host}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "AutodartsPanel"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        raw = r.read().decode("utf-8", errors="ignore") or "{}"
        return json.loads(raw)


def _wled_json_post(ip_or_host: str, payload: dict, path: str = "/json/state", timeout_s: float = 1.2):
    url = f"http://{ip_or_host}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "AutodartsPanel",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        raw = r.read().decode("utf-8", errors="ignore") or "{}"
        return json.loads(raw) if raw else {}


def _resolve_wled_target_for_slot_or_host(slot: int | None, fallback_host: str | None = None):
    cfg = load_wled_config()
    if not bool(cfg.get("master_enabled", True)):
        return None, None, "WLED ist global deaktiviert."

    host = (fallback_host or "").strip()
    if slot and slot > 0:
        targets = cfg.get("targets", []) or []
        if 1 <= slot <= len(targets):
            t = targets[slot - 1] or {}
            if not bool(t.get("enabled", False)):
                return None, None, f"WLED Slot {slot} ist deaktiviert."
            cfg_host = str(t.get("host", "")).strip()
            if cfg_host:
                host = cfg_host

    if not host:
        return None, None, "Kein WLED Host gefunden."

    ip = resolve_host_to_ip_fast(host, timeout_s=0.8)
    if not ip:
        return None, host, f"{host} konnte nicht aufgelöst werden."

    if not is_wled_reachable(ip, timeout_sec=1.0):
        return None, host, f"{host} ({ip}) ist nicht erreichbar."

    return ip, host, None


@app.route("/api/wled-presets/send", methods=["POST"])
def api_wled_presets_send():
    data = request.get_json(silent=True) or {}
    try:
        preset = int(data.get("preset") or 0)
    except Exception:
        preset = 0
    try:
        slot = int(data.get("slot") or 0)
    except Exception:
        slot = 0
    host_fallback = str(data.get("host") or "").strip()

    if preset < 1:
        return jsonify({"ok": False, "msg": "Ungültige Preset-Nummer."}), 400

    ip, host, err = _resolve_wled_target_for_slot_or_host(slot, host_fallback)
    if err:
        return jsonify({"ok": False, "msg": err}), 400

    try:
        state_before = _wled_json_get(ip, "/json/state", timeout_s=1.5) or {}
        is_on = bool(state_before.get("on", False))

        if is_on:
            loaded_existing = False
            try:
                _wled_json_post(ip, {"ps": preset}, "/json/state", timeout_s=1.5)
                time.sleep(0.25)
                state_after_load = _wled_json_get(ip, "/json/state", timeout_s=1.5) or {}
                loaded_existing = int(state_after_load.get("ps", -1)) == int(preset)
            except Exception:
                loaded_existing = False

            _wled_json_post(ip, {"psave": preset, "ib": True, "sb": True}, "/json/state", timeout_s=1.8)

            if loaded_existing:
                msg = f"Preset {preset} auf {host} übernommen und neu gespeichert."
            else:
                msg = f"Preset {preset} auf {host} neu aus dem aktuellen Zustand angelegt."

            return jsonify({"ok": True, "msg": msg, "host": host, "ip": ip, "preset": preset})

        default_payload = {
            "on": True,
            "bri": 180,
            "seg": [{
                "id": 0,
                "col": [[255, 160, 0], [0, 0, 0], [0, 0, 0]],
                "fx": 0,
                "sx": 128,
                "ix": 128,
                "pal": 0
            }],
            "psave": preset,
            "ib": True,
            "sb": True
        }
        _wled_json_post(ip, default_payload, "/json/state", timeout_s=1.8)
        return jsonify({
            "ok": True,
            "msg": f"Preset {preset} auf {host} neu mit Standard-Licht angelegt.",
            "host": host,
            "ip": ip,
            "preset": preset,
            "used_default": True
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": f"WLED Senden fehlgeschlagen: {e}"}), 500


@app.route("/api/wled-presets/load", methods=["GET"])
def api_wled_presets_load():
    ok, msg, rows, weps_text = load_wled_presets_state()
    status = 200 if ok else 400
    return jsonify({
        "ok": ok,
        "msg": msg,
        "rows": rows,
        "wepsText": weps_text,
        "path": DARTS_WLED_START_CUSTOM,
    }), status


@app.route("/api/wled-presets/save", methods=["POST"])
def api_wled_presets_save():
    data = request.get_json(silent=True) or {}
    rows = data.get("rows", [])
    ok, msg, saved_rows, weps_text = save_wled_presets_state(rows)
    status = 200 if ok else 400
    return jsonify({
        "ok": ok,
        "msg": msg,
        "rows": saved_rows,
        "wepsText": weps_text,
        "path": DARTS_WLED_START_CUSTOM,
    }), status


@app.route("/wled-open", methods=["GET"])
def wled_open():
    # Slot 1 öffnen
    return redirect(url_for("wled_open_slot", slot=1))


@app.route("/wled/open/<int:slot>", methods=["GET"])
def wled_open_slot(slot: int):
    cfg = load_wled_config()
    if not bool(cfg.get("master_enabled", True)):
        return (
            "<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>"
            "<h1>WLED deaktiviert</h1>"
            "<p>WLED wurde in der Weboberfläche deaktiviert.</p>"
            f"<p><a style='color:#3b82f6' href='{url_for('index')}'>Zurück</a></p>"
            "</body></html>",
            200,
        )

    targets = cfg.get("targets", [])
    if slot < 1 or slot > len(targets):
        return (
            "<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>"
            "<h1>Ungültiger WLED Slot</h1>"
            f"<p>Slot {slot} existiert nicht.</p>"
            f"<p><a style='color:#3b82f6' href='{url_for('index')}'>Zurück</a></p>"
            "</body></html>",
            404,
        )

    host = str(targets[slot - 1].get("host", "")).strip()
    slot_enabled = bool(targets[slot - 1].get("enabled", False))
    if not slot_enabled:
        return (
            "<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>"
            "<h1>WLED Slot deaktiviert</h1>"
            f"<p>Slot {slot} ist aktuell nicht aktiviert.</p>"
            f"<p><a style='color:#3b82f6' href='{url_for('index')}'>Zurück</a></p>"
            "</body></html>",
            200,
        )

    if not host:
        return (
            "<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>"
            "<h1>Kein WLED eingetragen</h1>"
            f"<p>Für Slot {slot} wurde noch kein Hostname/IP eingetragen.</p>"
            f"<p><a style='color:#3b82f6' href='{url_for('index')}'>Zurück</a></p>"
            "</body></html>",
            400,
        )

    ok, ip = is_http_reachable(host, timeout_s=0.8)
    if not ok:
        return (
            "<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>"
            "<h1>WLED nicht erreichbar</h1>"
            "<p>Sie haben kein offizielles LED Band mit Controller im Einsatz, oder der Controller ist aktuell nicht verbunden.</p>"
            f"<p>Host: <code>{host}</code></p>"
            f"<p><a style='color:#3b82f6' href='{url_for('index')}'>Zurück</a></p>"
            "</body></html>",
            503,
        )

    target = ip or host
    return redirect(f"http://{target}/")


@app.route("/wled/save-targets", methods=["POST"])
def wled_save_targets():
    cfg = load_wled_config()
    targets = cfg.get("targets", [])
    if not isinstance(targets, list):
        targets = []

    # Ensure 3 targets
    while len(targets) < 3:
        targets.append({"label": f"Dart LED{len(targets)+1}", "host": f"Dart-Led{len(targets)+1}.local", "enabled": False})
    targets = targets[:3]

    for i in range(1, 4):
        label = request.form.get(f"wled_label_{i}", f"Dart LED{i}").strip()[:40]
        host_raw = request.form.get(f"wled_host_{i}")
        if host_raw is None:
            host = str(targets[i - 1].get("host", "")).strip()
        else:
            host = host_raw.strip()
        enabled = request.form.get(f"wled_enabled_{i}") == "1"

        targets[i - 1]["label"] = label if label else f"Dart LED{i}"
        targets[i - 1]["host"] = host
        targets[i - 1]["enabled"] = bool(enabled)

    cfg["targets"] = targets
    save_wled_config(cfg)

    # Service handling + -WEPS Update
    msg_parts = ["WLED Targets gespeichert."]
    ok = True

    if service_exists(DARTS_WLED_SERVICE):
        master = bool(cfg.get("master_enabled", True))
        hosts = get_enabled_wled_hosts(cfg) if master else []

        if (not master) or (not hosts):
            service_disable_now(DARTS_WLED_SERVICE)
            if not master:
                msg_parts.append("WLED ist deaktiviert → darts-wled wurde gestoppt.")
            else:
                msg_parts.append("Kein Target aktiv → darts-wled wurde gestoppt.")
        else:
            ok_weps, msg_weps = update_darts_wled_start_custom_weps(hosts)
            msg_parts.append(msg_weps)
            if ok_weps:
                service_enable_now(DARTS_WLED_SERVICE)
                service_restart(DARTS_WLED_SERVICE)
            else:
                ok = False

    return redirect(url_for("index", ledcheck=("ok" if ok else "bad"), ledmsg="\n".join(msg_parts)))


@app.route("/wled/toggle", methods=["POST"])
def wled_toggle():
    cfg = load_wled_config()
    new_master = not bool(cfg.get("master_enabled", True))
    cfg["master_enabled"] = new_master
    save_wled_config(cfg)
    save_wled_flag(new_master)  # auch legacy flag

    ok = True
    msg_parts = []

    # Service handling
    if service_exists(DARTS_WLED_SERVICE):
        if not new_master:
            service_disable_now(DARTS_WLED_SERVICE)
            msg_parts.append("WLED deaktiviert (merkt sich das nach Neustart). darts-wled wurde gestoppt.")
        else:
            hosts = get_enabled_wled_hosts(cfg)
            if hosts:
                ok_weps, msg_weps = update_darts_wled_start_custom_weps(hosts)
                msg_parts.append("WLED aktiviert (merkt sich das nach Neustart).")
                msg_parts.append(msg_weps)
                if ok_weps:
                    service_enable_now(DARTS_WLED_SERVICE)
                    service_restart(DARTS_WLED_SERVICE)
                else:
                    ok = False
            else:
                service_disable_now(DARTS_WLED_SERVICE)
                msg_parts.append("WLED aktiviert, aber kein Target aktiv → darts-wled bleibt aus.")
    else:
        msg_parts.append("WLED Toggle gespeichert (Service nicht gefunden).")

    return redirect(url_for("index", ledcheck=("ok" if ok else "bad"), ledmsg="\n".join(msg_parts)))




@app.route("/wled/set-enabled/<int:slot>", methods=["POST"])
def wled_set_enabled(slot: int):
    # User-UI: Slot 1..3
    if slot < 1 or slot > 3:
        return jsonify({"ok": False, "msg": "Ungültiger Slot."}), 400

    enabled = request.form.get("enabled") == "1"
    cfg = load_wled_config()
    cfg["master_enabled"] = True  # User-UI hat keinen Master-Schalter

    targets = cfg.get("targets", []) or []
    while len(targets) < 3:
        targets.append({"label": f"Dart LED{len(targets)+1}", "host": "", "enabled": False})
    targets = targets[:3]

    targets[slot - 1]["enabled"] = bool(enabled)
    cfg["targets"] = targets
    save_wled_config(cfg)

    # Service handling
    if service_exists(DARTS_WLED_SERVICE):
        hosts = get_enabled_wled_hosts(cfg)
        if hosts:
            update_darts_wled_start_custom_weps(hosts)
            service_enable_now(DARTS_WLED_SERVICE)
        else:
            service_disable_now(DARTS_WLED_SERVICE)

    return jsonify({"ok": True})


@app.route("/wled/save-enabled", methods=["POST"])
def wled_save_enabled():
    cfg = load_wled_config()
    cfg["master_enabled"] = True  # User-UI hat keinen Master-Schalter

    targets = cfg.get("targets", []) or []
    while len(targets) < 3:
        targets.append({"label": f"Dart LED{len(targets)+1}", "host": "", "enabled": False})
    targets = targets[:3]

    for i in range(1, 4):
        enabled = request.form.get(f"wled_enabled_{i}") == "1"
        targets[i - 1]["enabled"] = bool(enabled)

    cfg["targets"] = targets
    save_wled_config(cfg)

    # Service handling: nur wenn installiert/exists
    if service_exists(DARTS_WLED_SERVICE):
        hosts = get_enabled_wled_hosts(cfg)
        if hosts:
            update_darts_wled_start_custom_weps(hosts)
            service_enable_now(DARTS_WLED_SERVICE)
        else:
            service_disable_now(DARTS_WLED_SERVICE)

    return redirect(url_for("index"))


@app.route("/wled/save-hosts", methods=["POST"])
def wled_save_hosts():
    if not bool(session.get("admin_unlocked", False)):
        return redirect(url_for("index", adminerr="1") + "#admin_details")

    cfg = load_wled_config()
    cfg["master_enabled"] = True  # User-UI hat keinen Master-Schalter

    targets = cfg.get("targets", []) or []
    while len(targets) < 3:
        targets.append({"label": f"Dart LED{len(targets)+1}", "host": "", "enabled": False})
    targets = targets[:3]

    for i in range(1, 4):
        host = (request.form.get(f"wled_host_{i}", "") or "").strip()
        if host:
            targets[i - 1]["host"] = host

    cfg["targets"] = targets
    save_wled_config(cfg)

    # Falls aktuell aktiv -> -WEPS updaten + service neu starten
    if service_exists(DARTS_WLED_SERVICE):
        hosts = get_enabled_wled_hosts(cfg)
        if hosts:
            update_darts_wled_start_custom_weps(hosts)
            service_enable_now(DARTS_WLED_SERVICE)
        else:
            service_disable_now(DARTS_WLED_SERVICE)

    return redirect(url_for("index"))


def _wled_check_one(host: str) -> tuple[bool, str | None]:
    """
    Schneller WLED-Check ohne DNS-Blocker:
    - löst Host via avahi/getent mit Timeout auf
    - prüft dann http://<ip>/json/info
    """
    host = (host or "").strip()
    if not host:
        return False, None

    # Cache über Host (für schnelle Reloads / mehrere Tabs)
    now = time.time()
    cached = WLED_STATUS_CACHE.get(host)
    if cached and (now - cached[0]) < WLED_STATUS_CACHE_TTL_SEC:
        d = cached[1]
        return bool(d.get("online", False)), d.get("ip")

    ip = resolve_host_to_ip_fast(host, timeout_s=0.6)
    if not ip:
        WLED_STATUS_CACHE[host] = (now, {"online": False, "ip": None})
        return False, None

    ok = False
    try:
        url = f"http://{ip}/json/info"
        req = urllib.request.Request(url, headers={"User-Agent": "AutodartsPanel"})
        with urllib.request.urlopen(req, timeout=0.6) as r:
            status = getattr(r, "status", 200)
            data = r.read(32)
            ok = (200 <= status < 300) and bool(data)
    except Exception:
        ok = False

    WLED_STATUS_CACHE[host] = (now, {"online": ok, "ip": ip})
    return ok, ip



@app.route("/api/wifi/signal", methods=["GET"])
def api_wifi_signal():
    """Signalstärke (0..100) des aktuellen WLANs – nur auf Knopfdruck."""
    now = time.time()
    try:
        if (now - float(WIFI_SIGNAL_CACHE.get('ts', 0.0))) < WIFI_SIGNAL_CACHE_TTL_SEC:
            return jsonify({"signal": WIFI_SIGNAL_CACHE.get('v')})
    except Exception:
        pass

    sig = get_wifi_signal_percent()
    try:
        WIFI_SIGNAL_CACHE['ts'] = now
        WIFI_SIGNAL_CACHE['v'] = sig
    except Exception:
        pass
    iface = _get_default_route_interface() or _get_connected_wifi_interface(prefer=WIFI_INTERFACE if WIFI_INTERFACE else None) or WIFI_INTERFACE
    if iface == AP_INTERFACE:
        iface = WIFI_INTERFACE
    return jsonify({"signal": sig, "iface": iface})

@app.route("/api/wled/status", methods=["GET"])
def api_wled_status():
    cfg = load_wled_config()
    targets = cfg.get("targets", []) or []
    while len(targets) < 3:
        targets.append({"label": f"Dart LED{len(targets)+1}", "host": "", "enabled": False})
    targets = targets[:3]

    bands = []
    work = []
    for i, t in enumerate(targets, start=1):
        enabled = bool(t.get("enabled", False))
        host = str(t.get("host", "")).strip()
        bands.append({"slot": i, "enabled": enabled, "online": None, "ip": None})
        if enabled and host:
            work.append((i, host))

    # Parallel (3 Stück max) -> schneller
    if work:
        with ThreadPoolExecutor(max_workers=min(3, len(work))) as ex:
            futures = {ex.submit(_wled_check_one, host): slot for slot, host in work}
            for fut, slot in futures.items():
                try:
                    ok, ip = fut.result(timeout=1.2)
                except Exception:
                    ok, ip = (False, None)
                bands[slot - 1]["online"] = bool(ok)
                bands[slot - 1]["ip"] = ip

        # enabled, aber kein host -> online bleibt None (wird als "Prüfe…" angezeigt)
    return jsonify({"bands": bands})


# === Pi Monitor Test API (Admin) ===
@app.route("/api/pi_monitor/status", methods=["GET"])
def api_pi_monitor_status():
    if not bool(session.get("admin_unlocked", False)):
        return jsonify({"ok": False, "msg": "Admin gesperrt."}), 403
    st = get_pi_monitor_status()
    st["ok"] = True
    return jsonify(st)


@app.route("/api/pi_monitor/start", methods=["POST"])
def api_pi_monitor_start():
    if not bool(session.get("admin_unlocked", False)):
        return jsonify({"ok": False, "msg": "Admin gesperrt."}), 403
    data = request.get_json(silent=True) or {}
    try:
        interval_s = int(data.get("interval_s") or 10)
        duration_min = int(data.get("duration_min") or 30)
    except Exception:
        interval_s, duration_min = 10, 30
    res = start_pi_monitor(interval_s=interval_s, duration_min=duration_min)
    if not res.get("ok"):
        return jsonify(res), 400
    return jsonify(res)


@app.route("/api/pi_monitor/stop", methods=["POST"])
def api_pi_monitor_stop():
    if not bool(session.get("admin_unlocked", False)):
        return jsonify({"ok": False, "msg": "Admin gesperrt."}), 403
    res = stop_pi_monitor()
    if not res.get("ok"):
        return jsonify(res), 400
    return jsonify(res)

@app.route("/admin/unlock", methods=["POST"])
def admin_unlock():
    pw = (request.form.get("admin_password") or "").strip()
    if pw == ADMIN_PASSWORD:
        session["admin_unlocked"] = True
        return redirect(url_for("index", admin="1") + "#admin_details")
    session.pop("admin_unlocked", None)
    return redirect(url_for("index", adminerr="1") + "#admin_details")


@app.route("/admin/lock", methods=["POST"])
def admin_lock():
    session.pop("admin_unlocked", None)
    return redirect(url_for("index") + "#admin_details")



@app.route("/admin/reboot", methods=["POST"])
def admin_reboot():
    # Nur im Admin-Modus erlauben
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    unit_name = f"autodarts-reboot-{int(time.time())}"

    # Im Hintergrund rebooten (damit die HTTP-Response noch rausgeht)
    try:
        subprocess.Popen(
            ["sudo", "-n", "systemd-run", "--unit", unit_name, "--no-block", "--collect", "/sbin/reboot"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        # Fallback (falls systemd-run nicht geht)
        try:
            subprocess.Popen(
                ["sudo", "-n", "/sbin/reboot"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    return (
        "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Reboot</title></head><body style='font-family:system-ui;background:#111;color:#eee;padding:20px;'>"
        "<h2>Neustart wird ausgeführt…</h2>"
        "<p>Der Raspberry Pi startet jetzt neu. Diese Seite ist gleich nicht mehr erreichbar.</p>"
        "</body></html>"
    )






@app.route("/admin/shutdown", methods=["POST"])
def admin_shutdown():
    # Nur im Admin-Modus erlauben
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    unit_name = f"autodarts-shutdown-{int(time.time())}"

    try:
        subprocess.Popen(
            ["sudo", "-n", "systemd-run", "--unit", unit_name, "--no-block", "--collect", "/sbin/shutdown", "-h", "now"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        try:
            subprocess.Popen(
                ["sudo", "-n", "/sbin/shutdown", "-h", "now"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    return (
        "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Herunterfahren</title></head><body style='font-family:system-ui;background:#111;color:#eee;padding:20px;'>"
        "<h2>Herunterfahren wird ausgeführt…</h2>"
        "<p>Der Raspberry Pi fährt jetzt sauber herunter. Diese Seite ist gleich nicht mehr erreichbar.</p>"
        "</body></html>"
    )


@app.route("/admin/pi-monitor/download/<kind>", methods=["GET"])
def admin_pi_monitor_download(kind: str):
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    kind = (kind or "").strip().lower()
    mapping = {
        "csv": (PI_MONITOR_CSV, "pi_monitor_test.csv", "text/csv; charset=utf-8"),
        "readme": (PI_MONITOR_README, "pi_monitor_test_README.txt", "text/plain; charset=utf-8"),
        "outlog": (PI_MONITOR_OUTLOG, "pi_monitor_test.out", "text/plain; charset=utf-8"),
    }
    entry = mapping.get(kind)
    if not entry:
        return ("Unbekannter Download.", 404)

    path, download_name, mimetype = entry
    if not os.path.exists(path):
        return (f"Datei nicht gefunden: {path}", 404)

    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=download_name)


@app.route("/admin/pi-monitor/tail", methods=["GET"])
def admin_pi_monitor_tail():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    source = (request.args.get("source") or "csv").strip().lower()
    if source == "outlog":
        path = PI_MONITOR_OUTLOG
        filename = "pi_monitor_test_last_2000_outlog_lines.txt"
    else:
        path = PI_MONITOR_CSV
        filename = "pi_monitor_test_last_2000_csv_lines.txt"

    try:
        n = int(request.args.get("n") or 2000)
    except Exception:
        n = 2000
    n = max(1, min(5000, n))

    if not os.path.exists(path):
        return Response(f"Datei nicht gefunden: {path}\n", status=404, mimetype="text/plain")

    text = tail_file(path, n=n, max_chars=400000)
    if not text:
        text = ""

    resp = Response(text, mimetype="text/plain")
    if request.args.get("download") == "1":
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.route("/admin/os/update", methods=["POST"])
def admin_os_update():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    ok, msg = start_os_update_background()
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=msg) + "#admin_details")




# ---------------- Admin: Firewall (UFW) ----------------

@app.route("/admin/ufw/refresh", methods=["POST"])
def admin_ufw_refresh():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)
    ufw_refresh_state()
    return redirect(url_for("index", admin="1", adminmsg="Firewall-Status aktualisiert.", adminok="1") + "#admin_details")


@app.route("/admin/ufw/install", methods=["POST"])
def admin_ufw_install():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)
    if ufw_is_installed():
        return redirect(url_for("index", admin="1", adminmsg="UFW ist bereits installiert.", adminok="1") + "#admin_details")
    ok, msg = start_ufw_install_background()
    return redirect(url_for("index", admin="1", adminmsg=msg, adminok=("1" if ok else "0")) + "#admin_details")


@app.route("/admin/ufw/apply_ports", methods=["POST"])
def admin_ufw_apply_ports():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)
    ok, msg = ufw_apply_port_rules()
    # Status NICHT automatisch prüfen – wir aktualisieren nur den Cache (Button ist explizit)
    ufw_refresh_state()
    short = (msg.splitlines()[0] if msg else ("OK" if ok else "Fehler"))
    return redirect(url_for("index", admin="1", adminmsg=short, adminok=("1" if ok else "0")) + "#admin_details")


@app.route("/admin/ufw/enable", methods=["POST"])
def admin_ufw_enable():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)
    ok, msg = ufw_set_enabled(True)
    ufw_refresh_state()
    short = (msg.splitlines()[0] if msg else ("OK" if ok else "Fehler"))
    return redirect(url_for("index", admin="1", adminmsg=short, adminok=("1" if ok else "0")) + "#admin_details")


@app.route("/admin/ufw/disable", methods=["POST"])
def admin_ufw_disable():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)
    ok, msg = ufw_set_enabled(False)
    ufw_refresh_state()
    short = (msg.splitlines()[0] if msg else ("OK" if ok else "Fehler"))
    return redirect(url_for("index", admin="1", adminmsg=short, adminok=("1" if ok else "0")) + "#admin_details")
@app.route("/admin/autodarts/check", methods=["POST"])
def admin_autodarts_check():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    installed = get_autodarts_version()
    latest = fetch_latest_autodarts_version()
    channel = _get_updater_channel()
    data = {
        "ts": int(time.time()),
        "installed": installed,
        "latest": latest,
        "channel": channel,
    }
    save_update_check(data)

    if installed and latest:
        if installed == latest:
            msg = f"Kein Update verfügbar (bereits v{installed})."
        else:
            msg = f"Update verfügbar: v{installed} → v{latest}."
    else:
        msg = "Update-Check nicht möglich (Version oder Internet nicht verfügbar)."

    return redirect(url_for("index", admin="1", adminmsg=msg, adminok="1") + "#admin_details")


@app.route("/admin/autodarts/update", methods=["POST"])
def admin_autodarts_update():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    installed = get_autodarts_version()
    latest = fetch_latest_autodarts_version()

    # Wenn wir sicher wissen, dass es kein Update gibt → nicht starten
    if installed and latest and installed == latest:
        msg = f"Kein Update verfügbar (bereits v{installed})."
        return redirect(url_for("index", admin="1", adminok="1", adminmsg=msg) + "#admin_details")

    ok, msg = start_autodarts_update_background()
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=msg) + "#admin_details")


@app.route("/admin/webpanel/check", methods=["POST"])
def admin_webpanel_check():
    # Admin muss entsperrt sein
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    installed = get_webpanel_version()
    latest = fetch_latest_webpanel_version()

    save_webpanel_update_check({
        "ts": int(time.time()),
        "installed": installed,
        "latest": latest,
    })

    if latest and installed and installed == latest:
        flash("Webpanel: Kein Update verfügbar.", "info")
    elif latest:
        flash(f"Webpanel: Update verfügbar ({installed or 'unknown'} → {latest}).", "success")
    else:
        flash("Webpanel: Update-Check fehlgeschlagen.", "warning")

    return redirect(url_for("index", admin="1") + "#admin_details")


@app.route("/admin/webpanel/update", methods=["POST"])
def admin_webpanel_update():
    # Admin muss entsperrt sein
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    installed = get_webpanel_version()
    latest = fetch_latest_webpanel_version()

    # Wenn latest nicht ermittelbar: erst prüfen lassen
    if not latest:
        flash("Webpanel: Konnte 'latest' nicht ermitteln – bitte zuerst 'Update prüfen'.", "warning")
        return redirect(url_for("index", admin="1") + "#admin_details")

    if installed and installed == latest:
        flash("Webpanel: Kein Update verfügbar.", "info")
        return redirect(url_for("index", admin="1") + "#admin_details")

    ok, err = start_webpanel_update_background()
    if ok:
        flash("Webpanel-Update gestartet. Die Weboberfläche kann kurz neu starten.", "success")
    else:
        flash(f"Webpanel-Update konnte nicht gestartet werden: {err or 'unbekannt'}.", "danger")

    return redirect(url_for("index", admin="1") + "#admin_details")



@app.route("/admin/webpanel/uvc-install", methods=["POST"])
def admin_webpanel_uvc_install():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    info = get_uvc_backup_info()
    if (not info.get("backup_exists")) and info.get("marker_exists"):
        msg = f"UVC Hack nicht gestartet: Kein Original-Backup vorhanden, aber alter UVC-Marker gefunden. Bitte Backup nach {info.get('backup_dir')} kopieren."
        return redirect(url_for("index", admin="1", adminok="0", adminmsg=msg) + "#admin_details")

    ok, err = start_webpanel_update_background(mode="uvc-hack", allow_self_update=False)
    msg = "UVC Hack gestartet. Bitte danach kurz das Webpanel-Log prüfen." if ok else f"UVC Hack konnte nicht gestartet werden: {err or 'unbekannt'}."
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=msg) + "#admin_details")


@app.route("/admin/webpanel/uvc-uninstall", methods=["POST"])
def admin_webpanel_uvc_uninstall():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    info = get_uvc_backup_info()
    if not info.get("backup_exists"):
        msg = f"UVC Hack wurde NICHT deinstalliert: Kein lokales Original-Backup vorhanden. Bitte Backup nach {info.get('backup_dir')} kopieren."
        return redirect(url_for("index", admin="1", adminok="0", adminmsg=msg) + "#admin_details")

    ok, err = start_webpanel_update_background(mode="uvc-uninstall", allow_self_update=False)
    msg = "UVC Deinstallation gestartet. Der Mini PC startet danach automatisch neu." if ok else f"UVC Deinstallation konnte nicht gestartet werden: {err or 'unbekannt'}."
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=msg) + "#admin_details")


@app.route("/admin/wled-update", methods=["POST"])
def admin_wled_update():
    # Admin muss entsperrt sein
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    ok, msg = start_extensions_update_background("all")
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=(msg or "")) + "#admin_details")




@app.route("/admin/gpio-image", methods=["GET"])
def admin_gpio_image():
    if not bool(session.get("admin_unlocked", False)):
        return ("Forbidden", 403)

    if os.path.exists(ADMIN_GPIO_IMAGE):
        return send_file(ADMIN_GPIO_IMAGE, mimetype="image/jpeg")
    return (
        "<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>"
        "<h1>GPIO Bild nicht gefunden</h1>"
        f"<p>Datei fehlt: <code>{ADMIN_GPIO_IMAGE}</code></p>"
        f"<p><a style='color:#3b82f6' href='{url_for('index')}'>Zurück</a></p>"
        "</body></html>",
        404,
    )


@app.route("/set_cams", methods=["POST"])
def set_cams():
    cfg = load_cam_config()
    if not _camera_mode_runtime_active(cfg, autodarts_active=is_autodarts_active()):
        _set_camera_mode_state(cfg, False)
        save_cam_config(cfg)
        return redirect(url_for("index"))

    cameras = detect_camera_inventory(MAX_CAMERAS)
    slots = _normalize_camera_slots(cameras, cfg.get("camera_slots"))
    cfg["camera_inventory"] = cameras
    cfg["camera_slots"] = slots
    cfg["desired_cams"] = len(cameras)
    cfg["devices"] = _devices_for_slots(cameras, slots)
    save_cam_config(cfg)

    return redirect(url_for("index"))


@app.route("/set_cam_slot/<int:slot_id>", methods=["POST"])
def set_cam_slot(slot_id: int):
    cfg = load_cam_config()
    if not _camera_mode_runtime_active(cfg, autodarts_active=is_autodarts_active()):
        _set_camera_mode_state(cfg, False)
        save_cam_config(cfg)
        return redirect(url_for("index"))

    cameras = cfg.get("camera_inventory", [])
    if not isinstance(cameras, list) or not cameras:
        cameras = detect_camera_inventory(MAX_CAMERAS)

    slots = _normalize_camera_slots(cameras, cfg.get("camera_slots"))
    if slot_id < 1 or slot_id > len(slots):
        cfg["camera_inventory"] = cameras
        cfg["camera_slots"] = slots
        cfg["devices"] = _devices_for_slots(cameras, slots)
        cfg["desired_cams"] = len(cameras)
        save_cam_config(cfg)
        return redirect(url_for("index"))

    selected_camera_id = str(request.form.get("camera_id") or "").strip()
    valid_ids = {str(cam.get("id") or "").strip() for cam in cameras if isinstance(cam, dict)}
    if selected_camera_id not in valid_ids:
        return redirect(url_for("index"))

    for slot in slots:
        if slot.get("slot") != slot_id and str(slot.get("camera_id") or "") == selected_camera_id:
            slot["camera_id"] = ""
    slots[slot_id - 1]["camera_id"] = selected_camera_id
    slots = _normalize_camera_slots(cameras, slots)

    cfg["camera_inventory"] = cameras
    cfg["camera_slots"] = slots
    cfg["devices"] = _devices_for_slots(cameras, slots)
    cfg["desired_cams"] = len(cameras)
    save_cam_config(cfg)

    return redirect(url_for("index"))


@app.route("/camera-mode/start", methods=["POST"])
def camera_mode_start():
    """Kamera-Einstellung starten: Autodarts stoppen, Streams stoppen, Flag setzen."""
    subprocess.run(["systemctl", "stop", AUTODARTS_SERVICE], capture_output=True, text=True)
    subprocess.run(["pkill", "-f", "mjpg_streamer"], capture_output=True, text=True)

    cfg = load_cam_config()
    cameras = detect_camera_inventory(MAX_CAMERAS)
    slots = _normalize_camera_slots(cameras, cfg.get("camera_slots"))
    cfg["camera_inventory"] = cameras
    cfg["camera_slots"] = slots
    cfg["desired_cams"] = len(cameras)
    cfg["devices"] = _devices_for_slots(cameras, slots)
    _set_camera_mode_state(cfg, True)
    save_cam_config(cfg)

    return redirect(url_for("index"))


@app.route("/camera-mode/end", methods=["POST"])
def camera_mode_end():
    """Kamera-Einstellung beenden: Streams stoppen, Autodarts neu starten, Flag zurücksetzen."""
    subprocess.run(["pkill", "-f", "mjpg_streamer"], capture_output=True, text=True)
    subprocess.run(["systemctl", "restart", AUTODARTS_SERVICE], capture_output=True, text=True)

    cfg = load_cam_config()
    _set_camera_mode_state(cfg, False)
    save_cam_config(cfg)

    return redirect(url_for("index", ad_restarted=1))


def _nmcli_terse_split(line: str) -> list[str]:
    """
    Split nmcli -t output line by unescaped ':'.
    nmcli escapes ':' as '\:' and '\' as '\\'.
    """
    parts = []
    cur = []
    esc = False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == ':':
            parts.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append(''.join(cur))
    return parts


def _nmcli_unescape(val: str) -> str:
    return val.replace(r'\:', ':').replace(r'\\', '\\')


@app.get("/api/wifi/scan")
def api_wifi_scan():
    """
    Scan available WiFi networks via nmcli and return a clean list for a dropdown.
    Does NOT change connection logic – only discovery.
    """
    try:
        # --rescan yes can take longer; we keep it fast and let the user refresh if needed
        r = subprocess.run(
            ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, msg="WLAN-Scan dauert zu lange (Timeout). Bitte erneut versuchen."), 504
    except Exception as e:
        return jsonify(ok=False, msg=f"WLAN-Scan fehlgeschlagen: {e}"), 500

    if r.returncode != 0:
        return jsonify(ok=False, msg="nmcli Fehler: " + interpret_nmcli_error(r.stdout, r.stderr)), 500

    # Merge duplicate SSIDs: keep best signal, combine security labels
    merged: dict[str, dict] = {}

    for raw in r.stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        parts = _nmcli_terse_split(raw)
        # Expected: IN-USE,SSID,SIGNAL,SECURITY (some may be missing)
        while len(parts) < 4:
            parts.append("")
        in_use, ssid, signal, sec = parts[0], parts[1], parts[2], parts[3]
        ssid = _nmcli_unescape((ssid or "").strip())
        if not ssid:
            # hidden SSID: can't be selected reliably -> user can use manual entry
            continue
        try:
            sig_i = int((signal or "0").strip() or 0)
        except Exception:
            sig_i = 0
        sec = _nmcli_unescape((sec or "").strip())
        key = ssid

        item = merged.get(key)
        if item is None:
            merged[key] = {
                "ssid": ssid,
                "signal": sig_i,
                "security": sec,
                "in_use": (in_use.strip() == "*"),
            }
        else:
            # keep best signal; mark in_use if any entry is in use
            item["signal"] = max(int(item.get("signal") or 0), sig_i)
            item["in_use"] = bool(item.get("in_use")) or (in_use.strip() == "*")
            # combine security labels
            if sec and sec not in (item.get("security") or ""):
                item["security"] = (item.get("security") + ("/" if item.get("security") else "") + sec)

    networks = sorted(merged.values(), key=lambda x: (x.get("in_use") is True, int(x.get("signal") or 0)), reverse=True)
    return jsonify(ok=True, networks=networks)


@app.route("/wifi", methods=["GET", "POST"])
def wifi():
    message = ""
    success = False

    if request.method == "POST":
        ssid = request.form.get("ssid", "").strip()
        password = request.form.get("password", "").strip()

        if not ssid:
            message = "Bitte WLAN-Namen (SSID) eingeben."
        else:
            # 1) Nur den WLAN-USB-Dongle weich zurücksetzen
            soft_reset_wifi_dongle()

            # 2) Alte Verbindung löschen (wenn vorhanden) – Fehler ignorieren
            subprocess.run(
                ["nmcli", "connection", "delete", WIFI_CONNECTION_NAME],
                capture_output=True,
                text=True,
            )

            # 3) Neue Verbindung anlegen (mit unserem WLAN-Stick als Interface)
            add = subprocess.run(
                [
                    "nmcli",
                    "connection",
                    "add",
                    "type",
                    "wifi",
                    "ifname",
                    WIFI_INTERFACE,
                    "con-name",
                    WIFI_CONNECTION_NAME,
                    "ssid",
                    ssid,
                ],
                capture_output=True,
                text=True,
            )

            if add.returncode != 0:
                message = "Fehler beim Anlegen der WLAN-Verbindung: " + interpret_nmcli_error(add.stdout, add.stderr)
            else:
                # 4) Passwort + IP-Konfiguration setzen
                subprocess.run(
                    [
                        "nmcli",
                        "connection",
                        "modify",
                        WIFI_CONNECTION_NAME,
                        "wifi-sec.key-mgmt",
                        "wpa-psk",
                        "wifi-sec.psk",
                        password,
                    ],
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    [
                        "nmcli",
                        "connection",
                        "modify",
                        WIFI_CONNECTION_NAME,
                        "ipv4.method",
                        "auto",
                        "ipv6.method",
                        "ignore",
                    ],
                    capture_output=True,
                    text=True,
                )

                # 5) Erster Verbindungsversuch
                up = subprocess.run(
                    ["nmcli", "connection", "up", WIFI_CONNECTION_NAME],
                    capture_output=True,
                    text=True,
                )

                if up.returncode == 0:
                    message = "Erfolgreich mit WLAN verbunden."
                    success = True
                else:
                    err_text_full = (up.stderr or up.stdout or "")
                    err_lower = err_text_full.lower()

                    device_error = (
                        "no suitable device" in err_lower
                        or "no wifi device" in err_lower
                        or "no device" in err_lower
                        or "device not available because profile is not compatible" in err_lower
                        or "profile is not compatible with device" in err_lower
                        or "mismatching interface name" in err_lower
                    )

                    if device_error:
                        soft_reset_wifi_dongle()
                        up2 = subprocess.run(
                            ["nmcli", "connection", "up", WIFI_CONNECTION_NAME],
                            capture_output=True,
                            text=True,
                        )

                        if up2.returncode == 0:
                            message = (
                                "Verbindung fehlgeschlagen, wird erneut versucht ...\n"
                                "Der zweite Versuch war erfolgreich. "
                                "(Hinweis: WLAN-USB-Stick wurde kurz neu initialisiert.)"
                            )
                            success = True
                        else:
                            message = (
                                "Verbindung fehlgeschlagen, wird erneut versucht ...\n"
                                "Auch der zweite Versuch ist fehlgeschlagen: "
                                + interpret_nmcli_error(up2.stdout, up2.stderr)
                            )
                    else:
                        message = "Verbindung konnte nicht hergestellt werden: " + interpret_nmcli_error(up.stdout, up.stderr)

    # Aktuellen Status des WLAN-Dongles anzeigen
    ssid_cur, ip_cur = get_wifi_status()
    wifi_signal = get_wifi_signal_percent()
    if ssid_cur and ip_cur:
        current_info = f"Aktuell verbunden mit <strong>{ssid_cur}</strong> (IP {ip_cur})" + (f" · Signal: <strong>{wifi_signal}%</strong>." if wifi_signal is not None else "." )
    elif ssid_cur and not ip_cur:
        current_info = f"WLAN verbunden mit <strong>{ssid_cur}</strong>, aber es wurde keine IPv4-Adresse vergeben." + (f" (Signal: <strong>{wifi_signal}%</strong>)" if wifi_signal is not None else "")
    else:
        current_info = "Der USB-Dongle ist aktuell mit keinem WLAN verbunden."

    return render_template(
        "wifi.html",
        message=message,
        success=success,
        current_info=current_info,
        wifi_interface=WIFI_INTERFACE,
        wifi_connection_name=WIFI_CONNECTION_NAME,
        admin_unlocked=bool(session.get('admin_unlocked', False)),
    )






# ---------------- WLAN Tools (Autoconnect / gespeicherte WLANs löschen) ----------------

def _active_wifi_connection_name(iface: str) -> str | None:
    """
    Gibt den aktuell aktiven Connection-Namen auf dem Interface zurück (z.B. für Autoconnect Toggle).
    """
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "dev", "show", iface],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if r.returncode != 0:
            return None
        # Format: "GENERAL.CONNECTION:<NAME>"
        val = (r.stdout or "").split(":", 1)[-1].strip()
        if not val or val in ("--", "unknown"):
            return None
        return val
    except Exception:
        return None



def get_active_wifi_autoconnect_state() -> tuple[str | None, bool | None]:
    """Return (active_wifi_connection_name, autoconnect_enabled). Values may be None."""
    conn = _active_wifi_connection_name(WIFI_INTERFACE)
    if not conn:
        return None, None
    try:
        r = subprocess.run(
            ["nmcli", "-g", "connection.autoconnect", "connection", "show", conn],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
        if r.returncode != 0:
            return conn, None
        val = (r.stdout or "").strip().lower()
        if not val:
            return conn, None
        enabled = val in ("yes", "true", "1", "on")
        return conn, enabled
    except Exception:
        return conn, None


@app.route("/wifi/autoconnect/<mode>", methods=["POST"])
def wifi_autoconnect(mode: str):
    # Schutz: nur im Admin-Modus
    if not bool(session.get("admin_unlocked", False)):
        flash("Bitte zuerst im Admin-Bereich entsperren.", "warning")
        return redirect(url_for("index", admin="1") + "#admin_details")

    enable = (mode or "").lower() in ("on", "enable", "yes", "1", "true")
    conn = _active_wifi_connection_name(WIFI_INTERFACE)
    if not conn:
        flash("Kein aktives WLAN-Profil auf dem Internet-WLAN-Interface gefunden.", "warning")
        return redirect(url_for("wifi"))

    try:
        cmd = ["nmcli", "connection", "modify", conn, "connection.autoconnect", ("yes" if enable else "no")]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=6.0)
        if r.returncode == 0:
            flash(f"Autoconnect {'aktiviert' if enable else 'deaktiviert'} für: {conn}", "success")
        else:
            err = (r.stderr or r.stdout or "").strip()
            flash(f"Autoconnect konnte nicht gesetzt werden: {err or 'unbekannt'}", "danger")
    except Exception as e:
        flash(f"Autoconnect Fehler: {e}", "danger")

    return redirect(url_for("wifi"))


@app.route("/wifi/forget-saved", methods=["POST"])
def wifi_forget_saved():
    # Schutz: nur im Admin-Modus
    if not bool(session.get("admin_unlocked", False)):
        flash("Bitte zuerst im Admin-Bereich entsperren.", "warning")
        return redirect(url_for("index", admin="1") + "#admin_details")

    KEEP = "Autodarts-AP"
    deleted = []
    try:
        # NAME,TYPE -> wifi oder 802-11-wireless
        r = subprocess.run(
            ["nmcli", "-g", "NAME,TYPE", "connection", "show"],
            capture_output=True,
            text=True,
            timeout=4.0,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "nmcli error").strip())

        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            name, typ = line.split(":", 1)
            typ = typ.strip().lower()
            name = name.strip()
            if typ not in ("wifi", "802-11-wireless"):
                continue
            if name == KEEP:
                continue

            cmd = ["nmcli", "connection", "delete", name]
            if os.geteuid() != 0:
                cmd = ["sudo", "-n"] + cmd
            subprocess.run(cmd, capture_output=True, text=True, timeout=6.0)
            deleted.append(name)

        if deleted:
            flash("Gespeicherte WLAN-Verbindungen gelöscht: " + ", ".join(deleted), "success")
        else:
            flash("Keine gespeicherten WLAN-Verbindungen gefunden (außer Autodarts-AP).", "info")
    except Exception as e:
        flash(f"Konnte gespeicherte WLANs nicht löschen: {e}", "danger")

    return redirect(url_for("wifi"))
@app.route("/autoupdate/toggle", methods=["POST"])
def autoupdate_toggle():
    """Legacy Toggle (für alte Links)."""
    cur = autodarts_autoupdate_is_enabled()
    if cur is None:
        return redirect(url_for("index", msg="Auto-Update Service nicht gefunden."))
    ok, msg = autodarts_set_autoupdate(not bool(cur))
    return redirect(url_for("index", msg=msg))


@app.route("/autoupdate/set/<mode>", methods=["POST"])
def autoupdate_set(mode: str):
    """Deterministisch ein/aus schalten."""
    mode = (mode or "").strip().lower()
    if mode in ("on", "enable", "1", "true"):
        desired = True
    elif mode in ("off", "disable", "0", "false"):
        desired = False
    else:
        return redirect(url_for("index", msg="Ungültiger Modus.", open_adver="1") + "#ad-version")

    cur = autodarts_autoupdate_is_enabled()

    # AUS
    if not desired:
        ok, msg = autodarts_set_autoupdate(False)
        return redirect(url_for("index", msg=msg, open_adver="1") + "#ad-version")

    # AN
    if cur is True:
        return redirect(url_for("index", msg="Auto-Update ist bereits AN.", open_adver="1") + "#ad-version")
    if cur is False:
        ok, msg = autodarts_set_autoupdate(True)
        return redirect(url_for("index", msg=msg, open_adver="1") + "#ad-version")

    # Service fehlt -> via Installer (re)erstellen
    ver = get_autodarts_version() or ""
    ver = (ver or "").strip().lstrip("v")
    if ver and not re.match(r"^\d+\.\d+\.\d+(?:-(?:beta|alpha)\.\d+)?$", ver):
        ver = ""  # lieber nichts erzwingen

    if ver:
        cmd = f"bash <(curl -sL get.autodarts.io) {ver}"
        req = ver
    else:
        cmd = "bash <(curl -sL get.autodarts.io)"
        req = "latest"

    ok, _m = start_autodarts_update_background(
        cmd_override=cmd,
        requested_version=req,
        purpose="enable-autoupdate",
        disable_autoupdate_after=False,
    )
    if ok:
        return redirect(url_for("index", msg="Aktivierung gestartet (Installer erstellt Auto-Update Service)."))
    return redirect(url_for("index", msg=_m))


@app.route("/autodarts/version/install", methods=["POST"])
def autodarts_install_version():
    """
    Installiert gezielt eine freigegebene Autodarts-Version (kein Freitext).

    Quellen:
      - Dropdown aus AUTODARTS_VERSION_MENU (oben im Script)
      - "Auf stabile Version wechseln" -> erste feste Version (SemVer) in der Liste
      - "aktuell" -> neueste Online-Version (Installer ohne Versionsangabe)
      - "zuletzt" -> Rollback auf die zuletzt installierte Version (merkt sich das Panel automatisch)
    """

    v_raw = (request.form.get("version") or "").strip()
    if not v_raw:
        return redirect(url_for("index", msg="Bitte eine Version auswählen.", open_adver="1") + "#ad-version")

    stable = autodarts_stable_from_menu()

    special: str | None = None
    req_label = ""

    # "stable" => erste SemVer aus Liste
    if v_raw.lower() == "stable":
        if not stable:
            return redirect(url_for("index", msg="Keine stabile Version hinterlegt. Bitte oben in AUTODARTS_VERSION_MENU eine feste Version (z.B. 1.0.4) eintragen.", open_adver="1") + "#ad-version")
        selected = stable
        req_label = f"Stabil ({stable})"
    else:
        # Nur freigegebene Dropdown-Werte erlauben
        allowed = {str(opt.get("value")) for opt in get_autodarts_versions_choices()}
        if v_raw not in allowed:
            return redirect(url_for("index", msg="Diese Version ist nicht freigegeben. Bitte über das Dropdown auswählen.", open_adver="1") + "#ad-version")

        selected = v_raw
        if selected in ("__LATEST__", "__LAST__"):
            special = selected

        if special == "__LATEST__":
            latest = autodarts_latest_cached()
            req_label = f"Aktuellste (online: {latest})" if latest else "Aktuellste"
        elif special == "__LAST__":
            last = autodarts_last_version()
            req_label = f"Zuletzt (Rollback: {last})" if last else "Zuletzt (Rollback)"
        else:
            if stable and selected == stable:
                req_label = f"Stabil ({selected})"
            else:
                req_label = str(selected)

    # Aktuell installierte Version ermitteln (für "zuletzt")
    installed = (get_autodarts_version() or "").strip().lstrip("v")

    # Ziel bestimmen + Command bauen
    if special == "__LATEST__":
        # Wichtig: Keine Versionsangabe -> Installer nimmt die neueste Version.
        cmd = "bash <(curl -sL get.autodarts.io) -u"
        req = "latest"

        # vorherige Version merken
        if installed and _SEMVER_RE.match(installed):
            autodarts_set_last_version(installed)

    elif special == "__LAST__":
        target = autodarts_last_version()
        if not target:
            return redirect(url_for("index", msg="Rollback nicht möglich: Es ist noch keine 'zuletzt'-Version gespeichert.", open_adver="1") + "#ad-version")

        # Toggle-Verhalten: aktuelle Version als 'zuletzt' merken, damit man wieder zurück kann
        if installed and _SEMVER_RE.match(installed):
            autodarts_set_last_version(installed)

        cmd = f"bash <(curl -sL get.autodarts.io) -u {target}"
        req = target

    else:
        v = (str(selected) or "").strip().lstrip("v")
        if not _SEMVER_RE.match(v):
            return redirect(url_for("index", msg="Ungültige Versionsangabe.", open_adver="1") + "#ad-version")

        # vorherige Version merken
        if installed and _SEMVER_RE.match(installed):
            autodarts_set_last_version(installed)

        cmd = f"bash <(curl -sL get.autodarts.io) -u {v}"
        req = v

    ok, _m = start_autodarts_update_background(
        cmd_override=cmd,
        requested_version=req,
        purpose="install-version",
        disable_autoupdate_after=True,  # Default soll AUS bleiben
    )
    if ok:
        return redirect(url_for("index", msg=f"Autodarts Install/Update gestartet (Ziel: {req_label}).", open_adver="1") + "#ad-version")
    return redirect(url_for("index", msg=_m, open_adver="1") + "#ad-version")


@app.route("/wifi/ping/start", methods=["POST"])
def wifi_ping_start():
    ok, msg, job_id = start_ping_test(count=30)
    iface_label = None
    if job_id and job_id in PING_JOBS:
        iface_label = PING_JOBS[job_id].get("iface_label")
    return jsonify({"ok": bool(ok), "msg": msg, "job_id": job_id, "iface_label": iface_label})


@app.route("/wifi/ping/status/<job_id>", methods=["GET"])
def wifi_ping_status(job_id: str):
    job = PING_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "msg": "Job nicht gefunden."}), 404

    # Auto-cleanup nach 15min
    try:
        if time.time() - float(job.get("started", 0)) > 900:
            PING_JOBS.pop(job_id, None)
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "target": job.get("target"),
        "iface": job.get("iface"),
        "iface_label": job.get("iface_label"),
        "count": job.get("count", 30),
        "progress": job.get("progress", 0),
        "received": job.get("received", 0),
        "done": bool(job.get("done", False)),
        "min_ms": job.get("min_ms"),
        "max_ms": job.get("max_ms"),
        "avg_ms": job.get("avg_ms"),
        "error": job.get("error"),
    })

@app.route("/wifi/ping/ui", methods=["GET"])
def wifi_ping_ui():
    """Fallback/UI-Seite für den Verbindungstest (falls JS im Hauptscreen nicht greift)."""
    return render_template("wifi_ping_ui.html")


@app.route("/ap", methods=["GET", "POST"])
def ap_config():
    message = ""
    success = False
    current_ssid = get_ap_ssid()
    ap_choices = AP_SSID_CHOICES
    selected_ssid = current_ssid if (current_ssid in ap_choices) else (ap_choices[0] if ap_choices else "")

    if request.method == "POST":
        new_ssid = (request.form.get("ap_ssid_select") or "").strip()

        if not new_ssid:
            message = "Bitte einen Access-Point-Namen auswählen."
        elif new_ssid not in ap_choices:
            message = "Ungültige Auswahl. Bitte einen Namen aus der Liste wählen."
        elif len(new_ssid) > 32:
            message = "Der Access-Point-Name ist zu lang (max. 32 Zeichen)."
        else:
            res = subprocess.run(
                ["nmcli", "connection", "modify", AP_CONNECTION_NAME, "802-11-wireless.ssid", new_ssid],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                message = "Fehler beim Ändern des Access-Point-Namens: " + interpret_nmcli_error(res.stdout, res.stderr)
            else:
                subprocess.run(["nmcli", "connection", "down", AP_CONNECTION_NAME], capture_output=True, text=True)
                subprocess.run(["nmcli", "connection", "up", AP_CONNECTION_NAME], capture_output=True, text=True)
                success = True
                current_ssid = new_ssid
                message = f"Access-Point-Name wurde geändert auf „{new_ssid}“."

    return render_template(
        "ap_config.html",
        message=message,
        success=success,
        current_ssid=current_ssid,
        ap_connection_name=AP_CONNECTION_NAME,
        ap_choices=ap_choices,
        selected_ssid=selected_ssid,
    )



@app.route("/cam/<int:cam_id>", methods=["GET"])
def cam_view(cam_id: int):
    """
    Live-View einer Kamera:
      - nur möglich, wenn Kamera-Einstellung aktiv ist
      - startet genau EINEN mjpg_streamer (alle anderen werden beendet)
      - löst den Slot vor dem Start erneut auf die aktuelle physische Kamera auf
    """
    cam_config = load_cam_config()
    autodarts_active = is_autodarts_active()
    camera_mode = _camera_mode_runtime_active(cam_config, autodarts_active=autodarts_active)

    if not camera_mode:
        if bool(cam_config.get("camera_mode", False)):
            _set_camera_mode_state(cam_config, False)
            save_cam_config(cam_config)
        return (
            "<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>"
            "<h1>Kamera-Einstellung ist nicht aktiv</h1>"
            "<p>Bitte gehen Sie zurück und klicken Sie zuerst auf <strong>Kamera einstellen</strong>.</p>"
            f"<p><a style='color:#3b82f6' href='{url_for('index')}'>Zurück</a></p>"
            "</body></html>",
            400,
        )

    live_inventory = detect_camera_inventory(MAX_CAMERAS)
    if not live_inventory:
        live_inventory = cam_config.get("camera_inventory", [])
        if not isinstance(live_inventory, list):
            live_inventory = []
        if not live_inventory:
            live_inventory = _legacy_inventory_from_devices(cam_config.get("devices", []))

    cam_slots = _normalize_camera_slots(live_inventory, cam_config.get("camera_slots"))
    cam_count = len(cam_slots)

    if cam_count == 0 or cam_id < 1 or cam_id > cam_count:
        return (
            f"<html><body><h1>Kamera {cam_id} nicht verfügbar</h1>"
            f"<p>Es sind aktuell {cam_count} Kamera(s) konfiguriert.</p>"
            f"<p>Bitte gehen Sie zurück zur Hauptseite und führen Sie die Kamera-Erkennung erneut aus.</p>"
            f"<p><a href='{url_for('index')}'>Zurück</a></p>"
            f"</body></html>",
            404,
        )

    slot = cam_slots[cam_id - 1]
    selected_camera_id = str(slot.get("camera_id") or "")
    cam_entry = next((cam for cam in live_inventory if str(cam.get("id") or "") == selected_camera_id), None)
    if not cam_entry and 0 <= (cam_id - 1) < len(live_inventory):
        cam_entry = live_inventory[cam_id - 1]

    if not cam_entry:
        return (
            f"<html><body><h1>Kamera {cam_id} nicht verfügbar</h1>"
            "<p>Die gespeicherte Kamera-Zuordnung konnte nicht mehr aufgelöst werden.</p>"
            f"<p><a href='{url_for('index')}'>Zurück</a></p>"
            "</body></html>",
            404,
        )

    dev = str(cam_entry.get("preferred_dev") or "").strip()
    if not dev:
        return (
            f"<html><body><h1>Kamera {cam_id} nicht verfügbar</h1>"
            "<p>Für diese Kamera konnte aktuell kein gültiges /dev/videoX gefunden werden.</p>"
            f"<p><a href='{url_for('index')}'>Zurück</a></p>"
            "</body></html>",
            404,
        )

    # Konfiguration mit Live-Zuordnung aktualisieren, ohne extra im Idle zu scannen
    cfg_dirty = False
    if cam_config.get("camera_inventory") != live_inventory:
        cam_config["camera_inventory"] = live_inventory
        cfg_dirty = True
    if cam_config.get("camera_slots") != cam_slots:
        cam_config["camera_slots"] = cam_slots
        cfg_dirty = True
    live_devices = _devices_for_slots(live_inventory, cam_slots)
    if cam_config.get("devices") != live_devices:
        cam_config["devices"] = live_devices
        cfg_dirty = True
    if cfg_dirty:
        save_cam_config(cam_config)

    subprocess.run(["pkill", "-f", "mjpg_streamer"], capture_output=True, text=True)

    port = STREAM_BASE_PORT + (cam_id - 1)

    # Probe device capabilities (hilft bei "gefunden, aber kein Stream")
    probe = probe_v4l2_device(dev)
    preferred_formats = ["MJPG", "YUYV"]
    fmt, res = _best_resolution_for_formats(probe.get("resolutions", {}) if isinstance(probe, dict) else {}, preferred_formats)

    force_yuyv = (fmt == "YUYV")

    input_args = f"input_uvc.so -d {dev}"
    if res:
        input_args += f" -r {res}"
    if force_yuyv:
        input_args += " -y"

    log_path = f"/var/log/autodarts_mjpg_streamer_cam{cam_id}.log"
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except Exception:
        pass

    try:
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} cam{cam_id} dev={dev} args={input_args} ---\n")
            p = subprocess.Popen(
                [
                    "mjpg_streamer",
                    "-i",
                    input_args,
                    "-o",
                    f"output_http.so -p {port}",
                ],
                stdout=logf,
                stderr=logf,
            )

        time.sleep(0.3)
        if p.poll() is not None:
            tail = tail_file(log_path, n=120, max_chars=7000)
            fmts = ""
            hint = ""
            if probe.get("ok"):
                fmts = ", ".join(sorted(list(probe.get("formats") or [])))
                if fmts:
                    fmts = f"<p><strong>Erkannte Formate:</strong> {fmts}</p>"
                if probe.get("formats") and ("MJPG" not in probe["formats"]) and ("YUYV" not in probe["formats"]):
                    hint = (
                        "<p style='color:#ffb347'><strong>Hinweis:</strong> Die Kamera bietet kein MJPG/YUYV an "
                        "(z.B. nur H264). In diesem Fall kann mjpg_streamer oft keinen MJPEG-Stream erzeugen.</p>"
                    )
            else:
                err = (probe.get("error") or "").strip()
                if err:
                    fmts = f"<p><strong>v4l2 Probe-Fehler:</strong> {err}</p>"

            return (
                "<html><body style='font-family:system-ui;background:#111;color:#eee;padding:20px'>"
                "<h1>Fehler: Kamera-Stream konnte nicht gestartet werden</h1>"
                f"<p>Device: <code>{dev}</code></p>"
                f"<p>mjpg_streamer Input: <code>{input_args}</code></p>"
                f"{fmts}"
                f"{hint}"
                "<p>Log (letzte Zeilen):</p>"
                f"<pre style='white-space:pre-wrap;background:#0b0b0b;border:1px solid #333;padding:12px;border-radius:10px'>{tail}</pre>"
                f"<p><a style='color:#3b82f6' href='{url_for('index')}'>Zurück</a></p>"
                "</body></html>",
                500,
            )

    except FileNotFoundError:
        return (
            "<html><body><h1>Fehler: mjpg_streamer nicht gefunden</h1>"
            "<p>Bitte installieren Sie mjpg-streamer oder passen Sie den Aufruf im Script an.</p>"
            f"<p><a href='{url_for('index')}'>Zurück</a></p>"
            "</body></html>",
            500,
        )

    host = request.host.split(":", 1)[0]
    stream_url = f"http://{host}:{port}/?action=stream"

    return render_template(
        "cam_view.html",
        cam_id=cam_id,
        stream_url=stream_url,
    )



# ------------------------------
# Admin: Live Journal Viewer
# ------------------------------
@app.route("/admin/journal")
def admin_journal():
    # same admin gate as other admin endpoints
    if not session.get("admin_unlocked", False):
        return redirect(url_for("index", adminerr="1") + "#admin_details")

    return render_template("admin_journal.html")


@app.route("/admin/journal/stream")
def admin_journal_stream():
    if not session.get("admin_unlocked", False):
        return ("forbidden", 403)

    unit = request.args.get("unit", "darts-caller.service").strip()
    if unit not in ALLOWED_JOURNAL_UNITS:
        return ("invalid unit", 400)

    env = os.environ.copy()
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")

    def generate():
        cmd = ["journalctl", "-u", unit, "-f", "-o", "cat", "-n", "200"]
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        try:
            for raw in iter(p.stdout.readline, ""):
                line = redact_journal_line(raw.rstrip("\n"))
                yield f"data: {line}\n\n"
        except GeneratorExit:
            pass
        finally:
            try:
                p.terminate()
            except Exception:
                pass

    resp = Response(stream_with_context(generate()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
