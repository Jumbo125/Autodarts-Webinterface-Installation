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
  const st = document.getElementById('st');
  const bar = document.getElementById('bar');
  const out = document.getElementById('out');
  const again = document.getElementById('again');

  function setProgress(done, total) {
    const pct = total ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
    if (bar) bar.style.width = pct + '%';
  }

  function classify(s, total, recv) {
    const sent = Number(s.count || total || 30);
    const rec = Number(recv || 0);
    if (!sent || sent <= 0) return { label: t('wifi_ping.unknown', 'Unbekannt'), loss: null };

    const lost = Math.max(0, sent - rec);
    const loss = Math.round((lost * 1000) / sent) / 10;

    const minMs = (s.min_ms != null) ? Number(s.min_ms) : null;
    const maxMs = (s.max_ms != null) ? Number(s.max_ms) : null;
    const avgMs = (s.avg_ms != null) ? Number(s.avg_ms) : null;

    if (minMs === null || maxMs === null || avgMs === null) {
      if (loss === 0) return { label: t('wifi_ping.quality.good', 'Gute Verbindung'), loss };
      if (loss < 3) return { label: t('wifi_ping.quality.maybe', 'Könnte funktionieren'), loss };
      return { label: t('wifi_ping.quality.unplayable', 'Nicht mehr spielbar'), loss };
    }

    if (loss === 0 && avgMs <= 30 && maxMs <= 60) return { label: t('wifi_ping.quality.super', 'Super Verbindung'), loss };
    if (loss < 1 && avgMs <= 60 && maxMs <= 120) return { label: t('wifi_ping.quality.good', 'Gute Verbindung'), loss };
    if (loss < 3 && avgMs <= 120 && maxMs <= 300) return { label: t('wifi_ping.quality.maybe', 'Könnte funktionieren'), loss };
    return { label: t('wifi_ping.quality.unplayable', 'Nicht mehr spielbar'), loss };
  }

  async function run() {
    if (again) {
      again.classList.add('btn-disabled');
      again.disabled = true;
    }
    if (out) out.textContent = '—';
    if (st) st.textContent = t('wifi_ping.status.starting', 'Starte…');
    setProgress(0, 30);

    try {
      const r = await fetch('/wifi/ping/start', { method: 'POST', cache: 'no-store' });
      const j = await r.json().catch(() => ({ ok: false, msg: t('wifi_ping.invalid_response', 'Ungültige Antwort') }));
      if (!j.ok) {
        if (st) st.textContent = j.msg || t('wifi_ping.start_failed', 'Ping konnte nicht gestartet werden.');
        if (out) out.textContent = (j.msg || t('common.error', 'Fehler'));
        if (again) { again.classList.remove('btn-disabled'); again.disabled = false; }
        return;
      }

      const jobId = j.job_id;
      const total = 30;
      let tries = 0;

      const timer = setInterval(async () => {
        tries += 1;
        try {
          const rs = await fetch('/wifi/ping/status/' + jobId, { cache: 'no-store' });
          const s = await rs.json().catch(() => ({ ok: false, msg: t('wifi_ping.invalid_response', 'Ungültige Antwort') }));
          if (!s.ok) {
            clearInterval(timer);
            if (st) st.textContent = s.msg || t('wifi_ping.status_error', 'Fehler beim Status.');
            if (out) out.textContent = (s.msg || t('common.error', 'Fehler'));
            if (again) { again.classList.remove('btn-disabled'); again.disabled = false; }
            return;
          }

          const prog = Number(s.progress || 0);
          const recv = Number(s.received || 0);
          if (st) {
            st.textContent =
              `${prog} ${t('wifi_ping.of', 'von')} ${total} ${t('wifi_ping.packets_progress', 'Paketen… (empfangen: ')}${recv})`;
          }
          setProgress(prog, total);

          if (s.done) {
            clearInterval(timer);

            const q = classify(s, total, recv);
            let result =
              `${recv} ${t('wifi_ping.of', 'von')} ${Number(s.count || total)} ${t('wifi_ping.result_packets_sent', 'Paketen wurden erfolgreich gesendet.')}`;
            if (q.loss != null) result += ` · ${t('wifi_ping.packet_loss', 'Paketverlust:')} ${q.loss}%`;
            if (s.min_ms != null && s.max_ms != null && s.avg_ms != null) {
              result += ` ${t('wifi_ping.fastest', 'Schnellstes:')} ${s.min_ms} ms · ${t('wifi_ping.slowest', 'Langsamstes:')} ${s.max_ms} ms · ${t('wifi_ping.average', 'Durchschnitt:')} ${s.avg_ms} ms`;
            }
            if (s.error) result += ` (${t('wifi_ping.note', 'Hinweis:')} ${s.error})`;

            const via = (s && s.iface_label) ? (s.iface_label + "\n") : "";
            if (out) out.textContent = via + `${t('wifi_ping.connection_quality', 'Verbindungsqualität:')} ${q.label}\n` + result;
            if (st) st.textContent = t('wifi_ping.done', 'Fertig.');
            setProgress(total, total);

            if (again) { again.classList.remove('btn-disabled'); again.disabled = false; }
          }
        } catch (e) {
          if (tries > 120) {
            clearInterval(timer);
            if (st) st.textContent = t('wifi_ping.timeout', 'Timeout.');
            if (out) out.textContent = t('wifi_ping.aborted_timeout', 'Verbindungstest abgebrochen (Timeout).');
            if (again) { again.classList.remove('btn-disabled'); again.disabled = false; }
          }
        }
      }, 600);

    } catch (e) {
      if (st) st.textContent = t('wifi_ping.failed', 'Fehlgeschlagen.');
      if (out) out.textContent = t('wifi_ping.test_failed', 'Verbindungstest fehlgeschlagen.');
      if (again) { again.classList.remove('btn-disabled'); again.disabled = false; }
    }
  }

  if (again) {
    again.addEventListener('click', run);
  }
  run();
});