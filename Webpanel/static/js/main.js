/*
 * Copyright (c) 2026 Peter Rottmann
 * All rights reserved.
 * Proprietary and not open source.
 * No use, modification, distribution, publication, sublicensing,
 * or commercial use without prior express written permission.
 * Applies only to parts created by Peter Rottmann.
 * Third-party components remain under their respective licenses.
 * Provided "as is", without warranty.
 */

document.addEventListener('DOMContentLoaded', () => {
  const appUrls = window.app_urls || {};
  const appData = window.app_data || {};

  initPingUi();
  initAdminDetailsAutoOpen();
  initWledUi(appUrls);
  initPiMonitorUi(appData);
  initApClientInternetUi(appUrls);
});


/* =========================================================
   1) Kleine Hilfsfunktion für Übersetzungen in JS
   ========================================================= */
function tr(key, fallback, vars) {
  if (typeof window.t === 'function') {
    return window.t(key, fallback, vars);
  }

  let text = (fallback === undefined || fallback === null) ? key : String(fallback);

  if (vars && typeof vars === 'object') {
    Object.keys(vars).forEach(function(varKey) {
      const regex = new RegExp('\\{' + varKey + '\\}', 'g');
      text = text.replace(regex, vars[varKey]);
    });
  }

  return text;
}


/* =========================================================
   2) Admin-Details automatisch öffnen
   ========================================================= */
function initAdminDetailsAutoOpen() {
  try {
    const p = new URLSearchParams(window.location.search);
    if (window.location.hash === '#admin_details' || p.get('admin') === '1' || p.get('adminerr') === '1') {
      const d = document.getElementById('admin_details');
      if (d) d.open = true;
    }
  } catch (e) {}
}


/* =========================================================
   3) WLED UI
   ========================================================= */
function initWledUi(appUrls) {
  const statusUrl = appUrls.api_wled_status;
  if (!statusUrl) return;

  function setStatus(slot, state) {
    const el = document.getElementById('wled_status_' + slot);
    if (!el) return;

    if (state === 'off') {
      el.innerHTML = '<span class="dot dot-gray"></span>' + tr('common.off', 'AUS');
      return;
    }

    if (state === 'checking') {
      el.innerHTML = '<span class="dot dot-gray"></span>' + tr('led.checking', 'Prüfe…');
      return;
    }

    if (state === 'ok') {
      el.innerHTML = '<span class="dot dot-green"></span>' + tr('wled.status_reachable', 'Erreichbar');
      return;
    }

    if (state === 'bad') {
      el.innerHTML = '<span class="dot dot-red"></span>' + tr('wled.status_unreachable', 'Nicht erreichbar');
      return;
    }

    el.innerHTML = '<span class="dot dot-gray"></span>—';
  }

  function storeReachableTargets(data) {
    const reachable = [];

    (data.bands || []).forEach((b) => {
      if (!b.enabled || b.online !== true) return;

      const slot = Number(b.slot || 0);
      const hostInput = document.getElementById('wled_host_' + slot);
      const host = hostInput ? String(hostInput.value || '').trim() : '';
      if (!host) return;

      reachable.push({
        slot,
        host,
        label: tr('wled.target_label', 'LED Band {slot} – {host}', {
          slot: slot,
          host: host
        })
      });
    });

    try {
      localStorage.setItem('reachableWledTargets', JSON.stringify(reachable));
    } catch (e) {}
  }

  async function refresh() {
    try {
      const r = await fetch(statusUrl, { cache: 'no-store' });
      const data = await r.json();

      (data.bands || []).forEach((b) => {
        if (!b.enabled) return setStatus(b.slot, 'off');
        if (b.online === true) return setStatus(b.slot, 'ok');
        if (b.online === false) return setStatus(b.slot, 'bad');
        setStatus(b.slot, 'checking');
      });

      storeReachableTargets(data);
    } catch (e) {}
  }

  refresh();

  const presetsBtn = document.getElementById('openPresetsBtn');
  if (presetsBtn) {
    presetsBtn.addEventListener('click', async (ev) => {
      if (presetsBtn.classList.contains('btn-disabled') || presetsBtn.getAttribute('aria-disabled') === 'true') {
        ev.preventDefault();
        return;
      }

      try {
        const r = await fetch(statusUrl, { cache: 'no-store' });
        const data = await r.json();
        storeReachableTargets(data);
      } catch (e) {}
    });
  }

  document.querySelectorAll('a[id^="wled_cfgbtn_"]').forEach((a) => {
    a.addEventListener('click', (ev) => {
      if (a.classList.contains('btn-disabled') || a.getAttribute('aria-disabled') === 'true') {
        ev.preventDefault();
      }
    });
  });

  document.querySelectorAll('input[type=checkbox][name^=wled_enabled_]').forEach((cb) => {
    cb.addEventListener('change', async () => {
      const slot = (cb.name || '').split('_').pop();
      if (!slot) return;

      setStatus(slot, cb.checked ? 'checking' : 'off');

      const cfgBtn = document.getElementById('wled_cfgbtn_' + slot);
      if (cfgBtn) {
        if (cb.checked) {
          cfgBtn.classList.remove('btn-disabled');
          cfgBtn.removeAttribute('aria-disabled');
        } else {
          cfgBtn.classList.add('btn-disabled');
          cfgBtn.setAttribute('aria-disabled', 'true');
        }
      }

      cb.disabled = true;

      try {
        const body = new URLSearchParams();
        body.set('enabled', cb.checked ? '1' : '0');

        const r = await fetch('/wled/set-enabled/' + slot, { method: 'POST', body });
        const j = await r.json().catch(() => ({ ok: false, msg: '' }));

        if (!j.ok) {
          cb.checked = !cb.checked;
          setStatus(slot, cb.checked ? 'checking' : 'off');

          if (cfgBtn) {
            if (cb.checked) {
              cfgBtn.classList.remove('btn-disabled');
              cfgBtn.removeAttribute('aria-disabled');
            } else {
              cfgBtn.classList.add('btn-disabled');
              cfgBtn.setAttribute('aria-disabled', 'true');
            }
          }
        }
      } catch (e) {
        cb.checked = !cb.checked;
        setStatus(slot, cb.checked ? 'checking' : 'off');

        if (cfgBtn) {
          if (cb.checked) {
            cfgBtn.classList.remove('btn-disabled');
            cfgBtn.removeAttribute('aria-disabled');
          } else {
            cfgBtn.classList.add('btn-disabled');
            cfgBtn.setAttribute('aria-disabled', 'true');
          }
        }
      } finally {
        cb.disabled = false;
        setTimeout(refresh, 250);
      }
    });
  });
}


/* =========================================================
   4) Pi Monitor UI
   ========================================================= */
function initPiMonitorUi(appData) {
  const statusEl = document.getElementById('piMonStatusText');
  const startBtn = document.getElementById('piMonStartBtn');
  const stopBtn = document.getElementById('piMonStopBtn');
  const intervalSel = document.getElementById('piMonInterval');
  const durationSel = document.getElementById('piMonDuration');

  if (!statusEl || !startBtn || !stopBtn || !intervalSel || !durationSel) {
    return;
  }

  let pollTimer = null;

  function fmtSeconds(s) {
    s = Math.max(0, Number(s || 0));
    const m = Math.floor(s / 60);
    const r = s % 60;
    if (m <= 0) return r + 's';
    return m + 'm ' + r + 's';
  }

  function setRunningUI(st) {
    const running = !!(st && st.running);

    if (running) {
      startBtn.classList.add('btn-disabled');
      startBtn.disabled = true;
      stopBtn.style.display = 'inline-block';

      const rem = fmtSeconds(st.remaining_sec || 0);
      statusEl.textContent =
        (st.msg || tr('pi_monitor.running', 'Läuft…')) +
        (st.remaining_sec != null ? (' · ' + tr('pi_monitor.remaining', 'Rest:') + ' ' + rem) : '');
    } else {
      startBtn.classList.remove('btn-disabled');
      startBtn.disabled = false;
      stopBtn.style.display = 'none';
      statusEl.textContent = (st && st.msg) ? st.msg : tr('pi_monitor.not_active', 'Nicht aktiv.');
    }
  }

  async function fetchStatus() {
    try {
      const rs = await fetch('/api/pi_monitor/status');
      const st = await rs.json();

      if (st && st.ok === false) {
        setRunningUI({
          running: false,
          msg: st.msg || tr('common.error', 'Fehler')
        });
        return null;
      }

      setRunningUI(st);
      return st;
    } catch (e) {
      setRunningUI({
        running: false,
        msg: tr('pi_monitor.status_unreachable', 'Status nicht erreichbar: {error}', {
          error: String(e)
        })
      });
      return null;
    }
  }

  function startPolling() {
    if (pollTimer) return;

    pollTimer = setInterval(async () => {
      const st = await fetchStatus();
      if (!st || !st.running) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }, 5000);
  }

  startBtn.addEventListener('click', async () => {
    if (startBtn.disabled) return;

    startBtn.classList.add('btn-disabled');
    startBtn.disabled = true;
    statusEl.textContent = tr('overlay.starting', 'Starte…');

    const interval_s = parseInt(intervalSel.value, 10);
    const duration_min = parseInt(durationSel.value, 10);

    try {
      const rs = await fetch('/api/pi_monitor/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ interval_s, duration_min })
      });

      const st = await rs.json();

      if (!st.ok) {
        setRunningUI({
          running: false,
          msg: st.msg || tr('pi_monitor.start_failed', 'Start fehlgeschlagen.')
        });
        return;
      }

      setRunningUI(st);
      startPolling();
    } catch (e) {
      setRunningUI({
        running: false,
        msg: tr('pi_monitor.start_failed_error', 'Start fehlgeschlagen: {error}', {
          error: String(e)
        })
      });
    }
  });

  stopBtn.addEventListener('click', async () => {
    stopBtn.classList.add('btn-disabled');
    stopBtn.disabled = true;

    try {
      await fetch('/api/pi_monitor/stop', { method: 'POST' });
      stopBtn.classList.remove('btn-disabled');
      stopBtn.disabled = false;
      await fetchStatus();
    } catch (e) {
      stopBtn.classList.remove('btn-disabled');
      stopBtn.disabled = false;
      statusEl.textContent = tr('pi_monitor.stop_failed', 'Stop fehlgeschlagen: {error}', {
        error: String(e)
      });
    }
  });

  const initial = appData.pi_mon_status || null;
  setRunningUI(initial);
  if (initial && initial.running) startPolling();
}


/* =========================================================
   5) Ping UI
   ========================================================= */
function initPingUi() {
  const btn = document.getElementById('pingBtn');
  const overlay = document.getElementById('pingOverlay');
  const txt = document.getElementById('pingOverlayText');
  const bar = document.getElementById('pingOverlayBar');
  const out = document.getElementById('pingOverlayOut');
  const titleEl = document.getElementById('pingOverlayTitle');
  const againBtn = document.getElementById('pingOverlayAgain');
  const closeBtn = document.getElementById('pingOverlayClose');

  if (!btn || !overlay || !txt || !bar || !out || !titleEl) {
    return;
  }

  let pingTimer = null;
  let pingRunning = false;
  let pollTries = 0;

  function showOverlay() {
    overlay.style.display = 'flex';
  }

  function hideOverlay(force = false) {
    if (pingRunning && !force) return;
    overlay.style.display = 'none';
  }

  function setProgress(done, total) {
    const pct = total ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
    bar.style.width = pct + '%';
  }

  function setBusy(isBusy) {
    pingRunning = isBusy;

    btn.classList.toggle('btn-disabled', isBusy);
    btn.disabled = isBusy;

    if (againBtn) {
      againBtn.classList.toggle('btn-disabled', isBusy);
      againBtn.disabled = isBusy;
    }

    if (closeBtn) {
      closeBtn.classList.toggle('btn-disabled', isBusy);
      closeBtn.disabled = isBusy;
    }
  }

  function resetUi() {
    titleEl.textContent = tr('ping.running_title', 'Verbindungstest läuft…');
    txt.textContent = tr('overlay.starting', 'Starte…');
    out.textContent = '—';
    setProgress(0, 30);
  }

  function stopPolling() {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  }

  function classifyPingQuality(s, total, recv) {
    const sent = Number(s.count || total || 30);
    const rec = Number(recv || 0);

    if (!sent || sent <= 0) {
      return { level: 'unknown', label: tr('common.unknown', 'Unbekannt'), loss: null };
    }

    const lost = Math.max(0, sent - rec);
    const loss = Math.round((lost * 1000) / sent) / 10;

    const minMs = (s.min_ms != null) ? Number(s.min_ms) : null;
    const maxMs = (s.max_ms != null) ? Number(s.max_ms) : null;
    const avgMs = (s.avg_ms != null) ? Number(s.avg_ms) : null;

    if (minMs === null || maxMs === null || avgMs === null) {
      if (loss === 0) return { level: 'gut', label: tr('ping.quality_good', 'Gute Verbindung'), loss };
      if (loss < 3) return { level: 'grenzwertig', label: tr('ping.quality_ok', 'Könnte funktionieren'), loss };
      return { level: 'nicht_spielbar', label: tr('ping.quality_bad', 'Nicht mehr spielbar'), loss };
    }

    if (loss === 0 && avgMs <= 30 && maxMs <= 60) {
      return { level: 'super', label: tr('ping.quality_super', 'Super Verbindung'), loss };
    }
    if (loss < 1 && avgMs <= 60 && maxMs <= 120) {
      return { level: 'gut', label: tr('ping.quality_good', 'Gute Verbindung'), loss };
    }
    if (loss < 3 && avgMs <= 120 && maxMs <= 300) {
      return { level: 'grenzwertig', label: tr('ping.quality_ok', 'Könnte funktionieren'), loss };
    }
    return { level: 'nicht_spielbar', label: tr('ping.quality_bad', 'Nicht mehr spielbar'), loss };
  }

  async function startPing() {
    if (pingRunning) return;

    resetUi();
    showOverlay();
    setBusy(true);
    pollTries = 0;

    try {
      const r = await fetch('/wifi/ping/start', {
        method: 'POST',
        cache: 'no-store'
      });

      const j = await r.json().catch(() => ({
        ok: false,
        msg: tr('ping.invalid_response', 'Ungültige Antwort')
      }));

      if (!j.ok) {
        titleEl.textContent = tr('ping.failed_title', 'Verbindungstest fehlgeschlagen');
        txt.textContent = j.msg || tr('ping.could_not_start', 'Ping konnte nicht gestartet werden.');
        out.textContent = j.msg || tr('common.error', 'Fehler');
        setBusy(false);
        return;
      }

      const jobId = j.job_id;
      const total = 30;

      pingTimer = setInterval(async () => {
        pollTries += 1;

        try {
          const rs = await fetch('/wifi/ping/status/' + jobId, { cache: 'no-store' });
          const s = await rs.json().catch(() => ({
            ok: false,
            msg: tr('ping.invalid_response', 'Ungültige Antwort')
          }));

          if (!s.ok) {
            stopPolling();
            titleEl.textContent = tr('ping.failed_title', 'Verbindungstest fehlgeschlagen');
            txt.textContent = s.msg || tr('ping.status_error', 'Fehler beim Status.');
            out.textContent = s.msg || tr('common.error', 'Fehler');
            setBusy(false);
            return;
          }

          const prog = Number(s.progress || 0);
          const recv = Number(s.received || 0);

          txt.textContent = tr('ping.progress', '{prog} von {total} Paketen… (empfangen: {recv})', {
            prog: prog,
            total: total,
            recv: recv
          });

          setProgress(prog, total);

          if (s.done) {
            stopPolling();

            const sent = Number(s.count || total);
            const q = classifyPingQuality(s, total, recv);

            let result = tr('ping.result_received', '{recv} von {sent} Paketen wurden erfolgreich empfangen.', {
              recv: recv,
              sent: sent
            });

            if (q.loss != null) {
              result += '\n' + tr('ping.packet_loss', 'Paketverlust: {loss}%', {
                loss: q.loss
              });
            }

            if (s.min_ms != null && s.max_ms != null && s.avg_ms != null) {
              result += '\n' + tr('ping.stats', 'Schnellstes: {min} ms · Langsamstes: {max} ms · Durchschnitt: {avg} ms', {
                min: s.min_ms,
                max: s.max_ms,
                avg: s.avg_ms
              });
            }

            if (s.error) {
              result += '\n' + tr('ping.note', 'Hinweis: {error}', {
                error: s.error
              });
            }

            const via = (s && s.iface_label) ? (String(s.iface_label) + '\n') : '';

            out.textContent = q && q.label
              ? via + tr('ping.quality_result', 'Verbindungsqualität: {label}', { label: q.label }) + '\n' + result
              : via + result;

            titleEl.textContent = tr('ping.completed_title', 'Verbindungstest abgeschlossen');
            txt.textContent = tr('ping.completed_text', 'TEST erfolgreich durchgeführt. Ergebnis: {label}', {
              label: q && q.label ? q.label : tr('common.unknown', 'Unbekannt')
            });

            setProgress(total, total);
            setBusy(false);
          }
        } catch (e) {
          if (pollTries > 120) {
            stopPolling();
            titleEl.textContent = tr('ping.aborted_title', 'Verbindungstest abgebrochen');
            txt.textContent = tr('ping.timeout', 'Timeout.');
            out.textContent = tr('ping.timeout_result', 'Verbindungstest abgebrochen (Timeout).');
            setBusy(false);
          }
        }
      }, 600);
    } catch (e) {
      stopPolling();
      titleEl.textContent = tr('ping.failed_title', 'Verbindungstest fehlgeschlagen');
      txt.textContent = tr('ping.failed_short', 'Fehlgeschlagen.');
      out.textContent = tr('ping.failed_message', 'Verbindungstest fehlgeschlagen.');
      setBusy(false);
    }
  }

  document.addEventListener('keydown', (ev) => {
    if (ev && ev.key === 'Escape') {
      hideOverlay();
    }
  });

  btn.addEventListener('click', startPing);

  if (againBtn) {
    againBtn.addEventListener('click', startPing);
  }

  if (closeBtn) {
    closeBtn.addEventListener('click', () => hideOverlay());
  }
}


/* =========================================================
   6) WLAN Signal abrufen
   ========================================================= */
async function fetchWifiSignal() {
  const out = document.getElementById('wifiSignalOut');
  const btn = document.getElementById('wifiSignalBtn');
  const appUrls = window.app_urls || {};
  const wifiSignalUrl = appUrls.api_wifi_signal;

  if (!wifiSignalUrl) {
    if (out) out.textContent = tr('common.not_available_short', 'n/a');
    return;
  }

  if (btn) {
    btn.classList.add('btn-disabled');
    btn.disabled = true;
  }

  try {
    const r = await fetch(wifiSignalUrl, { cache: 'no-store' });
    const j = await r.json();

    if (out) {
      out.textContent = (j && j.signal !== null && j.signal !== undefined)
        ? (String(j.signal) + '%')
        : tr('common.not_available_short', 'n/a');
    }
  } catch (e) {
    if (out) out.textContent = tr('common.not_available_short', 'n/a');
  } finally {
    if (btn) {
      btn.classList.remove('btn-disabled');
      btn.disabled = false;
    }
  }
}

window.fetchWifiSignal = fetchWifiSignal;


/* =========================================================
   7) AP-Internet für verbundene Geräte
   ========================================================= */
function initApClientInternetUi(appUrls) {
  const statusUrl = appUrls.api_ap_client_internet_status;
  const card = document.getElementById('apClientInternetCard');
  const dot = document.getElementById('apClientInternetDot');
  const textEl = document.getElementById('apClientInternetText');
  const hintEl = document.getElementById('apClientInternetHint');
  if (!statusUrl || !card || !dot || !textEl || !hintEl) return;

function applyState(ok, note) {
  card.classList.remove('ap-internet-green', 'ap-internet-red');
  dot.classList.remove('dot-green', 'dot-red', 'dot-gray');

  const tr = (key, fallback) => {
    try {
      if (typeof window.t === 'function') {
        const v = window.t(key);
        if (v && v !== key) return v;
      }
    } catch (e) {}
    return fallback;
  };

  if (ok) {
    card.classList.add('ap-internet-green');
    dot.classList.add('dot-green');
    textEl.textContent = note || tr('ap_internet.clients_online', 'AP clients have internet access');
    hintEl.textContent = tr('ap_internet.hint_online', 'Green = devices connected to the Raspberry Pi access point can access the internet.');
  } else {
    card.classList.add('ap-internet-red');
    dot.classList.add('dot-red');
    textEl.textContent = note || tr('ap_internet.clients_offline', 'AP clients currently have no internet access');
    hintEl.textContent = tr('ap_internet.hint_offline', 'Red = devices connected to the Raspberry Pi access point currently cannot access the internet.');
  }
}

  async function refresh() {
    try {
      const r = await fetch(statusUrl, { cache: 'no-store' });
      const j = await r.json();
      applyState(!!(j && j.client_internet), j && j.note ? String(j.note) : '');
    } catch (e) {
      applyState(false, 'Status konnte nicht gelesen werden.');
    }
  }

  refresh();
  window.setInterval(refresh, 10000);
}
