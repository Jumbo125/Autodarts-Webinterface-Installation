#!/usr/bin/env python3
from gpiozero import Button, LED
from signal import pause
import subprocess
import time
import threading
import json
from pathlib import Path
from datetime import datetime, timedelta

# ======= KONFIGURATION ANPASSEN, WENN NÖTIG =======
BUTTON_PIN = 17                   # GPIO für Taster (BCM 17, Pin 11)
LED_PIN = 27                      # GPIO für LED / Signalleuchte (BCM 27, Pin 13)

SERVICE_NAME = "autodarts.service"      # Name des Autodarts-Dienstes (ggf. anpassen!)
CAM_CFG_PATH = Path("/var/lib/autodarts/cam-config.json")
# ================================================

# Zeiten (Sekunden)
MIN_SHORT = 0.3       # unter 0,3s: ignorieren
SHUTDOWN_MIN = 3.0    # ab 3s: „Ausschaltmodus scharf“

# ======= LED- / STATUS-TUNING (CPU vs. Reaktionsgeschwindigkeit) =======
LED_SHUTDOWN_BLINK   = 0.5   # Blinkgeschwindigkeit beim tatsächlichen Shutdown
LED_RESTART_SLEEP    = 0.5   # Pause, wenn "service_restarting" aktiv ist
LED_ON_SLEEP         = 0.5   # Pause, wenn Autodarts läuft (LED dauerhaft an)
LED_BLINK_NO_SERVER  = 1.0   # Blink-Periode wenn Autodarts aus, aber Netz verbunden
LED_BLINK_NO_NET     = 2.0   # Blink-Periode wenn Autodarts aus und kein Netz
STATUS_REFRESH_SECONDS = 3.0 # wie oft Autodarts-/Netz-Status neu geprüft wird

LED_ARMED_BLINK      = 0.1   # sehr schnelles Blinken, wenn Shutdown „scharf“ ist
# =======================================================================

# ======= DIAG-LOGGING =======
DIAG_DIR = Path("/tmp/")
RESTART_LOG_PREFIX = "Manueller_Autodarts_reboot"
RESTART_MAX_LOG_SECONDS = 120
RESTART_LOG_INTERVAL_SECONDS = 1.0
# ============================

button = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)
led = LED(LED_PIN)

press_time = None

# Zustände
service_restarting = False
shutting_down = False
shutdown_armed = False
running = True


def run_cmd(cmd, timeout=10):
    """Kommando robust ausführen, ohne das Script hart scheitern zu lassen."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        return 124, out, f"[timeout after {timeout}s]\n{err}"
    except Exception as e:
        return 999, "", f"[exception] {e}"


def append_section(log_path: Path, title: str, text: str):
    with log_path.open("a", encoding="utf-8", errors="ignore") as f:
        f.write(f"--- {title} ---\n")
        if text:
            if not text.endswith("\n"):
                text += "\n"
            f.write(text)
        else:
            f.write("(leer)\n")
        f.write("\n")


def run_and_log(log_path: Path, title: str, cmd, timeout=10):
    rc, out, err = run_cmd(cmd, timeout=timeout)
    body = f"$ {' '.join(cmd)}\n[returncode={rc}]\n"
    if out:
        body += out
        if not out.endswith("\n"):
            body += "\n"
    if err:
        body += "[stderr]\n" + err
        if not err.endswith("\n"):
            body += "\n"
    append_section(log_path, title, body)


def is_autodarts_active() -> bool:
    """Prüfen, ob der Autodarts-Dienst läuft."""
    result = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME],
        capture_output=True,
        text=True
    )
    return result.stdout.strip() == "active"


def is_network_connected() -> bool:
    """
    Prüfen, ob irgendein Netzwerk-Interface (WLAN oder LAN)
    als 'connected' gemeldet wird.
    """
    result = subprocess.run(
        ["nmcli", "-t", "-f", "DEVICE,STATE,TYPE", "device"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        return False

    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        dev, state, devtype = parts[0], parts[1], parts[2]
        if devtype in ("wifi", "ethernet") and state == "connected":
            return True
    return False


def get_main_pid() -> int:
    rc, out, _ = run_cmd(["systemctl", "show", SERVICE_NAME, "-p", "MainPID", "--value"], timeout=5)
    if rc != 0:
        return 0
    try:
        return int((out or "0").strip() or "0")
    except ValueError:
        return 0


def write_snapshot(log_path: Path, phase: str, dmesg_since: str, journal_since: str):
    now = datetime.now().isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8", errors="ignore") as f:
        f.write(f"=== SNAPSHOT {now} phase={phase} ===\n\n")

    run_and_log(
        log_path,
        "service show",
        ["systemctl", "show", SERVICE_NAME, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
         "-p", "ExecMainPID", "-p", "ExecMainStatus", "-p", "Result", "-p", "StateChangeTimestamp"],
        timeout=8,
    )
    run_and_log(log_path, "service status", ["systemctl", "status", SERVICE_NAME, "--no-pager", "-l"], timeout=8)

    pid = get_main_pid()
    if pid > 0:
        run_and_log(
            log_path,
            f"autodarts threads pid={pid}",
            ["ps", "-T", "-p", str(pid), "-o", "pid,tid,stat,pcpu,pmem,comm,wchan:32"],
            timeout=8,
        )
        run_and_log(log_path, f"autodarts open files pid={pid}", ["lsof", "-p", str(pid)], timeout=12)
    else:
        append_section(log_path, "autodarts threads", "Kein MainPID verfügbar.\n")

    run_and_log(log_path, "process overview (autodarts/systemctl/mjpg)", [
        "bash", "-lc",
        "ps -eo pid,ppid,stat,wchan:32,cmd | grep -E 'autodarts|systemctl|mjpg_streamer' | grep -v grep"
    ], timeout=8)

    run_and_log(log_path, "lsusb -t", ["lsusb", "-t"], timeout=8)
    run_and_log(log_path, "lsusb", ["lsusb"], timeout=8)

    run_and_log(
        log_path,
        f"dmesg since {dmesg_since}",
        [
            "bash", "-lc",
            f"dmesg --since '{dmesg_since}' 2>/dev/null | grep -Ei 'usb|uvc|video|xhci|reset|disconnect|timeout|error|hung task|blocked for more than|watchdog' || true"
        ],
        timeout=8,
    )
    run_and_log(
        log_path,
        f"journalctl {SERVICE_NAME} since {journal_since}",
        ["journalctl", "-u", SERVICE_NAME, "--since", journal_since, "--no-pager", "-o", "short-iso"],
        timeout=12,
    )


def create_restart_log_path() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DIAG_DIR / f"{RESTART_LOG_PREFIX}_{ts}.log"


def restart_autodarts():
    """
    Kurz drücken: Autodarts-Dienst neu starten, Kamera-Setup aufräumen und
    Diagnose-Log vor/nach dem Restart mitschreiben.
    """
    global service_restarting
    service_restarting = True
    led.off()

    log_path = create_restart_log_path()
    started = datetime.now()
    dmesg_since = (started - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    journal_since = (started - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")

    with log_path.open("w", encoding="utf-8", errors="ignore") as f:
        f.write("manual_autodarts_reboot\n")
        f.write(f"timestamp_iso={started.isoformat(timespec='seconds')}\n")
        f.write(f"service={SERVICE_NAME}\n")
        f.write(f"button_pin={BUTTON_PIN}\n")
        f.write(f"led_pin={LED_PIN}\n")
        f.write(f"max_log_seconds={RESTART_MAX_LOG_SECONDS}\n")
        f.write(f"log_interval_seconds={RESTART_LOG_INTERVAL_SECONDS}\n\n")

    # Zustand vor dem Restart festhalten
    write_snapshot(log_path, "pre-restart", dmesg_since, journal_since)
    dmesg_since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    journal_since = dmesg_since

    # 1) Alle laufenden Streams stoppen
    run_and_log(log_path, "pkill mjpg_streamer", ["pkill", "-f", "mjpg_streamer"], timeout=5)

    # 2) camera_mode-Flag im JSON zurücksetzen
    cam_info = ""
    try:
        data = {}
        if CAM_CFG_PATH.exists():
            with CAM_CFG_PATH.open("r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {}

        data["camera_mode"] = False

        tmp_path = CAM_CFG_PATH.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        tmp_path.replace(CAM_CFG_PATH)
        cam_info = f"camera_mode=False geschrieben nach {CAM_CFG_PATH}\n"
    except Exception as e:
        cam_info = f"Warnung beim Setzen von camera_mode=False: {e}\n"
        print(f"[autodarts-button] {cam_info.strip()}")

    append_section(log_path, "camera_mode update", cam_info)

    # 3) Restart asynchron anstoßen, damit wir währenddessen weiterloggen können
    try:
        restart_proc = subprocess.Popen(
            ["systemctl", "restart", SERVICE_NAME],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        append_section(log_path, "restart command", "$ systemctl restart autodarts.service\n[started asynchronously]\n")
    except Exception as e:
        append_section(log_path, "restart command", f"Fehler beim Starten von systemctl restart: {e}\n")
        service_restarting = False
        return

    deadline = time.monotonic() + RESTART_MAX_LOG_SECONDS
    success = False
    restart_return_logged = False

    while time.monotonic() < deadline:
        write_snapshot(log_path, "restart-wait", dmesg_since, journal_since)
        now_marker = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dmesg_since = now_marker
        journal_since = now_marker

        active = is_autodarts_active()
        proc_done = restart_proc.poll() is not None

        append_section(
            log_path,
            "restart state",
            f"service_active={active}\nrestart_proc_done={proc_done}\nrestart_proc_returncode={restart_proc.poll()}\n",
        )

        if proc_done and not restart_return_logged:
            out, err = restart_proc.communicate(timeout=1)
            body = f"[returncode={restart_proc.returncode}]\n"
            if out:
                body += out
                if not out.endswith("\n"):
                    body += "\n"
            if err:
                body += "[stderr]\n" + err
                if not err.endswith("\n"):
                    body += "\n"
            append_section(log_path, "restart command result", body)
            restart_return_logged = True

        if active:
            success = True
            break

        time.sleep(RESTART_LOG_INTERVAL_SECONDS)

    if not restart_return_logged and restart_proc.poll() is not None:
        try:
            out, err = restart_proc.communicate(timeout=1)
            body = f"[returncode={restart_proc.returncode}]\n"
            if out:
                body += out
                if not out.endswith("\n"):
                    body += "\n"
            if err:
                body += "[stderr]\n" + err
                if not err.endswith("\n"):
                    body += "\n"
            append_section(log_path, "restart command result", body)
        except Exception as e:
            append_section(log_path, "restart command result", f"Fehler beim Einsammeln der Ausgabe: {e}\n")

    final_phase = "service-active" if success else "timeout"
    write_snapshot(log_path, final_phase, dmesg_since, journal_since)
    append_section(
        log_path,
        "summary",
        f"log_path={log_path}\nservice_active_at_end={is_autodarts_active()}\nrestart_proc_returncode={restart_proc.poll()}\n"
        f"logged_until={datetime.now().isoformat(timespec='seconds')}\n",
    )

    # LED-Loop übernimmt wieder das Anzeigen des Status
    service_restarting = False


def shutdown_pi():
    """>=3s gedrückt UND losgelassen: Pi sauber runterfahren, LED schnell blinken bis Shutdown greift."""
    global shutting_down
    shutting_down = True
    subprocess.run(
        ["shutdown", "-h", "now"],
        capture_output=True
    )
    # Danach ist eh Feierabend, Script wird vom Shutdown gekillt.


def led_manager():
    """
    LED-Logik:
      - shutting_down: schnell blinken (Pi fährt runter)
      - service_restarting: LED aus
      - shutdown_armed + Taste noch gedrückt: extrem schnelles Blinken
      - Autodarts läuft: LED dauerhaft an
      - Autodarts läuft NICHT + Netz verbunden: mittleres Blinken
      - Autodarts läuft NICHT + KEIN Netz: langsames Blinken
    """
    global running, shutdown_armed, press_time

    last_status_check = 0.0
    cached_server_ok = False
    cached_net_ok = False
    blink_state = False

    while running:
        if shutting_down:
            led.toggle()
            time.sleep(LED_SHUTDOWN_BLINK)
            continue

        if service_restarting:
            led.off()
            time.sleep(LED_RESTART_SLEEP)
            continue

        if press_time is not None and not shutdown_armed:
            if time.monotonic() - press_time >= SHUTDOWN_MIN:
                shutdown_armed = True

        if shutdown_armed and button.is_pressed:
            led.toggle()
            time.sleep(LED_ARMED_BLINK)
            continue

        now = time.monotonic()
        if now - last_status_check >= STATUS_REFRESH_SECONDS:
            cached_server_ok = is_autodarts_active()
            cached_net_ok = is_network_connected()
            last_status_check = now

        server_ok = cached_server_ok
        net_ok = cached_net_ok

        if server_ok:
            led.on()
            time.sleep(LED_ON_SLEEP)
        else:
            blink_state = not blink_state
            if blink_state:
                led.on()
            else:
                led.off()

            if net_ok:
                time.sleep(LED_BLINK_NO_SERVER)
            else:
                time.sleep(LED_BLINK_NO_NET)


def on_press():
    global press_time, shutdown_armed
    press_time = time.monotonic()
    shutdown_armed = False


def on_release():
    global press_time, shutdown_armed
    if press_time is None:
        return

    duration = time.monotonic() - press_time
    press_time = None

    armed = shutdown_armed
    shutdown_armed = False

    if duration < MIN_SHORT:
        return

    if duration >= SHUTDOWN_MIN or armed:
        shutdown_pi()
    else:
        restart_autodarts()


button.when_pressed = on_press
button.when_released = on_release

threading.Thread(target=led_manager, daemon=True).start()

pause()
