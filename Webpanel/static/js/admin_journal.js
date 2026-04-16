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
  let unit = "darts-caller.service";
  let es = null;
  let paused = false;
  const logEl = document.getElementById("log");

  function appendLine(s) {
    if (paused) return;
    if (!s) return;
    logEl.textContent += s + "";
    logEl.scrollTop = logEl.scrollHeight;
  }

  function connect() {
    if (es) {
      es.close();
      es = null;
    }
    appendLine(
      t('journal.connecting', '---- connecting: ') +
      unit +
      t('journal.connecting_suffix', ' ----')
    );
    es = new EventSource("/admin/journal/stream?unit=" + encodeURIComponent(unit));
    es.onmessage = (ev) => appendLine(ev.data);
    es.onerror = () => {
      appendLine(t('journal.disconnected_retrying', '---- disconnected (retrying) ----'));
      try { es.close(); } catch (e) {}
      es = null;
      if (!paused) {
        setTimeout(connect, 1200);
      }
    };
  }

  document.getElementById("tabs").addEventListener("click", (e) => {
    const a = e.target.closest("a[data-unit]");
    if (!a) return;
    e.preventDefault();
    unit = a.getAttribute("data-unit");
    [...document.querySelectorAll("#tabs a")].forEach(x => x.classList.remove("active"));
    a.classList.add("active");
    connect();
  });

  document.getElementById("btnPause").addEventListener("click", (e) => {
    e.preventDefault();
    paused = !paused;
    e.target.textContent = paused
      ? t('journal.continue', '▶ Continue')
      : t('journal.pause', '⏸ Pause');
    if (!paused && !es) connect();
  });

  document.getElementById("btnClear").addEventListener("click", (e) => {
    e.preventDefault();
    logEl.textContent = "";
  });

  document.getElementById("btnClose").addEventListener("click", (e) => {
    e.preventDefault();
    try { if (es) es.close(); } catch (err) {}
    window.close();
    setTimeout(() => { window.location.href = "/"; }, 100);
  });

  connect();
});