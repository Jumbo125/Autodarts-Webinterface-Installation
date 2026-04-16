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

  function detectPlatform() {
    const nav = window.navigator || {};
    const ua = String(nav.userAgent || '').toLowerCase();
    const platform = String(nav.platform || '').toLowerCase();
    const maxTouchPoints = Number(nav.maxTouchPoints || 0);

    const isAndroid = /android/.test(ua);
    const isFirefox = /firefox|fxios|fennec/.test(ua);
    const isIOS = /iphone|ipad|ipod/.test(ua) || (platform === 'macintel' && maxTouchPoints > 1);
    const isDesktop = !isAndroid && !isIOS;

    if (isIOS) return 'ios';
    if (isAndroid && isFirefox) return 'android-firefox';
    if (isAndroid) return 'android-other';
    if (isDesktop && isFirefox) return 'desktop-firefox';
    return 'desktop-chromium';
  }

  function setRecommendedButton(button, activeClass) {
    if (!button) return;
    button.classList.add(activeClass);
    button.style.boxShadow = '0 0 0 2px rgba(255,255,255,0.18) inset';
  }

  document.addEventListener('DOMContentLoaded', function () {
    const root = byId('autodartsToolsCard');
    const hint = byId('autodartsToolsPlatformHint');
    const chromeBtn = byId('autodartsToolsBtnChrome');
    const firefoxBtn = byId('autodartsToolsBtnFirefox');
    const iosBtn = byId('autodartsToolsBtnIOS');
    if (!root || !hint) return;

    const detected = detectPlatform();
    hint.style.display = '';

    switch (detected) {
      case 'ios':
        hint.textContent = t('tools.recommend_ios', 'Empfohlen für dieses Gerät: App Store öffnen und „Tools for Autodarts“ als iPhone/iPad-App installieren.');
        setRecommendedButton(iosBtn, 'btn-primary');
        break;
      case 'android-firefox':
        hint.textContent = t('tools.recommend_android_firefox', 'Empfohlen für dieses Gerät: Firefox-Add-on über Mozilla Add-ons installieren.');
        setRecommendedButton(firefoxBtn, 'btn-primary');
        break;
      case 'android-other':
        hint.textContent = t('tools.recommend_android_other', 'Auf Android funktioniert der direkte Chrome-Web-Store-Weg meist nicht. Am besten Firefox installieren und dann das Firefox-Add-on öffnen.');
        setRecommendedButton(firefoxBtn, 'btn-primary');
        break;
      case 'desktop-firefox':
        hint.textContent = t('tools.recommend_desktop_firefox', 'Empfohlen für dieses Gerät: Firefox-Add-on über Mozilla Add-ons installieren.');
        setRecommendedButton(firefoxBtn, 'btn-primary');
        break;
      default:
        hint.textContent = t('tools.recommend_desktop_chromium', 'Empfohlen für dieses Gerät: Chrome Web Store im aktuellen Desktop-Browser öffnen.');
        setRecommendedButton(chromeBtn, 'btn-primary');
        break;
    }
  });
})();
