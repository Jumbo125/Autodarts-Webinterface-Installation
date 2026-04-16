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

  /* =========================================================
     1) Sprachdaten vom Server laden
     ========================================================= */
  function loadLangConfig(callback) {
    fetch('/api/langs', {
      method: 'GET',
      cache: 'no-store',
      headers: {
        'Accept': 'application/json'
      }
    })
      .then(function (res) {
        if (!res.ok) {
          throw new Error('HTTP ' + res.status);
        }
        return res.json();
      })
      .then(function (json) {
        window.lang_config = {
          default: json.default || null,
          data: json.data || {},
          count: json.count || 0,
          sources: json.sources || [],
          ok: !!json.ok,
          msg: json.msg || null
        };

        if (typeof callback === 'function') {
          callback(window.lang_config);
        }
      })
      .catch(function (err) {
        console.error('Fehler beim Laden der Sprachdaten:', err);

        window.lang_config = {
          default: null,
          data: {},
          count: 0,
          sources: [],
          ok: false,
          msg: err.message
        };

        if (typeof callback === 'function') {
          callback(window.lang_config);
        }
      });
  }


  /* =========================================================
     2) Sprach-Dropdown befüllen
     ========================================================= */
  function fillLangDropdown() {
    const select = document.getElementById('lang_dropdown');
    if (!select) return;

    select.innerHTML = '';

    if (
      !window.lang_config ||
      !window.lang_config.data ||
      typeof window.lang_config.data !== 'object'
    ) {
      return;
    }

    const defaultLang = window.lang_config.default || null;
    const langs = window.lang_config.data;

    Object.keys(langs).sort().forEach(function (code) {
      const lang = langs[code] || {};
      const config = lang.config || {};

      const option = document.createElement('option');
      option.value = code;
      option.textContent = (config.abk || code).toUpperCase();

      if (defaultLang === code) {
        option.selected = true;
      }

      select.appendChild(option);
    });
  }


  /* =========================================================
     3) Standardsprache auf dem Server speichern
     ========================================================= */
  function set_lang(callback) {
    const select = document.getElementById('lang_dropdown');

    if (!select) {
      console.error('Dropdown #lang_dropdown nicht gefunden');
      return;
    }

    const abk = select.value;

    if (!abk) {
      console.error('Keine Sprache gewählt');
      return;
    }

    fetch('/api/lang/default', {
      method: 'POST',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      body: JSON.stringify({
        default: abk
      })
    })
      .then(function (res) {
        if (!res.ok) {
          throw new Error('HTTP ' + res.status);
        }
        return res.json();
      })
      .then(function (json) {
        if (!json.ok) {
          throw new Error(json.msg || 'Default-Sprache konnte nicht gespeichert werden');
        }

        loadLangConfig(function (cfg) {
          fillLangDropdown();
          applyDefaultLanguage();

          if (typeof callback === 'function') {
            callback(cfg);
          }
        });
      })
      .catch(function (err) {
        console.error('Fehler beim Setzen der Sprache:', err);
      });
  }


  /* =========================================================
     4) Standardsprache auf alle data-* Platzhalter anwenden
     ========================================================= */
  function applyDefaultLanguage() {
    if (!window.lang_config || !window.lang_config.data) return;

    var lang = window.lang_config.default || 'de';
    var langData = window.lang_config.data[lang];

    if (!langData || !langData.placeholder) return;

    var texts = langData.placeholder;

    /* ---------- Sichtbare Texte ---------- */
    document.querySelectorAll('[data-key]').forEach(function (el) {
      var key = el.getAttribute('data-key');
      if (key && texts[key] !== undefined) {
        el.innerHTML = String(texts[key]).replace(/\n/g, '<br>');
      }
    });

    /* ---------- Placeholder ---------- */
    document.querySelectorAll('[data-placeholder-key]').forEach(function (el) {
      var key = el.getAttribute('data-placeholder-key');
      if (key && texts[key] !== undefined) {
        el.setAttribute('placeholder', texts[key]);
      }
    });

    /* ---------- Title ---------- */
    document.querySelectorAll('[data-title-key]').forEach(function (el) {
      var key = el.getAttribute('data-title-key');
      if (key && texts[key] !== undefined) {
        el.setAttribute('title', texts[key]);
      }
    });

    /* ---------- Alt ---------- */
    document.querySelectorAll('[data-alt-key]').forEach(function (el) {
      var key = el.getAttribute('data-alt-key');
      if (key && texts[key] !== undefined) {
        el.setAttribute('alt', texts[key]);
      }
    });

    /* ---------- Value ---------- */
    document.querySelectorAll('[data-value-key]').forEach(function (el) {
      var key = el.getAttribute('data-value-key');
      if (key && texts[key] !== undefined) {
        el.value = texts[key];
      }
    });

    /* ---------- Confirm-Text ---------- */
    document.querySelectorAll('[data-confirm-key]').forEach(function (el) {
      var key = el.getAttribute('data-confirm-key');
      if (key && texts[key] !== undefined) {
        el.setAttribute('data-confirm', texts[key]);
      }
    });

    /* ---------- HTML-Sprache setzen ---------- */
    document.documentElement.lang = lang;
  }


  /* =========================================================
     5) Übersetzungsfunktion für andere JS-Funktionen
     =========================================================
     Nutzung:
       t('common.save_btn')
       t('admin.wrong_password')
       t('test.key', 'Fallback Text')
       t('test.key', 'Hallo {name}', { name: 'Peter' })
     ========================================================= */
  function t(key, fallback, vars) {
    fallback = (fallback === undefined || fallback === null) ? key : fallback;

    if (
      !window.lang_config ||
      !window.lang_config.data ||
      typeof window.lang_config.data !== 'object'
    ) {
      return fallback;
    }

    var lang = window.lang_config.default || 'de';
    var langData = window.lang_config.data[lang];

    if (!langData || !langData.placeholder) {
      return fallback;
    }

    var text = langData.placeholder[key];

    if (text === undefined || text === null) {
      return fallback;
    }

    text = String(text);

    /* ---------- optionale Variablen ersetzen ---------- */
    if (vars && typeof vars === 'object') {
      Object.keys(vars).forEach(function (varKey) {
        var regex = new RegExp('\\{' + varKey + '\\}', 'g');
        text = text.replace(regex, vars[varKey]);
      });
    }

    return text;
  }


  /* =========================================================
     6) Funktionen global verfügbar machen
     =========================================================
     Dadurch kannst du sie auch in anderen JS-Dateien verwenden:
       t('common.save_btn')
       set_lang()
       applyDefaultLanguage()
       loadLangConfig(...)
     ========================================================= */
  window.t = t;
  window.set_lang = set_lang;
  window.applyDefaultLanguage = applyDefaultLanguage;
  window.loadLangConfig = loadLangConfig;


  /* =========================================================
     7) Initialisierung beim Seitenstart
     ========================================================= */
  loadLangConfig(function () {
    fillLangDropdown();
    applyDefaultLanguage();

    const btn = document.getElementById('set_lang');
    if (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        set_lang();
      });
    }
  });

});