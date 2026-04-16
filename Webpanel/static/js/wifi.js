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
  const form = document.getElementById('wifiForm');
  if (!form) return;

  form.addEventListener('submit', function () {
    const msg = document.getElementById('workmsg');
    if (msg) msg.style.display = 'block';
    const btn = form.querySelector('button[type="submit"]');
    if (btn) {
      btn.disabled = true;
      btn.textContent = t('wifi.please_wait', 'Bitte warten…');
    }
  });

  const pick = document.getElementById('ssidPick');
  const ssidInput = document.getElementById('ssid');
  const manual = document.getElementById('ssidManual');
  const refreshBtn = document.getElementById('wifiRefresh');
  const ssidLabel = document.getElementById('ssidLabel');

  function setManualMode(on) {
    if (on) {
      if (pick) {
        pick.style.display = 'none';
        pick.required = false;
      }
      if (refreshBtn) refreshBtn.style.display = 'none';
      if (ssidLabel) ssidLabel.style.display = '';
      if (ssidInput) {
        ssidInput.style.display = '';
        ssidInput.readOnly = false;
        ssidInput.required = true;
        ssidInput.focus();
      }
    } else {
      if (pick) {
        pick.style.display = '';
        pick.required = true;
      }
      if (refreshBtn) refreshBtn.style.display = '';
      if (ssidLabel) ssidLabel.style.display = 'none';
      if (ssidInput) {
        ssidInput.style.display = 'none';
        ssidInput.readOnly = true;
        ssidInput.required = false;
      }
    }
  }

  async function loadWifi() {
    if (!pick) return;
    pick.innerHTML = `<option>${t('wifi.searching_networks', 'Suche WLANs…')}</option>`;

    try {
      const r = await fetch('/api/wifi/scan', { cache: 'no-store' });
      const j = await r.json();
      if (!j.ok) {
        throw new Error(j.msg || t('wifi.scan_failed', 'Scan fehlgeschlagen'));
      }

      pick.innerHTML = '';
      const nets = j.networks || [];

      if (nets.length === 0) {
        pick.innerHTML = `<option value="">${t('wifi.no_ssids_found', '(Keine SSIDs gefunden – ggf. Refresh oder manuell)')}</option>`;
        setManualMode(true);
        if (manual) manual.checked = true;
        return;
      }

      const ph = document.createElement('option');
      ph.value = '';
      ph.textContent = t('wifi.select_network_placeholder', '(WLAN auswählen…)');
      pick.appendChild(ph);

      for (const n of nets) {
        const opt = document.createElement('option');
        opt.value = n.ssid;
        const sec = n.security ? n.security : t('wifi.security_open', 'open');
        const star = n.in_use ? '★ ' : '';
        opt.textContent = `${star}${n.ssid} (${n.signal}%, ${sec})`;
        if (n.in_use) opt.selected = true;
        pick.appendChild(opt);
      }

      if (pick.value) {
        ssidInput.value = pick.value;
      } else {
        const sel = pick.querySelector('option[selected]');
        if (sel && sel.value) {
          pick.value = sel.value;
          ssidInput.value = sel.value;
        }
      }

      setManualMode(false);
      if (manual) manual.checked = false;
    } catch (e) {
      setManualMode(true);
      if (manual) manual.checked = true;
      if (pick) {
        pick.innerHTML = `<option value="">${t('wifi.scan_failed_manual', '(Scan fehlgeschlagen – manuell eingeben)')}</option>`;
      }
      console.log(e);
    }
  }

  if (manual) {
    manual.addEventListener('change', () => setManualMode(manual.checked));
  }

  if (pick) {
    pick.addEventListener('change', () => {
      ssidInput.value = pick.value || '';
    });
  }

  if (refreshBtn) {
    refreshBtn.addEventListener('click', loadWifi);
  }
	
	
	const passwordInput = document.getElementById('password');
	const showPassword = document.getElementById('showPassword');

	
	showPassword.addEventListener("change", function () {
	  if (this.checked) {
		passwordInput.type = "text";
	  } else {
		passwordInput.type = "password";
	  }
	});
	
  loadWifi();
});