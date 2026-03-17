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
    render_template_string,
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
WEBPANEL_HARDCODED_VERSION = "1.47"


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

        fallback = f"nohup sudo -n {shlex.quote(WEBPANEL_UPDATE_SCRIPT)} {mode_arg} >> {shlex.quote(WEBPANEL_UPDATE_LOG)} 2>&1 &"
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

    html = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>Autodarts Installation</title>
  <style>
    body { font-family: system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#111; color:#eee; margin:0; padding:0; }
    .container { max-width: 900px; margin: 0 auto; padding: 24px 16px 60px; }
    h1 { font-size: 2rem; margin-bottom: 8px; }
    .subtitle { opacity: .8; margin-bottom: 24px; }
    .card { background:#1c1c1c; border-radius:12px; padding:16px 18px 18px; margin-bottom:18px; box-shadow:0 0 20px rgba(0,0,0,0.4); }
    .card h2 { margin-top:0; font-size:1.2rem; margin-bottom:8px; }
    .btn-row { display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; align-items:center; }
    .btn { display:inline-block; padding:10px 16px; border-radius:999px; border:1px solid #444; background:#2a2a2a; color:#eee; text-decoration:none; font-size:0.95rem; cursor:pointer; }
    .btn-primary { background:#3b82f6; border-color:#3b82f6; color:white; }
    .btn-danger { background:#b91c1c; border-color:#b91c1c; color:white; }
    .btn-small { padding:6px 12px; font-size:0.85rem; }
    .btn-disabled { opacity: 0.4; pointer-events: none; filter: grayscale(1); }
    textarea { width:100%; min-height:80px; border-radius:8px; border:1px solid #444; background:#111; color:#eee; padding:8px; resize:vertical; font-family:inherit; }
    label { font-size:0.9rem; display:block; margin-bottom:4px; }
    input[type=text], input[type=password] { width:100%; padding:8px; border-radius:8px; border:1px solid #444; background:#111; color:#eee; margin-bottom:10px; font-family:inherit; }
    select { padding:6px 10px; border-radius:8px; border:1px solid #444; background:#111; color:#eee; font-family:inherit; }
    .cam-buttons { display:flex; gap:8px; margin-bottom:12px; flex-wrap:wrap; }
    .slot-card { border:1px solid #333; border-radius:10px; padding:8px 10px; margin:8px 0; background:#151515; }
    .slot-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; justify-content:space-between; }
    .info-msg { font-size:0.85rem; margin:6px 0 0; color:#ffb347; }
    .hint { font-size:0.8rem; opacity:0.7; margin-top:4px; }
    .msg-ok { color:#6be26b; margin-top:10px; white-space:pre-line; }
    .msg-bad { color:#ff6b6b; margin-top:10px; white-space:pre-line; }
    footer { margin-top:24px; font-size:0.75rem; opacity:0.6; text-align:center; }
    .sysinfo { position:fixed; top:10px; right:10px; background:#1c1c1c; border-radius:8px; padding:8px 10px; font-size:0.8rem; box-shadow:0 0 10px rgba(0,0,0,0.6); z-index:1000; border:1px solid #333; }
    .sysinfo h3 { margin:0 0 4px; font-size:0.9rem; }
    .sysinfo-row { white-space:nowrap; }
    code { background:#0b0b0b; padding:2px 6px; border-radius:6px; border:1px solid #333; }
    .dot { display:inline-block; width:10px; height:10px; border-radius:999px; margin-right:6px; vertical-align:middle; }
    .dot-green { background:#6be26b; }
    .dot-red { background:#ff6b6b; }
    .dot-gray { background:#9ca3af; }
    details { border:1px solid #333; border-radius:12px; padding:12px 14px; background:#171717; }
    details summary { cursor:pointer; list-style:none; user-select:none; font-weight:600; }
    details summary::-webkit-details-marker { display:none; }
    .admin-hint { font-size:0.8rem; opacity:0.75; margin-top:6px; }

    .ad-status { display:inline-block; padding:4px 10px; border-radius:999px; font-size:0.9rem; margin-bottom:10px; font-weight:bold; }
    .ad-on { background:#16a34a33; border:1px solid #16a34a; color:#bbf7d0; }
    .ad-off { background:#b91c1c33; border:1px solid #b91c1c; color:#fecaca; }
    .slot-card { border:1px solid #333; border-radius:10px; padding:10px 12px; margin:10px 0; }
    .slot-row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; justify-content:space-between; }
  </style>
</head>
<body>

  {% if cpu_pct is not none or mem_used is not none or temp_c is not none or autoupdate_enabled is not none %}
  <div class="sysinfo">
    <h3>Mini PC</h3>
    {% if cpu_pct is not none %}
      <div class="sysinfo-row">CPU: {{ cpu_pct }}%</div>
    {% endif %}
    {% if mem_used is not none and mem_total is not none %}
      <div class="sysinfo-row">RAM: {{ mem_used }} / {{ mem_total }} GB</div>
    {% endif %}
    {% if temp_c is not none %}
      <div class="sysinfo-row">Temp: {{ "%.1f"|format(temp_c) }} °C</div>
    {% endif %}
    <div class="sysinfo-row" style="margin-top:6px;">
      Auto-Update:
      {% if autoupdate_enabled is none %}
        <span style="color:#ff6b6b; font-weight:700;">AUS</span>
        <span class="hint" style="font-size:11px;">(Service fehlt)</span>
      {% elif autoupdate_enabled %}
        <span style="color:#6be26b; font-weight:700;">AN</span>
      {% else %}
        <span style="color:#ff6b6b; font-weight:700;">AUS</span>
      {% endif %}
    </div>
    <div style="margin-top:6px; display:flex; gap:6px;">
      <form method="post" action="{{ url_for('autoupdate_set', mode='on') }}" style="flex:1; margin:0;">
        <button type="submit" class="btn btn-small" style="width:100%;" {% if autoupdate_enabled %}disabled{% endif %}>
          Auto-Update AN
        </button>
      </form>
      <form method="post" action="{{ url_for('autoupdate_set', mode='off') }}" style="flex:1; margin:0;">
        <button type="submit" class="btn btn-small" style="width:100%;" {% if not autoupdate_enabled %}disabled{% endif %}>
          Auto-Update AUS
        </button>
      </form>
    </div>

  </div>
  {% endif %}

  <div class="container">
    {% if autodarts_active %}
      <div class="ad-status ad-on">Autodarts: AKTIV</div>
    {% else %}
      <div class="ad-status ad-off">Autodarts: DEAKTIVIERT</div>
    {% endif %}
    <div class="hint" style="margin:-6px 0 14px 0;">
      Autodarts Version: <strong>{{ autodarts_version or 'unbekannt' }}</strong>
    </div>

    


    <h1>Willkommen bei der Autodarts Installation</h1>
  {% if msg %}
    <p class="info-msg">{{ msg }}</p>
  {% endif %}
    <div class="subtitle">by Peter Rottmann v.{{ webpanel_version or '1.20' }} (Multi-WLED)</div>

    {% if not wifi_ok and not lan_ok %}
      <div style="background:#7f1d1d; border:2px solid #f87171; color:#fee2e2;
                  padding:12px 16px; border-radius:10px; margin-bottom:18px;">
        <strong style="font-size:1.1rem;">
          ACHTUNG: Sie sind NICHT mit dem Internet verbunden, nicht in Ihrem Wlan!
        </strong>
        <p style="margin:6px 0 0; font-size:0.9rem;">
          Der <strong>Dartscheiben-Manager</strong> (Schritt 2) funktioniert nur,
          wenn der Mini PC mit Ihrem <strong>Heim-WLAN</strong> verbunden ist.
        </p>
      </div>
    {% endif %}

    <div class="btn-row" style="margin-bottom: 16px;">
      <a href="{{ url_for('help_page') }}" target="_blank" class="btn btn-small">
        Hilfe / Handbuch öffnen (PDF)
      </a>
      <a href="{{ url_for('ap_config') }}" class="btn btn-small">
        Access-Point (Mini PC-WLAN) auswählen
      </a>
    </div>

    <div class="card">
      <h2>1. Mit WLAN zuhause verbinden</h2>
      <p>
        {% if lan_ok %}
          <strong>LAN Verbindung erfolgreich vorhanden.</strong>
          Der Mini PC ist über <strong>eth0</strong> mit Ihrem Heimnetzwerk verbunden
          (IP-Adresse: <strong>{{ lan_ip }}</strong>).
        {% elif ssid and ip %}
          Erfolgreich mit Ihrem WLAN <strong>{{ ssid }}</strong> verbunden
          (IP-Adresse: <strong>{{ ip }}</strong>) · Signal: <strong id="wifiSignalOut">—</strong>
          <button type="button" class="btn btn-small" id="wifiSignalBtn" onclick="fetchWifiSignal()">Signal anzeigen</button>.
        {% elif ssid and not ip %}
          Mit WLAN <strong>{{ ssid }}</strong> verbunden,
          aber es wurde keine IPv4-Adresse vergeben. Signal: <strong id="wifiSignalOut">—</strong>
          <button type="button" class="btn btn-small" id="wifiSignalBtn" onclick="fetchWifiSignal()">Signal anzeigen</button>
        {% else %}
          Aktuell ist der Mini PC weder über WLAN noch über LAN mit Ihrem Heimnetzwerk verbunden.
        {% endif %}
      </p>

      {% if not dongle_ok and not lan_ok %}
        <p class="msg-bad">
          Der WLAN-USB-Stick wird aktuell nicht erkannt.
          Bitte ziehen Sie den USB-Stick kurz ab und stecken Sie ihn wieder ein,
          warten Sie ein paar Sekunden und versuchen Sie es erneut.
          <span class="hint"><br>(Für Profis: Kein WiFi-Device <code>{{ wifi_interface }}</code> im NetworkManager sichtbar.)</span>
        </p>
      {% endif %}

      <p>
        Verbinden Sie den Mini PC zuerst mit dem WLAN in Ihrem Zuhause.
        Falls bei Ihnen kein WLAN vorhanden ist, können Sie alternativ ein LAN-Kabel verwenden.
      </p>
      <div class="btn-row">
        <a href="{{ url_for('wifi') }}" class="btn btn-primary">WLAN-Verbindung einrichten / ändern</a>
        <a href="{{ url_for('wifi_ping_ui') }}" id="pingBtn" class="btn">Verbindungstest (30 Pakete)</a>
      </div>
  <div class="hint" style="margin-top:6px;">{% if net_ok %}{{ ping_uplink_label }}{% else %}Verbindungstest: Kein Uplink (nur Access Point oder nicht verbunden).{% endif %}</div>
  <div id="pingResult" class="hint" style="margin-top:10px;"></div>

  <details style="margin-top:10px;">
    <summary>WLAN – Erweitert</summary>
    {% if admin_unlocked %}
      <p class="hint" style="margin-top:8px;">
        Achtung: Wenn Sie gerade per WLAN verbunden sind, kann „Gespeicherte WLANs löschen“ die Verbindung sofort trennen.
      </p>
      <p class="hint" style="margin-top:8px;">
        Aktives WLAN-Profil: <strong>{{ wifi_conn_name or '—' }}</strong><br>
        Autoconnect: <strong>{% if wifi_autoconnect_enabled is none %}unbekannt{% elif wifi_autoconnect_enabled %}AN{% else %}AUS{% endif %}</strong>
      </p>
      <div class="btn-row" style="margin-top:10px;">
        <form method="post" action="{{ url_for('wifi_autoconnect', mode='off') }}" style="margin:0;">
          <button type="submit" class="btn {% if wifi_autoconnect_enabled is not none and not wifi_autoconnect_enabled %}btn-primary btn-disabled{% endif %}">Autoconnect AUS</button>
        </form>
        <form method="post" action="{{ url_for('wifi_autoconnect', mode='on') }}" style="margin:0;">
          <button type="submit" class="btn {% if wifi_autoconnect_enabled %}btn-primary btn-disabled{% endif %}">Autoconnect AN</button>
        </form>
        <form method="post" action="{{ url_for('wifi_forget_saved') }}"
              onsubmit="return confirm('Wirklich ALLE gespeicherten WLAN-Verbindungen löschen (außer Autodarts-AP)?\n\nWenn du gerade über WLAN verbunden bist, kann die Verbindung sofort weg sein!');"
              style="margin:0;">
          <button type="submit" class="btn btn-danger">Gespeicherte WLANs löschen</button>
        </form>
      </div>
    {% else %}
      <p class="admin-hint" style="margin-top:8px;">
        Tipp: Im Admin-Bereich entsperren, um Autoconnect umzuschalten oder gespeicherte WLANs zu löschen.
      </p>
    {% endif %}
  </details>

</div>

    <div class="card">
      <h2>2. Dartscheibe mit Autodarts-Account verknüpfen</h2>
      <p>
        Sie benötigen ein bestehendes Autodarts-Konto. Falls noch keines vorhanden ist,
        können Sie es auf <strong>autodarts.io</strong> anlegen.
      </p>
      <div class="btn-row">
        <a href="{{ darts_url }}" target="_blank" class="btn btn-primary">Dartscheiben-Manager öffnen</a>
      </div>
      <div id="ad-version"></div>
      <details style="margin-top:12px;" {% if open_adver %}open{% endif %}>
        <summary style="cursor:pointer; font-weight:700;">Autodarts – Version &amp; Auto-Update</summary>
        <div style="margin-top:10px;">
      <div class="hint" style="margin-top:-6px;">
        Wenn ein Update Probleme macht, kannst du hier schnell auf eine stabile Version wechseln oder gezielt die aktuellste installieren.
        Auto-Update bleibt dabei standardmäßig AUS (du kannst es unten jederzeit einschalten).
      </div>

      <div style="margin-top:10px;">
        Installiert: <strong>{{ autodarts_version or 'unbekannt' }}</strong>
        &nbsp;•&nbsp; Stabil: <strong>{{ autodarts_stable_version or '—' }}</strong>
        &nbsp;•&nbsp; Aktuell (online): <strong>{{ autodarts_latest_online or 'unbekannt' }}</strong>
        &nbsp;•&nbsp; Zuletzt: <strong>{{ autodarts_last_version or '—' }}</strong>
      </div>

      {% if msg and open_adver %}
        <p class="info-msg" style="margin-top:10px;">{{ msg }}</p>
      {% endif %}

      <div class="btn-row" style="margin-top:10px; align-items:center; gap:10px; flex-wrap:wrap;">
        <form method="post" action="{{ url_for('autodarts_install_version') }}" style="margin:0;"
              onsubmit="return confirm('Auf stabile Version wechseln?\n\nHinweis: Das kann ein paar Minuten dauern und Autodarts neu starten.');">
          <input type="hidden" name="version" value="stable">
          <button type="submit" class="btn btn-primary" {% if not autodarts_stable_version %}disabled title="Keine stabile Version in AUTODARTS_VERSION_MENU hinterlegt."{% endif %}>Auf stabile Version wechseln</button>
        </form>

        <form method="post" action="{{ url_for('autodarts_install_version') }}" style="margin:0; display:flex; gap:8px; align-items:center; flex-wrap:wrap;"
              onsubmit="return confirm('Gewählte Version installieren?\n\nHinweis: Das kann ein paar Minuten dauern und Autodarts neu starten.');">
          <select name="version" style="min-width:220px;">
            {% for opt in autodarts_versions_choices %}
              <option value="{{ opt.value }}">{{ opt.label }}</option>
            {% endfor %}
          </select>
          <button type="submit" class="btn">Version installieren</button>
        </form>
      </div>

      <div class="hint" style="margin-top:8px;">
        Tipp: Für Freunde/Familie ist „Stabil“ die sichere Wahl. „Aktuellste“ nimmt immer die neueste Online-Version{% if autodarts_latest_online %} (aktuell: {{ autodarts_latest_online }}){% endif %}.
      </div>

<hr style="border:none; border-top:1px solid #333; margin:14px 0;">

      <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center; justify-content:space-between;">
        <div>
          <div style="font-weight:700;">Auto-Update</div>
          <div class="hint" style="margin-top:2px;">
            Status:
            {% if autoupdate_enabled is none %}
              <span style="color:#ff6b6b; font-weight:700;">AUS</span> (Service fehlt)
            {% elif autoupdate_enabled %}
              <span style="color:#6be26b; font-weight:700;">AN</span>
            {% else %}
              <span style="color:#ff6b6b; font-weight:700;">AUS</span>
            {% endif %}
          </div>
        </div>

        <div class="btn-row" style="margin:0;">
          <form method="post" action="{{ url_for('autoupdate_set', mode='on') }}" style="margin:0;">
            <button type="submit" class="btn btn-small" {% if autoupdate_enabled %}disabled{% endif %}
                    onclick="return confirm('Auto-Update aktivieren?\n\nHinweis: Dadurch können Updates wieder automatisch installiert werden.');">
              Auto-Update AN
            </button>
          </form>
          <form method="post" action="{{ url_for('autoupdate_set', mode='off') }}" style="margin:0;">
            <button type="submit" class="btn btn-small" {% if not autoupdate_enabled %}disabled{% endif %}>
              Auto-Update AUS
            </button>
          </form>
        </div>
      </div>
    
        </div>
      </details>
    </div>

<div class="card">
  <h2>3. Kamera einstellen</h2>

  {% if autodarts_notice %}
    <p class="info-msg">
      Autodarts wurde wieder gestartet.
      Wenn Sie fertig sind, prüfen Sie bitte kurz im Dartscheiben-Manager (Schritt&nbsp;2),
      ob die Kamera-Streams dort wieder korrekt laufen.
    </p>
  {% endif %}

  <p>
    Hier können Sie die Kameras einstellen (Bild ausrichten, scharf stellen).
    Dafür wird Autodarts kurz pausiert und ein Live-Bild angezeigt.
  </p>

  <div class="btn-row">
    {% if not camera_mode %}
      <form method="post" action="{{ url_for('camera_mode_start') }}"
            onsubmit="return confirm('Während der Kamera-Einstellung wird Autodarts kurz pausiert. Fortfahren?');">
        <button type="submit" class="btn btn-primary">Kamera einstellen starten</button>
      </form>
    {% else %}
      <form method="post" action="{{ url_for('camera_mode_end') }}">
        <button type="submit" class="btn btn-danger">Autodarts wieder starten</button>
      </form>
    {% endif %}
  </div>

  <hr style="border:none; border-top:1px solid #333; margin:14px 0;">

  <details style="margin-top:10px;" {% if camera_mode %}open{% endif %}>
    <summary>{% if camera_mode %}Kamera-Auswahl & Live-Bilder{% else %}Kamera-Auswahl anzeigen{% endif %}</summary>

    {% if camera_mode %}
      <form method="post" action="{{ url_for('set_cams') }}" style="margin-top:12px;">
        <div class="btn-row">
          <button type="submit" class="btn btn-small btn-primary">Kameras suchen</button>
        </div>
      </form>
      <p class="hint" style="margin-top:8px;">
        Es werden bis zu {{ max_cams }} physische Kameras erkannt. Die Auswahl merkt sich eine echte Kamera – nicht nur ein altes <code>/dev/videoX</code>.
      </p>

      {% if cam_count_found > 0 %}
        <p style="margin-top:12px;">Gefundene Kameras: {{ cam_count_found }}</p>

        {% for slot in cam_slots %}
          <div class="slot-card">
            <div class="slot-row">
              <div>
                <strong>kamera_{{ slot.slot }}</strong><br>
                <span class="hint">Zuordnung: {{ slot.label or '—' }}</span><br>
                <span class="hint">Aktuelles Device: {% if slot.device %}<code>{{ slot.device }}</code>{% else %}nicht verfügbar{% endif %}</span>
              </div>

              <div class="btn-row" style="margin:0;">
                <form method="post" action="{{ url_for('set_cam_slot', slot_id=slot.slot) }}" style="margin:0; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                  <select name="camera_id">
                    {% for cam in cam_inventory %}
                      <option value="{{ cam.id }}" {% if cam.id == slot.camera_id %}selected{% endif %}>{{ cam.label }}</option>
                    {% endfor %}
                  </select>
                  <button type="submit" class="btn btn-small">Zuordnen</button>
                </form>

                <a href="{{ url_for('cam_view', cam_id=slot.slot) }}" target="_blank" class="btn btn-small btn-primary">
                  kamera_{{ slot.slot }} öffnen
                </a>
              </div>
            </div>
          </div>
        {% endfor %}
      {% else %}
        <p style="margin-top:12px;">Es wurden noch keine Kameras gesucht.</p>
      {% endif %}

      {% if cam_info_message %}
        <p class="info-msg">{{ cam_info_message }}</p>
      {% endif %}
    {% else %}
      <p class="hint" style="margin-top:10px;">
        Mit <strong>Kamera einstellen starten</strong> wird Autodarts pausiert und erst dann die Kamera-Auswahl eingeblendet.
      </p>
    {% endif %}
  </details>
</div>

<!-- LED Konfiguration -->
    <div class="card">
      <h2>LED Konfiguration</h2>

      <p class="hint" style="margin-top:0;">
        Achtung: Autodarts-Account darf <strong>keine 2-Faktor-Authentifizierung (2FA)</strong> aktiviert haben.
      </p>

      {% if ledmsg %}
        <p class="{{ 'msg-ok' if ledcheck=='ok' else 'msg-bad' }}">{{ ledmsg }}</p>
      {% endif %}

      {% if caller_err %}
        <p class="msg-bad">{{ caller_err }}</p>
      {% endif %}

      <p class="hint">
        Datei: <code>/var/lib/autodarts/extensions/darts-caller/start-custom.sh</code>
      </p>

      <!-- Darts-Caller Credentials -->
      <form method="post" action="{{ url_for('led_save') }}">
        <label for="ad_email">Autodarts-Account</label>
        <input type="text" id="ad_email" name="ad_email" value="{{ caller_email }}">

        <label for="ad_password">Autodarts-Passwort</label>
        <input type="password" id="ad_password" name="ad_password" value="" placeholder="(leer lassen = unverändert)">

        <label for="ad_board">Autodarts-Boardnummer / Board-ID</label>
        <input type="text" id="ad_board" name="ad_board" value="{{ caller_board_id }}">

        <div class="btn-row">
          <button type="submit" class="btn btn-primary {% if not can_save_creds %}btn-disabled{% endif %}">
            Speichern
          </button>
        </div>
      </form>

      {% if not caller_exists %}
        <p class="info-msg">
          darts-caller start-custom.sh nicht gefunden. LED Konfiguration ist deaktiviert.
        </p>
      {% endif %}

      
      <!-- LED-Bänder (WLED) -->
<h3 style="margin:0 0 6px; font-size:1.05rem;">LED Bänder (bis zu 3)</h3>
<p class="hint" style="margin-top:0;">Hier können Sie die LEDs verwalten, bzw. ein und ausschalten.</p>

{% if not wled_installed %}
  <p class="info-msg">Die LED-Steuerung ist auf diesem Mini PC nicht installiert.</p>
{% endif %}

{% if not wled_master_enabled %}
  <p class="msg-bad">WLED ist global deaktiviert (Master-Schalter AUS). Bitte im Admin-Bereich wieder aktivieren.</p>
{% endif %}

<form id="wledForm">
<fieldset {% if not wled_master_enabled %}disabled style="opacity:0.55"{% endif %}>
  {% for b in wled_bands %}
    <div style="border:1px solid #333; border-radius:12px; padding:12px 14px; margin:10px 0;">
      <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:center; justify-content:space-between;">
        <div style="font-weight:700;">LED Band {{ b.slot }}</div>

        <div style="display:flex; align-items:center; gap:12px;">
          <label style="display:flex; align-items:center; gap:8px; margin:0;">
            <input type="checkbox" name="wled_enabled_{{ b.slot }}" value="1"
                   {% if b.enabled %}checked{% endif %}
                   {% if not wled_installed %}disabled{% endif %}>
            <span>Ein / Aus</span>
          </label>

          <span id="wled_status_{{ b.slot }}" style="font-size:0.9rem;">
            {% if not b.enabled %}
              <span class="dot dot-gray"></span>Aus
            {% else %}
              <span class="dot dot-gray"></span>Prüfe…
            {% endif %}
          </span>

          <a href="{{ url_for('wled_open_slot', slot=b.slot) }}" target="_blank"
             id="wled_cfgbtn_{{ b.slot }}" class="btn btn-small {% if not wled_installed or not b.enabled %}btn-disabled{% endif %}">
            LED konfigurieren
          </a>
        </div>
      </div>
    </div>
  {% endfor %}

  </fieldset>
</form>

<script>
  // Wenn #admin_details oder ?adminerr / ?admin gesetzt ist: Admin-Details automatisch aufklappen
  (function () {
    try {
      var p = new URLSearchParams(window.location.search);
      if (window.location.hash === "#admin_details" || p.get("admin") === "1" || p.get("adminerr") === "1") {
        var d = document.getElementById("admin_details");
        if (d) d.open = true;
      }
    } catch (e) {}
  })();
</script>

<script>
  
  // WLAN-Signalstärke nur auf Knopfdruck abfragen (spart nmcli-Rescan beim Laden)
  async function fetchWifiSignal(){
    const out = document.getElementById("wifiSignalOut");
    const btn = document.getElementById("wifiSignalBtn");
    if(btn){ btn.classList.add("btn-disabled"); btn.disabled = true; }
    try{
      const r = await fetch("{{ url_for('api_wifi_signal') }}", { cache: "no-store" });
      const j = await r.json();
      if(j && j.iface_label && titleEl) titleEl.textContent = j.iface_label;
      if(out){
        out.textContent = (j && j.signal !== null && j.signal !== undefined) ? (String(j.signal) + "%") : "n/a";
      }
    }catch(e){
      if(out) out.textContent = "n/a";
    }finally{
      if(btn){ btn.classList.remove("btn-disabled"); btn.disabled = false; }
    }
  }

(function () {
    const statusUrl = "{{ url_for('api_wled_status') }}";

    function setStatus(slot, state) {
      const el = document.getElementById("wled_status_" + slot);
      if (!el) return;

      if (state === "off") {
        el.innerHTML = '<span class="dot dot-gray"></span>Aus';
        return;
      }
      if (state === "checking") {
        el.innerHTML = '<span class="dot dot-gray"></span>Prüfe…';
        return;
      }
      if (state === "ok") {
        el.innerHTML = '<span class="dot dot-green"></span>Erreichbar';
        return;
      }
      if (state === "bad") {
        el.innerHTML = '<span class="dot dot-red"></span>Nicht erreichbar';
        return;
      }
      el.innerHTML = '<span class="dot dot-gray"></span>—';
    }

    function refresh() {
      fetch(statusUrl, { cache: "no-store" })
        .then(r => r.json())
        .then(data => {
          (data.bands || []).forEach(b => {
            if (!b.enabled) return setStatus(b.slot, "off");
            if (b.online === true) return setStatus(b.slot, "ok");
            if (b.online === false) return setStatus(b.slot, "bad");
            setStatus(b.slot, "checking");
          });
        })
        .catch(() => {});
    }

    window.addEventListener("load", refresh);

    document.querySelectorAll("input[type=checkbox][name^=wled_enabled_]").forEach(cb => {
      cb.addEventListener("change", async () => {
        const slot = (cb.name || "").split("_").pop();
        if (!slot) return;

        // UI sofort reagieren
        setStatus(slot, cb.checked ? "checking" : "off");
        const cfgBtn = document.getElementById("wled_cfgbtn_" + slot);
        if(cfgBtn){
          if(cb.checked){ cfgBtn.classList.remove("btn-disabled"); }
          else { cfgBtn.classList.add("btn-disabled"); }
        }

        // sofort speichern (ohne extra "Speichern"-Knopf)
        cb.disabled = true;
        try{
          const body = new URLSearchParams();
          body.set("enabled", cb.checked ? "1" : "0");
          const r = await fetch(`/wled/set-enabled/${slot}`, { method:"POST", body });
          const j = await r.json().catch(()=>({ok:false,msg:""}));
          if(!j.ok){
            // rollback UI falls Fehler
            cb.checked = !cb.checked;
            setStatus(slot, cb.checked ? "checking" : "off");
          }
        }catch(e){
          cb.checked = !cb.checked;
          setStatus(slot, cb.checked ? "checking" : "off");
        }finally{
          cb.disabled = false;
          setTimeout(refresh, 250);
        }
      });
    });
  })();
</script>
    </div>

    <!-- NUR FÜR ADMINISTRATOR -->
    <div id="admin"></div>
    <div class="card">
      <details id="admin_details">
        <summary>Admin-Bereich anzeigen</summary>

        {% if adminmsg %}
          <p class="{{ 'msg-ok' if adminok else 'msg-bad' }}" style="margin-top:10px;">{{ adminmsg }}</p>
        {% endif %}

        {% if not admin_unlocked %}
          {% if adminerr %}
            <p class="msg-bad" style="margin-top:10px;">Falsches Admin-Passwort.</p>
          {% endif %}
          <form method="post" action="{{ url_for('admin_unlock') }}" style="margin-top:12px;">
            <label for="admin_password">Admin-Passwort</label>
            <input type="password" id="admin_password" name="admin_password" placeholder="Passwort eingeben" autocomplete="off">
            <div class="btn-row">
              <button type="submit" class="btn btn-primary">Freischalten</button>
            </div>
            <p class="hint">Nur für Einstellungen und Diagnose.</p>
          </form>
        {% else %}

          <form method="post" action="{{ url_for('admin_lock') }}" style="margin-top:12px;">
            <button type="submit" class="btn btn-small">Admin sperren</button>
          </form>


           <h3 style="margin:0 0 8px;">System</h3>
           <p class="hint" style="margin-top:0;">
             Neustart startet den Mini PC sofort neu. „System updaten & Reboot“ führt ein <code>apt-get update/upgrade</code> aus und startet danach automatisch neu.
           </p>

           <div class="btn-row">
             <form method="post" action="{{ url_for('admin_reboot') }}"
                   onsubmit="return confirm('Wollen Sie wirklich den Mini PC komplett neu starten?');" style="margin:0;">
               <button type="submit" class="btn btn-danger">System neu starten</button>
             </form>

             <form method="post" action="{{ url_for('admin_os_update') }}"
                   onsubmit="return confirm('System-Update starten?\nDer Pi rebootet danach automatisch.');" style="margin:0;">
               <button type="submit" class="btn btn-primary">System updaten & Reboot</button>
             </form>
           </div>

           {% if os_update_state.started %}
             <p class="hint" style="margin-top:8px;">
               Letztes System-Update gestartet: <code>{{ os_update_state.started }}</code>
               {% if os_update_state.unit %}(Unit: <code>{{ os_update_state.unit }}</code>){% endif %}
             </p>
           {% endif %}

           {% if os_update_log_tail %}
             <details style="margin-top:10px;">
               <summary>System-Update-Log anzeigen</summary>
               <pre style="background:#0b0b0b; padding:12px; border-radius:10px; border:1px solid #333; overflow:auto; white-space:pre-wrap; margin:10px 0 0;">{{ os_update_log_tail }}</pre>
             </details>
           {% endif %}

           <hr style="border:none; border-top:1px solid #333; margin:14px 0;">

           <h3 style="margin:0 0 8px;">Firewall (UFW)</h3>

           {% if not ufw_installed %}
             <p class="hint" style="margin-top:0;">
               UFW ist nicht installiert.
             </p>
           {% else %}
             <p class="hint" style="margin-top:0;">
               Status: <strong>
                 {% if ufw_state.status == 'active' %}aktiv
                 {% elif ufw_state.status == 'inactive' %}inaktiv
                 {% elif ufw_state.status == 'installing' %}Installation läuft…
                 {% else %}unbekannt{% endif %}
               </strong>
               {% if ufw_state.checked %} · zuletzt geprüft: <code>{{ ufw_state.checked }}</code>{% endif %}
             </p>
           {% endif %}

           <div class="btn-row">
             <form method="post" action="{{ url_for('admin_ufw_refresh') }}" style="margin:0;">
               <button type="submit" class="btn">Status aktualisieren</button>
             </form>

             <form method="post" action="{{ url_for('admin_ufw_install') }}"
                   onsubmit="return confirm('UFW installieren, Ports freigeben und aktivieren?\n\nSSH (22/tcp) bleibt erlaubt.');" style="margin:0;">
               <button type="submit" class="btn {% if ufw_installed %}btn-disabled{% endif %}" {% if ufw_installed %}disabled{% endif %}>
                 UFW installieren
               </button>
             </form>

             <form method="post" action="{{ url_for('admin_ufw_apply_ports') }}"
                   onsubmit="return confirm('Ports in UFW freigeben? (kann mehrfach ausgeführt werden)');" style="margin:0;">
               <button type="submit" class="btn {% if not ufw_installed %}btn-disabled{% endif %}" {% if not ufw_installed %}disabled{% endif %}>
                 Ports freigeben
               </button>
             </form>

             <form method="post" action="{{ url_for('admin_ufw_enable') }}"
                   onsubmit="return confirm('UFW aktivieren?');" style="margin:0;">
               <button type="submit" class="btn btn-primary {% if not ufw_installed %}btn-disabled{% endif %}" {% if not ufw_installed %}disabled{% endif %}>
                 UFW EIN
               </button>
             </form>

             <form method="post" action="{{ url_for('admin_ufw_disable') }}"
                   onsubmit="return confirm('UFW deaktivieren?');" style="margin:0;">
               <button type="submit" class="btn {% if not ufw_installed %}btn-disabled{% endif %}" {% if not ufw_installed %}disabled{% endif %}>
                 UFW AUS
               </button>
             </form>
           </div>

           {% if ufw_state.raw %}
             <details style="margin-top:10px;">
               <summary>UFW Status anzeigen</summary>
               <pre style="background:#0b0b0b; padding:12px; border-radius:12px; border:1px solid #333; white-space:pre-wrap; margin:10px 0 0;">{{ ufw_state.raw }}</pre>
             </details>
           {% endif %}

           <hr style="border:none; border-top:1px solid #333; margin:14px 0;">


          
          <h3 style="margin:0 0 8px;">Webpanel Software</h3>
          <p class="hint" style="margin-top:0;">
            Installierte Version: <strong>{{ webpanel_version or 'unbekannt' }}</strong>
          </p>

                <div class="btn-row">
                  <form method="post" action="{{ url_for('admin_webpanel_check') }}" style="margin:0;">
                    <button type="submit" class="btn">Update prüfen</button>
                  </form>

                  <form method="post" action="{{ url_for('admin_webpanel_update') }}"
                        onsubmit="return confirm('Webpanel jetzt aktualisieren?\nHinweis: Der Webpanel-Service startet danach neu.');" style="margin:0;">
                    <button type="submit" class="btn btn-primary {% if not webpanel_update_available %}btn-disabled{% endif %}">
                      {% if webpanel_update_available %}Update installieren{% else %}Webpanel aktualisieren{% endif %}
                    </button>
                  </form>

                  <form method="post" action="{{ url_for('admin_webpanel_uvc_install') }}"
                        onsubmit="return confirm('{% if uvc_backup_info.backup_exists %}Original-Backup für Kernel {{ uvc_backup_info.kernel }} ist bereits vorhanden und wird NICHT überschrieben.{% elif uvc_backup_info.marker_exists %}ACHTUNG: Kein Original-Backup für Kernel {{ uvc_backup_info.kernel }} vorhanden, aber alter UVC-Marker gefunden. Die sichere Installation wird deshalb abgebrochen, bis ein Backup nach {{ uvc_backup_info.backup_dir }} kopiert wurde.{% else %}Vor der Installation wird ein Original-Backup für Kernel {{ uvc_backup_info.kernel }} nach {{ uvc_backup_info.backup_dir }} geschrieben.{% endif %}\n\nNICHT BEI FOLGENDEN KAMERAS INSTALLIEREN:\nGXVISION IMX179\n\nUVC Hack jetzt installieren?');" style="margin:0;">
                    <button type="submit" class="btn">UVC Hack installieren</button>
                  </form>

                  <form method="post" action="{{ url_for('admin_webpanel_uvc_uninstall') }}"
                        onsubmit="return confirm('{% if uvc_backup_info.backup_exists %}Original-Backup für Kernel {{ uvc_backup_info.kernel }} ist vorhanden. Der UVC-Hack wird ausschließlich aus diesem lokalen Backup zurückgesetzt. Der Mini PC startet danach automatisch neu.{% else %}ACHTUNG: Kein Original-Backup vorhanden. Sichere Deinstallation per Button ist so NICHT möglich. Der Vorgang bricht ab, bis ein Backup nach {{ uvc_backup_info.backup_dir }} kopiert wurde.{% endif %}\n\nUVC Hack deinstallieren?');" style="margin:0;">
                    <button type="submit" class="btn btn-danger">UVC Hack deinstallieren</button>
                  </form>
                  <button type="button" class="btn" onclick="(function(btn){const box=document.getElementById('uvc-backup-info-box'); if(!box) return; const show = box.style.display === 'none' || box.style.display === ''; box.style.display = show ? 'block' : 'none'; btn.textContent = show ? 'Info ausblenden' : 'Info';})(this);">Info</button>
                </div>

                <div id="uvc-backup-info-box" style="display:none; margin-top:8px;">
                  <p class="hint" style="margin-top:0;">
                    UVC Backup (Kernel {{ uvc_backup_info.kernel }}):
                    {% if uvc_backup_info.backup_exists %}<span style="color:#6be26b;">vorhanden</span>{% else %}<span style="color:#ff6b6b;">fehlt</span>{% endif %}
                    · Marker: {% if uvc_backup_info.marker_exists %}<span style="color:#ffb347;">vorhanden</span>{% else %}<span style="color:#6be26b;">fehlt</span>{% endif %}
                    · Pfad: <code>{{ uvc_backup_info.backup_dir }}</code>
                  </p>

                  {% if (not uvc_backup_info.backup_exists) and uvc_backup_info.marker_exists %}
                    <p class="info-msg" style="margin-top:8px;">
                      Für diesen Kernel wurde schon einmal ein UVC-Hack markiert, aber es gibt kein Original-Backup.
                      Bitte <code>uvcvideo.ko</code> und/oder <code>uvcvideo.ko.xz</code> in <code>{{ uvc_backup_info.backup_dir }}</code> ablegen.
                      Erst danach ist die sichere Deinstallation per Button möglich.
                    </p>
                  {% endif %}
                </div>

            <p class="hint" style="margin-top:8px;">
              {% if webpanel_check.latest %}
                Letzter Check: <strong>{{ webpanel_check.installed or 'unbekannt' }}</strong> → <strong>{{ webpanel_check.latest }}</strong>
                {% if webpanel_update_available %}
                  <span style="color:#ffb347;">(Update verfügbar)</span>
                {% else %}
                  <span style="color:#6be26b;">(aktuell)</span>
                {% endif %}
              {% else %}
                Tipp: Erst „Update prüfen“, dann nur bei Bedarf aktualisieren.
              {% endif %}
            </p>

          {% if webpanel_state.started %}
            <p class="hint" style="margin-top:8px;">
              Letztes Webpanel-Update gestartet: <code>{{ webpanel_state.started }}</code>
            </p>
          {% endif %}

          {% if webpanel_log_tail %}
            <details style="margin-top:10px;">
              <summary>Webpanel Update-Log anzeigen</summary>
              <pre style="background:#0b0b0b; padding:12px; border-radius:10px; border:1px solid #333; overflow:auto; white-space:pre-wrap; margin:10px 0 0;">{{ webpanel_log_tail }}</pre>
            </details>
          {% endif %}

          <hr style="border:none; border-top:1px solid #333; margin:14px 0;">

<h3 style="margin:0 0 8px;">Autodarts Software</h3>
          <p class="hint" style="margin-top:0;">
            Installierte Version: <strong>{{ autodarts_version or 'unbekannt' }}</strong>
          </p>

                <div class="btn-row">
                  <form method="post" action="{{ url_for('admin_autodarts_check') }}" style="margin:0;">
                    <button type="submit" class="btn">Update prüfen</button>
                  </form>

                  <form method="post" action="{{ url_for('admin_autodarts_update') }}"
                        onsubmit="return confirm('Autodarts jetzt aktualisieren?');" style="margin:0;">
                    <button type="submit" class="btn btn-primary {% if not update_available %}btn-disabled{% endif %}">
                      {% if update_available %}Update installieren{% else %}Autodarts aktualisieren{% endif %}
                    </button>
                  </form>
                </div>

            <p class="hint" style="margin-top:8px;">
              {% if update_check.latest %}
                Letzter Check: <strong>{{ update_check.installed or 'unbekannt' }}</strong> → <strong>{{ update_check.latest }}</strong>
                {% if update_available %}
                  <span style="color:#ffb347;">(Update verfügbar)</span>
                {% else %}
                  <span style="color:#6be26b;">(aktuell)</span>
                {% endif %}
              {% else %}
                Tipp: Erst „Update prüfen“, dann nur bei Bedarf aktualisieren.
              {% endif %}
            </p>

            <p class="hint" style="margin-top:6px;">
              Das Update nutzt das offizielle Autodarts-Update-Script (<code>updater.sh</code>) und schreibt ein Log.
            </p>

          {% if update_state.started %}
            <p class="hint" style="margin-top:8px;">
              Letztes Update gestartet: <code>{{ update_state.started }}</code>
            </p>
          {% endif %}

          {% if update_log_tail %}
            <details style="margin-top:10px;">
              <summary>Update-Log anzeigen</summary>
              <pre style="background:#0b0b0b; padding:12px; border-radius:10px; border:1px solid #333; overflow:auto; white-space:pre-wrap; margin:10px 0 0;">{{ update_log_tail }}</pre>
            </details>
          {% endif %}

          <hr style="border:none; border-top:1px solid #333; margin:14px 0;">

<h3 style="margin:0 0 8px;">Extensions (darts-caller / darts-wled)</h3>
<p class="hint" style="margin-top:0;">
  Dieser Button führt das lokal installierte Script <code>/usr/local/bin/autodarts-extensions-update.sh</code> aus und aktualisiert <strong>darts-caller</strong> und <strong>darts-wled</strong> nur dann, wenn wirklich ein Update vorhanden ist
  (ressourcenschonend: keine unnötigen pip/Reinstalls).
</p>

<div class="btn-row">
  <form method="post" action="{{ url_for('admin_wled_update') }}"
        onsubmit="return confirm('WLED UPDATE starten?\nHinweis: Wenn kein Update vorhanden ist, passiert nichts (Caller/WLED bleiben UNCHANGED).');"
        style="margin:0;">
    <button type="submit" class="btn btn-primary">WLED UPDATE</button>
  </form>
        <a href="{{ url_for('admin_journal') }}" class="btn btn-secondary" target="_blank" rel="noopener">📜 LIVE LOGS</a>
</div>

{% if extensions_state.started %}
  <p class="hint" style="margin-top:8px;">
    Letzter Start: <code>{{ extensions_state.started }}</code>
    {% if extensions_state.unit %} · Unit: <code>{{ extensions_state.unit }}</code>{% endif %}
  </p>
{% endif %}

{% if extensions_last.ts %}
  <p class="hint" style="margin-top:8px;">
    Letztes Ergebnis ({{ extensions_last.ts }}):
    Caller: <code>{{ extensions_last.caller or 'n/a' }}</code> ·
    WLED: <code>{{ extensions_last.wled or 'n/a' }}</code>
    {% if extensions_last.backup %} · Backup: <code>{{ extensions_last.backup }}</code>{% endif %}
  </p>
{% endif %}

{% if extensions_log_tail %}
  <details style="margin-top:10px;">
    <summary>Extensions Update-Log anzeigen</summary>
    <pre style="background:#0b0b0b; padding:12px; border-radius:10px; border:1px solid #333; overflow:auto; white-space:pre-wrap; margin:10px 0 0;">{{ extensions_log_tail }}</pre>
  </details>
{% endif %}

<hr style="border:none; border-top:1px solid #333; margin:14px 0;">


          <h3 style="margin:0 0 8px;">LED-Bänder – Adressen</h3>
          <p class="hint" style="margin-top:0;">
            Hier stellen Sie die Adresse/Hostname für LED Band 1–3 ein.
          </p>

          <form method="post" action="{{ url_for('wled_save_hosts') }}">
            <label for="wled_host_1">LED Band 1 Adresse/Hostname</label>
            <input type="text" id="wled_host_1" name="wled_host_1" value="{{ wled_hosts[0] }}"
                   {% if not wled_installed %}disabled{% endif %}>

            <label for="wled_host_2">LED Band 2 Adresse/Hostname</label>
            <input type="text" id="wled_host_2" name="wled_host_2" value="{{ wled_hosts[1] }}"
                   {% if not wled_installed %}disabled{% endif %}>

            <label for="wled_host_3">LED Band 3 Adresse/Hostname</label>
            <input type="text" id="wled_host_3" name="wled_host_3" value="{{ wled_hosts[2] }}"
                   {% if not wled_installed %}disabled{% endif %}>

            <div class="btn-row">
              <button type="submit" class="btn btn-primary {% if not wled_installed %}btn-disabled{% endif %}">
                Speichern
              </button>
            </div>
          </form>

          <hr style="border:none; border-top:1px solid #333; margin:14px 0;">




      <p class="hint" style="margin-top:0;">
        <strong>Wichtige Pfade (für Zukunft/Backup):</strong><br>
        <code>{{ usr_local_bin_dir }}</code> → Webdaten + GPIO/Tools (z.B. pi_monitor_test.sh)<br>
        <code>{{ autodarts_data_dir }}</code> → Manual + GPIO Bild (GPIO_Setup.jpeg)<br>
        <code>{{ extensions_dir }}</code> → Extensions (darts-caller, darts-wled)
      </p>

      <hr style="border:none; border-top:1px solid #333; margin:14px 0;">

      <h3 style="margin:0 0 8px;">GPIO Setup</h3>
      {% if admin_gpio_exists %}
        <img src="{{ url_for('admin_gpio_image') }}" alt="GPIO Setup"
             style="width:100%; border-radius:12px; border:1px solid #333; margin-top:10px;">
        <p class="hint" style="margin-top:8px;">
          Bildpfad: <code>{{ autodarts_data_dir }}/GPIO_Setup.jpeg</code>
        </p>
      {% else %}
        <p class="info-msg">GPIO_Setup.jpeg nicht gefunden unter <code>{{ autodarts_data_dir }}</code></p>
      {% endif %}

      <hr style="border:none; border-top:1px solid #333; margin:14px 0;">

      <h3 style="margin:0 0 8px;">Pi Monitor Test / Log</h3>
<div style="margin:10px 0 12px; padding:12px; border:1px solid #333; border-radius:12px; background:#101010;">
  <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:end;">
    <div>
      <label for="piMonInterval">Intervall</label>
      <select id="piMonInterval">
        <option value="5">5s</option>
        <option value="10" selected>10s</option>
        <option value="15">15s</option>
        <option value="30">30s</option>
        <option value="60">60s</option>
      </select>
    </div>
    <div>
      <label for="piMonDuration">Dauer</label>
      <select id="piMonDuration">
        <option value="5">5min</option>
        <option value="10">10min</option>
        <option value="15">15min</option>
        <option value="30" selected>30min</option>
        <option value="60">60min</option>
      </select>
    </div>

    <button id="piMonStartBtn" class="btn">Pi Monitor starten</button>
    <button id="piMonStopBtn" class="btn btn-danger" style="display:none;">Stop</button>
  </div>

  <div id="piMonStatusText" class="hint" style="margin-top:8px;"></div>
</div>

<script>
  (function(){
    const statusEl = document.getElementById('piMonStatusText');
    const startBtn = document.getElementById('piMonStartBtn');
    const stopBtn  = document.getElementById('piMonStopBtn');
    const intervalSel = document.getElementById('piMonInterval');
    const durationSel = document.getElementById('piMonDuration');

    let pollTimer = null;

    function fmtSeconds(s){
      s = Math.max(0, Number(s||0));
      const m = Math.floor(s/60);
      const r = s % 60;
      if(m <= 0) return r + "s";
      return m + "m " + r + "s";
    }

    function setRunningUI(st){
      const running = !!(st && st.running);
      if(running){
        startBtn.classList.add('btn-disabled');
        startBtn.disabled = true;
        stopBtn.style.display = 'inline-block';
        const rem = fmtSeconds(st.remaining_sec || 0);
        statusEl.textContent = (st.msg || "Läuft…") + (st.remaining_sec != null ? (" · Rest: " + rem) : "");
      }else{
        startBtn.classList.remove('btn-disabled');
        startBtn.disabled = false;
        stopBtn.style.display = 'none';
        statusEl.textContent = (st && st.msg) ? st.msg : "Nicht aktiv.";
      }
    }

    async function fetchStatus(){
      try{
        const rs = await fetch('/api/pi_monitor/status');
        const st = await rs.json();
        if(st && st.ok === false){
          setRunningUI({running:false, msg: st.msg || "Fehler"});
          return null;
        }
        setRunningUI(st);
        return st;
      }catch(e){
        setRunningUI({running:false, msg: "Status nicht erreichbar: " + e});
        return null;
      }
    }

    function startPolling(){
      if(pollTimer) return;
      pollTimer = setInterval(async ()=>{
        const st = await fetchStatus();
        if(!st || !st.running){
          clearInterval(pollTimer);
          pollTimer = null;
        }
      }, 5000);
    }

    startBtn.addEventListener('click', async ()=>{
      if(startBtn.disabled) return;
      startBtn.classList.add('btn-disabled');
      startBtn.disabled = true;
      statusEl.textContent = "Starte…";
      const interval_s = parseInt(intervalSel.value, 10);
      const duration_min = parseInt(durationSel.value, 10);
      try{
        const rs = await fetch('/api/pi_monitor/start', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({interval_s, duration_min})
        });
        const st = await rs.json();
        if(!st.ok){
          setRunningUI({running:false, msg: st.msg || "Start fehlgeschlagen."});
          return;
        }
        setRunningUI(st);
        startPolling();
      }catch(e){
        setRunningUI({running:false, msg:"Start fehlgeschlagen: " + e});
      }
    });

    stopBtn.addEventListener('click', async ()=>{
      stopBtn.classList.add('btn-disabled');
      stopBtn.disabled = true;
      try{
        const rs = await fetch('/api/pi_monitor/stop', {method:'POST'});
        const st = await rs.json();
        stopBtn.classList.remove('btn-disabled');
        stopBtn.disabled = false;
        await fetchStatus();
      }catch(e){
        stopBtn.classList.remove('btn-disabled');
        stopBtn.disabled = false;
        statusEl.textContent = "Stop fehlgeschlagen: " + e;
      }
    });

    // initialer Status aus Server-Render
    const initial = {{ pi_mon_status|tojson }};
    setRunningUI(initial);
    if(initial && initial.running) startPolling();
  })();
</script>

      <pre style="background:#0b0b0b; padding:12px; border-radius:10px; border:1px solid #333; overflow:auto; white-space:pre-wrap; margin:0;">
# 1) Script bearbeiten (falls nötig)
sudo nano /usr/local/bin/pi_monitor_test.sh

# 2) Script ausführbar machen (einmalig)
sudo chmod +x /usr/local/bin/pi_monitor_test.sh

# 3) Test starten (DEFAULT: 30min, alle 5s, CSV wird vorher gelöscht)
sudo /usr/local/bin/pi_monitor_test.sh

# 4) Beispiel: 15min, alle 15s
sudo /usr/local/bin/pi_monitor_test.sh 15 15

# 5) Beispiel: 30min, alle 10s
sudo /usr/local/bin/pi_monitor_test.sh 10 30

# 6) Ergebnis ansehen (letzte 20 Zeilen)
tail -n 20 /var/log/pi_monitor_test.csv

# 7) Erklärung + Summary ansehen
cat /var/log/pi_monitor_test_README.txt

# 8) Datei auf den Desktop/Home kopieren (zum leichter runterladen)
cp /var/log/pi_monitor_test.csv ~/
cp /var/log/pi_monitor_test_README.txt ~/
      </pre>

      <p class="hint" style="margin-top:10px;">
        Log-Dateien:<br>
        Script: <code>{{ pi_monitor_script }}</code><br>
        CSV: <code>{{ pi_monitor_csv }}</code><br>
        README: <code>{{ pi_monitor_readme }}</code>
      </p>

      {% if pi_csv_exists %}
        <h3 style="margin:14px 0 8px;">Letzte 20 Zeilen (CSV)</h3>
        <pre style="background:#0b0b0b; padding:12px; border-radius:10px; border:1px solid #333; overflow:auto; white-space:pre-wrap; margin:0;">{{ pi_csv_tail }}</pre>
      {% else %}
        <p class="hint">CSV existiert aktuell nicht (noch kein Test gelaufen).</p>
      {% endif %}

      <p class="hint" style="margin-top:12px;">
        Extensions Details:<br>
        Caller: <code>{{ extensions_dir }}/darts-caller</code><br>
        LED: <code>{{ extensions_dir }}/darts-wled</code>
      </p>

        {% endif %}
      </details>
    </div>


    <footer>
      Autodarts Installations-Panel · 10.77.0.1 · Mini PC-AP: {{ current_ap_ssid or 'Autodartsinstall1' }}
    </footer>
  </div>

<div id="pingOverlay" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75); z-index:2000; align-items:center; justify-content:center;">
  <div style="background:#1c1c1c; border:1px solid #333; border-radius:14px; padding:18px 18px; width:min(520px,92vw); box-shadow:0 0 30px rgba(0,0,0,.7);">
    <div id="pingOverlayTitle" style="font-weight:800; margin-bottom:10px;">Verbindungstest läuft…</div>
    <div id="pingOverlayText" style="margin-bottom:10px; opacity:.9;">Starte…</div>
    <div style="height:10px; background:#0b0b0b; border:1px solid #333; border-radius:999px; overflow:hidden;">
      <div id="pingOverlayBar" style="height:100%; width:0%; background:#3b82f6;"></div>
    </div>
    <div class="hint" style="margin-top:10px;">Bitte warten – das dauert ca. 30 Sekunden.</div>
  </div>
</div>

<script>
(function(){
  function initPingUi(){
    const btn = document.getElementById('pingBtn');
    const overlay = document.getElementById('pingOverlay');
    const txt = document.getElementById('pingOverlayText');
    const bar = document.getElementById('pingOverlayBar');
    const out = document.getElementById('pingResult');
    const titleEl = document.getElementById('pingOverlayTitle');

    function showOverlay(){ if(overlay){ overlay.style.display='flex'; } }
    function hideOverlay(){ if(overlay){ overlay.style.display='none'; } }
    function setProgress(done, total){
      const pct = total ? Math.max(0, Math.min(100, Math.round((done/total)*100))) : 0;
      if(bar) bar.style.width = pct + '%';
    }

    // Verbindungseinstufung (strenger Range)
    function classifyPingQuality(s, total, recv){
      const sent = Number(s.count || total || 30);
      const rec = Number(recv || 0);
      if(!sent || sent <= 0){
        return { level: 'unknown', label: 'Unbekannt', loss: null };
      }
      const lost = Math.max(0, sent - rec);
      const loss = Math.round((lost * 1000) / sent) / 10; // 1 Nachkommastelle

      const minMs = (s.min_ms != null) ? Number(s.min_ms) : null;
      const maxMs = (s.max_ms != null) ? Number(s.max_ms) : null;
      const avgMs = (s.avg_ms != null) ? Number(s.avg_ms) : null;

      // Falls keine Laufzeiten, nur grobe Einstufung
      if(minMs === null || maxMs === null || avgMs === null){
        if(loss === 0) return { level: 'gut', label: 'Gute Verbindung', loss };
        if(loss < 3) return { level: 'grenzwertig', label: 'Könnte funktionieren', loss };
        return { level: 'nicht_spielbar', label: 'Nicht mehr spielbar', loss };
      }

      // Super
      if(loss === 0 && avgMs <= 30 && maxMs <= 60){
        return { level: 'super', label: 'Super Verbindung', loss };
      }
      // Gut
      if(loss < 1 && avgMs <= 60 && maxMs <= 120){
        return { level: 'gut', label: 'Gute Verbindung', loss };
      }
      // Grenzwertig
      if(loss < 3 && avgMs <= 120 && maxMs <= 300){
        return { level: 'grenzwertig', label: 'Könnte funktionieren', loss };
      }
      // Schlecht
      return { level: 'nicht_spielbar', label: 'Nicht mehr spielbar', loss };
    }

    function esc(s){ return (s == null) ? '' : String(s); }

    async function startPing(){
      if(!btn) return;
      if(btn.classList.contains('btn-disabled')) return;

      btn.classList.add('btn-disabled');
      if(out) out.textContent = '';
      showOverlay();
      if(titleEl) titleEl.textContent = 'Verbindungstest läuft…';
      if(txt) txt.textContent = 'Starte…';
      setProgress(0, 30);

      try{
        const r = await fetch('/wifi/ping/start', {method:'POST', cache:'no-store'});
        const j = await r.json().catch(()=>({ok:false,msg:'Ungültige Antwort'}));

        if(!j.ok){
          hideOverlay();
          if(out) out.textContent = j.msg || 'Ping konnte nicht gestartet werden.';
          btn.classList.remove('btn-disabled');
          return;
        }

        const jobId = j.job_id;
        const total = 30;
        let tries = 0;

        const timer = setInterval(async ()=>{
          tries += 1;
          try{
            const rs = await fetch('/wifi/ping/status/' + jobId, {cache:'no-store'});
            const s = await rs.json().catch(()=>({ok:false,msg:'Ungültige Antwort'}));

            if(!s.ok){
              clearInterval(timer);
              hideOverlay();
              if(out) out.textContent = s.msg || 'Fehler beim Status.';
              btn.classList.remove('btn-disabled');
              return;
            }

            const prog = Number(s.progress||0);
            const recv = Number(s.received||0);
            if(txt) txt.textContent = `${prog} von ${total} Paketen… (empfangen: ${recv})`;
            setProgress(prog, total);

            if(s.done){
              clearInterval(timer);

              const sent = Number(s.count || total);
              const rec = recv;
              const q = classifyPingQuality(s, sent, rec);

              let result = `${rec} von ${sent} Paketen wurden erfolgreich gesendet.`;
              if(q.loss != null) result += ` · Paketverlust: ${q.loss}%`;
              if(s.min_ms!=null && s.max_ms!=null && s.avg_ms!=null){
                result += ` Schnellstes: ${s.min_ms} ms · Langsamstes: ${s.max_ms} ms · Durchschnitt: ${s.avg_ms} ms`;
              }
              if(s.error){
                result += ` (Hinweis: ${s.error})`;
              }

              const via = (s && s.iface_label) ? (s.iface_label + "\n") : "";
              if(out){
                if(q && q.label){
                  out.textContent = via + `Verbindungsqualität: ${q.label}\n` + result;
                }else{
                  out.textContent = via + result;
                }
              }

              if(titleEl) titleEl.textContent = 'Verbindungstest abgeschlossen';
              if(txt){
                const label = q && q.label ? q.label : 'Unbekannt';
                txt.textContent = `TEST erfolgreich durchgeführt. Ergebnis: ${label}`;
              }
              setProgress(total, total);

              setTimeout(()=>{ hideOverlay(); }, 3500);
              btn.classList.remove('btn-disabled');
            }
          }catch(e){
            if(tries > 120){
              clearInterval(timer);
              hideOverlay();
              if(out) out.textContent = 'Verbindungstest abgebrochen (Timeout).';
              btn.classList.remove('btn-disabled');
            }
          }
        }, 600);

      }catch(e){
        hideOverlay();
        if(out) out.textContent = 'Verbindungstest fehlgeschlagen.';
        btn.classList.remove('btn-disabled');
      }
    }

    // Overlay schließen (ESC oder Klick auf dunklen Bereich)
    if(overlay){
      overlay.addEventListener('click', (ev)=>{
        if(ev && ev.target === overlay) hideOverlay();
      });
    }
    document.addEventListener('keydown', (ev)=>{
      if(ev && ev.key === 'Escape') hideOverlay();
    });

    if(btn){
      // pingBtn ist ein Link (Fallback), also Navigation verhindern wenn JS aktiv ist
      btn.addEventListener('click', (ev)=>{
        try{ ev.preventDefault(); }catch(e){}
        startPing();
      });
    }
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', initPingUi);
  }else{
    initPingUi();
  }
})();
</script>


</body>
</html>
"""
    return render_template_string(
        html,
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

    html = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>WLAN Konfiguration</title>
  <style>
    body { font-family: system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#111; color:#eee; margin:0; padding:0; }
    .container { max-width: 700px; margin: 0 auto; padding: 24px 16px 60px; }
    h1 { font-size: 1.6rem; margin-bottom: 8px; }
    .card { background:#1c1c1c; border-radius:12px; padding:16px 18px 18px; margin-bottom:18px; box-shadow:0 0 20px rgba(0,0,0,0.4); }
    label { font-size:0.9rem; display:block; margin-bottom:4px; }
    input[type=text], input[type=password] { width:100%; padding:8px; border-radius:8px; border:1px solid #444; background:#111; color:#eee; margin-bottom:10px; font-family:inherit; }
    .btn-row { display:flex; gap:10px; margin-top:10px; flex-wrap:wrap; }
    .btn { display:inline-block; padding:10px 16px; border-radius:999px; border:1px solid #444; background:#2a2a2a; color:#eee; text-decoration:none; font-size:0.95rem; cursor:pointer; }
    .btn-primary { background:#3b82f6; border-color:#3b82f6; color:white; }
    .btn-danger { background:#ef4444; border-color:#ef4444; color:white; }
    .msg-ok { color:#6be26b; margin-bottom:8px; white-space:pre-line; }
    .msg-bad { color:#ff6b6b; margin-bottom:8px; white-space:pre-line; }
    .hint { font-size:0.8rem; opacity:0.7; margin-top:4px; }
    code { background:#0b0b0b; padding:2px 6px; border-radius:6px; border:1px solid #333; }
    .dot { display:inline-block; width:10px; height:10px; border-radius:999px; margin-right:6px; vertical-align:middle; }
    .dot-green { background:#6be26b; }
    .dot-red { background:#ff6b6b; }
    .dot-gray { background:#9ca3af; }
    details { border:1px solid #333; border-radius:12px; padding:12px 14px; background:#171717; }
    details summary { cursor:pointer; list-style:none; user-select:none; font-weight:600; }
    details summary::-webkit-details-marker { display:none; }
    .admin-hint { font-size:0.8rem; opacity:0.75; margin-top:6px; }

  
    .msg-info { color:#9ad1ff; margin-bottom:8px; white-space:pre-line; }

    .row { display:flex; gap:10px; align-items:center; }
    .row.small { font-size: 0.95em; margin-top: 6px; }
    #ssidPick { flex: 1; min-width: 220px; }
    .hint-small { font-size: 0.9em; opacity: 0.85; }
</style>
</head>
<body>
  <div class="container">
    <h1>WLAN Konfiguration (USB-Dongle)</h1>

    <div class="card">
      <p>{{ current_info | safe }}</p>

      {% if message %}
        <p class="{{ 'msg-ok' if success else 'msg-bad' }}">{{ message }}</p>
      {% endif %}

        <p id=\"workmsg\" class=\"msg-info\" style=\"display:none;\">Verbindung wird hergestellt … bitte warten (kann 10–30 Sekunden dauern).</p>

      <form method=\"post\" id=\"wifiForm\">
        <label for="ssidPick">WLAN auswählen</label>
        <div class="row">
          <select id="ssidPick" aria-label="WLAN auswählen"></select>
          <button type="button" class="btn" id="wifiRefresh">Refresh</button>
        </div>
        <label class="row small"><input type="checkbox" id="ssidManual"> SSID manuell eingeben (Hidden/unsichtbar)</label>

        <label for="ssid" id="ssidLabel">WLAN Name (SSID)</label>
        <input type="text" id="ssid" name="ssid" required>

        <label for="password">WLAN Passwort</label>
        <input type="password" id="password" name="password">

        <p class="hint">
          Für Profis: Interface = <code>{{ wifi_interface }}</code>,
          Verbindung = <code>{{ wifi_connection_name }}</code><br>
          Hinweis: Bei bestimmten Gerätefehlern wird die Verbindung
          automatisch ein zweites Mal versucht (inkl. kurzem WLAN-Dongle-Reset).
        

</p>

        <div class="btn-row">
          <button type="submit" class="btn btn-primary">Verbinden</button>
          <a href="{{ url_for('index') }}" class="btn">Zurück</a>
        </div>
      </form>
    </div>
  </div>

  <script>
  (function(){
    const form = document.getElementById('wifiForm');
    if (!form) return;
    form.addEventListener('submit', function(){
      const msg = document.getElementById('workmsg');
      if (msg) msg.style.display = 'block';
      const btn = form.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; btn.textContent = 'Bitte warten…'; }
    });
  })();
  </script>

<script>
(function(){
  const pick = document.getElementById('ssidPick');
  const ssidInput = document.getElementById('ssid');
  const manual = document.getElementById('ssidManual');
  const refreshBtn = document.getElementById('wifiRefresh');
  const ssidLabel = document.getElementById('ssidLabel');

  function setManualMode(on){
    if(on){
      if(pick) pick.style.display = 'none';
      if(refreshBtn) refreshBtn.style.display = 'none';
      if(ssidLabel) ssidLabel.style.display = '';
      if(ssidInput){ ssidInput.style.display = ''; ssidInput.readOnly = false; ssidInput.focus(); }
    }else{
      if(pick) pick.style.display = '';
      if(refreshBtn) refreshBtn.style.display = '';
      if(ssidLabel) ssidLabel.style.display = 'none';
      if(ssidInput){ ssidInput.style.display = 'none'; ssidInput.readOnly = true; }
    }
  }

  async function loadWifi(){
    if(!pick) return;
    pick.innerHTML = '<option>Suche WLANs…</option>';
    try{
      const r = await fetch('/api/wifi/scan', {cache:'no-store'});
      const j = await r.json();
      if(!j.ok){ throw new Error(j.msg || 'Scan fehlgeschlagen'); }

      pick.innerHTML = '';
      const nets = j.networks || [];
      if(nets.length === 0){
        pick.innerHTML = '<option value="">(Keine SSIDs gefunden – ggf. Refresh oder manuell)</option>';
        setManualMode(true);
        if(manual) manual.checked = true;
        return;
      }

      // Placeholder
      const ph = document.createElement('option');
      ph.value = '';
      ph.textContent = '(WLAN auswählen…)';
      pick.appendChild(ph);

      for(const n of nets){
        const opt = document.createElement('option');
        opt.value = n.ssid;
        const sec = n.security ? n.security : 'open';
        const star = n.in_use ? '★ ' : '';
        opt.textContent = `${star}${n.ssid} (${n.signal}%, ${sec})`;
        if(n.in_use) opt.selected = true;
        pick.appendChild(opt);
      }

      // If something selected, sync into the real input
      if(pick.value){
        ssidInput.value = pick.value;
      }else{
        // If a ★ network exists, it will already be selected; ensure sync
        const sel = pick.querySelector('option[selected]');
        if(sel && sel.value){ pick.value = sel.value; ssidInput.value = sel.value; }
      }

      setManualMode(false);
      if(manual) manual.checked = false;
    }catch(e){
      // On any error: fall back to manual entry
      setManualMode(true);
      if(manual) manual.checked = true;
      if(pick) pick.innerHTML = '<option value="">(Scan fehlgeschlagen – manuell eingeben)</option>';
      console.log(e);
    }
  }

  if(manual){
    manual.addEventListener('change', ()=> setManualMode(manual.checked));
  }
  if(pick){
    pick.addEventListener('change', ()=>{
      ssidInput.value = pick.value || '';
      // if placeholder chosen, keep empty so required triggers on submit
    });
  }
  if(refreshBtn){
    refreshBtn.addEventListener('click', loadWifi);
  }

  // Start
  loadWifi();
})();
</script>
</body>
</html>
"""
    return render_template_string(
        html,
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
    html = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Verbindungstest</title>
  <style>
    body { font-family: system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#111; color:#eee; margin:0; padding:0; }
    .container { max-width: 720px; margin: 0 auto; padding: 24px 16px 60px; }
    h1 { font-size: 1.6rem; margin: 0 0 10px; }
    .card { background:#1c1c1c; border-radius:12px; padding:16px 18px 18px; margin-bottom:18px; box-shadow:0 0 20px rgba(0,0,0,0.4); border:1px solid #333; }
    .hint { font-size:0.9rem; opacity:0.8; white-space:pre-wrap; }
    .btn-row { display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }
    .btn { display:inline-block; padding:10px 14px; border-radius:10px; border:1px solid #444; background:#222; color:#eee; text-decoration:none; cursor:pointer; }
    .btn-primary { background:#2563eb; border-color:#2563eb; }
    .btn-disabled { opacity:0.5; pointer-events:none; }
    pre { background:#0b0b0b; padding:12px; border-radius:10px; border:1px solid #333; overflow:auto; white-space:pre-wrap; margin:10px 0 0; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Verbindungstest (30 Pakete)</h1>

    <div class="card">
      <div class="hint" style="margin-top:0;">
        Dieser Test pingt den <strong>Gateway/Router</strong> (nicht autodarts.io) und zeigt Paketverlust &amp; Latenz.
        Dauer: ca. 30 Sekunden.
      </div>

      <div id="st" class="hint" style="margin-top:10px;">Starte…</div>

      <div style="height:10px; background:#0b0b0b; border:1px solid #333; border-radius:999px; overflow:hidden; margin-top:10px;">
        <div id="bar" style="height:100%; width:0%; background:#3b82f6;"></div>
      </div>

      <pre id="out">—</pre>

      <div class="btn-row">
        <a href="{{ url_for('index') }}" class="btn">Zurück</a>
        <button type="button" id="again" class="btn btn-primary">Erneut starten</button>
      </div>
    </div>
  </div>

<script>
(function(){
  const st = document.getElementById('st');
  const bar = document.getElementById('bar');
  const out = document.getElementById('out');
  const again = document.getElementById('again');

  function setProgress(done, total){
    const pct = total ? Math.max(0, Math.min(100, Math.round((done/total)*100))) : 0;
    if(bar) bar.style.width = pct + '%';
  }

  function classify(s, total, recv){
    const sent = Number(s.count || total || 30);
    const rec = Number(recv || 0);
    if(!sent || sent <= 0) return {label:'Unbekannt', loss:null};

    const lost = Math.max(0, sent - rec);
    const loss = Math.round((lost * 1000) / sent) / 10;

    const minMs = (s.min_ms != null) ? Number(s.min_ms) : null;
    const maxMs = (s.max_ms != null) ? Number(s.max_ms) : null;
    const avgMs = (s.avg_ms != null) ? Number(s.avg_ms) : null;

    if(minMs === null || maxMs === null || avgMs === null){
      if(loss === 0) return {label:'Gute Verbindung', loss};
      if(loss < 3) return {label:'Könnte funktionieren', loss};
      return {label:'Nicht mehr spielbar', loss};
    }

    if(loss === 0 && avgMs <= 30 && maxMs <= 60) return {label:'Super Verbindung', loss};
    if(loss < 1 && avgMs <= 60 && maxMs <= 120) return {label:'Gute Verbindung', loss};
    if(loss < 3 && avgMs <= 120 && maxMs <= 300) return {label:'Könnte funktionieren', loss};
    return {label:'Nicht mehr spielbar', loss};
  }

  async function run(){
    if(again){
      again.classList.add('btn-disabled');
      again.disabled = true;
    }
    if(out) out.textContent = '—';
    if(st) st.textContent = 'Starte…';
    setProgress(0, 30);

    try{
      const r = await fetch('/wifi/ping/start', {method:'POST', cache:'no-store'});
      const j = await r.json().catch(()=>({ok:false,msg:'Ungültige Antwort'}));
      if(!j.ok){
        if(st) st.textContent = j.msg || 'Ping konnte nicht gestartet werden.';
        if(out) out.textContent = (j.msg || 'Fehler');
        if(again){ again.classList.remove('btn-disabled'); again.disabled = false; }
        return;
      }

      const jobId = j.job_id;
      const total = 30;
      let tries = 0;

      const timer = setInterval(async ()=>{
        tries += 1;
        try{
          const rs = await fetch('/wifi/ping/status/' + jobId, {cache:'no-store'});
          const s = await rs.json().catch(()=>({ok:false,msg:'Ungültige Antwort'}));
          if(!s.ok){
            clearInterval(timer);
            if(st) st.textContent = s.msg || 'Fehler beim Status.';
            if(out) out.textContent = (s.msg || 'Fehler');
            if(again){ again.classList.remove('btn-disabled'); again.disabled = false; }
            return;
          }

          const prog = Number(s.progress||0);
          const recv = Number(s.received||0);
          if(st) st.textContent = `${prog} von ${total} Paketen… (empfangen: ${recv})`;
          setProgress(prog, total);

          if(s.done){
            clearInterval(timer);

            const q = classify(s, total, recv);
            let result = `${recv} von ${Number(s.count || total)} Paketen wurden erfolgreich gesendet.`;
            if(q.loss != null) result += ` · Paketverlust: ${q.loss}%`;
            if(s.min_ms!=null && s.max_ms!=null && s.avg_ms!=null){
              result += ` Schnellstes: ${s.min_ms} ms · Langsamstes: ${s.max_ms} ms · Durchschnitt: ${s.avg_ms} ms`;
            }
            if(s.error) result += ` (Hinweis: ${s.error})`;

            const via = (s && s.iface_label) ? (s.iface_label + "\\n") : "";
            if(out) out.textContent = via + `Verbindungsqualität: ${q.label}\\n` + result;
            if(st) st.textContent = 'Fertig.';
            setProgress(total, total);

            if(again){ again.classList.remove('btn-disabled'); again.disabled = false; }
          }
        }catch(e){
          if(tries > 120){
            clearInterval(timer);
            if(st) st.textContent = 'Timeout.';
            if(out) out.textContent = 'Verbindungstest abgebrochen (Timeout).';
            if(again){ again.classList.remove('btn-disabled'); again.disabled = false; }
          }
        }
      }, 600);

    }catch(e){
      if(st) st.textContent = 'Fehlgeschlagen.';
      if(out) out.textContent = 'Verbindungstest fehlgeschlagen.';
      if(again){ again.classList.remove('btn-disabled'); again.disabled = false; }
    }
  }

  if(again){
    again.addEventListener('click', run);
  }
  run();
})();
</script>
</body>
</html>
"""
    return render_template_string(html)


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

    html = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>Access-Point auswählen</title>
  <style>
    body { font-family: system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#111; color:#eee; margin:0; padding:0; }
    .container { max-width: 700px; margin: 0 auto; padding: 24px 16px 60px; }
    h1 { font-size: 1.6rem; margin-bottom: 8px; }
    .card { background:#1c1c1c; border-radius:12px; padding:16px 18px 18px; margin-bottom:18px; box-shadow:0 0 20px rgba(0,0,0,0.4); }
    label { font-size:0.9rem; display:block; margin-bottom:4px; }
    input[type=text], select { width:100%; padding:8px; border-radius:8px; border:1px solid #444; background:#111; color:#eee; margin-bottom:10px; font-family:inherit; }
    .btn-row { display:flex; gap:10px; margin-top:10px; flex-wrap:wrap; }
    .btn { display:inline-block; padding:10px 16px; border-radius:999px; border:1px solid #444; background:#2a2a2a; color:#eee; text-decoration:none; font-size:0.95rem; cursor:pointer; }
    .btn-primary { background:#3b82f6; border-color:#3b82f6; color:white; }
    .msg-ok { color:#6be26b; margin-bottom:8px; }
    .msg-bad { color:#ff6b6b; margin-bottom:8px; }
    .hint { font-size:0.8rem; opacity:0.7; margin-top:4px; }
    code { background:#0b0b0b; padding:2px 6px; border-radius:6px; border:1px solid #333; }
    .dot { display:inline-block; width:10px; height:10px; border-radius:999px; margin-right:6px; vertical-align:middle; }
    .dot-green { background:#6be26b; }
    .dot-red { background:#ff6b6b; }
    .dot-gray { background:#9ca3af; }
    details { border:1px solid #333; border-radius:12px; padding:12px 14px; background:#171717; }
    details summary { cursor:pointer; list-style:none; user-select:none; font-weight:600; }
    details summary::-webkit-details-marker { display:none; }
    .admin-hint { font-size:0.8rem; opacity:0.75; margin-top:6px; }

  </style>
</head>
<body>
  <div class="container">
    <h1>Access-Point (Mini PC-WLAN) auswählen</h1>

    <div class="card">
      {% if current_ssid %}
        <p>Aktueller Access-Point-Name (SSID): <strong>{{ current_ssid }}</strong></p>
      {% else %}
        <p>Der Access-Point <code>{{ ap_connection_name }}</code> wurde nicht gefunden.
           Bitte prüfen, ob das Access-Point-Setup korrekt installiert ist.</p>
      {% endif %}

      {% if message %}
        <p class="{{ 'msg-ok' if success else 'msg-bad' }}">{{ message }}</p>
      {% endif %}

      <form method="post">
        <label for="ap_ssid_select">Neuer Name für das Mini PC-WLAN</label>
        <select id="ap_ssid_select" name="ap_ssid_select" required>
          {% for opt in ap_choices %}
            <option value="{{ opt }}" {% if opt == selected_ssid %}selected{% endif %}>{{ opt }}</option>
          {% endfor %}
        </select>

        <p class="hint">
          Hinweis: Nach dem Speichern müssen Sie sich mit Ihren Geräten mit dem neuen WLAN-Namen verbinden
          (Access-Point des Mini PC).
        </p>

        <div class="btn-row">
          <button type="submit" class="btn btn-primary">Speichern</button>
          <a href="{{ url_for('index') }}" class="btn">Zurück</a>
        </div>
      </form>
    </div>
  </div>
</body>
</html>
"""
    return render_template_string(
        html,
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

    html = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>Kamera {{ cam_id }} – Live-Stream</title>
  <style>
    body { margin:0; padding:10px; background:#000; color:#eee; font-family: system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    h1 { font-size:1.4rem; margin-bottom:8px; }
    .info { font-size:0.9rem; opacity:0.8; margin-bottom:8px; }
    img { display:block; width:100%; max-height:90vh; object-fit:contain; background:#000; }
  </style>
</head>
<body>
  <h1>Kamera {{ cam_id }} – Live-Stream</h1>
  <div class="info">
    Autodarts wurde für die Kamera-Feinjustierung vorübergehend angehalten.<br>
    Stream-URL: {{ stream_url }}
  </div>
  <img src="{{ stream_url }}" alt="Kamera-Stream {{ cam_id }}">
</body>
</html>
"""
    return render_template_string(html, cam_id=cam_id, stream_url=stream_url)



# ------------------------------
# Admin: Live Journal Viewer
# ------------------------------
@app.route("/admin/journal")
def admin_journal():
    # same admin gate as other admin endpoints
    if not session.get("admin_unlocked", False):
        return redirect(url_for("index", adminerr="1") + "#admin_details")

    page = '''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Live Logs</title>
  <style>
    body{font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:#0b0f14; color:#e6edf3; margin:0; padding:16px;}
    .wrap{max-width:1100px; margin:0 auto;}
    .top{display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:12px;}
    .btn{display:inline-block; padding:10px 12px; border-radius:10px; background:#1f2937; color:#e6edf3; text-decoration:none; border:1px solid #334155;}
    .btn.active{background:#2563eb; border-color:#2563eb;}
    .btn.danger{background:#7f1d1d; border-color:#7f1d1d;}
    .hint{opacity:.8; font-size:14px; margin:6px 0 14px;}
    #log{white-space:pre-wrap; font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
         background:#0f172a; border:1px solid #243244; border-radius:14px; padding:12px; min-height:60vh; overflow:auto;}
    .row{display:flex; gap:10px; align-items:center; flex-wrap:wrap;}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="row" id="tabs">
        <a class="btn active" href="#" data-unit="darts-caller.service">darts-caller</a>
        <a class="btn" href="#" data-unit="darts-wled.service">darts-wled</a>
      </div>
      <div class="row" style="margin-left:auto;">
        <a class="btn" href="#" id="btnPause">⏸ Pause</a>
        <a class="btn" href="#" id="btnClear">🧹 Clear</a>
        <a class="btn danger" href="#" id="btnClose">✖ Close</a>
      </div>
    </div>
    <div class="hint">
      Live-Stream aus <code>journalctl -f -o cat</code>. (Admin-only) – Passwörter werden grob maskiert.
    </div>
    <div id="log"></div>
  </div>

  <script>
    let unit = "darts-caller.service";
    let es = null;
    let paused = false;
    const logEl = document.getElementById("log");

    function appendLine(s){
      if(paused) return;
      if(!s) return;
      logEl.textContent += s + "\n";
      logEl.scrollTop = logEl.scrollHeight;
    }

    function connect(){
      if(es){ es.close(); es = null; }
      appendLine("---- connecting: " + unit + " ----");
      es = new EventSource("/admin/journal/stream?unit=" + encodeURIComponent(unit));
      es.onmessage = (ev) => appendLine(ev.data);
      es.onerror = () => {
        appendLine("---- disconnected (retrying) ----");
        try{ es.close(); }catch(e){}
        es = null;
        if(!paused){
          setTimeout(connect, 1200);
        }
      };
    }

    document.getElementById("tabs").addEventListener("click", (e) => {
      const a = e.target.closest("a[data-unit]");
      if(!a) return;
      e.preventDefault();
      unit = a.getAttribute("data-unit");
      [...document.querySelectorAll("#tabs a")].forEach(x => x.classList.remove("active"));
      a.classList.add("active");
      connect();
    });

    document.getElementById("btnPause").addEventListener("click", (e) => {
      e.preventDefault();
      paused = !paused;
      e.target.textContent = paused ? "▶ Continue" : "⏸ Pause";
      if(!paused && !es) connect();
    });

    document.getElementById("btnClear").addEventListener("click", (e) => {
      e.preventDefault();
      logEl.textContent = "";
    });

    document.getElementById("btnClose").addEventListener("click", (e) => {
      e.preventDefault();
      try{ if(es) es.close(); }catch(err){}
      window.close();
      setTimeout(()=>{ window.location.href = "/"; }, 100);
    });

    connect();
  </script>
</body>
</html>
'''

    return render_template_string(page)


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