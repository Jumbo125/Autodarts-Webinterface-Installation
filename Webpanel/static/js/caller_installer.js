(function () {
  function byId(id) {
    return document.getElementById(id);
  }

  function t(key, fallback) {
    if (window.i18n && typeof window.i18n.t === 'function') {
      const value = window.i18n.t(key);
      if (value && value !== key) return value;
    }
    return fallback;
  }

  document.addEventListener('DOMContentLoaded', function () {
    const installBtn = byId('callerInstallPiBtn');
    const status = byId('callerInstallerStatus');
    if (!installBtn || !status) return;

    installBtn.addEventListener('click', async function () {
      const confirmed = window.confirm(t('caller.confirm_pi_install', 'Caller jetzt auf diesem Raspberry herunterladen und auf dem Desktop entpacken?'));
      if (!confirmed) return;

      installBtn.disabled = true;
      status.textContent = t('caller.install_running', 'Caller wird heruntergeladen und installiert ...');
      status.className = 'hint';

      try {
        const res = await fetch('/api/caller/install-local', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ action: 'install' })
        });
        const data = await res.json().catch(function () { return {}; });
        if (!res.ok || !data.ok) {
          throw new Error(data.message || t('caller.install_failed', 'Caller konnte nicht installiert werden.'));
        }
        status.textContent = data.message || t('caller.install_success', 'Caller wurde erfolgreich installiert.');
        status.className = 'msg-ok';
      } catch (err) {
        status.textContent = (err && err.message) ? err.message : t('caller.install_failed', 'Caller konnte nicht installiert werden.');
        status.className = 'msg-bad';
      } finally {
        installBtn.disabled = false;
      }
    });
  });
})();
