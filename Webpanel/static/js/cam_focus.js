(function () {
  const cfg = window.camFocusConfig || {};
  const img = document.getElementById('focusStream');
  const overlay = document.getElementById('roiOverlay');
  const canvas = document.getElementById('focusCanvas');
  const sizeSelect = document.getElementById('roiSize');
  const statusEl = document.getElementById('focusStatus');
  const scoreEl = document.getElementById('focusScore');
  const bestEl = document.getElementById('focusBest');
  const trendEl = document.getElementById('focusTrend');
  if (!img || !canvas || !sizeSelect || !statusEl) return;

  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const state = {
    roi: null,
    best: 0,
    history: [],
    imgReady: false,
    raf: null,
  };

  function currentRoiSize() {
    const value = parseInt(sizeSelect.value, 10);
    return Number.isFinite(value) && value > 20 ? value : 120;
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function clampRoiCenter(cx, cy, size) {
    const maxX = Math.max(0, img.naturalWidth - size);
    const maxY = Math.max(0, img.naturalHeight - size);
    return {
      x: Math.min(Math.max(0, Math.round(cx - size / 2)), maxX),
      y: Math.min(Math.max(0, Math.round(cy - size / 2)), maxY),
      w: size,
      h: size,
    };
  }

  function setCenteredRoi() {
    if (!img.naturalWidth || !img.naturalHeight) return;
    const size = currentRoiSize();
    state.roi = clampRoiCenter(img.naturalWidth / 2, img.naturalHeight / 2, size);
    state.history = [];
    updateOverlay();
    setStatus('Messfeld liegt auf dem Fadenkreuz – Siemensstern mittig ausrichten und Fokus fein drehen.');
  }

  function updateOverlay() {
    if (!state.roi || !img.naturalWidth || !img.naturalHeight) {
      overlay.hidden = true;
      return;
    }
    const rect = img.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      overlay.hidden = true;
      return;
    }
    const scaleX = rect.width / img.naturalWidth;
    const scaleY = rect.height / img.naturalHeight;
    overlay.style.left = `${state.roi.x * scaleX}px`;
    overlay.style.top = `${state.roi.y * scaleY}px`;
    overlay.style.width = `${state.roi.w * scaleX}px`;
    overlay.style.height = `${state.roi.h * scaleY}px`;
    overlay.hidden = false;
  }

  function setRoiFromClick(event) {
    if (!img.naturalWidth || !img.naturalHeight) return;
    const rect = img.getBoundingClientRect();
    const scaleX = img.naturalWidth / rect.width;
    const scaleY = img.naturalHeight / rect.height;
    const cx = (event.clientX - rect.left) * scaleX;
    const cy = (event.clientY - rect.top) * scaleY;
    const size = currentRoiSize();
    state.roi = clampRoiCenter(cx, cy, size);
    state.history = [];
    updateOverlay();
    setStatus('Messfeld gesetzt – jetzt langsam am Fokus drehen.');
  }

  function computeSharpness(imageData) {
    const { data, width, height } = imageData;
    if (!data || width < 3 || height < 3) return 0;

    const gray = new Float32Array(width * height);
    for (let i = 0, j = 0; i < data.length; i += 4, j++) {
      gray[j] = data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
    }

    let total = 0;
    let count = 0;
    for (let y = 1; y < height - 1; y++) {
      const row = y * width;
      for (let x = 1; x < width - 1; x++) {
        const idx = row + x;
        const gx = gray[idx + 1] - gray[idx - 1];
        const gy = gray[idx + width] - gray[idx - width];
        total += gx * gx + gy * gy;
        count++;
      }
    }
    return count ? total / count : 0;
  }

  function updateTrend(score) {
    state.history.push(score);
    if (state.history.length > 8) state.history.shift();
    if (score > state.best) state.best = score;

    const rounded = Math.round(score);
    scoreEl.textContent = rounded.toLocaleString('de-DE');
    bestEl.textContent = Math.round(state.best).toLocaleString('de-DE');

    if (state.history.length < 3) {
      trendEl.textContent = '—';
      trendEl.className = '';
      return;
    }

    const last = state.history[state.history.length - 1];
    const prev = state.history[state.history.length - 3];
    const diff = last - prev;
    if (diff > 250) {
      trendEl.textContent = 'steigt';
      trendEl.className = 'score-up';
    } else if (diff < -250) {
      trendEl.textContent = 'fällt';
      trendEl.className = 'score-down';
    } else {
      trendEl.textContent = 'ruhig';
      trendEl.className = 'score-flat';
    }
  }

  function processFrame() {
    if (img.complete && img.naturalWidth && img.naturalHeight) {
      state.imgReady = true;
      if (canvas.width !== img.naturalWidth || canvas.height !== img.naturalHeight) {
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
      }
      try {
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        if (state.roi) {
          const frame = ctx.getImageData(state.roi.x, state.roi.y, state.roi.w, state.roi.h);
          const score = computeSharpness(frame);
          updateTrend(score);
        }
      } catch (err) {
        setStatus('Stream wird geladen …');
      }
      updateOverlay();
    }
    state.raf = window.requestAnimationFrame(processFrame);
  }

  img.addEventListener('click', setRoiFromClick);
  img.addEventListener('load', function () {
    state.imgReady = true;
    if (!state.roi) {
      setCenteredRoi();
    }
    updateOverlay();
  });
  sizeSelect.addEventListener('change', function () {
    if (!state.roi) return;
    const cx = state.roi.x + state.roi.w / 2;
    const cy = state.roi.y + state.roi.h / 2;
    const size = currentRoiSize();
    state.roi = clampRoiCenter(cx, cy, size);
    state.history = [];
    updateOverlay();
  });
  window.addEventListener('resize', updateOverlay);
  window.addEventListener('beforeunload', function () {
    if (state.raf) window.cancelAnimationFrame(state.raf);
  });

  setStatus('Stream wird geladen …');
  processFrame();
})();
