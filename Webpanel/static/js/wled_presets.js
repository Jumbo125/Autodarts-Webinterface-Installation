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
  const TYPES = [
    {id:'player1', label:t('wled_presets.type.player1', 'Spieler 1 / Player 1'), kind:'fixed', arg:'-IDE', duration:false},
    {id:'player2', label:t('wled_presets.type.player2', 'Spieler 2 / Player 2'), kind:'fixed', arg:'-IDE2', duration:false},
    {id:'player3', label:t('wled_presets.type.player3', 'Spieler 3 / Player 3'), kind:'fixed', arg:'-IDE3', duration:false},
    {id:'player4', label:t('wled_presets.type.player4', 'Spieler 4 / Player 4'), kind:'fixed', arg:'-IDE4', duration:false},
    {id:'player5', label:t('wled_presets.type.player5', 'Spieler 5 / Player 5'), kind:'fixed', arg:'-IDE5', duration:false},
    {id:'player6', label:t('wled_presets.type.player6', 'Spieler 6 / Player 6'), kind:'fixed', arg:'-IDE6', duration:false},
    {id:'leg', label:t('wled_presets.type.leg', 'Leg gewonnen / Game won'), kind:'fixed', arg:'-G', duration:true},
    {id:'match', label:t('wled_presets.type.match', 'Match gewonnen / Match won'), kind:'fixed', arg:'-M', duration:true},
    {id:'checkout', label:t('wled_presets.type.checkout', 'Checkout / Takeout'), kind:'fixed', arg:'-TOE', duration:true},
    {id:'bull', label:t('wled_presets.type.bull', 'Doppelbull / Bull'), kind:'fixed', arg:'-DSBULL', duration:true},
    {id:'score0', label:t('wled_presets.type.score0', 'Score 0 / Score 0'), kind:'fixed', arg:'-S0', duration:true},
    {id:'score_exact', label:t('wled_presets.type.score_exact', 'Exakter Score / Exact score'), kind:'score_exact', duration:true},
    {id:'score_range', label:t('wled_presets.type.score_range', 'Score-Bereich / Score range'), kind:'score_range', duration:true}
  ];

  const state = { rows: [], wepsText: '"Dart-Led1.local"', targets: [] };

  const typeSelect = document.getElementById('typeSelect');
  const rowsEl = document.getElementById('rows');
  const messageEl = document.getElementById('message');
  const previewEl = document.getElementById('preview');
  const mainGrid = document.getElementById('mainGrid');
  const debugBtn = document.getElementById('debugBtn');
  const loadBtn = document.getElementById('loadBtn');
  const clearBtn = document.getElementById('clearBtn');
  const targetsSelect = document.getElementById('targetsSelect');
  const targetsCards = document.getElementById('targetsCards');
  const targetsHint = document.getElementById('targetsHint');
  const targetsAllBtn = document.getElementById('targetsAllBtn');
  const targetsNoneBtn = document.getElementById('targetsNoneBtn');
  const apiLoadUrl = window.WLED_PRESETS_CONFIG?.apiLoadUrl;
  const apiSaveUrl = window.WLED_PRESETS_CONFIG?.apiSaveUrl;
  const apiSendUrl = window.WLED_PRESETS_CONFIG?.apiSendUrl;
  const apiDeleteUrl = window.WLED_PRESETS_CONFIG?.apiDeleteUrl;

  TYPES.forEach(ti => {
    const opt = document.createElement('option');
    opt.value = ti.id;
    opt.textContent = ti.label;
    typeSelect.appendChild(opt);
  });

  function uid(){ return Math.random().toString(36).slice(2, 10); }

  function clampInt(v, min, max){
    let n = parseInt(v, 10);
    if(Number.isNaN(n)) n = min;
    return Math.max(min, Math.min(max, n));
  }

  function setDebugOpen(open){
    mainGrid.classList.toggle('preview-hidden', !open);
    debugBtn.textContent = open
      ? t('wled_presets.hide_debug', '🐞 Debug ausblenden')
      : t('wled_presets.show_debug', '🐞 Debug anzeigen');
  }

  function findType(id){ return TYPES.find(ti => ti.id === id); }

  function showMessage(text, kind='info'){
    if(!text){ messageEl.innerHTML=''; return; }
    const cls = kind === 'warn' ? 'warn-box' : 'info';
    messageEl.innerHTML = `<div class="${cls}">${text}</div>`;
  }

  function escapeHtml(v){
    return String(v)
      .replaceAll('&','&amp;')
      .replaceAll('<','&lt;')
      .replaceAll('>','&gt;')
      .replaceAll('"','&quot;')
      .replaceAll("'", '&#39;');
  }

  function hasDuplicateFixed(typeId){
    return state.rows.some(r => r.typeId === typeId);
  }

  function normalizeRow(raw){
    const row = Object.assign({
      id: uid(),
      preset: null,
      typeId: 'fixed',
      label: t('wled_presets.row.entry', 'Eintrag'),
      kind: 'fixed',
      arg: null,
      duration: false,
      seconds: '',
      score: 180,
      from: 0,
      to: 60,
    }, raw || {});
    if(!row.id) row.id = uid();

    const preset = clampInt(row.preset, 0, 999);
    row.preset = preset > 0 ? preset : null;

    if(row.kind === 'score_exact'){
      row.score = clampInt(row.score, 0, 180);
      row.duration = true;
    }
    if(row.kind === 'score_range'){
      row.from = clampInt(row.from, 0, 180);
      row.to = clampInt(row.to, 0, 180);
      row.duration = true;
    }
    if(row.kind === 'unknown'){
      row.arg = row.arg || '-?';
      row.label = row.label || `${t('wled_presets.unknown', 'Unbekannt')} / ${row.arg}`;
    }
    return row;
  }

  function sortRowsInPlace(){
    state.rows.sort((a, b) => {
      const ap = clampInt(a.preset, 0, 999) || 999999;
      const bp = clampInt(b.preset, 0, 999) || 999999;
      if(ap !== bp) return ap - bp;
      return String(a.id || '').localeCompare(String(b.id || ''));
    });
  }

  function assignMissingPresets(){
    const used = new Set();
    const missing = [];

    state.rows.forEach((row) => {
      const preset = clampInt(row.preset, 0, 999);
      if(preset >= 1 && !used.has(preset)){
        row.preset = preset;
        used.add(preset);
      } else {
        row.preset = null;
        missing.push(row);
      }
    });

    let nextPreset = 1;
    missing.forEach((row) => {
      while(used.has(nextPreset)) nextPreset += 1;
      row.preset = nextPreset;
      used.add(nextPreset);
      nextPreset += 1;
    });
  }

  function nextFreePreset(){
    const used = new Set(
      state.rows
        .map(row => clampInt(row.preset, 0, 999))
        .filter(v => v >= 1)
    );
    let preset = 1;
    while(used.has(preset)) preset += 1;
    return preset;
  }

  function setRows(rows){
    state.rows = (rows || []).map(normalizeRow);
    assignMissingPresets();
    sortRowsInPlace();
    render();
  }

  function setBusy(button, busy, busyText){
    if(!button) return;
    if(busy){
      button.dataset.originalText = button.textContent;
      button.disabled = true;
      button.textContent = busyText;
    } else {
      button.disabled = false;
      if(button.dataset.originalText) button.textContent = button.dataset.originalText;
    }
  }

  function normalizeTarget(item, index){
    if(typeof item === 'string'){
      const host = item.trim();
      if(!host) return null;
      return { slot: index + 1, host, label: host, online: true, selected: true };
    }
    if(!item || typeof item !== 'object') return null;
    const slot = Number(item.slot || 0) || (index + 1);
    const host = String(item.host || '').trim();
    if(!host) return null;
    const label = String(item.label || item.name || host || `${t('wled_presets.target.wled', 'WLED')} ${index + 1}`).trim();
    return { slot, host, label, online: item.online !== false, selected: item.online !== false };
  }

  function loadTargetsFromStorage(){
    let raw = null;
    try{
      raw = localStorage.getItem('wledPresetTargets') || localStorage.getItem('reachableWledTargets');
      const arr = JSON.parse(raw || '[]');
      state.targets = Array.isArray(arr) ? arr.map(normalizeTarget).filter(Boolean) : [];
    } catch {
      state.targets = [];
    }
    renderTargets();
  }

  function renderTargets(){
    targetsSelect.innerHTML = '';
    targetsCards.innerHTML = '';

    if(!state.targets.length){
      const opt = document.createElement('option');
      opt.disabled = true;
      opt.textContent = t('wled_presets.no_targets', 'Keine erreichbaren WLEDs von der Hauptseite übernommen');
      targetsSelect.appendChild(opt);
      targetsCards.innerHTML = `<div class="target-empty">${t('wled_presets.no_targets_open_main', 'Keine erreichbaren WLEDs von der Hauptseite übernommen. Öffne die Preset-Seite bitte über die Hauptseite.')}</div>`;
      targetsHint.textContent = t('wled_presets.open_via_main', 'Bitte über die Hauptseite öffnen, damit die WLED-Liste übernommen wird.');
      return;
    }

    state.targets.forEach((target) => {
      const selected = target.selected !== false;

      const opt = document.createElement('option');
      opt.value = target.host;
      opt.textContent = target.label;
      opt.selected = selected;
      targetsSelect.appendChild(opt);

      const card = document.createElement('label');
      card.className = `target-card${selected ? ' active' : ''}${target.online === false ? ' disabled' : ''}`;
      card.dataset.host = target.host;
      card.innerHTML = `
        <input type="checkbox" class="target-check" ${selected ? 'checked' : ''} />
        <div class="target-main">
          <div class="target-title">${escapeHtml(target.label)}</div>
          <div class="target-host">${escapeHtml(target.host)}</div>
          <div class="target-status">${target.online === false ? t('wled_presets.target.offline', 'Nicht erreichbar') : t('wled_presets.target.online', 'Erreichbar')}</div>
        </div>
      `;

      const checkbox = card.querySelector('.target-check');
      checkbox.addEventListener('click', (e) => e.stopPropagation());
      checkbox.addEventListener('change', () => setTargetSelected(target.host, checkbox.checked));
      card.addEventListener('click', (e) => {
        if(e.target instanceof HTMLInputElement) return;
        setTargetSelected(target.host, !checkbox.checked);
      });

      targetsCards.appendChild(card);
    });

    const onlineCount = state.targets.filter(ti => ti.online !== false).length;
    const selectedCount = state.targets.filter(ti => ti.selected !== false).length;
    targetsHint.textContent = `${selectedCount} ${t('wled_presets.targets_selected', 'ausgewählt')} · ${onlineCount} ${t('wled_presets.targets_online', 'erreichbar')}`;
  }

  function setTargetSelected(host, selected){
    const target = state.targets.find(ti => ti.host === host);
    if(!target) return;
    target.selected = !!selected;
    renderTargets();
  }

  function setAllTargets(selected){
    state.targets = state.targets.map(ti => ({ ...ti, selected: !!selected }));
    renderTargets();
  }

  function getSelectedTargets(){
    return state.targets
      .filter(ti => ti.selected !== false)
      .map(ti => ({ slot: ti.slot || 0, host: ti.host || '', label: ti.label || ti.host || `${t('wled_presets.target.wled', 'WLED')} ${ti.slot || ''}` }))
      .filter(ti => ti.host);
  }

  async function sendPresetToTargets(presetNumber, triggerButton){
    const targets = getSelectedTargets();
    if(!targets.length){
      showMessage(t('wled_presets.select_one_target', 'Bitte mindestens einen WLED-Controller auswählen.'), 'warn');
      return;
    }

    setBusy(triggerButton, true, '…');
    let ok = 0;
    const failed = [];

    try{
      for(const target of targets){
        try{
          const res = await fetch(apiSendUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              preset: presetNumber,
              slot: target.slot || 0,
              host: target.host || ''
            })
          });
          const data = await res.json().catch(() => ({}));
          if(!res.ok || !data.ok) throw new Error(data.msg || `HTTP ${res.status}`);
          ok++;
        } catch(err){
          failed.push(`${target.label || target.host}: ${err && err.message ? err.message : err}`);
        }
      }

      if(!failed.length){
        showMessage(
          t('wled_presets.preset_saved_on_count', 'Preset {preset} wurde auf {count} WLED-Controller gespeichert.')
            .replace('{preset}', presetNumber)
            .replace('{count}', ok)
        );
      } else {
        showMessage(
          t('wled_presets.preset_saved_partial', 'Preset {preset} gespeichert: {ok} ok, {failed} fehlgeschlagen.<br>{details}')
            .replace('{preset}', presetNumber)
            .replace('{ok}', ok)
            .replace('{failed}', failed.length)
            .replace('{details}', escapeHtml(failed.join(' | '))),
          'warn'
        );
      }
    } finally {
      setBusy(triggerButton, false);
    }
  }

  async function deletePresetFromTargets(presetNumber, triggerButton){
    const targets = getSelectedTargets();
    if(!targets.length || !apiDeleteUrl){
      showMessage(
        t('wled_presets.deleted_local_only', 'Preset {preset} wurde lokal gelöscht. Es war kein WLED-Controller ausgewählt.')
          .replace('{preset}', presetNumber),
        'warn'
      );
      return;
    }

    setBusy(triggerButton, true, '…');
    let ok = 0;
    const failed = [];

    try{
      for(const target of targets){
        try{
          const res = await fetch(apiDeleteUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              preset: presetNumber,
              slot: target.slot || 0,
              host: target.host || ''
            })
          });
          const data = await res.json().catch(() => ({}));
          if(!res.ok || !data.ok) throw new Error(data.msg || `HTTP ${res.status}`);
          ok++;
        } catch(err){
          failed.push(`${target.label || target.host}: ${err && err.message ? err.message : err}`);
        }
      }

      if(!failed.length){
        showMessage(
          t('wled_presets.preset_deleted_on_count', 'Preset {preset} wurde auf {count} WLED-Controller gelöscht.')
            .replace('{preset}', presetNumber)
            .replace('{count}', ok)
        );
      } else {
        showMessage(
          t('wled_presets.preset_deleted_partial', 'Preset {preset} lokal gelöscht: {ok} ok, {failed} fehlgeschlagen.<br>{details}')
            .replace('{preset}', presetNumber)
            .replace('{ok}', ok)
            .replace('{failed}', failed.length)
            .replace('{details}', escapeHtml(failed.join(' | '))),
          'warn'
        );
      }
    } finally {
      setBusy(triggerButton, false);
    }
  }

  async function loadCurrentSettings(autoLoad=false){
    try{
      setBusy(loadBtn, true, t('wled_presets.loading', 'Lade…'));
      const res = await fetch(apiLoadUrl, {cache:'no-store'});
      const data = await res.json();
      if(!res.ok || !data.ok) throw new Error(data.msg || t('wled_presets.load_failed', 'Laden fehlgeschlagen.'));
      state.wepsText = data.wepsText || '"Dart-Led1.local"';
      setRows(data.rows || []);
      if(!autoLoad) showMessage(data.msg || t('wled_presets.loaded_current', 'Aktuelle Einstellungen geladen.'));
    } catch(err){
      showMessage(
        t('wled_presets.load_current_failed', 'Aktuelle Einstellungen konnten nicht geladen werden: {error}')
          .replace('{error}', escapeHtml(err.message || err)),
        'warn'
      );
    } finally {
      setBusy(loadBtn, false);
    }
  }

  async function saveCurrentSettings(opts = {}){
    const silent = opts.silent === true;

    assignMissingPresets();
    sortRowsInPlace();

    try{
      const res = await fetch(apiSaveUrl, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({rows: state.rows})
      });

      const data = await res.json();
      if(!res.ok || !data.ok) throw new Error(data.msg || t('wled_presets.save_failed', 'Speichern fehlgeschlagen.'));

      state.wepsText = data.wepsText || state.wepsText;
      setRows(data.rows || state.rows);

      if(!silent){
        showMessage(data.msg || t('wled_presets.saved', 'Gespeichert.'));
      }

      return true;
    } catch(err){
      showMessage(
        t('wled_presets.save_failed_with_error', 'Speichern fehlgeschlagen: {error}')
          .replace('{error}', escapeHtml(err.message || err)),
        'warn'
      );
      return false;
    }
  }

  function addRow(typeId){
    const type = findType(typeId);
    if(!type) return;

    if(type.kind === 'fixed' && hasDuplicateFixed(typeId)){
      showMessage(
        t('wled_presets.duplicate_fixed', '„{label}“ ist schon vorhanden. Feste Ereignisse sollen nur einmal vorkommen.')
          .replace('{label}', type.label),
        'warn'
      );
      return;
    }

    const row = {
      id: uid(),
      preset: nextFreePreset(),
      typeId: type.id,
      label: type.label,
      kind: type.kind,
      arg: type.arg || null,
      duration: type.duration,
      seconds: type.duration ? '3' : '',
      score: 180,
      from: 0,
      to: 60,
    };

    if(type.id === 'leg') row.seconds = '4';
    if(type.id === 'match') row.seconds = '5';
    if(type.id === 'score0') row.seconds = '3';
    if(type.id === 'checkout') row.seconds = '3';
    if(type.id === 'bull') row.seconds = '3';

    state.rows.push(row);
    assignMissingPresets();
    sortRowsInPlace();
    showMessage('');
    render();
  }

  async function deleteRowAndSync(id, triggerButton){
    const currentRow = state.rows.find(r => r.id === id);
    if(!currentRow) return;

    const presetNumber = clampInt(currentRow.preset, 0, 999);
    const snapshot = state.rows.map((row) => ({ ...row }));
    state.rows = state.rows.filter(r => r.id !== id);
    assignMissingPresets();
    sortRowsInPlace();

    const saved = await saveCurrentSettings({ silent: true });
    if(!saved){
      state.rows = snapshot;
      render();
      return;
    }

    if(presetNumber >= 1){
      await deletePresetFromTargets(presetNumber, triggerButton);
    } else {
      showMessage(t('wled_presets.deleted', 'Preset gelöscht.'));
    }
  }

  function rowArgDisplay(row){
    if(row.kind === 'fixed' || row.kind === 'unknown') return row.arg;
    if(row.kind === 'score_exact') return `-S${row.score}`;
    return t('wled_presets.arg_auto', '(auto: -A1 / -A2 / ...)');
  }

  function lineForRow(row, presetNumber, areaIndex){
    if(row.kind === 'fixed'){
      const spacing = row.arg.length < 4 ? '   ' : '  ';
      if(row.duration){
        const sec = (row.seconds || '').trim();
        return `  ${row.arg}${spacing}"ps|${presetNumber}${sec ? `|${sec}` : ''}"`;
      }
      return `  ${row.arg}${spacing}"ps|${presetNumber}"`;
    }
    if(row.kind === 'unknown'){
      const arg = row.arg || '-?';
      const spacing = arg.length < 4 ? '   ' : '  ';
      const sec = (row.seconds || '').trim();
      return `  ${arg}${spacing}"ps|${presetNumber}${sec ? `|${sec}` : ''}"`;
    }
    if(row.kind === 'score_exact'){
      const score = clampInt(row.score, 0, 180);
      const sec = (row.seconds || '').trim();
      return `  -S${score} "ps|${presetNumber}${sec ? `|${sec}` : ''}"`;
    }
    const from = clampInt(row.from, 0, 180);
    const to = clampInt(row.to, 0, 180);
    const sec = (row.seconds || '').trim();
    return `  -A${areaIndex} ${from}-${to} "ps|${presetNumber}${sec ? `|${sec}` : ''}"`;
  }

  function buildLines(){
    assignMissingPresets();
    sortRowsInPlace();
    let areaCounter = 0;
    return state.rows.map((row) => {
      let areaIndex = null;
      if(row.kind === 'score_range') areaIndex = ++areaCounter;
      return lineForRow(row, row.preset || 0, areaIndex);
    });
  }

  function buildPreview(){
    const slash = '\\';
    const header = [
      '#!/usr/bin/env bash',
      'set -e',
      '',
      'cd /var/lib/autodarts/extensions/darts-wled',
      'source .venv/bin/activate',
      '',
      '# ' + t('wled_presets.preview.player_idle', 'Player/Idle (bleibt stehen)'),
      '# ' + t('wled_presets.preview.events_seconds', 'Events (mit Sekunden)'),
      '',
      'exec python darts-wled.py ' + slash,
      '  -CON "127.0.0.1:8079" ' + slash,
      '  -WEPS ' + (state.wepsText || '"Dart-Led1.local"') + ' ' + slash
    ];

    const lines = buildLines();
    if(lines.length === 0){
      return header.join('\n') + '\n  # ' + t('wled_presets.preview.no_lines', 'hier würden die Preset-Zeilen stehen');
    }

    const body = lines.map((line, i) => i === lines.length - 1 ? line : (line + ' ' + slash));
    return header.join('\n') + '\n' + body.join('\n');
  }

  function renderExtra(row){
    if(row.kind === 'score_exact'){
      return `<div class="extra">
        <input class="mini" type="number" min="0" max="180" step="1" value="${escapeHtml(row.score)}" data-field="score" data-id="${row.id}" />
        <span class="readonly">${t('wled_presets.range_0_180', '0 bis 180')}</span>
      </div>`;
    }
    if(row.kind === 'score_range'){
      return `<div class="extra">
        <input class="mini" type="number" min="0" max="180" step="1" value="${escapeHtml(row.from)}" data-field="from" data-id="${row.id}" />
        <span class="readonly">${t('wled_presets.to', 'bis')}</span>
        <input class="mini" type="number" min="0" max="180" step="1" value="${escapeHtml(row.to)}" data-field="to" data-id="${row.id}" />
        <span class="readonly">${t('wled_presets.arg_auto_assigned', 'Arg wird automatisch zugeteilt')}</span>
      </div>`;
    }
    if(row.kind === 'unknown'){
      return `<span class="readonly">${t('wled_presets.unknown_arg_taken_directly', 'unbekanntes Argument wird direkt übernommen')}</span>`;
    }
    return `<span class="readonly">${t('wled_presets.no_extra_needed', 'kein Zusatz nötig')}</span>`;
  }

  function render(){
    assignMissingPresets();
    sortRowsInPlace();
    rowsEl.innerHTML = '';

    state.rows.forEach((row) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><span class="preset-box">Preset ${escapeHtml(row.preset || '?')}</span></td>
        <td>
          <div class="badge">${escapeHtml(rowArgDisplay(row))}</div>
          <small>${escapeHtml(row.label)}</small>
        </td>
        <td>${row.duration ? `<input class="mini" type="number" min="0" step="1" value="${escapeHtml(row.seconds)}" data-field="seconds" data-id="${row.id}" />` : `<span class="readonly">${t('wled_presets.duration_permanent', 'dauerhaft')}</span>`}</td>
        <td>${renderExtra(row)}</td>
        <td>
          <div class="row-actions">
            <button class="icon secondary" data-action="send" data-id="${row.id}" title="${t('wled_presets.save_and_send_this_preset', 'Dieses Preset speichern und senden')}">📤</button>
            <button class="icon danger" data-action="remove" data-id="${row.id}" title="${t('wled_presets.delete', 'Löschen')}">🗑</button>
          </div>
        </td>
      `;
      rowsEl.appendChild(tr);
    });

    previewEl.value = buildPreview();
    validate();
  }

  function validate(){
    const warnings = [];

    const exactSeen = new Map();
    state.rows.forEach((r) => {
      if(r.kind === 'score_exact'){
        const s = clampInt(r.score, 0, 180);
        if(exactSeen.has(s)) {
          warnings.push(
            t('wled_presets.warning.exact_duplicate', 'Exakter Score {score} ist mehrfach vorhanden (Presets {line1} und {line2}).')
              .replace('{score}', s)
              .replace('{line1}', exactSeen.get(s))
              .replace('{line2}', r.preset)
          );
        } else {
          exactSeen.set(s, r.preset);
        }
      }
      if(r.kind === 'score_range'){
        const from = clampInt(r.from, 0, 180);
        const to = clampInt(r.to, 0, 180);
        if(from > to) {
          warnings.push(
            t('wled_presets.warning.invalid_range', 'Bereich in Preset {line} ist ungültig: von {from} bis {to}.')
              .replace('{line}', r.preset)
              .replace('{from}', from)
              .replace('{to}', to)
          );
        }
      }
    });

    if(warnings.length){
      showMessage(warnings.join('<br>'), 'warn');
    } else if(state.rows.length){
      showMessage(t('wled_presets.preview_active', 'Vorschau aktiv. Feste Presets bleiben erhalten und neue Einträge bekommen automatisch das nächste freie Preset.'));
    } else {
      showMessage('');
    }
  }

  rowsEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('button');
    if(!btn) return;
    const id = btn.dataset.id;
    const action = btn.dataset.action;
    if(action === 'remove'){
      await deleteRowAndSync(id, btn);
      return;
    }
    if(action === 'send'){
      const row = state.rows.find(r => r.id === id);
      if(row && row.preset){
        const saved = await saveCurrentSettings({ silent: true });
        if(!saved) return;
        await sendPresetToTargets(row.preset, btn);
      }
    }
  });

  rowsEl.addEventListener('input', (e) => {
    const el = e.target;
    if(!(el instanceof HTMLInputElement)) return;
    const id = el.dataset.id;
    const field = el.dataset.field;
    const row = state.rows.find(r => r.id === id);
    if(!row || !field) return;
    row[field] = el.value;
    previewEl.value = buildPreview();
    validate();
  });

  document.getElementById('addBtn').addEventListener('click', () => addRow(typeSelect.value));
  loadBtn.addEventListener('click', () => loadCurrentSettings(false));
  debugBtn.addEventListener('click', () => {
    const isHidden = mainGrid.classList.contains('preview-hidden');
    setDebugOpen(isHidden);
  });
  if(clearBtn){
    clearBtn.style.display = 'none';
    clearBtn.addEventListener('click', () => {
      showMessage(t('wled_presets.clear_disabled', 'Alles leeren ist aus Sicherheitsgründen ausgeblendet.'), 'warn');
    });
  }
  targetsAllBtn.addEventListener('click', () => setAllTargets(true));
  targetsNoneBtn.addEventListener('click', () => setAllTargets(false));

  setDebugOpen(false);
  render();
  loadTargetsFromStorage();
  loadCurrentSettings(true);
});
