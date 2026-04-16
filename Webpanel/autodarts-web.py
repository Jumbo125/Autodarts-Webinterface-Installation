#!/usr/bin/env python3

# Copyright (c) 2026 Peter Rottmann
# All rights reserved.
# Proprietary and not open source.
# No use, modification, distribution, publication, sublicensing,
# or commercial use without prior express written permission.
# Applies only to parts created by Peter Rottmann.
# Third-party components remain under their respective licenses.
# Provided "as is", without warranty.

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
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
import time  # für weichen Dongle-Reset
import uuid
from datetime import datetime
import threading
import pwd

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
    stream_with_context,
    has_request_context,)


app = Flask(__name__)
app.secret_key = os.environ.get('AUTODARTS_WEB_SECRET', 'autodarts-web-admin')

# === KONFIGURATION ===

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

AUTODARTS_DATA_DIR = os.environ.get("AUTODARTS_DATA_DIR", "/home/peter/autodarts-data")
DATA_DIR = Path(AUTODARTS_DATA_DIR).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
HELP_PDF_FILENAME = "Autodarts_install_manual.pdf"
SIEMENS_STAR_DIR = Path("/usr/local/bin/static")
SIEMENS_STAR_FILENAME = "siemens-star-a4.pdf"

LANG_JSON_DIR = Path(BASE_DIR) / "static" / "lang"
LANG_CONFIG_PATH = LANG_JSON_DIR / "config_lang.json"
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
AP_INTERNET_STATUS_PATH = "/var/lib/autodarts/ap-internet-status.json"

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
AUTODARTS_THEME_DIR = Path(os.environ.get("AUTODARTS_THEME_DIR", "/usr/local/bin/theme"))
AUTODARTS_THEME_STATE_PATH = Path(os.environ.get("AUTODARTS_THEME_STATE_PATH", "/var/lib/autodarts/autodarts-theme-state.json"))
AUTODARTS_THEME_PANEL_URL = os.environ.get("AUTODARTS_THEME_PANEL_URL", "http://10.77.0.1")
#AUTODARTS_THEME_STORE_URL = os.environ.get("AUTODARTS_THEME_STORE_URL", "").strip()
AUTODARTS_THEME_STORE_URL = "https://chromewebstore.google.com/detail/autodarts-themes/chjcoefebpojnaihcjegalpfmlblgkcf?hl=de&utm_source=ext_sidebar"

AUTODARTS_CALLER_ZIP_URL = os.environ.get("AUTODARTS_CALLER_ZIP_URL", "https://github.com/Jumbo125/Autodarts-Webinterface-Installation/releases/download/V1/Caller_Webpanel.zip").strip()
AUTODARTS_CALLER_DIR_NAME = os.environ.get("AUTODARTS_CALLER_DIR_NAME", "Caller").strip() or "Caller"
AUTODARTS_CALLER_WINDOWS_INSTALLER_FILENAME = "Windows_Caller_install.bat"
AUTODARTS_CALLER_INSTALL_LOCK = threading.Lock()

AUTODARTS_DISPLAY_SECTION_PI5_ONLY = str(os.environ.get("AUTODARTS_DISPLAY_SECTION_PI5_ONLY", "1")).strip().lower() not in {"0", "false", "no", "off"}
AUTODARTS_DISPLAY_XRANDR = os.environ.get("AUTODARTS_DISPLAY_XRANDR", shutil.which("xrandr") or "xrandr")
AUTODARTS_DISPLAY_USER = os.environ.get("AUTODARTS_DISPLAY_USER", "").strip()
AUTODARTS_DISPLAY_PREFERRED_USERS = [x for x in [AUTODARTS_DISPLAY_USER, os.environ.get("SUDO_USER", "").strip(), "peter", "pi"] if x]


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
            label = t("autodarts.latest_online", "Aktuellste (online: {latest})", latest=latest) if latest else t("autodarts.latest_online_unknown", "Aktuellste (online: unbekannt)")
            choices.append({"value": "__LATEST__", "label": label})
        elif tok == "__LAST__":
            label = t("autodarts.last_rollback", "Zuletzt (Rollback: {last})", last=last) if last else t("autodarts.last_rollback_unavailable", "Zuletzt (Rollback: noch nicht verfügbar)")
            choices.append({"value": "__LAST__", "label": label})
        else:
            if not _SEMVER_RE.match(tok):
                continue
            if stable and tok == stable:
                choices.append({"value": tok, "label": t("autodarts.stable_label", "Stabil ({version})", version=tok)})
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
WEBPANEL_HARDCODED_VERSION = "1.57"


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

    user_msg = t("wifi.connection_failed", "Verbindung konnte nicht hergestellt werden.")

    if "no device" in lower or "unknown device" in lower:
        user_msg = t("wifi.dongle_not_detected", "WLAN-USB-Stick wird nicht richtig erkannt.")
    elif "no wifi device" in lower:
        user_msg = t("wifi.no_valid_device", "Kein gültiges WLAN-Gerät gefunden (WLAN-Stick fehlt oder wird nicht richtig erkannt).")
    elif "no network with ssid" in lower or "wifi network could not be found" in lower or "ssid not found" in lower:
        user_msg = t("wifi.ssid_not_found", "Der eingegebene WLAN-Name (SSID) wurde nicht gefunden. Bitte Schreibweise und Abstand zum Router prüfen.")
    elif "wrong password" in lower or "secrets were required, but not provided" in lower or "invalid passphrase" in lower:
        user_msg = t("wifi.password_invalid", "Das WLAN-Passwort scheint nicht zu stimmen. Bitte erneut eingeben.")
    elif "no suitable device found" in lower or "profile is not compatible with device" in lower:
        user_msg = t("wifi.profile_not_compatible", "Das WLAN-Profil passt nicht zum Gerät (z.B. falsches Interface wie eth0 statt WLAN-Stick).")
    elif "activation failed" in lower:
        user_msg = t("wifi.router_rejected", "Der Router hat die Verbindung abgelehnt oder es gibt ein Problem mit den WLAN-Einstellungen.")

    debug_msg = t("wifi.debug_details", " (Details für Profis: {details})", details=short) if short else ""
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
            return False, t("autodarts.update_already_running", "Update läuft bereits.")
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
        return False, t("jobs.start_failed", "Job konnte nicht gestartet werden: {error}", error=e)

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


def fetch_latest_webpanel_version(timeout_s: float = 5.0) -> str | None:
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
        return False, t("webpanel.update_script_missing", "Update-Script nicht gefunden: {path}", path=WEBPANEL_UPDATE_SCRIPT)

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
                return False, t("webpanel.job_already_running", "Webpanel-Job läuft bereits.")
            if ts and (time.time() - ts) > 1800:
                try:
                    os.remove(lock_path)
                except Exception:
                    return False, t("webpanel.job_already_running_lock", "Webpanel-Job läuft bereits (Lock konnte nicht entfernt werden).")
            else:
                return False, t("webpanel.job_already_running", "Webpanel-Job läuft bereits.")
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


def fetch_latest_webpanel_version(timeout_s: float = 5.0) -> str | None:
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


UFW_BIN_CANDIDATES = (
    "/usr/sbin/ufw",
    "/sbin/ufw",
    "/usr/bin/ufw",
    "/bin/ufw",
)


def _get_ufw_cmd() -> str | None:
    found = shutil.which("ufw")
    if found:
        return found
    for candidate in UFW_BIN_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def ufw_is_installed() -> bool:
    # robust gegen fehlendes /usr/sbin im PATH des Webpanel-Services
    return bool(_get_ufw_cmd())


def _run_root(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _systemd_unit_state(unit_name: str, timeout: float = 5.0) -> str:
    unit_name = str(unit_name or "").strip()
    if not unit_name:
        return "missing"
    try:
        r = _run_root(["systemctl", "show", unit_name, "--property=ActiveState,LoadState", "--value"], timeout=timeout)
        if r.returncode != 0:
            return "missing"
        values = [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]
        active = values[0] if len(values) > 0 else ""
        load = values[1] if len(values) > 1 else ""
        if load == "not-found":
            return "missing"
        if active in {"active", "activating", "reloading"}:
            return "running"
        if active in {"failed", "inactive", "deactivating"}:
            return "finished"
    except Exception:
        pass
    return "missing"


def ufw_refresh_state() -> dict:
    """
    Liest UFW Status (active/inactive) aus. Wird auf Button-Klick und bei hängendem Install-Status genutzt.
    """
    st = load_ufw_state() or {}
    st["checked_ts"] = time.time()
    st["checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ufw_cmd = _get_ufw_cmd()
    st["installed"] = bool(ufw_cmd)

    if not st["installed"]:
        unit_state = _systemd_unit_state(st.get("unit") or "") if st.get("status") == "installing" else "missing"
        st["enabled"] = False
        if st.get("status") == "installing" and unit_state == "running":
            st["status"] = "installing"
        elif st.get("status") == "installing":
            st["status"] = "install_failed"
            tail = tail_file(UFW_LOG, n=50, max_chars=4000)
            if tail:
                st["raw"] = tail
            if not st.get("error"):
                st["error"] = t("ufw.install_not_completed", "UFW Installations-Job beendet, aber UFW wurde nicht gefunden. Bitte Log prüfen und erneut versuchen.")
        else:
            st["status"] = "not_installed"
            st["raw"] = ""
        save_ufw_state(st)
        return st

    try:
        r = _run_root([ufw_cmd, "status"], timeout=6.0)
        raw = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        st["raw"] = raw.strip()
        st.pop("error", None)
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
    ufw_cmd = _get_ufw_cmd()
    if not ufw_cmd:
        return False, t("ufw.not_installed", "UFW ist nicht installiert.")

    logs = []
    ok = True
    for rule in UFW_PORT_RULES:
        try:
            r = _run_root([ufw_cmd, "allow", rule], timeout=10.0)
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

    msg = t("ufw.ports_applied", "Ports angewendet.") if ok else t("ufw.ports_applied_with_errors", "Ports angewendet, aber es gab Fehler.")
    return ok, msg + ("\n" + "\n".join(logs[-10:]) if logs else "")


def ufw_set_enabled(enable: bool) -> tuple[bool, str]:
    ufw_cmd = _get_ufw_cmd()
    if not ufw_cmd:
        return False, t("ufw.not_installed", "UFW ist nicht installiert.")

    if enable:
        ok_ports, msg_ports = ufw_apply_port_rules()
        try:
            r = _run_root([ufw_cmd, "--force", "enable"], timeout=10.0)
            ok = (r.returncode == 0) and ok_ports
            msg = t("ufw.enabled", "UFW aktiviert.") if ok else t("ufw.enable_failed", "UFW Aktivierung fehlgeschlagen.")
            extra = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            extra = extra.strip()
            if msg_ports:
                extra = (msg_ports + "\n" + extra).strip()
            return ok, (msg + ("\n" + extra if extra else ""))
        except Exception as e:
            return False, t("ufw.enable_failed_error", "UFW Aktivierung fehlgeschlagen: {error}", error=e)
    else:
        try:
            r = _run_root([ufw_cmd, "disable"], timeout=10.0)
            ok = (r.returncode == 0)
            extra = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            extra = extra.strip()
            return ok, (t("ufw.disabled", "UFW deaktiviert.") + ("\n" + extra if extra else ""))
        except Exception as e:
            return False, t("ufw.disable_failed_error", "UFW deaktivieren fehlgeschlagen: {error}", error=e)


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
        "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH; "
        "set -e; "
        "echo '[ufw] apt-get update'; apt-get update -y; "
        "echo '[ufw] install'; apt-get install -y ufw; "
        f"echo '[ufw] rules'; {rules} "
        "echo '[ufw] enable'; ufw --force enable; "
        "echo '[ufw] done'; ufw status"
    )
    # Logfile
    cmdline = f"{{ {cmdline}; }} >> '{UFW_LOG}' 2>&1"

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
            return True, t("ufw.install_started", "UFW Installation gestartet (läuft im Hintergrund).")
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
            return True, t("system.update_started", "System-Update gestartet – der Pi rebootet danach automatisch.")

        state["unit"] = None
        state["method"] = "systemd-run-failed"
        state["error"] = (res.stderr or res.stdout or "").strip()[:300]
        save_os_update_state(state)
        return False, t("system.update_start_failed", "System-Update konnte nicht gestartet werden: {error}", error=state["error"])

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
            return True, t("autoupdate.already_disabled_missing_service", "Auto-Update ist bereits deaktiviert (Service fehlt).")
        return False, t("autoupdate.service_missing", "{service} nicht gefunden.", service=AUTOUPDATE_SERVICE)
    try:
        cmd = ["systemctl", ("enable" if enabled else "disable"), "--now", AUTOUPDATE_SERVICE]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True, (t("autoupdate.enabled", "Auto-Update aktiviert.") if enabled else t("autoupdate.disabled", "Auto-Update deaktiviert."))
        err = (r.stderr or r.stdout or "").strip()
        short = (err.splitlines()[0] if err else "systemctl fehlgeschlagen.")
        return False, short
    except Exception as e:
        return False, t("autoupdate.change_failed", "Auto-Update konnte nicht geändert werden: {error}", error=e)



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
        state["error"] = t("extensions.update_script_missing", "{path} fehlt. Bitte Webpanel-Update ausführen oder das Script manuell installieren.", path=EXTENSIONS_UPDATE_SCRIPT)
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
            return True, t("extensions.update_started", "WLED UPDATE gestartet (Caller/WLED werden nur aktualisiert, wenn nötig).")

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
            return True, t("autoupdate.already_disabled_missing_service", "Auto-Update ist bereits deaktiviert (Service fehlt).")
        return False, t("autoupdate.service_missing", "{service} nicht gefunden.", service=AUTOUPDATE_SERVICE)
    try:
        cmd = ["systemctl", ("enable" if enabled else "disable"), "--now", AUTOUPDATE_SERVICE]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True, (t("autoupdate.enabled", "Auto-Update aktiviert.") if enabled else t("autoupdate.disabled", "Auto-Update deaktiviert."))
        err = (r.stderr or r.stdout or "").strip()
        short = (err.splitlines()[0] if err else "systemctl fehlgeschlagen.")
        return False, short
    except Exception as e:
        return False, t("autoupdate.change_failed", "Auto-Update konnte nicht geändert werden: {error}", error=e)

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
                msg = t("autoupdate.default_disabled", "Auto-Update wurde standardmäßig deaktiviert (kann bei Bedarf wieder eingeschaltet werden).")

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
        return t("ping.label_eth0", "Verbindungstest über Kabel (eth0)")
    if iface == WIFI_INTERFACE:
        return t("ping.label_wifi", "Verbindungstest über WLAN ({iface})", iface=iface)
    return t("ping.label_generic", "Verbindungstest über {iface}", iface=iface)

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
        return False, t("ping.no_gateway_found", "Kein Gateway gefunden (nur Access Point oder nicht verbunden?)."), None

    gw = get_default_gateway_for_interface(iface)
    if not gw:
        return False, t("ping.no_gateway_on_iface", "Kein Gateway auf {iface} gefunden (nicht verbunden?).", iface=iface), None

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
        return False, t("wled.start_custom_missing", "start-custom.sh nicht gefunden: {path}", path=DARTS_WLED_START_CUSTOM)

    try:
        with open(DARTS_WLED_START_CUSTOM, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return False, t("wled.start_custom_read_failed", "start-custom.sh konnte nicht gelesen werden: {error}", error=e)

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
        return False, t("wled.weps_line_missing_unexpected", "Keine -WEPS Zeile in start-custom.sh gefunden (unerwartetes Format).")

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
        return False, t("wled.start_custom_write_failed", "start-custom.sh konnte nicht geschrieben werden: {error}", error=e)

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


def _wled_presets_int(value, default: int, min_val: int = 0, max_val: int = 999) -> int:
    try:
        num = int(str(value).strip())
    except Exception:
        num = default
    return max(min_val, min(max_val, num))


def _wled_presets_sort_key(row: dict) -> tuple[int, str]:
    preset = _wled_presets_int((row or {}).get("preset"), 0, 0, 999)
    arg = str((row or {}).get("arg") or "")
    return (preset if preset > 0 else 10**9, arg)


def _wled_presets_prepare_rows(rows_payload) -> list[dict]:
    rows_in = rows_payload if isinstance(rows_payload, list) else []
    normalized = [_wled_presets_normalize_row(r if isinstance(r, dict) else {}) for r in rows_in]

    used: set[int] = set()
    missing: list[dict] = []
    for row in normalized:
        preset = _wled_presets_int(row.get("preset"), 0, 0, 999)
        if preset >= 1 and preset not in used:
            row["preset"] = preset
            used.add(preset)
        else:
            row["preset"] = 0
            missing.append(row)

    next_preset = 1
    for row in missing:
        while next_preset in used:
            next_preset += 1
        row["preset"] = next_preset
        used.add(next_preset)
        next_preset += 1

    normalized.sort(key=_wled_presets_sort_key)
    return normalized


def _wled_presets_row_from_line(line: str) -> dict | None:
    cleaned = _wled_presets_strip_line(line)
    if not cleaned or cleaned.startswith("#"):
        return None

    m = re.match(r'^(-A\d+)\s+(\d+)-(\d+)\s+"ps\|(\d+)(?:\|([^\"]+))?"$', cleaned)
    if m:
        return {
            "preset": int(m.group(4)),
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
        preset = int(m.group(3))
        if score_val == 0:
            meta = WLED_PRESET_FIXED_TYPES["-S0"]
            return {
                "preset": preset,
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
            "preset": preset,
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
        preset = int(m.group(2))
        sec = (m.group(3) or "").strip()
        meta = WLED_PRESET_FIXED_TYPES.get(arg)
        if meta:
            return {
                "preset": preset,
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
            "preset": preset,
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
        "preset": 0,
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
        return False, t("wled.start_custom_missing", "start-custom.sh nicht gefunden: {path}", path=DARTS_WLED_START_CUSTOM), [], _wled_presets_default_weps_text()

    try:
        lines = _wled_presets_read_file()
    except Exception as e:
        return False, t("wled.start_custom_read_failed", "start-custom.sh konnte nicht gelesen werden: {error}", error=e), [], _wled_presets_default_weps_text()

    weps_idx = _wled_presets_find_weps_index(lines)
    if weps_idx < 0:
        return False, t("wled.weps_line_missing", "Keine -WEPS Zeile gefunden."), [], _wled_presets_default_weps_text()

    weps_text = _wled_presets_extract_weps_text(lines, weps_idx)
    block_end = _wled_presets_block_end(lines, weps_idx + 1)
    rows: list[dict] = []
    for line in lines[weps_idx + 1:block_end]:
        row = _wled_presets_row_from_line(line)
        if row:
            rows.append(row)

    rows = _wled_presets_prepare_rows(rows)
    return True, "Aktuelle Einstellungen geladen.", rows, weps_text


def _wled_presets_normalize_row(row: dict) -> dict:
    preset = _wled_presets_int((row or {}).get("preset"), 0, 0, 999)
    kind = str((row or {}).get("kind") or "fixed").strip()
    type_id = str((row or {}).get("typeId") or "").strip()
    arg = str((row or {}).get("arg") or "").strip()
    label = str((row or {}).get("label") or "").strip()
    seconds = str((row or {}).get("seconds") or "").strip()

    if kind == "score_exact":
        score = _wled_presets_int((row or {}).get("score"), 180, 0, 180)
        return {
            "preset": preset,
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
            "preset": preset,
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
            "preset": preset,
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
        "preset": preset,
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
        return False, t("wled.start_custom_missing", "start-custom.sh nicht gefunden: {path}", path=DARTS_WLED_START_CUSTOM), [], _wled_presets_default_weps_text()

    try:
        lines = _wled_presets_read_file()
    except Exception as e:
        return False, t("wled.start_custom_read_failed", "start-custom.sh konnte nicht gelesen werden: {error}", error=e), [], _wled_presets_default_weps_text()

    weps_idx = _wled_presets_find_weps_index(lines)
    if weps_idx < 0:
        return False, t("wled.weps_line_missing", "Keine -WEPS Zeile gefunden."), [], _wled_presets_default_weps_text()

    weps_text = _wled_presets_extract_weps_text(lines, weps_idx)
    rows = _wled_presets_prepare_rows(rows_payload)

    area_counter = 0
    rendered_lines: list[str] = []
    for row in rows:
        area_idx = None
        if row.get("kind") == "score_range":
            area_counter += 1
            area_idx = area_counter
        preset_number = _wled_presets_int(row.get("preset"), 0, 0, 999)
        if preset_number < 1:
            continue
        rendered_lines.append(_wled_presets_line_for_row(row, preset_number, area_idx))

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
        return False, t("wled.start_custom_write_failed", "start-custom.sh konnte nicht geschrieben werden: {error}", error=e), rows, weps_text

    restarted = False
    try:
        if service_exists(DARTS_WLED_SERVICE) and service_is_active(DARTS_WLED_SERVICE):
            service_restart(DARTS_WLED_SERVICE)
            restarted = True
    except Exception:
        restarted = False

    msg = t("wled.presets_saved", "Preset-Einstellungen gespeichert.")
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
        return {"running": False, "msg": t("pi_monitor.not_active", "Nicht aktiv.")}

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
        "msg": t("pi_monitor.running_status", "Läuft (PID {pid}). Start: {started} · Ende: {ends}", pid=st.get("pid"), started=started, ends=ends),
        **st,
    }


def start_pi_monitor(interval_s: int, duration_min: int) -> dict:
    # Guard: nicht mehrfach starten
    st = _read_pi_monitor_state()
    if st.get("running"):
        return {"ok": False, "running": True, "msg": t("pi_monitor.already_running", "Pi Monitor läuft bereits – bitte warten bis er fertig ist.")}

    if interval_s < 1 or interval_s > 3600:
        return {"ok": False, "running": False, "msg": t("pi_monitor.invalid_interval", "Intervall ungültig.")}
    if duration_min < 1 or duration_min > 24 * 60:
        return {"ok": False, "running": False, "msg": t("pi_monitor.invalid_duration", "Dauer ungültig.")}

    if not os.path.exists(PI_MONITOR_SCRIPT):
        return {"ok": False, "running": False, "msg": t("pi_monitor.script_missing", "Script nicht gefunden: {path}", path=PI_MONITOR_SCRIPT)}

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
        return {"ok": False, "running": False, "msg": t("generic.start_failed", "Start fehlgeschlagen: {error}", error=e)}

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

    return {"ok": True, "running": True, "msg": t("pi_monitor.started", "Pi Monitor gestartet."), **get_pi_monitor_status()}


def stop_pi_monitor() -> dict:
    st = _read_pi_monitor_state()
    if not st.get("running"):
        return {"ok": True, "running": False, "msg": "Nicht aktiv."}
    pid = st.get("pid")
    try:
        os.kill(int(pid), 15)  # SIGTERM
    except Exception as e:
        return {"ok": False, "running": True, "msg": t("generic.stop_failed", "Konnte nicht stoppen: {error}", error=e)}
    return {"ok": True, "running": False, "msg": t("generic.stop_sent", "Stop gesendet.")}

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
        return email, password, board_id, True, t("caller.read_start_custom_failed", "Fehler beim Lesen von start-custom.sh: {error}", error=e)

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
        raise RuntimeError(t("caller.required_lines_missing", "start-custom.sh: benötigte Variablenzeilen nicht gefunden – es wurde NICHT geschrieben."))

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

def _normalize_lang_code(code: str) -> str:
    code = str(code or "").strip().lower().replace(" ", "")
    code = code.replace("-", "_")
    code = re.sub(r"[^a-z0-9_]", "", code)
    return code


def _iter_all_lang_paths():
    try:
        if not LANG_JSON_DIR.exists() or not LANG_JSON_DIR.is_dir():
            return
    except Exception:
        return

    seen: set[str] = set()
    try:
        for candidate in sorted(LANG_JSON_DIR.glob("lang_*.json")):
            try:
                if not candidate.is_file():
                    continue
            except Exception:
                continue

            candidate_key = str(candidate.resolve())
            if candidate_key in seen:
                continue
            seen.add(candidate_key)
            yield candidate
    except Exception:
        return


def _find_lang_path_by_code(code: str):
    normalized = _normalize_lang_code(code)
    if not normalized:
        return None, normalized

    for path in _iter_all_lang_paths() or []:
        stem = path.stem
        suffix = stem[5:] if stem.startswith("lang_") else ""
        suffix = _normalize_lang_code(suffix)
        if suffix == normalized:
            return path, normalized

    return None, normalized


def _default_flag_for_lang(code: str) -> str:
    normalized = _normalize_lang_code(code)
    if not normalized:
        return "xx"

    parts = [p for p in normalized.replace("-", "_").split("_") if p]
    lang = parts[0] if parts else normalized
    region = parts[1] if len(parts) > 1 else ""

    if region:
        return region.lower()

    lang_to_flag = {
        "ar": "sa",
        "bg": "bg",
        "cs": "cz",
        "da": "dk",
        "de": "de",
        "el": "gr",
        "en": "gb",
        "es": "es",
        "et": "ee",
        "fi": "fi",
        "fr": "fr",
        "ga": "ie",
        "he": "il",
        "hi": "in",
        "hr": "hr",
        "hu": "hu",
        "id": "id",
        "it": "it",
        "ja": "jp",
        "ko": "kr",
        "lt": "lt",
        "lv": "lv",
        "ms": "my",
        "mt": "mt",
        "nl": "nl",
        "no": "no",
        "pl": "pl",
        "pt": "pt",
        "ro": "ro",
        "ru": "ru",
        "sk": "sk",
        "sl": "si",
        "sr": "rs",
        "sv": "se",
        "th": "th",
        "tr": "tr",
        "uk": "ua",
        "vi": "vn",
        "zh": "cn",
    }
    return lang_to_flag.get(lang, lang[:2] or "xx")


def _normalize_lang_payload(data, fallback_code: str):
    if isinstance(data, dict):
        payload = dict(data)
    else:
        payload = {"placeholder": {}, "items": data}

    config = payload.get("config")
    if not isinstance(config, dict):
        config = {}
    else:
        config = dict(config)

    placeholder = payload.get("placeholder")
    if not isinstance(placeholder, dict):
        placeholder = {}

    abk = _normalize_lang_code(config.get("abk") or fallback_code)
    if not abk:
        abk = fallback_code

    flag = str(config.get("flag") or "").strip().lower()
    flag = re.sub(r"[^a-z0-9_-]", "", flag)
    if not flag:
        flag = _default_flag_for_lang(abk)

    config["abk"] = abk
    config["flag"] = flag
    config.pop("default", None)

    payload["config"] = config
    payload["placeholder"] = placeholder
    return payload


def _read_lang_default_config() -> tuple[str | None, str | None]:
    try:
        if not LANG_CONFIG_PATH.exists():
            return None, None

        raw = LANG_CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, "config_lang.json muss ein JSON-Objekt enthalten."

        code = _normalize_lang_code(data.get("default"))
        if not code:
            return None, None
        return code, None
    except json.JSONDecodeError as e:
        return None, f"config_lang.json ist ungültig: {e}"
    except Exception as e:
        return None, f"config_lang.json konnte nicht gelesen werden: {e}"


def _write_lang_default_config(code: str) -> tuple[bool, str | None]:
    normalized = _normalize_lang_code(code)
    if not normalized:
        return False, "Ungültiger Sprachcode."

    try:
        LANG_JSON_DIR.mkdir(parents=True, exist_ok=True)
        LANG_CONFIG_PATH.write_text(
            json.dumps({"default": normalized}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True, None
    except Exception as e:
        return False, f"config_lang.json konnte nicht geschrieben werden: {e}"


def _resolve_default_lang_code(languages: dict[str, dict]) -> tuple[str | None, str | None]:
    configured, err = _read_lang_default_config()
    if configured and configured in languages:
        return configured, err

    if configured and configured not in languages:
        err = f"Default-Sprache '{configured}' aus config_lang.json wurde nicht gefunden."

    fallback = next(iter(languages.keys()), None)
    return fallback, err


def _load_lang_json(code: str) -> tuple[dict | None, str | None, str | None, str]:
    path, normalized = _find_lang_path_by_code(code)
    if not normalized:
        return None, None, "Ungültiger Sprachcode.", normalized
    if path is None:
        return None, None, f"Sprachdatei lang_{normalized}.json nicht gefunden in {LANG_JSON_DIR}.", normalized

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, (dict, list)):
            return None, str(path), "Sprachdatei muss ein JSON-Objekt oder Array enthalten.", normalized
        payload = _normalize_lang_payload(data, normalized)
        return payload, str(path), None, normalized
    except json.JSONDecodeError as e:
        return None, str(path), f"JSON ungültig: {e}", normalized
    except Exception as e:
        return None, str(path), f"Datei konnte nicht gelesen werden: {e}", normalized


def _load_all_lang_jsons() -> tuple[dict, list[dict], str | None]:
    languages: dict[str, dict] = {}
    sources: list[dict] = []

    try:
        if not LANG_JSON_DIR.exists() or not LANG_JSON_DIR.is_dir():
            return {}, [], f"Sprachordner nicht gefunden: {LANG_JSON_DIR}"
    except Exception as e:
        return {}, [], f"Sprachordner konnte nicht gelesen werden: {e}"

    paths = list(_iter_all_lang_paths() or [])
    if not paths:
        return {}, [], f"Keine Sprachdateien in {LANG_JSON_DIR} gefunden."

    for path in paths:
        stem = path.stem
        suffix = stem[5:] if stem.startswith("lang_") else ""
        suffix = _normalize_lang_code(suffix)
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, (dict, list)):
                sources.append({
                    "file": str(path),
                    "code": suffix,
                    "ok": False,
                    "msg": "Sprachdatei muss ein JSON-Objekt oder Array enthalten.",
                })
                continue

            payload = _normalize_lang_payload(data, suffix)
            code = _normalize_lang_code(payload.get("config", {}).get("abk") or suffix)
            if not code:
                code = suffix
            payload["config"]["abk"] = code
            languages[code] = payload
            sources.append({"file": str(path), "code": code, "ok": True})
        except json.JSONDecodeError as e:
            sources.append({"file": str(path), "code": suffix, "ok": False, "msg": f"JSON ungültig: {e}"})
        except Exception as e:
            sources.append({"file": str(path), "code": suffix, "ok": False, "msg": f"Datei konnte nicht gelesen werden: {e}"})

    if not languages:
        return {}, sources, "Keine gültigen Sprachdateien gefunden."

    return languages, sources, None



def _get_current_lang_code() -> str:
    code = session.get("lang") or request.args.get("lang") or ""
    code = _normalize_lang_code(code)

    languages, _, _ = _load_all_lang_jsons()
    default_code, _ = _resolve_default_lang_code(languages)

    if code and code in languages:
        return code
    if default_code and default_code in languages:
        return default_code
    return "de"





def get_pi_model_name() -> str:
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            raw = Path(path).read_bytes()
            model = raw.decode("utf-8", "ignore").replace("\x00", "").strip()
            if model:
                return model
        except Exception:
            continue
    return "unknown"


def is_pi5_model(model: str | None = None) -> bool:
    model = (model or get_pi_model_name()).strip()
    return "Raspberry Pi 5" in model


def _display_user_exists(username: str | None) -> bool:
    username = (username or "").strip()
    if not username:
        return False
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False
    except Exception:
        return False


def _display_user_candidates() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def add(username: str | None):
        username = (username or "").strip()
        if not username or username in seen:
            return
        if _display_user_exists(username):
            seen.add(username)
            out.append(username)

    for candidate in AUTODARTS_DISPLAY_PREFERRED_USERS:
        add(candidate)

    try:
        current_user = pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        current_user = ""
    add(current_user)

    try:
        home_root = Path("/home")
        if home_root.exists():
            for p in sorted(home_root.iterdir()):
                if p.is_dir():
                    add(p.name)
    except Exception:
        pass

    try:
        proc = subprocess.run(
            ["ps", "-eo", "user=,args="],
            capture_output=True,
            text=True,
            timeout=1.8,
        )
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            user, args = parts[0].strip(), parts[1].strip()
            if not user or user == "root":
                continue
            if any(token in args for token in ("lxsession", "startlxde", "openbox", "xfce4-session", "xinit", "labwc", "wayfire")):
                add(user)
    except Exception:
        pass

    return out


def _display_candidates() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def add(value: str | None):
        value = (value or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        out.append(value)

    add(os.environ.get("DISPLAY", ""))
    add(":0")
    add(":1")
    return out


def _build_display_env(username: str, display_name: str) -> dict:
    env = os.environ.copy()
    env["DISPLAY"] = display_name
    try:
        pwent = pwd.getpwnam(username)
        env["HOME"] = pwent.pw_dir
        env.setdefault("USER", username)
        env.setdefault("LOGNAME", username)

        xauth_candidates = [
            Path(pwent.pw_dir) / ".Xauthority",
            Path(f"/run/user/{pwent.pw_uid}/gdm/Xauthority"),
            Path(f"/run/user/{pwent.pw_uid}/Xauthority"),
        ]
        for xauth in xauth_candidates:
            if xauth.exists():
                env["XAUTHORITY"] = str(xauth)
                break

        bus = Path(f"/run/user/{pwent.pw_uid}/bus")
        if bus.exists():
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")
    except Exception:
        pass
    return env


def _run_subprocess_as_user(cmd: list[str], username: str, env: dict, timeout: float = 6.0) -> subprocess.CompletedProcess:
    current_user = ""
    try:
        current_user = pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        current_user = ""

    if username and os.geteuid() == 0 and username != current_user:
        if shutil.which("sudo"):
            return subprocess.run(["sudo", "-u", username, "-H", *cmd], capture_output=True, text=True, timeout=timeout, env=env)
        if shutil.which("runuser"):
            shell_cmd = shlex.join(cmd)
            return subprocess.run(["runuser", "-l", username, "-c", "bash -lc " + shlex.quote(shell_cmd)], capture_output=True, text=True, timeout=timeout, env=env)
        if shutil.which("su"):
            shell_cmd = shlex.join(cmd)
            return subprocess.run(["su", "-", username, "-c", "bash -lc " + shlex.quote(shell_cmd)], capture_output=True, text=True, timeout=timeout, env=env)

    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _parse_xrandr_output(stdout: str) -> list[dict]:
    outputs: list[dict] = []
    current_output: dict | None = None
    mode_re = re.compile(r"^\s+([0-9]+x[0-9]+[A-Za-z]*)\s+(.*)$")
    header_re = re.compile(r"^(\S+)\s+connected(?:\s+primary)?(?:\s+([0-9]+x[0-9]+)\+[0-9]+\+[0-9]+)?")

    for raw_line in (stdout or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        if not raw_line.startswith((" ", "	")):
            current_output = None
            header = header_re.search(line)
            if not header:
                continue
            current_output = {
                "name": header.group(1),
                "current_mode": header.group(2) or "",
                "current_rate": "",
                "modes": [],
            }
            outputs.append(current_output)
            continue

        if not current_output:
            continue

        match = mode_re.match(line)
        if not match:
            continue

        mode_name = match.group(1).strip()
        rates_blob = match.group(2).strip()
        seen_keys: set[str] = set()
        for token in rates_blob.split():
            token = token.strip()
            if not token or not re.match(r"^[0-9.]+[*+]*$", token):
                continue
            rate = re.sub(r"[^0-9.]", "", token)
            key = f"{mode_name}@{rate}" if rate else mode_name
            if key in seen_keys:
                continue
            seen_keys.add(key)
            is_current = "*" in token
            is_preferred = "+" in token
            label = mode_name + (f" @ {rate} Hz" if rate else "")
            entry = {
                "key": key,
                "mode": mode_name,
                "rate": rate,
                "label": label,
                "is_current": is_current,
                "is_preferred": is_preferred,
            }
            current_output["modes"].append(entry)
            if is_current:
                current_output["current_mode"] = mode_name
                current_output["current_rate"] = rate

    parsed: list[dict] = []
    for output in outputs:
        if output.get("modes"):
            parsed.append(output)
    return parsed


def _probe_xrandr_session() -> dict:
    xrandr_cmd = str(AUTODARTS_DISPLAY_XRANDR or "xrandr").strip() or "xrandr"
    if not shutil.which(xrandr_cmd) and not Path(xrandr_cmd).exists():
        return {
            "ok": False,
            "reason": t("themes.display_no_xrandr", "xrandr wurde auf diesem System nicht gefunden."),
        }

    any_query_success = False
    first_open_display_error = ""

    for username in _display_user_candidates():
        for display_name in _display_candidates():
            env = _build_display_env(username, display_name)
            try:
                result = _run_subprocess_as_user([xrandr_cmd, "--query"], username, env, timeout=6.0)
            except Exception as exc:
                first_open_display_error = first_open_display_error or str(exc)
                continue

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            if result.returncode != 0:
                if stderr and not first_open_display_error:
                    first_open_display_error = stderr
                continue

            any_query_success = True
            outputs = _parse_xrandr_output(stdout)
            if outputs:
                return {
                    "ok": True,
                    "user": username,
                    "display": display_name,
                    "outputs": outputs,
                }

    if any_query_success:
        return {
            "ok": False,
            "reason": t("themes.display_no_monitor", "Es wurde kein aktiver Monitor am Raspberry erkannt."),
        }

    message = first_open_display_error or ""
    lowered = message.lower()
    if (not message) or ("open display" in lowered) or ("can't open display" in lowered) or ("unable to open display" in lowered) or ("authorization required" in lowered):
        message = t("themes.display_no_session", "Keine passende X11-Desktop-Sitzung gefunden.")
    return {
        "ok": False,
        "reason": message,
    }


def get_autodarts_display_info() -> dict:
    model = get_pi_model_name()
    is_pi5 = is_pi5_model(model)
    probe = _probe_xrandr_session()

    info = {
        "available": False,
        "show_in_theme_card": bool(is_pi5) if AUTODARTS_DISPLAY_SECTION_PI5_ONLY else True,
        "backend": "x11",
        "model": model,
        "is_pi5": is_pi5,
        "message": probe.get("reason") or "",
        "outputs": [],
    }

    if probe.get("ok"):
        outputs = probe.get("outputs") or []
        info.update({
            "available": bool(outputs),
            "show_in_theme_card": True,
            "message": t("themes.display_ready", "Lokaler Monitor erkannt. Die Änderung gilt sofort auf dem Pi-Bildschirm."),
            "outputs": outputs,
        })

    return info


def _find_display_mode(outputs: list[dict], output_name: str, mode_key: str) -> tuple[dict | None, dict | None]:
    for output in outputs or []:
        if str(output.get("name") or "") != output_name:
            continue
        for mode in output.get("modes", []) or []:
            if str(mode.get("key") or "") == mode_key:
                return output, mode
        return output, None
    return None, None


def apply_autodarts_display_mode(output_name: str, mode_key: str) -> dict:
    output_name = (output_name or "").strip()
    mode_key = (mode_key or "").strip()
    if not output_name:
        return {"ok": False, "message": t("themes.display_output_required", "Bitte zuerst einen Bildschirm-Ausgang auswählen.")}
    if not mode_key:
        return {"ok": False, "message": t("themes.display_mode_required", "Bitte zuerst eine Auflösung auswählen.")}

    probe = _probe_xrandr_session()
    if not probe.get("ok"):
        return {"ok": False, "message": probe.get("reason") or t("themes.display_no_session", "Keine passende X11-Desktop-Sitzung gefunden.")}

    outputs = probe.get("outputs") or []
    output, mode = _find_display_mode(outputs, output_name, mode_key)
    if not output:
        return {"ok": False, "message": t("themes.display_output_missing", "Der gewählte Bildschirm-Ausgang wurde nicht gefunden.")}
    if not mode:
        return {"ok": False, "message": t("themes.display_mode_missing", "Die gewählte Auflösung ist aktuell nicht verfügbar.")}

    env = _build_display_env(str(probe.get("user") or ""), str(probe.get("display") or ":0"))
    cmd = [str(AUTODARTS_DISPLAY_XRANDR or "xrandr").strip() or "xrandr", "--output", output_name, "--mode", str(mode.get("mode") or "")]
    if mode.get("rate"):
        cmd.extend(["--rate", str(mode.get("rate"))])

    try:
        result = _run_subprocess_as_user(cmd, str(probe.get("user") or ""), env, timeout=8.0)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return {
            "ok": False,
            "message": stderr or t("themes.display_apply_failed", "Die Auflösung konnte nicht gesetzt werden."),
        }

    refreshed = get_autodarts_display_info()
    return {
        "ok": True,
        "message": t("themes.display_applied", "Auflösung {mode} übernommen.", mode=(mode.get("label") or mode.get("mode") or mode_key)),
        "display_info": refreshed,
    }



def _ensure_autodarts_theme_state_parent():
    try:
        AUTODARTS_THEME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _autodarts_theme_default_state() -> dict:
    return {"selected": "default"}


def load_autodarts_theme_state() -> dict:
    try:
        data = json.loads(AUTODARTS_THEME_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            selected = str(data.get("selected") or "default").strip() or "default"
            return {"selected": selected}
    except Exception:
        pass
    return _autodarts_theme_default_state()


def save_autodarts_theme_state(selected: str) -> dict:
    payload = {"selected": (selected or "default").strip() or "default"}
    _ensure_autodarts_theme_state_parent()
    AUTODARTS_THEME_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _parse_autodarts_theme_header_value(head: str, labels: tuple[str, ...]) -> str:
    if not head:
        return ""
    label_group = "|".join(re.escape(label) for label in labels)
    patterns = [
        rf"^\s*/\*\s*(?:{label_group})\s*:\s*(.+?)\s*\*/",
        rf"^\s*(?:{label_group})\s*:\s*(.+?)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, head, flags=re.MULTILINE | re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""



def parse_autodarts_theme_metadata(css_text: str) -> dict[str, str]:
    if not css_text:
        return {"author": "", "modes": "", "resolution": ""}

    head = "\n".join(css_text.splitlines()[:30])
    author = _parse_autodarts_theme_header_value(head, ("Autor", "Author"))
    modes = _parse_autodarts_theme_header_value(head, ("Spielmodus", "Spielmodi", "Game mode", "Game modes", "Mode", "Modes"))
    resolution = _parse_autodarts_theme_header_value(head, ("Bildschirmauflösung", "Auflösung", "Resolution", "Screen resolution"))

    if not resolution:
        fallback_patterns = [
            r"für\s+Auflösung\s+([0-9]{3,5}\s*[x×]\s*[0-9]{3,5})",
            r"for\s+resolution\s+([0-9]{3,5}\s*[x×]\s*[0-9]{3,5})",
        ]
        for pattern in fallback_patterns:
            match = re.search(pattern, head, flags=re.IGNORECASE)
            if match:
                resolution = match.group(1).strip()
                break

    if resolution:
        resolution = re.sub(r"\s*[×x]\s*", " × ", resolution)

    if modes:
        modes = re.sub(r"\s*,\s*", ", ", modes.strip())

    return {
        "author": author,
        "modes": modes,
        "resolution": resolution,
    }



def parse_autodarts_theme_author(css_text: str) -> str:
    return parse_autodarts_theme_metadata(css_text).get("author", "")


def autodarts_theme_preview_path_for_css(path: Path) -> Path | None:
    stem = path.stem
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        candidate = path.with_suffix(ext)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def list_autodarts_themes() -> list[dict]:
    try:
        AUTODARTS_THEME_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    items: list[dict] = []
    try:
        candidates = sorted(AUTODARTS_THEME_DIR.glob("*.css"), key=lambda p: p.name.lower())
    except Exception:
        candidates = []

    for path in candidates:
        try:
            css_text = path.read_text(encoding="utf-8")
        except Exception:
            css_text = ""
        preview_path = autodarts_theme_preview_path_for_css(path)
        theme_meta = parse_autodarts_theme_metadata(css_text)
        items.append({
            "name": path.stem,
            "filename": path.name,
            "author": theme_meta.get("author", ""),
            "modes": theme_meta.get("modes", ""),
            "resolution": theme_meta.get("resolution", ""),
            "path": str(path),
            "preview_filename": preview_path.name if preview_path else "",
            "has_preview": bool(preview_path),
        })
    return items


def get_autodarts_theme_entry(filename: str | None):
    filename = (filename or "").strip()
    if not filename or filename == "default":
        return None
    for item in list_autodarts_themes():
        if item.get("filename") == filename:
            return item
    return None


def get_selected_autodarts_theme_name() -> str:
    state = load_autodarts_theme_state()
    selected = str(state.get("selected") or "default").strip() or "default"
    if selected == "default":
        return "default"
    return selected if get_autodarts_theme_entry(selected) else "default"


def read_selected_autodarts_theme_css() -> tuple[str, dict | None]:
    selected = get_selected_autodarts_theme_name()
    entry = get_autodarts_theme_entry(selected)
    if not entry:
        return "", None
    try:
        css = Path(entry["path"]).read_text(encoding="utf-8")
    except Exception:
        return "", entry
    return css, entry



def _caller_user_candidates() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def add(username: str | None):
        username = (username or "").strip()
        if not username or username in seen or username == "root":
            return
        try:
            home = Path(pwd.getpwnam(username).pw_dir)
        except Exception:
            return
        if not home.exists():
            return
        seen.add(username)
        out.append(username)

    for username in _display_user_candidates():
        add(username)

    try:
        for entry in sorted(Path('/home').iterdir()):
            if entry.is_dir():
                add(entry.name)
    except Exception:
        pass

    return out


def _caller_desktop_dir_for_user(username: str) -> Path:
    pwent = pwd.getpwnam(username)
    home_dir = Path(pwent.pw_dir)
    desktop_dir = home_dir / 'Desktop'

    xdg_cmd = shutil.which('xdg-user-dir')
    if xdg_cmd:
        env = os.environ.copy()
        env['HOME'] = str(home_dir)
        env.setdefault('USER', username)
        env.setdefault('LOGNAME', username)
        try:
            result = _run_subprocess_as_user([xdg_cmd, 'DESKTOP'], username, env, timeout=3.0)
            candidate = (result.stdout or '').strip()
            if result.returncode == 0 and candidate.startswith('/'):
                desktop_dir = Path(candidate)
        except Exception:
            pass

    return desktop_dir


def _caller_install_target() -> tuple[str | None, Path | None, Path | None, str]:
    for username in _caller_user_candidates():
        try:
            desktop_dir = _caller_desktop_dir_for_user(username)
        except Exception:
            continue
        target_dir = desktop_dir / AUTODARTS_CALLER_DIR_NAME
        return username, desktop_dir, target_dir, ''
    return None, None, None, t('caller.install_user_missing', 'Es konnte kein passender lokaler Desktop-Benutzer gefunden werden.')


def _caller_strip_root_folder(names: list[str]) -> str:
    roots: set[str] = set()
    for raw_name in names:
        name = str(raw_name or '').replace('\\', '/').strip('/')
        if not name or name.startswith('__MACOSX/'):
            continue
        parts = [p for p in name.split('/') if p]
        if not parts:
            continue
        roots.add(parts[0])
    if len(roots) != 1:
        return ''
    root = next(iter(roots))
    for raw_name in names:
        name = str(raw_name or '').replace('\\', '/').strip('/')
        if not name or name.startswith('__MACOSX/'):
            continue
        parts = [p for p in name.split('/') if p]
        if len(parts) <= 1:
            return ''
        if parts[0] != root:
            return ''
    return root


def _caller_extract_zip(zip_path: Path, target_dir: Path):
    with zipfile.ZipFile(zip_path, 'r') as zf:
        strip_root = _caller_strip_root_folder(zf.namelist())
        for info in zf.infolist():
            raw_name = str(info.filename or '').replace('\\', '/').strip('/')
            if not raw_name or raw_name.startswith('__MACOSX/'):
                continue

            parts = list(PurePosixPath(raw_name).parts)
            if any(part in ('..', '') for part in parts):
                continue
            if strip_root and parts and parts[0] == strip_root:
                parts = parts[1:]
            if not parts:
                continue

            dest = target_dir.joinpath(*parts)
            try:
                dest.relative_to(target_dir)
            except Exception:
                continue

            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, 'r') as src, open(dest, 'wb') as dst:
                shutil.copyfileobj(src, dst)

            mode = (info.external_attr >> 16) & 0o777
            if mode:
                try:
                    os.chmod(dest, mode)
                except Exception:
                    pass


def _caller_chown_recursive(path: Path, username: str):
    try:
        pwent = pwd.getpwnam(username)
    except Exception:
        return
    uid, gid = pwent.pw_uid, pwent.pw_gid

    for root, dirs, files in os.walk(path):
        try:
            os.chown(root, uid, gid)
        except Exception:
            pass
        for name in dirs:
            try:
                os.chown(os.path.join(root, name), uid, gid)
            except Exception:
                pass
        for name in files:
            try:
                os.chown(os.path.join(root, name), uid, gid)
            except Exception:
                pass

    try:
        os.chown(path, uid, gid)
    except Exception:
        pass


def install_caller_locally() -> dict:
    username, desktop_dir, target_dir, err = _caller_install_target()
    if not username or not desktop_dir or not target_dir:
        return {'ok': False, 'message': err or t('caller.install_failed', 'Caller konnte nicht installiert werden.')}

    if not AUTODARTS_CALLER_INSTALL_LOCK.acquire(blocking=False):
        return {'ok': False, 'message': t('caller.install_busy', 'Eine Caller-Installation läuft bereits. Bitte kurz warten und erneut versuchen.')}

    try:
        desktop_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='autodarts-caller-') as tmpdir:
            tmpdir_path = Path(tmpdir)
            zip_path = tmpdir_path / 'CALLER_ALL.zip'

            req = urllib.request.Request(AUTODARTS_CALLER_ZIP_URL, headers={'User-Agent': 'Autodarts-Webpanel/1.0'})
            with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, 'wb') as out_file:
                shutil.copyfileobj(resp, out_file)

            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            _caller_extract_zip(zip_path, target_dir)
            _caller_chown_recursive(target_dir, username)

        return {
            'ok': True,
            'message': t('caller.install_success', 'Caller wurde auf diesem Raspberry nach {path} installiert.', path=str(target_dir)),
            'target_path': str(target_dir),
            'desktop_path': str(desktop_dir),
            'username': username,
        }
    except urllib.error.URLError as exc:
        return {'ok': False, 'message': t('caller.install_download_failed', 'Download der Caller ZIP fehlgeschlagen: {error}', error=exc)}
    except zipfile.BadZipFile:
        return {'ok': False, 'message': t('caller.install_zip_invalid', 'Die geladene Caller ZIP ist ungültig oder beschädigt.')}
    except Exception as exc:
        return {'ok': False, 'message': t('caller.install_failed', 'Caller konnte nicht installiert werden: {error}', error=exc)}
    finally:
        try:
            AUTODARTS_CALLER_INSTALL_LOCK.release()
        except Exception:
            pass


def render_windows_caller_installer_bat() -> str:
    url = AUTODARTS_CALLER_ZIP_URL.replace("'", "''")
    folder = AUTODARTS_CALLER_DIR_NAME.replace('"', '')
    script = f"""@echo off
setlocal EnableExtensions

set "CALLER_URL={url}"
set "TARGET_NAME={folder}"

for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP_DIR=%%I"
if not defined DESKTOP_DIR set "DESKTOP_DIR=%USERPROFILE%\\Desktop"

set "TARGET_DIR=%DESKTOP_DIR%\\%TARGET_NAME%"
set "ZIP_FILE=%TEMP%\\CALLER_ALL.zip"

echo.
echo Caller wird nach "%TARGET_DIR%" installiert...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "try {{ Invoke-WebRequest -Uri '%CALLER_URL%' -UseBasicParsing -OutFile '%ZIP_FILE%' }} catch {{ exit 1 }}"
if errorlevel 1 goto :download_error

if exist "%TARGET_DIR%" rmdir /s /q "%TARGET_DIR%"
mkdir "%TARGET_DIR%" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command "try {{ Expand-Archive -LiteralPath '%ZIP_FILE%' -DestinationPath '%TARGET_DIR%' -Force }} catch {{ exit 1 }}"
if errorlevel 1 goto :extract_error

powershell -NoProfile -ExecutionPolicy Bypass -Command "$target='%TARGET_DIR%'; $items=Get-ChildItem -LiteralPath $target -Force; if($items.Count -eq 1 -and $items[0].PSIsContainer) {{ $inner=$items[0].FullName; Get-ChildItem -LiteralPath $inner -Force | ForEach-Object {{ Move-Item -LiteralPath $_.FullName -Destination $target -Force }}; Remove-Item -LiteralPath $inner -Force -Recurse }}"

del "%ZIP_FILE%" >nul 2>&1

echo.
echo Fertig. Der Ordner "%TARGET_DIR%" ist jetzt auf dem Desktop.
pause
exit /b 0

:download_error
echo Fehler: Caller ZIP konnte nicht heruntergeladen werden.
pause
exit /b 1

:extract_error
echo Fehler: Caller ZIP konnte nicht entpackt werden.
pause
exit /b 1
"""
    return script.replace("\n", "\r\n")


def t(key: str, fallback: str = "", **kwargs) -> str:
    languages, _, _ = _load_all_lang_jsons()
    code = _get_current_lang_code()
    lang_data = languages.get(code, {}) or {}

    placeholder = lang_data.get("placeholder", {})
    if not isinstance(placeholder, dict):
        placeholder = {}

    value = placeholder.get(key, fallback)

    if isinstance(value, (dict, list)):
        value = fallback

    text = str(value)

    if kwargs:
        try:
            text = text.format(**kwargs)
        except Exception:
            pass

    return text

@app.context_processor
def inject_i18n_helpers():
    return {
        "t": t,
        "current_lang": _get_current_lang_code(),
    }


def _forbidden_response():
    return (t("auth.forbidden", "Forbidden"), 403)


def _inline_notice_page(title: str, body_html: str, status: int = 200, html_lang: str | None = None) -> tuple[str, int]:
    lang = html_lang or _get_current_lang_code()
    html = (
        f"<!doctype html><html lang='{lang}'>"
        "<head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title></head>"
        "<body style='font-family:system-ui;background:#111;color:#eee;padding:20px;'>"
        f"<h1>{title}</h1>"
        f"{body_html}"
        "</body></html>"
    )
    return html, status

def _json_nocache(payload: dict, status: int = 200):
    resp = jsonify(payload)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/langs", methods=["GET"])
def api_langs():
    languages, sources, err = _load_all_lang_jsons()
    status = 200
    default_code, default_err = _resolve_default_lang_code(languages)

    msg_parts = [x for x in [err, default_err] if x]
    return _json_nocache({
        "ok": bool(languages),
        "count": len(languages),
        "dir": str(LANG_JSON_DIR),
        "config_file": str(LANG_CONFIG_PATH),
        "default": default_code,
        "sources": sources,
        "data": languages,
        "msg": " | ".join(msg_parts) if msg_parts else None,
    }, status)


@app.route("/api/lang/default", methods=["GET", "POST", "PUT"])
def api_lang_default():
    languages, sources, err = _load_all_lang_jsons()
    default_code, default_err = _resolve_default_lang_code(languages)

    if request.method == "GET":
        msg_parts = [x for x in [err, default_err] if x]
        return _json_nocache({
            "ok": bool(default_code),
            "default": default_code,
            "config_file": str(LANG_CONFIG_PATH),
            "available": sorted(languages.keys()),
            "sources": sources,
            "msg": " | ".join(msg_parts) if msg_parts else None,
        }, 200)

    payload = request.get_json(silent=True) or {}
    requested_code = (
        payload.get("default")
        or payload.get("abk")
        or payload.get("code")
        or request.form.get("default")
        or request.form.get("abk")
        or request.form.get("code")
        or request.args.get("default")
        or request.args.get("abk")
        or request.args.get("code")
    )
    normalized = _normalize_lang_code(requested_code)
    if not normalized:
        return _json_nocache({
            "ok": False,
            "msg": "Bitte einen Sprachcode senden, z. B. {'default':'de'}.",
            "available": sorted(languages.keys()),
        }, 200)

    if languages and normalized not in languages:
        return _json_nocache({
            "ok": False,
            "default": default_code,
            "requested": normalized,
            "available": sorted(languages.keys()),
            "msg": f"Sprache '{normalized}' wurde in {LANG_JSON_DIR} nicht gefunden.",
        }, 200)

    ok, write_err = _write_lang_default_config(normalized)
    if not ok:
        return _json_nocache({
            "ok": False,
            "requested": normalized,
            "msg": write_err,
            "config_file": str(LANG_CONFIG_PATH),
        }, 200)

    return _json_nocache({
        "ok": True,
        "default": normalized,
        "requested": normalized,
        "config_file": str(LANG_CONFIG_PATH),
        "available": sorted(languages.keys()),
        "msg": f"Default-Sprache wurde auf '{normalized}' gesetzt.",
    })


@app.route("/lang/all.js", methods=["GET"])
@app.route("/langs.js", methods=["GET"])
def lang_all_js():
    languages, sources, err = _load_all_lang_jsons()
    payload = json.dumps(languages, ensure_ascii=False)
    default_code, default_err = _resolve_default_lang_code(languages)

    js_lines = [
        f"window.lang_code = {payload};",
        "window.lang = window.lang_code || {};",
        f"window.lang_sources = {json.dumps(sources, ensure_ascii=False)};",
        f"window.lang_dir = {json.dumps(str(LANG_JSON_DIR), ensure_ascii=False)};",
        f"window.lang_default = {json.dumps(default_code, ensure_ascii=False)};",
        f"window.lang_config_file = {json.dumps(str(LANG_CONFIG_PATH), ensure_ascii=False)};",
    ]

    if err:
        js_lines.append(f"window.lang_error = {json.dumps(err, ensure_ascii=False)};")
    if default_err:
        js_lines.append(f"window.lang_default_error = {json.dumps(default_err, ensure_ascii=False)};")

    js = "\n".join(js_lines) + "\n"
    resp = Response(js, status=200, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/lang/<code>", methods=["GET"])
def api_lang(code: str):
    data, path, err, normalized = _load_lang_json(code)
    if err:
        return _json_nocache({"ok": False, "code": normalized, "msg": err, "source": path}, 200)

    languages, _, _ = _load_all_lang_jsons()
    default_code, default_err = _resolve_default_lang_code(languages)

    return _json_nocache({
        "ok": True,
        "code": normalized,
        "source": path,
        "is_default": normalized == default_code,
        "default": default_code,
        "default_msg": default_err,
        "data": data,
    })


@app.route("/lang/<code>.js", methods=["GET"])
def lang_js(code: str):
    data, path, err, normalized = _load_lang_json(code)
    if err:
        js = "window.lang_code = {};\n"
        js += "window.lang = window.lang || {};\n"
        js += f"window.lang[{json.dumps(normalized or code)}] = window.lang_code\n"
        js += f"window.lang_error = {json.dumps(err, ensure_ascii=False)}\n"
        return Response(js, status=200, mimetype="application/javascript")

    languages, _, _ = _load_all_lang_jsons()
    default_code, default_err = _resolve_default_lang_code(languages)

    payload = json.dumps(data, ensure_ascii=False)
    lang_key = normalized.replace("-", "_")
    js = "\n".join([
        f"window.lang_code = {payload};",
        "window.lang = window.lang || {};",
        f"window.lang[{json.dumps(normalized)}] = window.lang_code;",
        f'window["lang_{lang_key}"] = window.lang_code;',
        f"window.lang_source = {json.dumps(path, ensure_ascii=False)};",
        f"window.lang_default = {json.dumps(default_code, ensure_ascii=False)};",
        f"window.lang_is_default = {json.dumps(normalized == default_code)};",
        f"window.lang_config_file = {json.dumps(str(LANG_CONFIG_PATH), ensure_ascii=False)};",
    ]) + "\n"

    if default_err:
        js += f"window.lang_default_error = {json.dumps(default_err, ensure_ascii=False)}\n"

    resp = Response(js, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/help", methods=["GET"])
def help_page():
    """Handbuch als PDF (inline) aus AUTODARTS_DATA_DIR ausliefern."""
    pdf_path = DATA_DIR / HELP_PDF_FILENAME
    if not pdf_path.exists():
        return _inline_notice_page(
            t("help.not_found_title", "Handbuch nicht gefunden"),
            f"<p>{t('help.not_found_text', 'Die Datei <code>{filename}</code> wurde nicht gefunden.', filename=HELP_PDF_FILENAME)}</p>",
            status=404,
        )

    resp = send_from_directory(str(DATA_DIR), HELP_PDF_FILENAME)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'inline; filename="{HELP_PDF_FILENAME}"'
    return resp




@app.route("/api/autodarts/display-info", methods=["GET"])
def api_autodarts_display_info():
    return _json_nocache({
        "ok": True,
        "display_info": get_autodarts_display_info(),
    })


@app.route("/api/autodarts/display-mode", methods=["POST"])
def api_autodarts_display_mode():
    payload = request.get_json(silent=True) or {}
    output_name = str(payload.get("output") or "").strip()
    mode_key = str(payload.get("mode") or payload.get("mode_key") or "").strip()
    result = apply_autodarts_display_mode(output_name, mode_key)
    status = 200 if result.get("ok") else 200
    return _json_nocache(result, status)


@app.route("/api/autodarts-theme/preview/<path:filename>", methods=["GET"])
def api_autodarts_theme_preview(filename):
    safe_name = os.path.basename(str(filename or "").strip())
    if not safe_name:
        abort(404)
    css_name = Path(safe_name).stem + ".css"
    entry = get_autodarts_theme_entry(css_name)
    if not entry:
        abort(404)
    css_path = Path(entry.get("path", ""))
    preview_path = autodarts_theme_preview_path_for_css(css_path)
    if not preview_path:
        abort(404)
    return send_from_directory(str(AUTODARTS_THEME_DIR), preview_path.name, as_attachment=False)


@app.route("/api/autodarts-theme/state", methods=["GET"])
def api_autodarts_theme_state():
    themes = list_autodarts_themes()
    selected = get_selected_autodarts_theme_name()
    entry = get_autodarts_theme_entry(selected)
    return jsonify({
        "ok": True,
        "selected": selected,
        "author": (entry or {}).get("author", ""),
        "modes": (entry or {}).get("modes", ""),
        "resolution": (entry or {}).get("resolution", ""),
        "panel_url": AUTODARTS_THEME_PANEL_URL,
        "theme_dir": str(AUTODARTS_THEME_DIR),
        "store_url": AUTODARTS_THEME_STORE_URL,
        "themes": themes,
    })


@app.route("/api/autodarts-theme/select", methods=["POST"])
def api_autodarts_theme_select():
    payload = request.get_json(silent=True) if request.is_json else None
    selected = ""
    if isinstance(payload, dict):
        selected = str(payload.get("selected") or "")
    if not selected:
        selected = str(request.form.get("selected") or request.args.get("selected") or "")
    selected = selected.strip() or "default"

    if selected != "default" and not get_autodarts_theme_entry(selected):
        return jsonify({
            "ok": False,
            "message": t("themes.invalid", "Ungültiges Theme ausgewählt."),
        }), 400

    state = save_autodarts_theme_state(selected)
    entry = get_autodarts_theme_entry(state.get("selected"))
    author = (entry or {}).get("author", "")
    if state.get("selected") == "default":
        message = t("themes.saved_default", "Default-Theme aktiviert.")
    else:
        message = t("themes.saved_theme", "Theme '{theme}' aktiviert.", theme=(entry or {}).get("name", state.get("selected")))

    return jsonify({
        "ok": True,
        "selected": state.get("selected", "default"),
        "theme": (entry or {}).get("name", "default"),
        "filename": (entry or {}).get("filename", "default"),
        "author": author,
        "modes": (entry or {}).get("modes", ""),
        "resolution": (entry or {}).get("resolution", ""),
        "message": message,
    })


@app.route("/api/autodarts-theme.css", methods=["GET"])
def api_autodarts_theme_css():
    css, entry = read_selected_autodarts_theme_css()
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    if not css:
        return Response("", mimetype="text/css", headers=headers)
    return Response(css, mimetype="text/css", headers=headers)


@app.route("/api/caller/install-local", methods=["POST"])
def api_caller_install_local():
    result = install_caller_locally()
    status = 200 if result.get("ok") else 500
    return _json_nocache(result, status)


@app.route("/download/windows-caller-install.bat", methods=["GET"])
def download_windows_caller_install_bat():
    content = render_windows_caller_installer_bat()
    resp = Response(content, mimetype="application/octet-stream")
    resp.headers["Content-Disposition"] = f'attachment; filename="{AUTODARTS_CALLER_WINDOWS_INSTALLER_FILENAME}"'
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
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
            cam_info_message = t("camera.none_searched_yet", "Es wurden noch keine Kameras gesucht.")
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
    webpanel_check = load_webpanel_update_check()
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
    if admin_unlocked and ufw_state.get("status") == "installing":
        ufw_state = ufw_refresh_state()
        ufw_installed = bool(ufw_state.get("installed"))

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
        autodarts_themes=list_autodarts_themes(),
        autodarts_theme_selected=get_selected_autodarts_theme_name(),
        autodarts_theme_dir=str(AUTODARTS_THEME_DIR),
        autodarts_theme_panel_url=AUTODARTS_THEME_PANEL_URL,
        autodarts_theme_store_url=AUTODARTS_THEME_STORE_URL,
        autodarts_display_info=get_autodarts_display_info(),
    )


@app.route("/led/save", methods=["POST"])
def led_save():
    ad_email = request.form.get("ad_email", "").strip()
    ad_password = request.form.get("ad_password", "").strip()
    ad_board = request.form.get("ad_board", "").strip()

    cur_email, cur_pw, cur_board, exists, err = read_darts_caller_credentials()
    if not exists:
        return redirect(url_for("index", ledcheck="bad", ledmsg=t("caller.start_custom_missing_short", "start-custom.sh nicht gefunden.")))

    # leer lassen => unverändert
    if not ad_email:
        ad_email = cur_email
    if not ad_password:
        ad_password = cur_pw
    if not ad_board:
        ad_board = cur_board

    try:
        write_darts_caller_credentials(ad_email, ad_password, ad_board)
        return redirect(url_for("index", ledcheck="ok", ledmsg=t("caller.saved", "Gespeichert (start-custom.sh aktualisiert).")))
    except Exception as e:
        return redirect(url_for("index", ledcheck="bad", ledmsg=t("generic.save_failed", "Speichern fehlgeschlagen: {error}", error=e)))


@app.route("/led/check", methods=["POST"])
def led_check():
    email, pw, bid, exists, err = read_darts_caller_credentials()
    if not exists:
        return redirect(url_for("index", ledcheck="bad", ledmsg=t("caller.start_custom_missing_short", "start-custom.sh nicht gefunden.")))
    if err:
        return redirect(url_for("index", ledcheck="bad", ledmsg=err))
    if not email or not pw or not bid:
        return redirect(url_for("index", ledcheck="bad", ledmsg=t("caller.set_credentials_2fa_off", "Bitte Account/Passwort/Board-ID setzen. (2FA muss AUS sein)")))
    return redirect(url_for("index", ledcheck="ok", ledmsg=t("caller.data_present_2fa_off", "Daten vorhanden. Hinweis: 2FA muss AUS sein.")))




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
        return None, None, t("wled.globally_disabled", "WLED ist global deaktiviert.")

    host = (fallback_host or "").strip()
    if slot and slot > 0:
        targets = cfg.get("targets", []) or []
        if 1 <= slot <= len(targets):
            t = targets[slot - 1] or {}
            if not bool(t.get("enabled", False)):
                return None, None, t("wled.slot_disabled", "WLED Slot {slot} ist deaktiviert.", slot=slot)
            cfg_host = str(t.get("host", "")).strip()
            if cfg_host:
                host = cfg_host

    if not host:
        return None, None, t("wled.no_host_found", "Kein WLED Host gefunden.")

    ip = resolve_host_to_ip_fast(host, timeout_s=0.8)
    if not ip:
        return None, host, t("wled.host_could_not_resolve", "{host} konnte nicht aufgelöst werden.", host=host)

    if not is_wled_reachable(ip, timeout_sec=1.0):
        return None, host, t("wled.host_unreachable", "{host} ({ip}) ist nicht erreichbar.", host=host, ip=ip)

    return ip, host, None


def _wled_preset_name_from_editor(preset: int) -> str:
    ok, _msg, rows, _weps = load_wled_presets_state()
    if not ok or preset < 1:
        return f"Preset {preset}"

    row = next((r for r in rows if int(r.get("preset") or 0) == int(preset)), None)
    if not row:
        return f"Preset {preset}"

    kind = str(row.get("kind") or "")
    label = str(row.get("label") or "").strip()

    if " / " in label:
        label = label.split(" / ", 1)[0].strip()

    if kind == "score_exact":
        score = int(row.get("score") or 0)
        return f"Score {score}"

    if kind == "score_range":
        from_val = int(row.get("from") or 0)
        to_val = int(row.get("to") or 0)
        return f"Score {from_val}-{to_val}"

    return label or f"Preset {preset}"


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
        return jsonify({"ok": False, "msg": t("wled.invalid_preset_number", "Ungültige Preset-Nummer.")}), 400

    ip, host, err = _resolve_wled_target_for_slot_or_host(slot, host_fallback)
    if err:
        return jsonify({"ok": False, "msg": err}), 400

    try:
        preset_name = _wled_preset_name_from_editor(preset)
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

            _wled_json_post(ip, {"psave": preset, "n": preset_name, "ib": True, "sb": True}, "/json/state", timeout_s=1.8)

            if loaded_existing:
                msg = t("wled.preset_applied_and_saved", "Preset {preset} auf {host} übernommen und neu gespeichert.", preset=preset, host=host)
            else:
                msg = t("wled.preset_created_from_current_state", "Preset {preset} auf {host} neu aus dem aktuellen Zustand angelegt.", preset=preset, host=host)

            return jsonify({"ok": True, "msg": msg, "host": host, "ip": ip, "preset": preset, "name": preset_name})

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
            "n": preset_name,
            "ib": True,
            "sb": True
        }
        _wled_json_post(ip, default_payload, "/json/state", timeout_s=1.8)
        return jsonify({
            "ok": True,
            "msg": t("wled.preset_created_default_light", "Preset {preset} auf {host} neu mit Standard-Licht angelegt.", preset=preset, host=host),
            "host": host,
            "ip": ip,
            "preset": preset,
            "name": preset_name,
            "used_default": True
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": t("wled.send_failed", "WLED Senden fehlgeschlagen: {error}", error=e)}), 500


@app.route("/api/wled-presets/delete", methods=["POST"])
def api_wled_presets_delete():
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
        return jsonify({"ok": False, "msg": t("wled.invalid_preset_number", "Ungültige Preset-Nummer.")}), 400

    ip, host, err = _resolve_wled_target_for_slot_or_host(slot, host_fallback)
    if err:
        return jsonify({"ok": False, "msg": err}), 400

    try:
        _wled_json_post(ip, {"pdel": preset}, "/json/state", timeout_s=1.8)
        return jsonify({
            "ok": True,
            "msg": t("wled.preset_deleted_remote", "Preset {preset} wurde auf {host} gelöscht.", preset=preset, host=host),
            "host": host,
            "ip": ip,
            "preset": preset,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": t("wled.delete_failed", "WLED Löschen fehlgeschlagen: {error}", error=e)}), 500


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
        return _inline_notice_page(
            t("wled.disabled_title", "WLED deaktiviert"),
            (
                f"<p>{t('wled.disabled_text', 'WLED wurde in der Weboberfläche deaktiviert.')}</p>"
                f"<p><a style='color:#3b82f6' href='{url_for('index')}'>{t('generic.back', 'Zurück')}</a></p>"
            ),
            status=200,
        )

    targets = cfg.get("targets", [])
    if slot < 1 or slot > len(targets):
        return _inline_notice_page(
            t("wled.invalid_slot_title", "Ungültiger WLED Slot"),
            (
                f"<p>{t('wled.slot_does_not_exist', 'Slot {slot} existiert nicht.', slot=slot)}</p>"
                f"<p><a style='color:#3b82f6' href='{url_for('index')}'>{t('generic.back', 'Zurück')}</a></p>"
            ),
            status=404,
        )

    host = str(targets[slot - 1].get("host", "")).strip()
    slot_enabled = bool(targets[slot - 1].get("enabled", False))
    if not slot_enabled:
        return _inline_notice_page(
            t("wled.slot_disabled_title", "WLED Slot deaktiviert"),
            (
                f"<p>{t('wled.slot_currently_disabled', 'Slot {slot} ist aktuell nicht aktiviert.', slot=slot)}</p>"
                f"<p><a style='color:#3b82f6' href='{url_for('index')}'>{t('generic.back', 'Zurück')}</a></p>"
            ),
            status=200,
        )

    if not host:
        return _inline_notice_page(
            t("wled.no_host_title", "Kein WLED eingetragen"),
            (
                f"<p>{t('wled.no_host_for_slot', 'Für Slot {slot} wurde noch kein Hostname/IP eingetragen.', slot=slot)}</p>"
                f"<p><a style='color:#3b82f6' href='{url_for('index')}'>{t('generic.back', 'Zurück')}</a></p>"
            ),
            status=400,
        )

    ok, ip = is_http_reachable(host, timeout_s=0.8)
    if not ok:
        return _inline_notice_page(
            t("wled.unreachable_title", "WLED nicht erreichbar"),
            (
                f"<p>{t('wled.unreachable_text', 'Sie haben kein offizielles LED Band mit Controller im Einsatz, oder der Controller ist aktuell nicht verbunden.')}</p>"
                f"<p>{t('wled.host_label', 'Host')}: <code>{host}</code></p>"
                f"<p><a style='color:#3b82f6' href='{url_for('index')}'>{t('generic.back', 'Zurück')}</a></p>"
            ),
            status=503,
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
    msg_parts = [t("wled.targets_saved", "WLED Targets gespeichert.")]
    ok = True

    if service_exists(DARTS_WLED_SERVICE):
        master = bool(cfg.get("master_enabled", True))
        hosts = get_enabled_wled_hosts(cfg) if master else []

        if (not master) or (not hosts):
            service_disable_now(DARTS_WLED_SERVICE)
            if not master:
                msg_parts.append(t("wled.disabled_service_stopped", "WLED ist deaktiviert → darts-wled wurde gestoppt."))
            else:
                msg_parts.append(t("wled.no_target_service_stopped", "Kein Target aktiv → darts-wled wurde gestoppt."))
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
            msg_parts.append(t("wled.toggle_disabled_service_stopped", "WLED deaktiviert (merkt sich das nach Neustart). darts-wled wurde gestoppt."))
        else:
            hosts = get_enabled_wled_hosts(cfg)
            if hosts:
                ok_weps, msg_weps = update_darts_wled_start_custom_weps(hosts)
                msg_parts.append(t("wled.toggle_enabled", "WLED aktiviert (merkt sich das nach Neustart)."))
                msg_parts.append(msg_weps)
                if ok_weps:
                    service_enable_now(DARTS_WLED_SERVICE)
                    service_restart(DARTS_WLED_SERVICE)
                else:
                    ok = False
            else:
                service_disable_now(DARTS_WLED_SERVICE)
                msg_parts.append(t("wled.enabled_but_no_target", "WLED aktiviert, aber kein Target aktiv → darts-wled bleibt aus."))
    else:
        msg_parts.append(t("wled.toggle_saved_service_missing", "WLED Toggle gespeichert (Service nicht gefunden)."))

    return redirect(url_for("index", ledcheck=("ok" if ok else "bad"), ledmsg="\n".join(msg_parts)))




@app.route("/wled/set-enabled/<int:slot>", methods=["POST"])
def wled_set_enabled(slot: int):
    # User-UI: Slot 1..3
    if slot < 1 or slot > 3:
        return jsonify({"ok": False, "msg": t("generic.invalid_slot", "Ungültiger Slot.")}), 400

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


def _load_ap_client_internet_status():
    """Liest den Status der AP-Internetfreigabe für verbundene Geräte."""
    data = {
        "status": "red",
        "client_internet": False,
        "ap_active": False,
        "forwarding_ready": False,
        "pi_online": False,
        "uplinks": [],
        "note": t("ap_internet.status_file_missing", "Statusdatei nicht gefunden"),
    }
    try:
        with open(AP_INTERNET_STATUS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return data
        status = str(raw.get("status") or "").strip().lower()
        pi_online = bool(raw.get("pi_online", False))
        forwarding_ready = bool(raw.get("forwarding_ready", False))
        ap_active = bool(raw.get("ap_active", False))
        client_internet = bool(status == "green" and pi_online and forwarding_ready and ap_active)
        note = (
            t("ap_internet.clients_online", "AP-Clients haben Internet")
            if client_internet
            else t("ap_internet.clients_offline", "AP-Clients haben aktuell kein Internet")
        )
        return {
            "status": "green" if client_internet else "red",
            "client_internet": client_internet,
            "ap_active": ap_active,
            "forwarding_ready": forwarding_ready,
            "pi_online": pi_online,
            "uplinks": raw.get("uplinks") if isinstance(raw.get("uplinks"), list) else [],
            "note": note,
        }
    except Exception:
        return data


@app.route("/api/ap-client-internet-status", methods=["GET"])
def api_ap_client_internet_status():
    st = _load_ap_client_internet_status()
    st["ok"] = True
    return jsonify(st)


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
        return jsonify({"ok": False, "msg": t("admin.locked", "Admin gesperrt.")}), 403
    st = get_pi_monitor_status()
    st["ok"] = True
    return jsonify(st)


@app.route("/api/pi_monitor/start", methods=["POST"])
def api_pi_monitor_start():
    if not bool(session.get("admin_unlocked", False)):
        return jsonify({"ok": False, "msg": t("admin.locked", "Admin gesperrt.")}), 403
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
        return jsonify({"ok": False, "msg": t("admin.locked", "Admin gesperrt.")}), 403
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
    #if not bool(session.get("admin_unlocked", False)):
        #return _forbidden_response()

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

    return _inline_notice_page(
        t("admin.reboot_title", "Reboot"),
        (
            f"<h2>{t('admin.reboot_running', 'Neustart wird ausgeführt…')}</h2>"
            f"<p>{t('admin.reboot_running_text', 'Der Raspberry Pi startet jetzt neu. Diese Seite ist gleich nicht mehr erreichbar.')}</p>"
        ),
        html_lang=_get_current_lang_code(),
    )[0]






@app.route("/admin/shutdown", methods=["POST"])
def admin_shutdown():
    # Nur im Admin-Modus erlauben
    #if not bool(session.get("admin_unlocked", False)):
        #return _forbidden_response()

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

    return _inline_notice_page(
        t("admin.shutdown_title", "Herunterfahren"),
        (
            f"<h2>{t('admin.shutdown_running', 'Herunterfahren wird ausgeführt…')}</h2>"
            f"<p>{t('admin.shutdown_running_text', 'Der Raspberry Pi fährt jetzt sauber herunter. Diese Seite ist gleich nicht mehr erreichbar.')}</p>"
        ),
        html_lang=_get_current_lang_code(),
    )[0]


@app.route("/admin/pi-monitor/download/<kind>", methods=["GET"])
def admin_pi_monitor_download(kind: str):
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()

    kind = (kind or "").strip().lower()
    mapping = {
        "csv": (PI_MONITOR_CSV, "pi_monitor_test.csv", "text/csv; charset=utf-8"),
        "readme": (PI_MONITOR_README, "pi_monitor_test_README.txt", "text/plain; charset=utf-8"),
        "outlog": (PI_MONITOR_OUTLOG, "pi_monitor_test.out", "text/plain; charset=utf-8"),
    }
    entry = mapping.get(kind)
    if not entry:
        return (t("downloads.unknown_download", "Unbekannter Download."), 404)

    path, download_name, mimetype = entry
    if not os.path.exists(path):
        return (t("downloads.file_not_found", "Datei nicht gefunden: {path}", path=path), 404)

    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=download_name)


@app.route("/admin/pi-monitor/tail", methods=["GET"])
def admin_pi_monitor_tail():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()

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
        return Response(t("downloads.file_not_found", "Datei nicht gefunden: {path}", path=path) + "\n", status=404, mimetype="text/plain")

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
        return _forbidden_response()

    ok, msg = start_os_update_background()
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=msg) + "#admin_details")




# ---------------- Admin: Firewall (UFW) ----------------

@app.route("/admin/ufw/refresh", methods=["POST"])
def admin_ufw_refresh():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()
    ufw_refresh_state()
    return redirect(url_for("index", admin="1", adminmsg=t("ufw.status_refreshed", "Firewall-Status aktualisiert."), adminok="1") + "#admin_details")


@app.route("/admin/ufw/install", methods=["POST"])
def admin_ufw_install():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()
    if ufw_is_installed():
        return redirect(url_for("index", admin="1", adminmsg=t("ufw.already_installed", "UFW ist bereits installiert."), adminok="1") + "#admin_details")
    ok, msg = start_ufw_install_background()
    return redirect(url_for("index", admin="1", adminmsg=msg, adminok=("1" if ok else "0")) + "#admin_details")


@app.route("/admin/ufw/apply_ports", methods=["POST"])
def admin_ufw_apply_ports():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()
    ok, msg = ufw_apply_port_rules()
    # Status NICHT automatisch prüfen – wir aktualisieren nur den Cache (Button ist explizit)
    ufw_refresh_state()
    short = (msg.splitlines()[0] if msg else (t("generic.ok", "OK") if ok else t("generic.error", "Fehler")))
    return redirect(url_for("index", admin="1", adminmsg=short, adminok=("1" if ok else "0")) + "#admin_details")


@app.route("/admin/ufw/enable", methods=["POST"])
def admin_ufw_enable():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()
    ok, msg = ufw_set_enabled(True)
    ufw_refresh_state()
    short = (msg.splitlines()[0] if msg else (t("generic.ok", "OK") if ok else t("generic.error", "Fehler")))
    return redirect(url_for("index", admin="1", adminmsg=short, adminok=("1" if ok else "0")) + "#admin_details")


@app.route("/admin/ufw/disable", methods=["POST"])
def admin_ufw_disable():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()
    ok, msg = ufw_set_enabled(False)
    ufw_refresh_state()
    short = (msg.splitlines()[0] if msg else (t("generic.ok", "OK") if ok else t("generic.error", "Fehler")))
    return redirect(url_for("index", admin="1", adminmsg=short, adminok=("1" if ok else "0")) + "#admin_details")
@app.route("/admin/autodarts/check", methods=["POST"])
def admin_autodarts_check():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()

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
            msg = t("autodarts.no_update_available", "Kein Update verfügbar (bereits v{installed}).", installed=installed)
        else:
            msg = t("autodarts.update_available", "Update verfügbar: v{installed} → v{latest}.", installed=installed, latest=latest)
    else:
        msg = t("autodarts.update_check_unavailable", "Update-Check nicht möglich (Version oder Internet nicht verfügbar).")

    return redirect(url_for("index", admin="1", adminmsg=msg, adminok="1") + "#admin_details")


@app.route("/admin/autodarts/update", methods=["POST"])
def admin_autodarts_update():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()

    installed = get_autodarts_version()
    latest = fetch_latest_autodarts_version()

    # Wenn wir sicher wissen, dass es kein Update gibt → nicht starten
    if installed and latest and installed == latest:
        msg = t("autodarts.no_update_available", "Kein Update verfügbar (bereits v{installed}).", installed=installed)
        return redirect(url_for("index", admin="1", adminok="1", adminmsg=msg) + "#admin_details")

    ok, msg = start_autodarts_update_background()
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=msg) + "#admin_details")


@app.route("/admin/webpanel/check", methods=["POST"])
def admin_webpanel_check():
    # Admin muss entsperrt sein
    #if not bool(session.get("admin_unlocked", False)):
        #return _forbidden_response()

    installed = get_webpanel_version()
    latest = fetch_latest_webpanel_version()

    save_webpanel_update_check({
        "ts": int(time.time()),
        "installed": installed,
        "latest": latest,
    })

    if latest and installed and installed == latest:
        msg = t("webpanel.no_update", "Webpanel: Kein Update verfügbar.")
        ok = "1"
    elif latest:
        msg = t("webpanel.update_available", "Webpanel: Update verfügbar ({installed} → {latest}).", installed=(installed or "unknown"), latest=latest)
        ok = "1"
    else:
        msg = t("webpanel.update_check_failed", "Webpanel: Update-Check fehlgeschlagen.")
        ok = "0"

    return redirect(url_for("index", admin="1", adminmsg=msg, adminok=ok) + "#admin_details")


@app.route("/admin/webpanel/update", methods=["POST"])
def admin_webpanel_update():
    # Admin muss entsperrt sein
    #if not bool(session.get("admin_unlocked", False)):
        #return _forbidden_response()

    installed = get_webpanel_version()
    latest = fetch_latest_webpanel_version()

    # Wenn latest nicht ermittelbar: erst prüfen lassen
    if not latest:
        msg = t("webpanel.latest_not_determined", "Webpanel: Konnte 'latest' nicht ermitteln – bitte zuerst 'Update prüfen'.")
        return redirect(url_for("index", admin="1", adminmsg=msg, adminok="0") + "#admin_details")

    if installed and installed == latest:
        msg = t("webpanel.no_update", "Webpanel: Kein Update verfügbar.")
        return redirect(url_for("index", admin="1", adminmsg=msg, adminok="1") + "#admin_details")

    ok, err = start_webpanel_update_background()
    if ok:
        msg = t("webpanel.update_started", "Webpanel-Update gestartet. Die Weboberfläche kann kurz neu starten.")
    else:
        msg = t("webpanel.update_start_failed", "Webpanel-Update konnte nicht gestartet werden: {error}.", error=(err or t("generic.unknown", "unbekannt")))

    return redirect(url_for("index", admin="1", adminmsg=msg, adminok=("1" if ok else "0")) + "#admin_details")



@app.route("/admin/webpanel/uvc-install", methods=["POST"])
def admin_webpanel_uvc_install():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()

    info = get_uvc_backup_info()
    if (not info.get("backup_exists")) and info.get("marker_exists"):
        msg = t("webpanel.uvc_hack_not_started_backup_missing", "UVC Hack nicht gestartet: Kein Original-Backup vorhanden, aber alter UVC-Marker gefunden. Bitte Backup nach {path} kopieren.", path=info.get("backup_dir"))
        return redirect(url_for("index", admin="1", adminok="0", adminmsg=msg) + "#admin_details")

    ok, err = start_webpanel_update_background(mode="uvc-hack", allow_self_update=False)
    msg = t("webpanel.uvc_hack_started", "UVC Hack gestartet. Bitte danach kurz das Webpanel-Log prüfen.") if ok else t("webpanel.uvc_hack_start_failed", "UVC Hack konnte nicht gestartet werden: {error}.", error=(err or t("generic.unknown", "unbekannt")))
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=msg) + "#admin_details")


@app.route("/admin/webpanel/uvc-uninstall", methods=["POST"])
def admin_webpanel_uvc_uninstall():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()

    info = get_uvc_backup_info()
    if not info.get("backup_exists"):
        msg = t("webpanel.uvc_uninstall_not_started_backup_missing", "UVC Hack wurde NICHT deinstalliert: Kein lokales Original-Backup vorhanden. Bitte Backup nach {path} kopieren.", path=info.get("backup_dir"))
        return redirect(url_for("index", admin="1", adminok="0", adminmsg=msg) + "#admin_details")

    ok, err = start_webpanel_update_background(mode="uvc-uninstall", allow_self_update=False)
    msg = t("webpanel.uvc_uninstall_started", "UVC Deinstallation gestartet. Der Mini PC startet danach automatisch neu.") if ok else t("webpanel.uvc_uninstall_start_failed", "UVC Deinstallation konnte nicht gestartet werden: {error}.", error=(err or t("generic.unknown", "unbekannt")))
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=msg) + "#admin_details")


@app.route("/admin/wled-update", methods=["POST"])
def admin_wled_update():
    # Admin muss entsperrt sein
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()

    ok, msg = start_extensions_update_background("all")
    return redirect(url_for("index", admin="1", adminok=("1" if ok else "0"), adminmsg=(msg or "")) + "#admin_details")




@app.route("/admin/gpio-image", methods=["GET"])
def admin_gpio_image():
    if not bool(session.get("admin_unlocked", False)):
        return _forbidden_response()

    if os.path.exists(ADMIN_GPIO_IMAGE):
        return send_file(ADMIN_GPIO_IMAGE, mimetype="image/jpeg")
    return _inline_notice_page(
        t("admin.gpio_image_missing_title", "GPIO Bild nicht gefunden"),
        (
            f"<p>{t('downloads.file_missing', 'Datei fehlt')}: <code>{ADMIN_GPIO_IMAGE}</code></p>"
            f"<p><a style='color:#3b82f6' href='{url_for('index')}'>{t('generic.back', 'Zurück')}</a></p>"
        ),
        status=404,
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

    if not cameras:
        return redirect(url_for("index", msg=t("camera.none_connected", "Keine Kamera erkannt. Bitte Kamera anschließen und erneut versuchen.")))

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
    """Kamera-Einstellung starten: Nur bei gefundener Kamera in den Einstellmodus wechseln."""
    cfg = load_cam_config()
    cameras = detect_camera_inventory(MAX_CAMERAS)
    slots = _normalize_camera_slots(cameras, cfg.get("camera_slots"))
    cfg["camera_inventory"] = cameras
    cfg["camera_slots"] = slots
    cfg["desired_cams"] = len(cameras)
    cfg["devices"] = _devices_for_slots(cameras, slots)

    if not cameras:
        _set_camera_mode_state(cfg, False)
        save_cam_config(cfg)
        return redirect(url_for("index", msg=t("camera.none_connected", "Keine Kamera erkannt. Bitte Kamera anschließen und erneut versuchen.")))

    subprocess.run(["systemctl", "stop", AUTODARTS_SERVICE], capture_output=True, text=True)
    subprocess.run(["pkill", "-f", "mjpg_streamer"], capture_output=True, text=True)

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
        return jsonify(ok=False, msg=t("wifi.scan_timeout", "WLAN-Scan dauert zu lange (Timeout). Bitte erneut versuchen.")), 504
    except Exception as e:
        return jsonify(ok=False, msg=t("wifi.scan_failed1", "WLAN-Scan fehlgeschlagen: {error}", error=e)), 500

    if r.returncode != 0:
        return jsonify(ok=False, msg=t("wifi.nmcli_error", "nmcli Fehler: {error}", error=interpret_nmcli_error(r.stdout, r.stderr))), 500

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
            message = t("wifi.enter_ssid", "Bitte WLAN-Namen (SSID) eingeben.")
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
                message = t("wifi.create_connection_failed", "Fehler beim Anlegen der WLAN-Verbindung: {error}", error=interpret_nmcli_error(add.stdout, add.stderr))
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
                    message = t("wifi.connected_successfully", "Erfolgreich mit WLAN verbunden.")
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
                            message = t(
                                "wifi.retry_success_after_reset",
                                "Verbindung fehlgeschlagen, wird erneut versucht ...\nDer zweite Versuch war erfolgreich. (Hinweis: WLAN-USB-Stick wurde kurz neu initialisiert.)"
                            )
                            success = True
                        else:
                            message = t(
                                "wifi.retry_failed_after_reset",
                                "Verbindung fehlgeschlagen, wird erneut versucht ...\nAuch der zweite Versuch ist fehlgeschlagen: {error}",
                                error=interpret_nmcli_error(up2.stdout, up2.stderr),
                            )
                    else:
                        message = t("wifi.connect_failed", "Verbindung konnte nicht hergestellt werden: {error}", error=interpret_nmcli_error(up.stdout, up.stderr))

    # Aktuellen Status des WLAN-Dongles anzeigen
    ssid_cur, ip_cur = get_wifi_status()
    wifi_signal = get_wifi_signal_percent()
    if ssid_cur and ip_cur:
        current_info = t("wifi.current_info_connected", "Aktuell verbunden mit <strong>{ssid}</strong> (IP {ip})", ssid=ssid_cur, ip=ip_cur) + (t("wifi.current_info_signal", " · Signal: <strong>{signal}%</strong>.", signal=wifi_signal) if wifi_signal is not None else ".")
    elif ssid_cur and not ip_cur:
        current_info = t("wifi.current_info_connected_no_ipv4", "WLAN verbunden mit <strong>{ssid}</strong>, aber es wurde keine IPv4-Adresse vergeben.", ssid=ssid_cur) + (t("wifi.current_info_signal_paren", " (Signal: <strong>{signal}%</strong>)", signal=wifi_signal) if wifi_signal is not None else "")
    else:
        current_info = t("wifi.current_info_not_connected", "Der USB-Dongle ist aktuell mit keinem WLAN verbunden.")

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
        flash(t("admin.unlock_first", "Bitte zuerst im Admin-Bereich entsperren."), "warning")
        return redirect(url_for("index", admin="1") + "#admin_details")

    enable = (mode or "").lower() in ("on", "enable", "yes", "1", "true")
    conn = _active_wifi_connection_name(WIFI_INTERFACE)
    if not conn:
        flash(t("wifi.no_active_profile_on_uplink", "Kein aktives WLAN-Profil auf dem Internet-WLAN-Interface gefunden."), "warning")
        return redirect(url_for("wifi"))

    try:
        cmd = ["nmcli", "connection", "modify", conn, "connection.autoconnect", ("yes" if enable else "no")]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=6.0)
        if r.returncode == 0:
            flash(t("wifi.autoconnect_set", "Autoconnect {state} für: {conn}", state=(t("generic.enabled", "aktiviert") if enable else t("generic.disabled", "deaktiviert")), conn=conn), "success")
        else:
            err = (r.stderr or r.stdout or "").strip()
            flash(t("wifi.autoconnect_set_failed", "Autoconnect konnte nicht gesetzt werden: {error}", error=(err or t("generic.unknown", "unbekannt"))), "danger")
    except Exception as e:
        flash(t("wifi.autoconnect_error", "Autoconnect Fehler: {error}", error=e), "danger")

    return redirect(url_for("wifi"))


@app.route("/wifi/forget-saved", methods=["POST"])
def wifi_forget_saved():
    # Schutz: nur im Admin-Modus
    if not bool(session.get("admin_unlocked", False)):
        flash(t("admin.unlock_first", "Bitte zuerst im Admin-Bereich entsperren."), "warning")
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
            flash(t("wifi.saved_connections_deleted", "Gespeicherte WLAN-Verbindungen gelöscht: {names}", names=", ".join(deleted)), "success")
        else:
            flash(t("wifi.no_saved_connections_found", "Keine gespeicherten WLAN-Verbindungen gefunden (außer Autodarts-AP)."), "info")
    except Exception as e:
        flash(t("wifi.delete_saved_connections_failed", "Konnte gespeicherte WLANs nicht löschen: {error}", error=e), "danger")

    return redirect(url_for("wifi"))
@app.route("/autoupdate/toggle", methods=["POST"])
def autoupdate_toggle():
    """Legacy Toggle (für alte Links)."""
    cur = autodarts_autoupdate_is_enabled()
    if cur is None:
        return redirect(url_for("index", msg=t("autoupdate.service_not_found", "Auto-Update Service nicht gefunden.")))
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
        return redirect(url_for("index", msg=t("generic.invalid_mode", "Ungültiger Modus."), open_adver="1") + "#ad-version")

    cur = autodarts_autoupdate_is_enabled()

    # AUS
    if not desired:
        ok, msg = autodarts_set_autoupdate(False)
        return redirect(url_for("index", msg=msg, open_adver="1") + "#ad-version")

    # AN
    if cur is True:
        return redirect(url_for("index", msg=t("autoupdate.already_on", "Auto-Update ist bereits AN."), open_adver="1") + "#ad-version")
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
        return redirect(url_for("index", msg=t("autoupdate.activation_started", "Aktivierung gestartet (Installer erstellt Auto-Update Service).")))
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
        return redirect(url_for("index", msg=t("autodarts.select_version", "Bitte eine Version auswählen."), open_adver="1") + "#ad-version")

    stable = autodarts_stable_from_menu()

    special: str | None = None
    req_label = ""

    # "stable" => erste SemVer aus Liste
    if v_raw.lower() == "stable":
        if not stable:
            return redirect(url_for("index", msg=t("autodarts.no_stable_version_defined", "Keine stabile Version hinterlegt. Bitte oben in AUTODARTS_VERSION_MENU eine feste Version (z.B. 1.0.4) eintragen."), open_adver="1") + "#ad-version")
        selected = stable
        req_label = f"Stabil ({stable})"
    else:
        # Nur freigegebene Dropdown-Werte erlauben
        allowed = {str(opt.get("value")) for opt in get_autodarts_versions_choices()}
        if v_raw not in allowed:
            return redirect(url_for("index", msg=t("autodarts.version_not_allowed", "Diese Version ist nicht freigegeben. Bitte über das Dropdown auswählen."), open_adver="1") + "#ad-version")

        selected = v_raw
        if selected in ("__LATEST__", "__LAST__"):
            special = selected

        if special == "__LATEST__":
            latest = autodarts_latest_cached()
            req_label = t("autodarts.latest_online", "Aktuellste (online: {latest})", latest=latest) if latest else t("autodarts.latest_short", "Aktuellste")
        elif special == "__LAST__":
            last = autodarts_last_version()
            req_label = t("autodarts.last_rollback", "Zuletzt (Rollback: {last})", last=last) if last else t("autodarts.last_rollback_short", "Zuletzt (Rollback)")
        else:
            if stable and selected == stable:
                req_label = t("autodarts.stable_label", "Stabil ({version})", version=selected)
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
            return redirect(url_for("index", msg=t("autodarts.rollback_not_possible", "Rollback nicht möglich: Es ist noch keine 'zuletzt'-Version gespeichert."), open_adver="1") + "#ad-version")

        # Toggle-Verhalten: aktuelle Version als 'zuletzt' merken, damit man wieder zurück kann
        if installed and _SEMVER_RE.match(installed):
            autodarts_set_last_version(installed)

        cmd = f"bash <(curl -sL get.autodarts.io) -u {target}"
        req = target

    else:
        v = (str(selected) or "").strip().lstrip("v")
        if not _SEMVER_RE.match(v):
            return redirect(url_for("index", msg=t("autodarts.invalid_version", "Ungültige Versionsangabe."), open_adver="1") + "#ad-version")

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
        return redirect(url_for("index", msg=t("autodarts.install_update_started", "Autodarts Install/Update gestartet (Ziel: {target}).", target=req_label), open_adver="1") + "#ad-version")
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
        return jsonify({"ok": False, "msg": t("jobs.not_found", "Job nicht gefunden.")}), 404

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
            message = t("ap.select_name", "Bitte einen Access-Point-Namen auswählen.")
        elif new_ssid not in ap_choices:
            message = t("generic.invalid_selection", "Ungültige Auswahl. Bitte einen Namen aus der Liste wählen.")
        elif len(new_ssid) > 32:
            message = t("ap.name_too_long", "Der Access-Point-Name ist zu lang (max. 32 Zeichen).")
        else:
            res = subprocess.run(
                ["nmcli", "connection", "modify", AP_CONNECTION_NAME, "802-11-wireless.ssid", new_ssid],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                message = t("ap.rename_failed", "Fehler beim Ändern des Access-Point-Namens: {error}", error=interpret_nmcli_error(res.stdout, res.stderr))
            else:
                subprocess.run(["nmcli", "connection", "down", AP_CONNECTION_NAME], capture_output=True, text=True)
                subprocess.run(["nmcli", "connection", "up", AP_CONNECTION_NAME], capture_output=True, text=True)
                success = True
                current_ssid = new_ssid
                message = t("ap.renamed", "Access-Point-Name wurde geändert auf „{ssid}“.", ssid=new_ssid)

    return render_template(
        "ap_config.html",
        message=message,
        success=success,
        current_ssid=current_ssid,
        ap_connection_name=AP_CONNECTION_NAME,
        ap_choices=ap_choices,
        selected_ssid=selected_ssid,
    )



def _camera_stream_context(cam_id: int):
    """
    Bereitet den Live-Stream für eine Kamera vor und liefert alle nötigen Daten
    für Live-View oder Präzisions-Fokus.
    """
    cam_config = load_cam_config()
    autodarts_active = is_autodarts_active()
    camera_mode = _camera_mode_runtime_active(cam_config, autodarts_active=autodarts_active)

    if not camera_mode:
        if bool(cam_config.get("camera_mode", False)):
            _set_camera_mode_state(cam_config, False)
            save_cam_config(cam_config)
        return {
            "error": redirect(url_for("index", msg=t("camera.mode_not_active_text", "Bitte gehen Sie zurück und klicken Sie zuerst auf Kamera einstellen.")))
        }

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
        return {
            "error": redirect(url_for("index", msg=t("camera.run_detection_again", "Bitte gehen Sie zurück zur Hauptseite und führen Sie die Kamera-Erkennung erneut aus.")))
        }

    slot = cam_slots[cam_id - 1]
    selected_camera_id = str(slot.get("camera_id") or "")
    cam_entry = next((cam for cam in live_inventory if str(cam.get("id") or "") == selected_camera_id), None)
    if not cam_entry and 0 <= (cam_id - 1) < len(live_inventory):
        cam_entry = live_inventory[cam_id - 1]

    if not cam_entry:
        return {
            "error": redirect(url_for("index", msg=t("camera.saved_assignment_unresolved", "Die gespeicherte Kamera-Zuordnung konnte nicht mehr aufgelöst werden.")))
        }

    dev = str(cam_entry.get("preferred_dev") or "").strip()
    if not dev:
        return {
            "error": redirect(url_for("index", msg=t("camera.no_valid_video_device", "Für diese Kamera konnte aktuell kein gültiges /dev/videoX gefunden werden.")))
        }

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
                    fmts = f"<p><strong>{t('camera.detected_formats', 'Erkannte Formate')}:</strong> {fmts}</p>"
                if probe.get("formats") and ("MJPG" not in probe["formats"]) and ("YUYV" not in probe["formats"]):
                    hint = (
                        f"<p style='color:#ffb347'><strong>{t('generic.note', 'Hinweis')}:</strong> "
                        f"{t('camera.no_mjpg_yuyv_hint', 'Die Kamera bietet kein MJPG/YUYV an (z.B. nur H264). In diesem Fall kann mjpg_streamer oft keinen MJPEG-Stream erzeugen.')}</p>"
                    )
            else:
                err = (probe.get("error") or "").strip()
                if err:
                    fmts = f"<p><strong>{t('camera.v4l2_probe_error', 'v4l2 Probe-Fehler')}:</strong> {err}</p>"

            return {
                "error": _inline_notice_page(
                    t("camera.stream_start_failed_title", "Fehler: Kamera-Stream konnte nicht gestartet werden"),
                    (
                        f"<p>{t('camera.device_label', 'Device')}: <code>{dev}</code></p>"
                        f"<p>{t('camera.mjpg_streamer_input', 'mjpg_streamer Input')}: <code>{input_args}</code></p>"
                        f"{fmts}"
                        f"{hint}"
                        f"<p>{t('camera.log_last_lines', 'Log (letzte Zeilen)')}:</p>"
                        f"<pre style='white-space:pre-wrap;background:#0b0b0b;border:1px solid #333;padding:12px;border-radius:10px'>{tail}</pre>"
                        f"<p><a style='color:#3b82f6' href='{url_for('index')}'>{t('generic.back', 'Zurück')}</a></p>"
                    ),
                    status=500,
                )
            }

    except FileNotFoundError:
        return {
            "error": _inline_notice_page(
                t("camera.mjpg_streamer_missing_title", "Fehler: mjpg_streamer nicht gefunden"),
                (
                    f"<p>{t('camera.mjpg_streamer_missing_text', 'Bitte installieren Sie mjpg-streamer oder passen Sie den Aufruf im Script an.')}</p>"
                    f"<p><a href='{url_for('index')}'>{t('generic.back', 'Zurück')}</a></p>"
                ),
                status=500,
            )
        }

    host = request.host.split(":", 1)[0]
    stream_url = f"http://{host}:{port}/?action=stream"
    proxy_stream_url = url_for("cam_focus_proxy", cam_id=cam_id)

    return {
        "cam_id": cam_id,
        "dev": dev,
        "port": port,
        "stream_url": stream_url,
        "proxy_stream_url": proxy_stream_url,
    }


@app.route("/cam/<int:cam_id>", methods=["GET"])
def cam_view(cam_id: int):
    ctx = _camera_stream_context(cam_id)
    if ctx.get("error"):
        return ctx["error"]

    return render_template(
        "cam_view.html",
        cam_id=cam_id,
        stream_url=ctx["stream_url"],
    )


@app.route("/cam/<int:cam_id>/focus", methods=["GET"])
def cam_focus(cam_id: int):
    ctx = _camera_stream_context(cam_id)
    if ctx.get("error"):
        return ctx["error"]

    return render_template(
        "cam_focus.html",
        cam_id=cam_id,
        stream_url=ctx["stream_url"],
        proxy_stream_url=ctx["proxy_stream_url"],
        focus_pdf_url=url_for("siemens_star_pdf"),
    )


@app.route("/cam/<int:cam_id>/proxy.mjpg", methods=["GET"])
def cam_focus_proxy(cam_id: int):
    port = STREAM_BASE_PORT + (cam_id - 1)
    upstream_url = f"http://127.0.0.1:{port}/?action=stream"

    def generate():
        resp = None
        try:
            req = urllib.request.Request(upstream_url, headers={"User-Agent": "AutodartsWebPanel/1.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                yield chunk
        except Exception:
            return
        finally:
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass

    return Response(
        stream_with_context(generate()),
        mimetype="multipart/x-mixed-replace; boundary=boundarydonotcross",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.route("/tools/siemens-star.pdf", methods=["GET"])
def siemens_star_pdf():
    pdf_path = SIEMENS_STAR_DIR / SIEMENS_STAR_FILENAME
    if not pdf_path.exists():
        return _inline_notice_page(
            t("camera.focus_target_missing_title", "Fokusziel-PDF nicht gefunden"),
            (
                f"<p>{t('camera.focus_target_missing_text', 'Die Datei wurde nicht gefunden unter: {path}', path=str(pdf_path))}</p>"
                f"<p><a href='{url_for('index')}'>{t('generic.back', 'Zurück')}</a></p>"
            ),
            status=404,
        )

    download = request.args.get("download", "").strip().lower() in {"1", "true", "yes", "download"}
    resp = send_from_directory(
        str(SIEMENS_STAR_DIR),
        SIEMENS_STAR_FILENAME,
        as_attachment=download,
        download_name=SIEMENS_STAR_FILENAME,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
