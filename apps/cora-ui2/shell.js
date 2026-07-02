// ----- Theme toggle (persisted) ------------------------------------
// The stored theme is applied synchronously by an inline script in
// <head> (see index.html) so the page never flashes the wrong theme.
// This block only handles the user-facing toggle.
const themeToggle = document.getElementById('themeToggle');
if (themeToggle) {
  const syncPressed = () => {
    themeToggle.setAttribute(
      'aria-pressed',
      document.documentElement.dataset.theme === 'light' ? 'true' : 'false'
    );
  };
  syncPressed();
  themeToggle.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    if (next === 'dark') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
    try { localStorage.setItem('cora-theme', next); } catch (_) {}
    syncPressed();
  });
}

// ----- Refresh button ----------------------------------------------
// window.coraRefresh is defined in bootstrap.js; we delegate to it so
// the manual button and the auto-refresh timer share one code path.
const refreshBtn = document.getElementById('refreshBtn');
if (refreshBtn) {
  refreshBtn.addEventListener('click', () => {
    if (typeof window.coraRefresh === 'function') window.coraRefresh();
    else window.location.reload();
  });
}

// ----- Panel collapse toggle ---------------------------------------
const panel  = document.getElementById('activityPanel');
const toggle = document.getElementById('panelToggle');
toggle.addEventListener('click', () => {
  panel.classList.toggle('collapsed');
});

// ----- Status-dot demo cycle ---------------------------------------
// Cycles through listening → processing → idle so the states are
// visible. The mic button can take over and pin the state.
const dot   = document.getElementById('statusDot');
const label = dot.previousElementSibling;
const states = [
  { s: 'listening',  t: 'Listening',  dur: 4200 },
  { s: 'processing', t: 'Processing', dur: 2200 },
  { s: 'idle',       t: 'Idle',       dur: 2600 },
];
let si = 0, cycleTimer = null, cycleActive = true;
function cycle() {
  if (!cycleActive) return;
  const cur = states[si];
  dot.dataset.state = cur.s;
  label.textContent = cur.t;
  si = (si + 1) % states.length;
  cycleTimer = setTimeout(cycle, cur.dur);
}
function stopCycle() {
  cycleActive = false;
  clearTimeout(cycleTimer);
}
function startCycle() {
  cycleActive = true;
  cycle();
}
setTimeout(cycle, 1200);

// ----- Mic button --------------------------------------------------
const micBtn  = document.getElementById('micBtn');
const micHint = document.getElementById('micHint');
let listening = false;

function setListening(next) {
  listening = next;
  micBtn.classList.toggle('listening', listening);
  micBtn.setAttribute('aria-pressed', String(listening));
  micBtn.setAttribute('aria-label', listening ? 'Stop listening' : 'Start listening');
  micHint.innerHTML = listening
    ? 'Listening&hellip; tap to stop'
    : 'Tap or say &lsquo;Hey Cora&rsquo;';

  // Focus the scene: CSS hides UI chrome, scene.js green-shifts the
  // orb and starts driving the speech-rhythm deformation.
  if (listening) {
    document.documentElement.dataset.focus = 'speech';
  } else {
    delete document.documentElement.dataset.focus;
  }

  // Pin / release the header status indicator
  if (listening) {
    stopCycle();
    dot.dataset.state = 'listening';
    label.textContent = 'Listening';
  } else {
    startCycle();
  }

  // Public event — anything in the app can react to this.
  window.dispatchEvent(new CustomEvent('cora:mic-toggle', {
    detail: { listening },
  }));
}

micBtn.addEventListener('click', () => setListening(!listening));

// ----- Demo hookup: raise the orb while listening ------------------
// Real audio amplitude will drive window.CORA.voiceBright; this is
// a placeholder bump so the scene visibly reacts to the button.
window.addEventListener('cora:mic-toggle', (e) => {
  window.__coraListening = e.detail.listening;
});

// ----- Speech-overlay morph driver ---------------------------------
// Idle = slow heartbeat (two-beat "lub-dub" under a quiet baseline).
// Awake = simulated speech envelope. Transitions are eased so the
// rhythm smoothly accelerates on mic press and winds back down on
// release. Shape morph is applied via a CSS var so the outer shell
// and inner ring of the sphere deform together.
const speechOrb = document.getElementById('speechOrb');
(function driveSpeechOrb() {
  // Two-beat gaussian-peak heartbeat. Period ~1.6s: a primary "lub"
  // peak, a smaller "dub" shortly after, then a resting gap.
  function heartbeat(t) {
    const period = 1.6;
    const phase = t - Math.floor(t / period) * period;
    const w = 0.085;
    const beat1 = Math.exp(-Math.pow((phase - 0.10) / w, 2));
    const beat2 = Math.exp(-Math.pow((phase - 0.32) / w, 2)) * 0.55;
    return beat1 + beat2;
  }

  let amp = 0;
  const start = performance.now();
  function frame(now) {
    const t = (now - start) / 1000;
    const focused = document.documentElement.dataset.focus === 'speech';
    const voiceState = document.documentElement.dataset.voiceState;
    const speaking = voiceState === 'speaking';

    // Idle baseline: a soft floor (0.05) plus the heartbeat pulses.
    // Heartbeat amplitude caps around 1.55; scaled to ~0.28 peak so
    // the idle morph stays subtle.
    const idleAmp = 0.05 + heartbeat(t) * 0.18;

    if (speaking && typeof window.CORA?.voiceLevel === 'number') {
      // Drive directly from the live audio analyser — the orb
      // pulses with Cora's actual voice. Add a small floor so the
      // orb still flexes during quieter consonants. The user-tunable
      // multiplier `window.__voiceReactivity` (Settings → Voice
      // reactivity) scales the live contribution; the floor scales
      // too so 0% = essentially still, 200% = very animated.
      const reactivity = (typeof window.__voiceReactivity === 'number')
        ? window.__voiceReactivity : 1;
      const live = window.CORA.voiceLevel;
      const target = (0.18 + live * 1.6) * reactivity;
      amp += (target - amp) * 0.32;       // fast follow, ~30 ms time const
    } else if (focused) {
      // Listening: synthetic speech rhythm — placeholder until the
      // user starts speaking enough for analysis. Same as before.
      const raw =
        Math.sin(t * 4.2) * 0.34 +
        Math.sin(t * 7.1) * 0.26 +
        Math.sin(t * 11.5) * 0.18;
      const envelope = 0.55 + 0.45 * Math.sin(t * 0.7);
      const active = Math.max(0, raw) * envelope + 0.1;
      amp += (active - amp) * 0.1;  // quicker wake-up
    } else {
      amp += (idleAmp - amp) * 0.14; // follow heartbeat closely
    }

    if (speechOrb) {
      // Scale: soft breath floor + amp-driven pulse. Kept small so
      // the sphere reads as "alive", not as a bouncing ball.
      const breathe = 1 + 0.012 * Math.sin(t * 1.1);
      const pulse   = 1 + amp * 0.06;
      speechOrb.style.transform = `scale(${breathe * pulse})`;

      // Organic border-radius morph — shared by shell and inner
      // ring via a CSS variable. Modest coefficients keep the
      // deformation as "flexing membrane", not a blob.
      const a = 50 + Math.sin(t * 1.4) * amp * 8;
      const b = 100 - a;
      const c = 50 + Math.cos(t * 1.7) * amp * 6;
      const d = 100 - c;
      const e = 50 + Math.sin(t * 1.2 + 1.3) * amp * 6;
      const f = 100 - e;
      speechOrb.style.setProperty(
        '--orb-morph',
        `${a}% ${b}% ${c}% ${d}% / ${e}% ${f}% ${a}% ${b}%`
      );
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
(function driveOrb() {
  const start = performance.now();
  function loop(now) {
    const t = (now - start) / 1000;
    const idle = (Math.sin(t * (Math.PI * 2 / 4.0)) * 0.5 + 0.5) * 0.12;

    // Synthetic "active" rhythm — used while mic is on but Cora isn't
    // actively speaking yet (so the background orb still feels alive
    // during the listening phase).
    const synthActive = window.__coraListening
      ? 0.35 + (Math.sin(t * 5.2) * 0.5 + 0.5) * 0.35
      : 0;

    // Live audio reactive — only contributes while Cora is speaking.
    // Mapped a bit aggressively (level → 0.35..1.0) so the background
    // orb visibly brightens with each syllable. Reactivity slider
    // scales the live contribution.
    const speaking = document.documentElement.dataset.voiceState === 'speaking';
    const live = (speaking && typeof window.CORA?.voiceLevel === 'number')
      ? window.CORA.voiceLevel : 0;
    const reactivity = (typeof window.__voiceReactivity === 'number')
      ? window.__voiceReactivity : 1;
    const liveActive = live > 0 ? (0.35 + live * 0.65) * reactivity : 0;

    const active = Math.max(synthActive, liveActive);

    if (window.CORA) {
      window.CORA.voiceBright = Math.max(idle, active);
    }
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
})();

// ----- Draggable + openable response cards -----------------------
// A single pointer gesture can be either a click-to-open or a drag.
// We snapshot the shell's rect on pointerdown and only commit to drag
// once the pointer has travelled past DRAG_THRESHOLD px. A release
// below that threshold counts as a click; if the card is marked
// .openable the modal opens with a FLIP animation from its rect.
const DRAG_THRESHOLD = 4;
const CARD_POS_KEY = 'cora-card-positions';

function cardIsOpenable(shell) {
  const fig = shell.querySelector('.card-footer .figure');
  return !!fig && fig.textContent.includes('Open');
}

// Classify the accent hue from a card's data. Returns a class name
// ("hue-blue" | "hue-red" | "hue-green") or null for the default
// orange. Matches keywords in the category + title text.
const HUE_RULES = [
  { cls: 'hue-red',   re: /\b(alert|warning|incident|alarm|critical|outage)\b/i },
  { cls: 'hue-blue',  re: /\b(email|inbox|mail|reply|message)\b/i },
  { cls: 'hue-green', re: /\b(news|briefing|digest|brief|signal|scout|announcement)\b/i },
];
function hueClassFor(data) {
  const text = `${data.category || ''} ${data.title || ''}`;
  for (const rule of HUE_RULES) if (rule.re.test(text)) return rule.cls;
  return null;
}
function hueClassOf(el) {
  const m = el.className.match(/\bhue-(?:blue|red|green)\b/);
  return m ? m[0] : null;
}

function cardKey(shell) {
  for (const c of shell.classList) if (/^c\d+$/.test(c)) return c;
  return null;
}

function readSavedPositions() {
  try { return JSON.parse(localStorage.getItem(CARD_POS_KEY) || '{}'); }
  catch (_) { return {}; }
}

function writeSavedPositions(map) {
  try { localStorage.setItem(CARD_POS_KEY, JSON.stringify(map)); } catch (_) {}
}

function saveCardPosition(shell) {
  const key = cardKey(shell);
  if (!key) return;
  const rect = shell.getBoundingClientRect();
  const map = readSavedPositions();
  map[key] = { left: Math.round(rect.left), top: Math.round(rect.top) };
  writeSavedPositions(map);
}

// Restore any persisted positions before wiring drag handlers. We
// clamp to the current viewport so a card dragged on a larger screen
// can't land off-canvas after a window resize between sessions.
(function restoreCardPositions() {
  const map = readSavedPositions();
  document.querySelectorAll('.card-shell').forEach((shell) => {
    const key = cardKey(shell);
    const pos = key && map[key];
    if (!pos) return;
    const w = shell.offsetWidth;
    const h = shell.offsetHeight;
    const maxX = Math.max(0, window.innerWidth  - w);
    const maxY = Math.max(0, window.innerHeight - h);
    const left = Math.max(0, Math.min(maxX, pos.left));
    const top  = Math.max(0, Math.min(maxY, pos.top));
    shell.style.left   = left + 'px';
    shell.style.top    = top  + 'px';
    shell.style.right  = 'auto';
    shell.style.bottom = 'auto';
  });
})();

document.querySelectorAll('.card-shell').forEach((shell) => {
  const card = shell.querySelector('.response-card');
  if (!card) return;
  if (cardIsOpenable(shell)) shell.classList.add('openable');
  // Colour the card/bubble by data type (email/alert/news/default).
  const initData = collectSourceData(shell);
  const hue = initData && hueClassFor(initData);
  if (hue) shell.classList.add(hue);
  // Bubble-only shells never render as cards, so skip drag wiring.
  if (shell.classList.contains('bubble-only')) return;

  let startX = 0, startY = 0;
  let offsetX = 0, offsetY = 0;
  let initRect = null;
  let dragStarted = false;
  let activePointerId = null;

  card.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;

    initRect = shell.getBoundingClientRect();
    startX = e.clientX;
    startY = e.clientY;
    offsetX = e.clientX - initRect.left;
    offsetY = e.clientY - initRect.top;

    dragStarted = false;
    activePointerId = e.pointerId;
    card.setPointerCapture(e.pointerId);
    e.preventDefault();
  });

  card.addEventListener('pointermove', (e) => {
    if (e.pointerId !== activePointerId) return;

    if (!dragStarted) {
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
      // First movement past threshold — commit to drag.
      dragStarted = true;
      shell.style.left   = initRect.left + 'px';
      shell.style.top    = initRect.top  + 'px';
      shell.style.right  = 'auto';
      shell.style.bottom = 'auto';
      shell.classList.add('dragging');
    }
    shell.style.left = (e.clientX - offsetX) + 'px';
    shell.style.top  = (e.clientY - offsetY) + 'px';
  });

  const endPointer = (e) => {
    if (e.pointerId !== activePointerId) return;
    try { card.releasePointerCapture(activePointerId); } catch (_) {}
    activePointerId = null;

    if (dragStarted) {
      shell.classList.remove('dragging');
      saveCardPosition(shell);
    } else if (shell.classList.contains('openable')) {
      openModal(shell);
    }
    dragStarted = false;
  };
  card.addEventListener('pointerup', endPointer);
  card.addEventListener('pointercancel', endPointer);
});

// ----- Per-card motion pause/play toggle -------------------------
// Persisted as { c1: true|false, c2: ..., c3: ... } where true = paused.
// The button lives inside .response-card; we stopPropagation on all
// pointer events so it doesn't feed the drag/open handlers above.
const CARD_MOTION_KEY = 'cora-card-motion';

function readCardMotion() {
  try { return JSON.parse(localStorage.getItem(CARD_MOTION_KEY) || '{}'); }
  catch (_) { return {}; }
}
function writeCardMotion(map) {
  try { localStorage.setItem(CARD_MOTION_KEY, JSON.stringify(map)); } catch (_) {}
}

function syncMotionButton(shell, paused) {
  const btn = shell.querySelector('.card-motion-btn');
  if (!btn) return;
  btn.setAttribute('aria-label', paused ? 'Resume motion' : 'Pause motion');
  btn.setAttribute('aria-pressed', paused ? 'true' : 'false');
}

// Restore saved motion states.
(function restoreCardMotion() {
  const map = readCardMotion();
  document.querySelectorAll('.card-shell:not(.bubble-only)').forEach((shell) => {
    const key = cardKey(shell);
    const paused = key && map[key] === true;
    if (paused) shell.classList.add('motion-paused');
    syncMotionButton(shell, !!paused);
  });
})();

document.querySelectorAll('.card-shell:not(.bubble-only) .card-motion-btn').forEach((btn) => {
  const shell = btn.closest('.card-shell');
  if (!shell) return;

  // Keep the drag/open pipeline blind to clicks on this button.
  ['pointerdown', 'pointerup', 'click'].forEach((evt) => {
    btn.addEventListener(evt, (e) => e.stopPropagation());
  });

  btn.addEventListener('click', () => {
    const wasPaused = shell.classList.contains('motion-paused');
    const nextPaused = !wasPaused;
    shell.classList.toggle('motion-paused', nextPaused);
    syncMotionButton(shell, nextPaused);

    const key = cardKey(shell);
    if (key) {
      const map = readCardMotion();
      if (nextPaused) map[key] = true;
      else delete map[key];
      writeCardMotion(map);
    }
  });
});

// ----- Expanded-detail modal (FLIP from card rect) ---------------
const modalRoot   = document.getElementById('modalRoot');
const modalDialog = document.getElementById('modalDialog');
const modalBody   = document.getElementById('modalBody');
let currentSourceShell = null;
let lastFocus = null;

// Extract the canonical data the modal needs from either a
// .card-shell (reads from its child .response-card DOM) or a .bubble
// (reads from its data-* attributes). Detail fragment is looked up
// against the source card — bubbles carry a data-source-key that
// maps to .card-shell.<key>.
function collectSourceData(sourceEl) {
  if (sourceEl.classList.contains('bubble')) {
    const key = sourceEl.dataset.sourceKey || '';
    const sourceShell = key
      ? document.querySelector(`.card-shell.${key}`)
      : null;
    const detailTpl = sourceShell
      ? sourceShell.querySelector('template.card-detail')
      : null;
    return {
      category: sourceEl.dataset.category || '',
      title:    sourceEl.dataset.title    || '',
      body:     sourceEl.dataset.body     || '',
      meta:     sourceEl.dataset.meta     || '',
      detailFragment: detailTpl ? detailTpl.content.cloneNode(true) : null,
    };
  }

  // Activity panel entries are tiny — title + meta only. data-lane-label
  // (set in bootstrap.js's renderActivity) gives the eyebrow text. No
  // detail template; the modal just shows the bare entry data.
  if (sourceEl.classList.contains('entry')) {
    const titleEl = sourceEl.querySelector('.entry-title');
    const metaEl  = sourceEl.querySelector('.entry-meta');
    return {
      category: sourceEl.dataset.laneLabel || sourceEl.dataset.lane || '',
      title:    titleEl ? titleEl.textContent.trim() : '',
      body:     '',
      meta:     metaEl  ? metaEl.textContent.trim()  : '',
      detailFragment: null,
    };
  }

  const card = sourceEl.querySelector('.response-card');
  if (!card) return null;
  const eyebrowEl = card.querySelector('.card-eyebrow');
  const titleEl   = card.querySelector('.card-title');
  const bodyEl    = card.querySelector('.card-body');
  const footerEl  = card.querySelector('.card-footer');

  const categoryText = eyebrowEl
    ? eyebrowEl.textContent.replace(/\s+/g, ' ').trim()
    : '';
  const statusText = footerEl
    ? Array.from(footerEl.querySelectorAll(':scope > span'))
        .filter(s => !s.classList.contains('figure'))
        .map(s => s.textContent.trim())
        .filter(Boolean)
        .join(' · ')
    : '';
  const detailTpl = sourceEl.querySelector('template.card-detail');

  return {
    category: categoryText,
    title:    titleEl ? titleEl.textContent.trim() : '',
    body:     bodyEl  ? bodyEl.textContent.trim()  : '',
    meta:     statusText,
    detailFragment: detailTpl ? detailTpl.content.cloneNode(true) : null,
  };
}

function buildModalHeader(data) {
  const header = document.createElement('header');
  header.className = 'modal-header';

  const eyebrow = document.createElement('div');
  eyebrow.className = 'modal-eyebrow';
  const pip = document.createElement('span');
  pip.className = 'pip';
  eyebrow.append(pip);
  const meta = document.createElement('span');
  meta.textContent = data.meta
    ? `${data.category} · ${data.meta}`
    : data.category;
  eyebrow.append(meta);
  header.append(eyebrow);

  const title = document.createElement('h2');
  title.className = 'modal-title';
  title.id = 'modalTitle';
  title.textContent = data.title;
  header.append(title);

  if (data.body) {
    const lede = document.createElement('p');
    lede.className = 'modal-lede';
    lede.textContent = data.body;
    header.append(lede);
  }

  return header;
}

function flipTransform(sourceRect, targetRect) {
  const sx = sourceRect.width  / targetRect.width;
  const sy = sourceRect.height / targetRect.height;
  const tx = (sourceRect.left + sourceRect.width  / 2)
           - (targetRect.left + targetRect.width  / 2);
  const ty = (sourceRect.top  + sourceRect.height / 2)
           - (targetRect.top  + targetRect.height / 2);
  return `translate(${tx}px, ${ty}px) scale(${sx}, ${sy})`;
}

function openModal(sourceEl) {
  const data = collectSourceData(sourceEl);
  if (!data) return;

  currentSourceShell = sourceEl;
  lastFocus = document.activeElement;

  // Compose modal body from card data (category/title/body/meta),
  // plus any extended-detail fragment the source can find. The modal
  // never embeds the card's DOM directly — rebuilding it from data
  // also drops the "Open →" action that no longer makes sense.
  modalBody.innerHTML = '';
  modalBody.appendChild(buildModalHeader(data));
  if (data.detailFragment) modalBody.appendChild(data.detailFragment);

  // Bubble sources get the circular dialog treatment so the FLIP
  // stays round-to-round.
  modalDialog.classList.toggle('bubble-mode', sourceEl.classList.contains('bubble'));

  // Inherit the source's colour hue so the modal's accents (top bar,
  // eyebrow chip, detail headings) match the card/bubble you clicked.
  // Cards/bubbles already carry a .hue-* class; entries don't, so we
  // fall back to running the keyword classifier on the data we have.
  modalDialog.classList.remove('hue-blue', 'hue-red', 'hue-green');
  const sourceHue = hueClassOf(sourceEl) || hueClassFor(data);
  if (sourceHue) modalDialog.classList.add(sourceHue);

  // If source is a bubble, pause its wander for the duration.
  const b = findBubble(sourceEl);
  if (b) b.paused = true;

  // Hide the source so it appears to have become the modal.
  sourceEl.style.visibility = 'hidden';

  // Reveal modal in its final position, capture rects, then snap
  // backwards to the source rect so the next frame's transition
  // morphs it outward.
  modalDialog.style.transition = 'none';
  modalDialog.style.transform  = '';
  modalDialog.style.opacity    = '';
  modalRoot.hidden = false;
  modalRoot.classList.remove('open');

  // Force layout for accurate target measurement.
  modalDialog.offsetHeight;
  const target = modalDialog.getBoundingClientRect();
  const source = sourceEl.getBoundingClientRect();

  modalDialog.style.transform = flipTransform(source, target);
  modalDialog.style.opacity   = '0.6';

  requestAnimationFrame(() => {
    modalDialog.style.transition = '';
    modalRoot.classList.add('open');
    modalDialog.style.transform = '';
    modalDialog.style.opacity   = '1';
  });

  const focusClose = (e) => {
    if (e.propertyName !== 'transform') return;
    modalDialog.removeEventListener('transitionend', focusClose);
    modalRoot.querySelector('.modal-close')?.focus();
  };
  modalDialog.addEventListener('transitionend', focusClose);
}

function closeModal() {
  if (modalRoot.hidden) return;

  if (!currentSourceShell) {
    modalRoot.hidden = true;
    modalRoot.classList.remove('open');
    return;
  }

  const source = currentSourceShell.getBoundingClientRect();
  const target = modalDialog.getBoundingClientRect();

  modalRoot.classList.remove('open');
  modalDialog.style.transform = flipTransform(source, target);
  modalDialog.style.opacity   = '0';

  const finish = (e) => {
    if (e.propertyName !== 'transform') return;
    modalDialog.removeEventListener('transitionend', finish);
    modalRoot.hidden = true;
    modalDialog.style.transform = '';
    modalDialog.style.opacity   = '';
    modalDialog.classList.remove('bubble-mode');
    modalDialog.classList.remove('hue-blue', 'hue-red', 'hue-green');
    modalBody.innerHTML = '';
    if (currentSourceShell) {
      currentSourceShell.style.visibility = '';
      const b = findBubble(currentSourceShell);
      if (b) b.paused = false;
      currentSourceShell = null;
    }
    if (lastFocus && typeof lastFocus.focus === 'function') {
      lastFocus.focus();
      lastFocus = null;
    }
  };
  modalDialog.addEventListener('transitionend', finish);
}

modalRoot.addEventListener('click', (e) => {
  if (e.target.closest('[data-modal-close]')) closeModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !modalRoot.hidden) closeModal();
});

// Activity-panel entries → open modal. Document-level delegation so
// the handler survives surgical re-renders of .panel-scroll. Only
// fires when no modal is currently open (avoids re-triggering during
// FLIP transitions). Keyboard: Enter / Space on a focused entry does
// the same thing — entries are role="button" tabindex="0".
document.addEventListener('click', (e) => {
  if (!modalRoot.hidden) return;
  const entry = e.target.closest('.entry');
  if (!entry) return;
  openModal(entry);
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  if (!modalRoot.hidden) return;
  const entry = e.target.closest && e.target.closest('.entry');
  if (!entry) return;
  e.preventDefault();
  openModal(entry);
});

// ----- Floating-bubbles view --------------------------------------
// An alternative to the floating cards: one bubble per .card-shell,
// random size, slow RAF-driven wander with edge bouncing. Hover
// pauses the bubble and reveals its body text; click opens the same
// modal pipeline used by the cards, but in round "bubble-mode".
const bubbleLayer = document.getElementById('bubbleLayer');
const viewToggle  = document.getElementById('viewToggle');
const bubbles     = [];
let   bubbleRaf   = null;

function findBubble(el) {
  return bubbles.find(b => b.el === el) || null;
}

function buildBubbles(mode) {
  bubbleLayer.innerHTML = '';
  bubbles.length = 0;

  // In "bubbles" mode every shell becomes a bubble.
  // In "both" mode only .bubble-only shells do — the three primary
  // cards keep their card representation.
  const selector = (mode === 'both')
    ? '.card-shell.bubble-only'
    : '.card-shell';
  const shells = Array.from(document.querySelectorAll(selector));
  shells.forEach((shell) => {
    const data = collectSourceData(shell);
    if (!data) return;
    const key = (shell.className.match(/\bc\d+\b/) || [''])[0];

    const size = 150 + Math.random() * 70;  // 150–220 px

    const el = document.createElement('div');
    el.className = 'bubble';
    if (shell.classList.contains('openable')) el.classList.add('openable');
    const shellHue = hueClassOf(shell);
    if (shellHue) el.classList.add(shellHue);
    el.style.width  = size + 'px';
    el.style.height = size + 'px';
    el.dataset.sourceKey = key;
    el.dataset.category  = data.category;
    el.dataset.title     = data.title;
    el.dataset.body      = data.body;
    el.dataset.meta      = data.meta;

    const scale = document.createElement('div');
    scale.className = 'bubble-scale';
    const inner = document.createElement('div');
    inner.className = 'bubble-inner';

    if (data.category) {
      const eb = document.createElement('div');
      eb.className = 'bubble-eyebrow';
      eb.textContent = data.category;
      inner.append(eb);
    }
    const t = document.createElement('div');
    t.className = 'bubble-title';
    t.textContent = data.title;
    inner.append(t);
    if (data.body) {
      const d = document.createElement('div');
      d.className = 'bubble-desc';
      d.textContent = data.body;
      inner.append(d);
    }
    scale.append(inner);
    el.append(scale);
    bubbleLayer.append(el);

    // Random starting point, kept clear of the header (56px) and
    // mic bar (~120px).
    const w = window.innerWidth;
    const h = window.innerHeight;
    const x = 20 + Math.random() * Math.max(0, w - size - 40);
    const y = 72 + Math.random() * Math.max(0, h - size - 200);

    const speed = 0.25 + Math.random() * 0.35;
    const angle = Math.random() * Math.PI * 2;

    const b = {
      el,
      x, y,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      size,
      paused: false,
    };
    bubbles.push(b);
    applyBubblePos(b);

    // Drag + hover + click. A gesture under DRAG_THRESHOLD px counts
    // as a click (opens the modal); past the threshold it's a drag
    // that moves the bubble. On release from a drag the bubble
    // unpauses and resumes wandering from where it was dropped.
    let startX = 0, startY = 0;
    let offsetX = 0, offsetY = 0;
    let dragStarted = false;
    let activePointerId = null;

    el.addEventListener('pointerenter', () => {
      if (!dragStarted) b.paused = true;
    });
    el.addEventListener('pointerleave', () => {
      if (!dragStarted) b.paused = false;
    });

    el.addEventListener('pointerdown', (e) => {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      startX  = e.clientX;
      startY  = e.clientY;
      offsetX = e.clientX - b.x;
      offsetY = e.clientY - b.y;
      dragStarted = false;
      activePointerId = e.pointerId;
      el.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    el.addEventListener('pointermove', (e) => {
      if (e.pointerId !== activePointerId) return;
      if (!dragStarted) {
        const dx = e.clientX - startX;
        const dy = e.clientY - startY;
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
        dragStarted = true;
        el.classList.add('dragging');
        b.paused = true;
      }
      const m = BUBBLE_MARGIN;
      const w = window.innerWidth;
      const h = window.innerHeight;
      b.x = Math.max(m.left, Math.min(w - m.right  - b.size, e.clientX - offsetX));
      b.y = Math.max(m.top,  Math.min(h - m.bottom - b.size, e.clientY - offsetY));
      applyBubblePos(b);
    });

    const endPointer = (e) => {
      if (e.pointerId !== activePointerId) return;
      try { el.releasePointerCapture(activePointerId); } catch (_) {}
      activePointerId = null;
      if (dragStarted) {
        el.classList.remove('dragging');
        b.paused = false; // resume wander from the drop position
      } else {
        openModal(el);
      }
      dragStarted = false;
    };
    el.addEventListener('pointerup',     endPointer);
    el.addEventListener('pointercancel', endPointer);
  });
}

function applyBubblePos(b) {
  b.el.style.transform = `translate3d(${b.x}px, ${b.y}px, 0)`;
}

// Keep bubbles clear of the 56px header and the bottom mic bar zone.
const BUBBLE_MARGIN = { top: 72, right: 16, bottom: 120, left: 16 };

// Pairwise circle-circle collision resolution. Each bubble is treated
// as a circle centred at (x + size/2, y + size/2) with radius size/2.
// Separates overlapping pairs along the collision normal, then
// reflects velocity components along that normal (equal-mass elastic
// swap). Paused bubbles — hovered or dragged — act like static walls:
// they don't move, and the free bubble reflects off them.
function resolveBubbleCollisions() {
  const n = bubbles.length;
  for (let i = 0; i < n; i++) {
    const a = bubbles[i];
    const ra = a.size * 0.5;
    const ax = a.x + ra;
    const ay = a.y + ra;

    for (let j = i + 1; j < n; j++) {
      const b = bubbles[j];
      const rb = b.size * 0.5;
      const dx = (b.x + rb) - ax;
      const dy = (b.y + rb) - ay;
      const rSum = ra + rb;
      const distSq = dx * dx + dy * dy;
      if (distSq >= rSum * rSum) continue;

      const dist = Math.sqrt(distSq) || 1e-6;
      const nx = dx / dist;
      const ny = dy / dist;
      const overlap = rSum - dist;

      // Separate along the collision normal. A paused bubble gets 0
      // share of the shove; a free one takes 1; two free ones split
      // the overlap evenly.
      const aMobile = a.paused ? 0 : 1;
      const bMobile = b.paused ? 0 : 1;
      const totalMobile = aMobile + bMobile;
      if (totalMobile === 0) continue;
      const aShare = aMobile / totalMobile;
      const bShare = bMobile / totalMobile;
      a.x -= nx * overlap * aShare;
      a.y -= ny * overlap * aShare;
      b.x += nx * overlap * bShare;
      b.y += ny * overlap * bShare;

      // Relative velocity along the normal — positive means already
      // separating, so nothing to resolve.
      const vrn = (b.vx - a.vx) * nx + (b.vy - a.vy) * ny;
      if (vrn >= 0) continue;

      if (totalMobile === 2) {
        // Equal-mass elastic: swap normal components.
        a.vx += vrn * nx;
        a.vy += vrn * ny;
        b.vx -= vrn * nx;
        b.vy -= vrn * ny;
      } else if (aMobile) {
        // b is a wall; reflect a's velocity along the normal.
        const van = a.vx * nx + a.vy * ny;
        a.vx -= 2 * van * nx;
        a.vy -= 2 * van * ny;
      } else {
        const vbn = b.vx * nx + b.vy * ny;
        b.vx -= 2 * vbn * nx;
        b.vy -= 2 * vbn * ny;
      }
    }
  }
}

function bubbleTick() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  const m = BUBBLE_MARGIN;

  // 1. Integrate.
  for (const b of bubbles) {
    if (b.paused) continue;
    b.x += b.vx;
    b.y += b.vy;
  }

  // 2. Bubble-bubble collisions (may shove positions & reflect velocities).
  resolveBubbleCollisions();

  // 3. Edge clamp + DOM write.
  for (const b of bubbles) {
    if (b.paused) continue;
    if (b.x <= m.left)                  { b.x = m.left;               b.vx = -b.vx; }
    if (b.y <= m.top)                   { b.y = m.top;                b.vy = -b.vy; }
    if (b.x + b.size >= w - m.right)    { b.x = w - m.right  - b.size; b.vx = -b.vx; }
    if (b.y + b.size >= h - m.bottom)   { b.y = h - m.bottom - b.size; b.vy = -b.vy; }
    applyBubblePos(b);
  }
  bubbleRaf = requestAnimationFrame(bubbleTick);
}

// ----- View-mode controller (Cards / Bubbles / Both / None) ------
const viewMenu   = document.getElementById('viewMenu');
const VIEW_KEY   = 'cora-view-mode';
const VALID_VIEW = new Set(['cards', 'bubbles', 'both', 'none']);
let currentView  = 'cards';

function applyViewMode(mode) {
  if (!VALID_VIEW.has(mode)) mode = 'cards';
  currentView = mode;

  // Always tear down any existing bubble field first.
  cancelAnimationFrame(bubbleRaf);
  bubbleRaf = null;
  bubbles.length = 0;
  bubbleLayer.innerHTML = '';

  if (mode === 'cards') {
    delete document.documentElement.dataset.view;
    bubbleLayer.setAttribute('aria-hidden', 'true');
  } else if (mode === 'none') {
    // Hide both layers; CSS does the visual hiding via [data-view="none"].
    document.documentElement.dataset.view = mode;
    bubbleLayer.setAttribute('aria-hidden', 'true');
  } else {
    document.documentElement.dataset.view = mode;
    bubbleLayer.setAttribute('aria-hidden', 'false');
    buildBubbles(mode);
    bubbleTick();
  }

  // Reflect state on menu items.
  if (viewMenu) {
    viewMenu.querySelectorAll('.view-option').forEach((opt) => {
      opt.setAttribute(
        'aria-checked',
        opt.dataset.viewMode === mode ? 'true' : 'false'
      );
    });
  }

  try { localStorage.setItem(VIEW_KEY, mode); } catch (_) {}
}

// Menu open/close wiring.
function openViewMenu() {
  if (!viewMenu) return;
  viewMenu.classList.add('open');
  viewToggle.setAttribute('aria-expanded', 'true');
}
function closeViewMenu() {
  if (!viewMenu) return;
  viewMenu.classList.remove('open');
  viewToggle.setAttribute('aria-expanded', 'false');
}

if (viewToggle && viewMenu) {
  viewToggle.addEventListener('click', (e) => {
    e.stopPropagation();
    // Don't mutate state while the modal is open — wait for close.
    if (!modalRoot.hidden) return;
    if (viewMenu.classList.contains('open')) closeViewMenu();
    else openViewMenu();
  });
  viewMenu.addEventListener('click', (e) => {
    const btn = e.target.closest('.view-option');
    if (!btn) return;
    const mode = btn.dataset.viewMode;
    applyViewMode(mode);
    closeViewMenu();
  });
  // Click outside / Escape closes the menu.
  document.addEventListener('click', (e) => {
    if (!viewMenu.classList.contains('open')) return;
    if (e.target.closest('.view-switcher')) return;
    closeViewMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && viewMenu.classList.contains('open')) closeViewMenu();
  });
}

// Apply persisted choice on load.
let storedView = null;
try { storedView = localStorage.getItem(VIEW_KEY); } catch (_) {}
applyViewMode(VALID_VIEW.has(storedView) ? storedView : 'cards');

// ----- Orb settings modal (colour + idle transparency) ------------
// CSS variables --orb-rgb and --orb-alpha drive every orb accent;
// this block lets the user pick them. A head-side script already
// applied the saved values before first paint to avoid a flash.
const ORB_SETTINGS_KEY = 'cora-orb-settings';
const ORB_DEFAULT      = {
  color: '#35AE7D', alpha: 0.55, activeAlpha: 1, size: 88, center: 'default',
  plexusSize: 60, ringOrganic: 55, plexusOrganic: 44,
  ringOuter: true, ringInner: true,
  voiceReactivity: 150,  // 0..200 (% multiplier on live-audio orb morph)
};
const ORB_CENTERS      = new Set(['default', 'plexus']);
// Plexus cannot exceed the inner-wall diameter (outer shell inset: 8%).
const PLEXUS_MIN = 30;
const PLEXUS_MAX = 84;
// Slider 0-100 → SVG turbulence scale. Outer shell warps more than
// the inner ring at the same slider position so they don't read as
// parallel rings.
const RING_OUTER_MAX = 80;
const RING_INNER_MAX = 55;
// Slider 0-100 → plexus radial displacement in virtual units.
const PLEXUS_DISP_MAX = 16;

const settingsBtn            = document.getElementById('settingsBtn');
const orbColorInput          = document.getElementById('orbColorInput');
const orbAlphaInput          = document.getElementById('orbAlphaInput');
const orbActiveAlphaInput    = document.getElementById('orbActiveAlphaInput');
const orbSizeInput           = document.getElementById('orbSizeInput');
const orbRingOrganicInput    = document.getElementById('orbRingOrganicInput');
const orbRingOuterInput      = document.getElementById('orbRingOuterInput');
const orbRingInnerInput      = document.getElementById('orbRingInnerInput');
const orbPlexusSizeInput     = document.getElementById('orbPlexusSizeInput');
const orbPlexusOrganicInput  = document.getElementById('orbPlexusOrganicInput');
const orbVoiceReactivityInput= document.getElementById('orbVoiceReactivityInput');
const orbCenterInputs        = document.querySelectorAll('input[name="orbCenter"]');
const orbColorValueEl        = document.getElementById('orbColorValue');
const orbAlphaValueEl        = document.getElementById('orbAlphaValue');
const orbActiveAlphaValueEl  = document.getElementById('orbActiveAlphaValue');
const orbSizeValueEl         = document.getElementById('orbSizeValue');
const orbRingOrganicValueEl  = document.getElementById('orbRingOrganicValue');
const orbPlexusSizeValueEl   = document.getElementById('orbPlexusSizeValue');
const orbPlexusOrganicValueEl= document.getElementById('orbPlexusOrganicValue');
const orbVoiceReactivityValueEl = document.getElementById('orbVoiceReactivityValue');
const orbDefaultBtn          = document.getElementById('orbDefaultBtn');
const orbSaveBtn             = document.getElementById('orbSaveBtn');

// SVG feDisplacementMap nodes — slider writes their scale attribute.
const ringOuterDisplacement = document.querySelector('#orbTurbulence feDisplacementMap');
const ringInnerDisplacement = document.querySelector('#orbTurbulenceInner feDisplacementMap');

function hexToRgbString(hex) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `${r}, ${g}, ${b}`;
}

function applyOrbVars(color, alpha, activeAlpha, size, center, plexusSize, ringOrganic, plexusOrganic, ringOuter, ringInner, voiceReactivity) {
  const root = document.documentElement.style;
  root.setProperty('--orb-rgb',          hexToRgbString(color));
  root.setProperty('--orb-alpha',        String(alpha));
  root.setProperty('--orb-active-alpha', String(activeAlpha));
  root.setProperty('--orb-size',         size + 'vmin');
  if (center === 'plexus') document.documentElement.dataset.orbCenter = 'plexus';
  else delete document.documentElement.dataset.orbCenter;
  const clampedSize = Math.max(PLEXUS_MIN, Math.min(PLEXUS_MAX, plexusSize));
  root.setProperty('--plexus-inset', ((100 - clampedSize) / 2) + '%');

  // Ring organic → SVG turbulence scale on each filter.
  const ro = Math.max(0, Math.min(100, ringOrganic));
  if (ringOuterDisplacement) {
    ringOuterDisplacement.setAttribute('scale', String((ro / 100) * RING_OUTER_MAX));
  }
  if (ringInnerDisplacement) {
    ringInnerDisplacement.setAttribute('scale', String((ro / 100) * RING_INNER_MAX));
  }

  // Plexus organic → radial displacement magnitude (picked up by
  // the canvas tick via window.__plexusDisp).
  const po = Math.max(0, Math.min(100, plexusOrganic));
  window.__plexusDisp = (po / 100) * PLEXUS_DISP_MAX;

  // Ring visibility toggles — absent attribute = on (default), "off"
  // triggers the CSS rules that strip the ring.
  if (ringOuter === false) document.documentElement.dataset.ringOuter = 'off';
  else delete document.documentElement.dataset.ringOuter;
  if (ringInner === false) document.documentElement.dataset.ringInner = 'off';
  else delete document.documentElement.dataset.ringInner;

  // Voice reactivity: 0..200 slider → 0..2.0 multiplier on live-audio
  // orb morph + plexus DISP (read by driveSpeechOrb / driveOrb /
  // plexus tick from window.__voiceReactivity).
  const vr = Math.max(0, Math.min(200, voiceReactivity ?? 100));
  window.__voiceReactivity = vr / 100;
}

function readSavedOrbSettings() {
  try {
    const raw = localStorage.getItem(ORB_SETTINGS_KEY);
    if (!raw) return { ...ORB_DEFAULT };
    const parsed = JSON.parse(raw);
    const color = /^#[0-9A-Fa-f]{6}$/.test(parsed.color) ? parsed.color : ORB_DEFAULT.color;
    const alpha = (typeof parsed.alpha === 'number' && parsed.alpha >= 0 && parsed.alpha <= 1)
      ? parsed.alpha : ORB_DEFAULT.alpha;
    const activeAlpha = (typeof parsed.activeAlpha === 'number' && parsed.activeAlpha >= 0 && parsed.activeAlpha <= 1)
      ? parsed.activeAlpha : ORB_DEFAULT.activeAlpha;
    const size  = (typeof parsed.size === 'number' && parsed.size >= 10 && parsed.size <= 200)
      ? parsed.size : ORB_DEFAULT.size;
    const center = ORB_CENTERS.has(parsed.center) ? parsed.center : ORB_DEFAULT.center;
    const plexusSize = (typeof parsed.plexusSize === 'number'
      && parsed.plexusSize >= PLEXUS_MIN && parsed.plexusSize <= PLEXUS_MAX)
      ? parsed.plexusSize : ORB_DEFAULT.plexusSize;
    const ringOrganic = (typeof parsed.ringOrganic === 'number'
      && parsed.ringOrganic >= 0 && parsed.ringOrganic <= 100)
      ? parsed.ringOrganic : ORB_DEFAULT.ringOrganic;
    const plexusOrganic = (typeof parsed.plexusOrganic === 'number'
      && parsed.plexusOrganic >= 0 && parsed.plexusOrganic <= 100)
      ? parsed.plexusOrganic : ORB_DEFAULT.plexusOrganic;
    const ringOuter = (typeof parsed.ringOuter === 'boolean')
      ? parsed.ringOuter : ORB_DEFAULT.ringOuter;
    const ringInner = (typeof parsed.ringInner === 'boolean')
      ? parsed.ringInner : ORB_DEFAULT.ringInner;
    const voiceReactivity = (typeof parsed.voiceReactivity === 'number'
      && parsed.voiceReactivity >= 0 && parsed.voiceReactivity <= 200)
      ? parsed.voiceReactivity : ORB_DEFAULT.voiceReactivity;
    return {
      color, alpha, activeAlpha, size, center, plexusSize,
      ringOrganic, plexusOrganic, ringOuter, ringInner, voiceReactivity,
    };
  } catch (_) { return { ...ORB_DEFAULT }; }
}

function writeOrbSettings(settings) {
  try { localStorage.setItem(ORB_SETTINGS_KEY, JSON.stringify(settings)); } catch (_) {}
}

function syncOrbInputs(color, alpha, activeAlpha, size, center, plexusSize, ringOrganic, plexusOrganic, ringOuter, ringInner, voiceReactivity) {
  orbColorInput.value    = color;
  orbColorValueEl.textContent = color.toUpperCase();
  orbAlphaInput.value    = Math.round(alpha * 100);
  orbAlphaValueEl.textContent = Math.round(alpha * 100) + '%';
  if (orbActiveAlphaInput)    orbActiveAlphaInput.value = Math.round(activeAlpha * 100);
  if (orbActiveAlphaValueEl)  orbActiveAlphaValueEl.textContent = Math.round(activeAlpha * 100) + '%';
  orbSizeInput.value     = Math.round(size);
  orbSizeValueEl.textContent = Math.round(size) + '%';
  orbPlexusSizeInput.value = Math.round(plexusSize);
  orbPlexusSizeValueEl.textContent = Math.round(plexusSize) + '%';
  orbRingOrganicInput.value = Math.round(ringOrganic);
  orbRingOrganicValueEl.textContent = Math.round(ringOrganic) + '%';
  orbPlexusOrganicInput.value = Math.round(plexusOrganic);
  orbPlexusOrganicValueEl.textContent = Math.round(plexusOrganic) + '%';
  if (orbVoiceReactivityInput) {
    const vr = Math.round(voiceReactivity ?? ORB_DEFAULT.voiceReactivity);
    orbVoiceReactivityInput.value = vr;
    orbVoiceReactivityValueEl.textContent = vr + '%';
  }
  orbRingOuterInput.checked = !!ringOuter;
  orbRingInnerInput.checked = !!ringInner;
  orbCenterInputs.forEach((input) => { input.checked = input.value === center; });
}

function currentCenterChoice() {
  for (const input of orbCenterInputs) if (input.checked) return input.value;
  return ORB_DEFAULT.center;
}

// Remember the last-saved snapshot so close-without-save reverts.
let orbSnapshot = readSavedOrbSettings();

function applySnapshot(s) {
  applyOrbVars(
    s.color, s.alpha, s.activeAlpha, s.size, s.center,
    s.plexusSize, s.ringOrganic, s.plexusOrganic,
    s.ringOuter, s.ringInner, s.voiceReactivity
  );
}
function syncFromSnapshot(s) {
  syncOrbInputs(
    s.color, s.alpha, s.activeAlpha, s.size, s.center,
    s.plexusSize, s.ringOrganic, s.plexusOrganic,
    s.ringOuter, s.ringInner, s.voiceReactivity
  );
}

// Helpers used by the unified Main Settings modal — capture the
// orb's currently-saved state when opening so closing without Save
// can revert any live-preview tweaks. closeMainSettingsModal calls
// `revertOrbToSnapshot()` when the user dismisses without saving.
function syncOrbInputsFromSaved() {
  orbSnapshot = readSavedOrbSettings();
  syncFromSnapshot(orbSnapshot);
}
function revertOrbToSnapshot() {
  applySnapshot(orbSnapshot);
}

if (orbColorInput) {
  // Live preview while dragging the controls.
  const previewFromInputs = () => {
    const snap = {
      color:         orbColorInput.value,
      alpha:         Number(orbAlphaInput.value) / 100,
      activeAlpha:   Number(orbActiveAlphaInput?.value ?? 100) / 100,
      size:          Number(orbSizeInput.value),
      center:        currentCenterChoice(),
      plexusSize:    Number(orbPlexusSizeInput.value),
      ringOrganic:   Number(orbRingOrganicInput.value),
      plexusOrganic: Number(orbPlexusOrganicInput.value),
      ringOuter:     orbRingOuterInput.checked,
      ringInner:     orbRingInnerInput.checked,
      voiceReactivity: Number(orbVoiceReactivityInput?.value ?? ORB_DEFAULT.voiceReactivity),
    };
    applySnapshot(snap);
    orbColorValueEl.textContent        = snap.color.toUpperCase();
    orbAlphaValueEl.textContent        = orbAlphaInput.value + '%';
    if (orbActiveAlphaValueEl && orbActiveAlphaInput) {
      orbActiveAlphaValueEl.textContent = orbActiveAlphaInput.value + '%';
    }
    orbSizeValueEl.textContent         = orbSizeInput.value + '%';
    orbPlexusSizeValueEl.textContent   = orbPlexusSizeInput.value + '%';
    orbRingOrganicValueEl.textContent  = orbRingOrganicInput.value + '%';
    orbPlexusOrganicValueEl.textContent= orbPlexusOrganicInput.value + '%';
    if (orbVoiceReactivityValueEl && orbVoiceReactivityInput) {
      orbVoiceReactivityValueEl.textContent = orbVoiceReactivityInput.value + '%';
    }
  };
  orbColorInput.addEventListener('input', previewFromInputs);
  orbAlphaInput.addEventListener('input', previewFromInputs);
  if (orbActiveAlphaInput) orbActiveAlphaInput.addEventListener('input', previewFromInputs);
  orbSizeInput.addEventListener('input',  previewFromInputs);
  orbPlexusSizeInput.addEventListener('input', previewFromInputs);
  orbRingOrganicInput.addEventListener('input', previewFromInputs);
  orbPlexusOrganicInput.addEventListener('input', previewFromInputs);
  if (orbVoiceReactivityInput) orbVoiceReactivityInput.addEventListener('input', previewFromInputs);
  orbRingOuterInput.addEventListener('change', previewFromInputs);
  orbRingInnerInput.addEventListener('change', previewFromInputs);
  orbCenterInputs.forEach((input) => input.addEventListener('change', previewFromInputs));

  if (orbDefaultBtn) {
    orbDefaultBtn.addEventListener('click', () => {
      // Reset inputs + live preview, but don't persist until Save.
      syncFromSnapshot(ORB_DEFAULT);
      applySnapshot(ORB_DEFAULT);
    });
  }

  if (orbSaveBtn) {
    orbSaveBtn.addEventListener('click', () => {
      const next = {
        color:         orbColorInput.value,
        alpha:         Number(orbAlphaInput.value) / 100,
        activeAlpha:   Number(orbActiveAlphaInput?.value ?? 100) / 100,
        size:          Number(orbSizeInput.value),
        center:        currentCenterChoice(),
        plexusSize:    Number(orbPlexusSizeInput.value),
        ringOrganic:   Number(orbRingOrganicInput.value),
        plexusOrganic: Number(orbPlexusOrganicInput.value),
        ringOuter:     orbRingOuterInput.checked,
        ringInner:     orbRingInnerInput.checked,
        voiceReactivity: Number(orbVoiceReactivityInput?.value ?? ORB_DEFAULT.voiceReactivity),
      };
      writeOrbSettings(next);
      applySnapshot(next);
      orbSnapshot = next;
    });
  }

  // Apply snapshot once on load so the filters pick up saved ring
  // organic (which the inline head script can't touch pre-parse).
  applySnapshot(readSavedOrbSettings());
}

// ----- Plexus center: wireframe dot-sphere with connecting lines -
// Points are distributed on a unit sphere via Fibonacci spiral, then
// pairs within CONN_THRESHOLD are joined by line segments. Each
// frame we rotate every point about the Y axis and update:
//   - dots: cx, r, opacity (from rotated Z)
//   - lines: x1/x2, stroke-width, opacity (from avg rotated Z)
// Only the rotated X and Z change each frame; Y is set once at init.
(function buildPlexusCenter() {
  const host = document.getElementById('speechOrbPlexus');
  if (!host) return;
  const canvas = host.querySelector('canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  const N = 3000;                      // dense, small, non-uniform
  const R = 90;                        // sphere radius in virtual units (-100..100)
  const phi = Math.PI * (3 - Math.sqrt(5));

  // RGB↔HSL for deriving warm/cool gradient endpoints from the orb colour.
  function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    const l = (max + min) / 2;
    if (max === min) return { h: 0, s: 0, l: l * 100 };
    const d = max - min;
    const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    let h;
    if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    return { h: h * 60, s: s * 100, l: l * 100 };
  }
  function hslToRgb(h, s, l) {
    s /= 100; l /= 100;
    const c = (1 - Math.abs(2 * l - 1)) * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = l - c / 2;
    let r, g, b;
    const H = ((h % 360) + 360) % 360;
    if (H < 60)       [r, g, b] = [c, x, 0];
    else if (H < 120) [r, g, b] = [x, c, 0];
    else if (H < 180) [r, g, b] = [0, c, x];
    else if (H < 240) [r, g, b] = [0, x, c];
    else if (H < 300) [r, g, b] = [x, 0, c];
    else              [r, g, b] = [c, 0, x];
    return [
      Math.round((r + m) * 255),
      Math.round((g + m) * 255),
      Math.round((b + m) * 255),
    ];
  }

  // Generate jittered body-space points. Fibonacci base + small
  // angle/radius/size perturbation so the field doesn't read as a
  // perfect lattice.
  const pts = new Array(N);
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2;
    const ring = Math.sqrt(1 - y * y);
    const theta = phi * i + (Math.random() - 0.5) * 0.18;  // angle jitter
    const rScale = 1 + (Math.random() - 0.5) * 0.035;      // radius jitter
    const rr = R * rScale;
    const x = Math.cos(theta) * ring * rr;
    const yv = y * rr;
    const z = Math.sin(theta) * ring * rr;
    pts[i] = {
      x, y: yv, z,
      ux: x / rr, uy: y, uz: z / rr,
      sizeBase: 0.45 + Math.random() * 0.75,        // per-dot size variety
      mixJitter: (Math.random() - 0.5) * 0.18,      // per-dot hue variance
      bodyX: x,                                     // stored for gradient mix
    };
  }

  // ~11° X-axis tilt.
  const tiltAngle = 0.20;
  const cosT = Math.cos(tiltAngle);
  const sinT = Math.sin(tiltAngle);
  for (const p of pts) {
    const ny  = p.y  * cosT - p.z  * sinT;
    const nz  = p.y  * sinT + p.z  * cosT;
    const nuy = p.uy * cosT - p.uz * sinT;
    const nuz = p.uy * sinT + p.uz * cosT;
    p.y = ny; p.z = nz; p.uy = nuy; p.uz = nuz;
  }

  // Per-dot colour = orb colour hue-shifted by position. Recomputed
  // only when --orb-rgb changes (so the gradient updates live when
  // the settings modal slides the colour picker).
  let lastOrbRgb = '';
  function refreshColours() {
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue('--orb-rgb').trim();
    if (!v || v === lastOrbRgb) return;
    lastOrbRgb = v;
    const parts = v.split(',').map((s) => parseInt(s.trim(), 10));
    const [r, g, b] = parts.length === 3 && parts.every(Number.isFinite)
      ? parts : [53, 174, 125];
    const hsl = rgbToHsl(r, g, b);
    // Warm side = hue - 35°, cool side = hue + 35° (relative to orb).
    // Boost saturation a touch so the gradient reads against the
    // glassy shell without shouting.
    const satBoost = Math.min(100, hsl.s + 25);
    const warm = hslToRgb(hsl.h - 35, satBoost, hsl.l);
    const cool = hslToRgb(hsl.h + 35, satBoost, hsl.l);
    for (const p of pts) {
      const mix = Math.max(0, Math.min(1,
        (p.bodyX + R) / (2 * R) + p.mixJitter));
      p.r = Math.round(warm[0] + (cool[0] - warm[0]) * mix);
      p.g = Math.round(warm[1] + (cool[1] - warm[1]) * mix);
      p.b = Math.round(warm[2] + (cool[2] - warm[2]) * mix);
    }
  }
  refreshColours();

  // Canvas sizing — fits the host and respects devicePixelRatio for
  // crisp dots at any screen density.
  let cssW = 200, cssH = 200, dpr = 1;
  function sizeCanvas() {
    dpr = window.devicePixelRatio || 1;
    const rect = host.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    cssW = rect.width;
    cssH = rect.height;
    canvas.width  = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
  }
  sizeCanvas();
  window.addEventListener('resize', sizeCanvas);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(sizeCanvas).observe(host);
  }

  // Cheap 3D flow-noise: four out-of-phase sine layers.
  function flowNoise(x, y, z, t) {
    return (
      Math.sin(x * 0.044 + y * 0.032 + t * 0.70) +
      Math.cos(z * 0.050 - y * 0.041 + t * 0.52) +
      Math.sin((x + z) * 0.038       + t * 0.38) +
      Math.cos(y * 0.058 + x * 0.024 - t * 0.58)
    ) * 0.25; // ~[-1, 1]
  }

  let angle = 0;
  let lastFrame = performance.now();

  function tick(now) {
    const dt = Math.min(0.05, (now - lastFrame) / 1000);
    lastFrame = now;

    if (document.documentElement.dataset.orbCenter === 'plexus') {
      refreshColours();
      // window.__plexusDisp is set by the settings modal (and by the
      // inline head script from saved settings); fall back if unset.
      const baseDisp = (typeof window.__plexusDisp === 'number') ? window.__plexusDisp : 7;
      // When Cora is actively speaking, push the displacement higher
      // so the plexus visibly pulses with her voice. The level (0..1)
      // is set by the AnalyserNode in the voice IIFE.
      const speaking = document.documentElement.dataset.voiceState === 'speaking';
      const live = (speaking && typeof window.CORA?.voiceLevel === 'number')
        ? window.CORA.voiceLevel : 0;
      const reactivity = (typeof window.__voiceReactivity === 'number')
        ? window.__voiceReactivity : 1;
      const DISP = baseDisp + live * 9 * reactivity;
      // Slightly faster swirl while speaking too — also reactivity-scaled.
      angle += dt * (0.14 + live * 0.18 * reactivity);
      const cosA = Math.cos(angle);
      const sinA = Math.sin(angle);
      const t = now * 0.001;

      // Set up virtual coord system: origin at canvas centre, 100
      // virtual units per half-side. Then clear and draw.
      const s = Math.min(cssW, cssH) / 200 * dpr;
      ctx.setTransform(s, 0, 0, s, (cssW * dpr) / 2, (cssH * dpr) / 2);
      ctx.clearRect(-100, -100, 200, 200);
      // Additive blending: overlapping bright dots compound into
      // glowing wave crests, exactly like the reference.
      ctx.globalCompositeOperation = 'lighter';

      for (let i = 0; i < N; i++) {
        const p = pts[i];

        const flow = flowNoise(p.x, p.y, p.z, t);
        const flowNorm = (flow + 1) * 0.5;

        const disp = flow * DISP;
        const bx = p.x + p.ux * disp;
        const by = p.y + p.uy * disp;
        const bz = p.z + p.uz * disp;

        const rx =  bx * cosA + bz * sinA;
        const rz = -bx * sinA + bz * cosA;
        const zNorm = (rz + R) / (2 * R);

        // Brightness from depth and flow together.
        const bright = Math.pow(zNorm, 1.1) * (0.18 + flowNorm * 0.92);
        if (bright < 0.015) continue;       // skip invisible

        const size = p.sizeBase * (0.35 + bright * 1.1);
        const op   = Math.min(1, bright * 0.95);

        ctx.fillStyle = `rgba(${p.r},${p.g},${p.b},${op.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(rx, by, size, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
})();

// ----- Main settings modal (app background) ----------------------
// Lets the user override the app background with either a flat
// colour or an image (URL or uploaded file). Live-preview while
// editing; Save persists; Default reverts to the app's original
// background (var(--cosmic-base) per theme — i.e. no inline override).
// The inline head script in index.html applies any persisted choice
// before first paint via a <style id="cora-main-settings-style">.
const MAIN_SETTINGS_KEY = 'cora-main-settings';
const REFRESH_INTERVAL_MAX = 86400; // 24h cap, mirrors bootstrap.js
const MAIN_DEFAULT      = {
  type: 'color', color: '', image: '',
  refreshIntervalSeconds: 0,
  showStatusIndicator: false,    // IDLE/LISTENING/PROCESSING — off by default
};

const mainSettingsModal = document.getElementById('mainSettingsModal');
const bgTypeInputs      = document.querySelectorAll('input[name="bgType"]');
const bgColorInput      = document.getElementById('bgColorInput');
const bgColorValueEl    = document.getElementById('bgColorValue');
const bgImageUrlInput   = document.getElementById('bgImageUrlInput');
const bgImageFileInput  = document.getElementById('bgImageFileInput');
const bgImageFileHint   = document.getElementById('bgImageFileHint');
const bgImagePreview    = document.getElementById('bgImagePreview');
const bgColorSection    = document.getElementById('bgColorSection');
const bgImageSection    = document.getElementById('bgImageSection');
const refreshIntervalInput = document.getElementById('refreshIntervalInput');
const statusIndicatorInput = document.getElementById('statusIndicatorInput');
const mainDefaultBtn    = document.getElementById('mainDefaultBtn');
const mainSaveBtn       = document.getElementById('mainSaveBtn');

function clampInterval(value) {
  const m = Number(value);
  if (!Number.isFinite(m) || m <= 0) return 0;
  return Math.min(REFRESH_INTERVAL_MAX, Math.floor(m));
}

function readMainSettings() {
  try {
    const raw = localStorage.getItem(MAIN_SETTINGS_KEY);
    if (!raw) return { ...MAIN_DEFAULT };
    const parsed = JSON.parse(raw);
    // Migrate any old refreshIntervalMinutes value forward (×60). After
    // first save it'll be written as seconds-only and the legacy field
    // can be cleaned up.
    let seconds = parsed.refreshIntervalSeconds;
    if (seconds == null && parsed.refreshIntervalMinutes != null) {
      seconds = Number(parsed.refreshIntervalMinutes) * 60;
    }
    return {
      type:  (parsed.type === 'image' || parsed.type === 'color')
        ? parsed.type : MAIN_DEFAULT.type,
      color: typeof parsed.color === 'string' ? parsed.color : MAIN_DEFAULT.color,
      image: typeof parsed.image === 'string' ? parsed.image : MAIN_DEFAULT.image,
      refreshIntervalSeconds: clampInterval(seconds),
      showStatusIndicator: parsed.showStatusIndicator === true,
    };
  } catch (_) { return { ...MAIN_DEFAULT }; }
}
function writeMainSettings(s) {
  try { localStorage.setItem(MAIN_SETTINGS_KEY, JSON.stringify(s)); }
  catch (_) {
    // localStorage may reject very large data URLs (~5MB cap). Keep
    // the in-memory preview but warn the user.
    console.warn('Could not save background — image may be too large.');
  }
}

// Apply settings to <body>. Empty/invalid values clear the override
// so the CSS default (var(--cosmic-base)) takes back over. When a
// custom background is set we also force --canvas-opacity to 0 so
// the WebGL nebula doesn't bury the user's chosen colour/image; the
// speech-overlay orb is a separate CSS layer and stays visible.
function applyMainSettings(s) {
  const body = document.body;
  body.style.backgroundImage      = '';
  body.style.backgroundColor      = '';
  body.style.backgroundSize       = '';
  body.style.backgroundPosition   = '';
  body.style.backgroundRepeat     = '';
  body.style.backgroundAttachment = '';
  let active = false;
  if (s.type === 'image' && s.image) {
    const safe = s.image.replace(/"/g, '\\"');
    body.style.backgroundImage      = `url("${safe}")`;
    body.style.backgroundSize       = 'cover';
    body.style.backgroundPosition   = 'center center';
    body.style.backgroundRepeat     = 'no-repeat';
    body.style.backgroundAttachment = 'fixed';
    active = true;
  } else if (s.type === 'color' && s.color
          && /^#[0-9A-Fa-f]{6}$/.test(s.color)) {
    body.style.backgroundColor = s.color;
    active = true;
  }
  if (active) {
    document.documentElement.style.setProperty('--canvas-opacity', '0');
  } else {
    document.documentElement.style.removeProperty('--canvas-opacity');
  }
  // Show / hide the IDLE/LISTENING/PROCESSING indicator. The pre-paint
  // script in <head> sets data-status-indicator="off" by default; here
  // we apply the persisted choice (or revert on cancel).
  if (s.showStatusIndicator === true) {
    delete document.documentElement.dataset.statusIndicator;
  } else {
    document.documentElement.dataset.statusIndicator = 'off';
  }
  // Once we've taken control of body inline styles, drop the
  // pre-paint <style> tag so it doesn't double-apply.
  const preStyle = document.getElementById('cora-main-settings-style');
  if (preStyle) preStyle.remove();
}

function syncBgSectionVisibility(type) {
  if (bgColorSection) bgColorSection.hidden = type !== 'color';
  if (bgImageSection) bgImageSection.hidden = type !== 'image';
}
function updateBgPreview(src) {
  if (!bgImagePreview) return;
  if (src) {
    bgImagePreview.style.backgroundImage = `url("${src.replace(/"/g, '\\"')}")`;
    bgImagePreview.classList.add('has-image');
  } else {
    bgImagePreview.style.backgroundImage = '';
    bgImagePreview.classList.remove('has-image');
  }
}
function syncMainInputs(s) {
  bgTypeInputs.forEach((i) => { i.checked = i.value === s.type; });
  const validColor = /^#[0-9A-Fa-f]{6}$/.test(s.color || '') ? s.color : '#0E0F13';
  bgColorInput.value = validColor;
  bgColorValueEl.textContent = validColor.toUpperCase();
  bgImageUrlInput.value = s.image || '';
  if (bgImageFileInput) bgImageFileInput.value = '';
  if (bgImageFileHint) bgImageFileHint.textContent = 'No file chosen';
  updateBgPreview(s.image || '');
  syncBgSectionVisibility(s.type);
  if (refreshIntervalInput) refreshIntervalInput.value = String(s.refreshIntervalSeconds ?? 0);
  if (statusIndicatorInput) statusIndicatorInput.checked = s.showStatusIndicator === true;
}
function currentBgType() {
  for (const i of bgTypeInputs) if (i.checked) return i.value;
  return MAIN_DEFAULT.type;
}

let mainSnapshot = readMainSettings();

function openMainSettingsModal() {
  if (!mainSettingsModal) return;
  mainSnapshot = readMainSettings();
  syncMainInputs(mainSnapshot);
  // Capture the saved orb state too — the orb tab lives inside this
  // modal now, and closing without Save should revert any live-preview
  // tweaks the user made on the orb tab. syncOrbInputsFromSaved is a
  // no-op if the orb inputs aren't in the DOM (defensive).
  if (typeof syncOrbInputsFromSaved === 'function') syncOrbInputsFromSaved();
  mainSettingsModal.hidden = false;
  requestAnimationFrame(() => mainSettingsModal.classList.add('open'));
}
function closeMainSettingsModal(revert) {
  if (!mainSettingsModal) return;
  if (revert) {
    applyMainSettings(mainSnapshot);
    // Revert orb tweaks too — the orb's own Save button updates
    // orbSnapshot in place, so nothing to revert if the user
    // already saved on the orb tab.
    if (typeof revertOrbToSnapshot === 'function') revertOrbToSnapshot();
  }
  mainSettingsModal.classList.remove('open');
  setTimeout(() => { mainSettingsModal.hidden = true; }, 280);
}

if (mainSettingsModal) {
  mainSettingsModal.addEventListener('click', (e) => {
    if (e.target.closest('[data-main-settings-close]')) closeMainSettingsModal(true);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !mainSettingsModal.hidden) closeMainSettingsModal(true);
  });

  // Live preview while editing — same pattern as the orb modal.
  const previewMain = () => {
    const type = currentBgType();
    syncBgSectionVisibility(type);
    const snap = {
      type,
      color: bgColorInput.value,
      image: bgImageUrlInput.value,
      // Include the checkbox state so editing the background doesn't
      // accidentally hide an enabled status indicator (or vice versa).
      showStatusIndicator: !!(statusIndicatorInput && statusIndicatorInput.checked),
    };
    applyMainSettings(snap);
    bgColorValueEl.textContent = bgColorInput.value.toUpperCase();
    updateBgPreview(snap.image);
  };
  bgTypeInputs.forEach((i) => i.addEventListener('change', previewMain));
  bgColorInput.addEventListener('input', previewMain);
  bgImageUrlInput.addEventListener('input', previewMain);
  if (statusIndicatorInput) statusIndicatorInput.addEventListener('change', previewMain);

  if (bgImageFileInput) {
    bgImageFileInput.addEventListener('change', () => {
      const file = bgImageFileInput.files && bgImageFileInput.files[0];
      if (!file) return;
      if (bgImageFileHint) bgImageFileHint.textContent = file.name;
      const reader = new FileReader();
      reader.onload = () => {
        bgImageUrlInput.value = String(reader.result);
        // Switch to image mode so the preview is meaningful.
        bgTypeInputs.forEach((i) => { i.checked = i.value === 'image'; });
        previewMain();
      };
      reader.readAsDataURL(file);
    });
  }

  mainDefaultBtn.addEventListener('click', () => {
    // Reset inputs + live preview, but don't persist until Save.
    syncMainInputs(MAIN_DEFAULT);
    applyMainSettings(MAIN_DEFAULT);
  });

  mainSaveBtn.addEventListener('click', () => {
    const next = {
      type:  currentBgType(),
      color: bgColorInput.value,
      image: bgImageUrlInput.value,
      refreshIntervalSeconds: clampInterval(refreshIntervalInput?.value),
      showStatusIndicator: !!(statusIndicatorInput && statusIndicatorInput.checked),
    };
    const intervalChanged = next.refreshIntervalSeconds !== mainSnapshot.refreshIntervalSeconds;
    writeMainSettings(next);
    applyMainSettings(next);
    mainSnapshot = next;
    closeMainSettingsModal(false);
    if (intervalChanged) {
      window.dispatchEvent(new CustomEvent('cora:refresh-interval-changed', {
        detail: { seconds: next.refreshIntervalSeconds },
      }));
    }
  });

  // Hand inline styles back to the body so the head <style> can be
  // dropped (otherwise both would compete after the user opens the
  // modal). No-op visually if nothing is saved.
  applyMainSettings(readMainSettings());
}

// ----- Main settings tabs + Voice picker --------------------------
// Tab switching for the redesigned Main settings modal: click a tab,
// the matching .settings-tab-panel becomes visible. Tabs share state
// with the modal (no animation between tabs — feels snappier than
// fading the panels in/out).
//
// Voice picker (the Voice tab):
//   - Fetches `/api/common-data?category=tts_voice` once, populates a
//     <select> with each voice's label + voice ID + lang_code.
//   - Restores the user's previously-saved choice from
//     localStorage['cora-voice-settings'] on open.
//   - Test button: POST `/api/tts-test` to Spark with {voice, text,
//     lang_code} → receive a WAV blob → play through the preview <audio>.
//   - Save: persists {voice, langCode} to localStorage so the next
//     mic-tap session can pass it to Spark via /api/offer query string.
(function setupMainSettingsTabs() {
  const modal = document.getElementById('mainSettingsModal');
  if (!modal) return;
  const tabBtns   = modal.querySelectorAll('.settings-tab[data-tab]');
  const tabPanels = modal.querySelectorAll('.settings-tab-panel[data-tab]');
  if (!tabBtns.length) return;

  function switchTo(tabName) {
    tabBtns.forEach((b) => {
      const active = b.dataset.tab === tabName;
      b.classList.toggle('active', active);
      b.setAttribute('aria-selected', String(active));
    });
    tabPanels.forEach((p) => {
      const active = p.dataset.tab === tabName;
      p.classList.toggle('active', active);
      p.hidden = !active;
    });
  }

  tabBtns.forEach((b) => {
    b.addEventListener('click', () => switchTo(b.dataset.tab));
  });
})();

(function setupVoicePicker() {
  const modal       = document.getElementById('mainSettingsModal');
  const select      = document.getElementById('voicePickerSelect');
  const desc        = document.getElementById('voicePickerDesc');
  const testText    = document.getElementById('voicePickerTestText');
  const testBtn     = document.getElementById('voicePickerTestBtn');
  const testBtnLbl  = document.getElementById('voicePickerTestBtnLabel');
  const status      = document.getElementById('voicePickerStatus');
  const previewAud  = document.getElementById('voicePickerPreviewAudio');
  if (!modal || !select || !testBtn || !previewAud) return;

  const STORAGE_KEY = 'cora-voice-settings';

  // Read saved voice settings; falls back to {} if nothing yet.
  function readSaved() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (_) { return {}; }
  }

  function writeSaved(obj) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(obj)); } catch (_) {}
  }

  // Spark voice service — same default + override pattern as the WebRTC
  // path (`cora-voice-endpoint` localStorage key set by setupVoice()).
  const DEFAULT_ENDPOINT = 'https://spark-a84c.tail343b33.ts.net';
  function voiceEndpoint() {
    return (localStorage.getItem('cora-voice-endpoint') || DEFAULT_ENDPOINT)
      .replace(/\/$/, '');
  }

  // Voices arrive once per modal open (cached after first fetch).
  let voicesLoaded = false;
  let voicesByValue = new Map();

  async function loadVoices() {
    if (voicesLoaded) return;
    select.innerHTML = '<option value="">Loading voices...</option>';
    try {
      const res = await fetch('/api/common-data?category=tts_voice', {
        cache: 'no-store',
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      const rows = await res.json();
      // Sort by display_order so the dropdown groups by accent + sex.
      rows.sort((a, b) => (a.display_order ?? 0) - (b.display_order ?? 0));
      select.innerHTML = '';
      for (const r of rows) {
        if (!r.is_active) continue;
        const opt = document.createElement('option');
        opt.value = r.value;
        opt.textContent = r.label || r.value;
        if (r.description) opt.title = r.description;
        select.appendChild(opt);
        voicesByValue.set(r.value, r);
      }
      voicesLoaded = true;
    } catch (err) {
      console.error('[voice-picker] load failed', err);
      select.innerHTML = '<option value="">(failed to load voices)</option>';
    }
  }

  function syncDescription() {
    const row = voicesByValue.get(select.value);
    desc.textContent = row?.description || '';
  }

  // Load + restore selection when the modal opens.
  // We don't have a clean "modal opened" hook; observe the [hidden]
  // attribute change instead. Cheap, runs only when the modal toggles.
  const observer = new MutationObserver(async () => {
    if (modal.hidden) return;
    await loadVoices();
    const saved = readSaved();
    if (saved.voice && voicesByValue.has(saved.voice)) {
      select.value = saved.voice;
    }
    syncDescription();
  });
  observer.observe(modal, { attributes: true, attributeFilter: ['hidden'] });

  select.addEventListener('change', syncDescription);

  // Save the chosen voice when the user clicks the modal's Save button.
  // The mainSaveBtn handler runs first; we just piggy-back on the same
  // click event so the persistence happens in the same moment the user
  // expects it.
  const mainSaveBtn = document.getElementById('mainSaveBtn');
  if (mainSaveBtn) {
    mainSaveBtn.addEventListener('click', () => {
      const v = select.value;
      const row = voicesByValue.get(v);
      if (!v || !row) return;
      const next = {
        voice: v,
        langCode: row.metadata?.lang_code || 'a',
      };
      writeSaved(next);
      console.log('[voice-picker] saved', next);
    });
  }

  // ----- Test button: synth + play preview ------------------
  testBtn.addEventListener('click', async () => {
    const voice = select.value;
    const row = voicesByValue.get(voice);
    if (!voice || !row) {
      status.textContent = 'Pick a voice first.';
      status.dataset.state = 'error';
      return;
    }
    const text = (testText.value || '').trim().slice(0, 400);
    if (!text) {
      status.textContent = 'Type something to test.';
      status.dataset.state = 'error';
      return;
    }

    status.textContent = 'Synthesising...';
    status.dataset.state = '';
    testBtn.dataset.state = 'loading';
    testBtnLbl.textContent = 'Synthesising...';

    try {
      const res = await fetch(`${voiceEndpoint()}/api/tts-test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        cache: 'no-store',
        body: JSON.stringify({
          voice,
          text,
          lang_code: row.metadata?.lang_code || 'a',
        }),
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => '');
        throw new Error(`HTTP ${res.status}: ${detail || res.statusText}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      // Wire up cleanup for the previous URL.
      if (previewAud.dataset.lastUrl) {
        URL.revokeObjectURL(previewAud.dataset.lastUrl);
      }
      previewAud.dataset.lastUrl = url;
      previewAud.src = url;
      previewAud.onended = () => {
        testBtn.dataset.state = '';
        testBtnLbl.textContent = 'Play sample';
        status.textContent = '';
      };
      testBtnLbl.textContent = 'Playing...';
      testBtn.dataset.state = 'playing';
      status.textContent = '';
      await previewAud.play();
    } catch (err) {
      console.error('[voice-picker] test failed', err);
      status.textContent = `Failed: ${err.message || err}`;
      status.dataset.state = 'error';
      testBtn.dataset.state = '';
      testBtnLbl.textContent = 'Play sample';
    }
  });
})();

// ----- Wake word ('Hey Cora') ------------------------------------
// Listens in the background using the browser's built-in
// SpeechRecognition API (Chrome/Edge). On a phrase match, dispatches
// the existing cora:mic-toggle event so the WebRTC voice flow opens
// the same way it does when the mic button is clicked.
//
// Off by default. Toggle in Main Settings → Voice → "Wake word".
// State stored in localStorage['cora-wake-word-enabled'].
//
// Pauses while a voice session is active so we don't fight WebRTC for
// the mic; resumes when the session ends.
//
// Future: swap the SpeechRecognition backend for a local Porcupine
// WASM model (better privacy + offline) without changing the wiring.
(function setupWakeWord() {
  const STORAGE_KEY = 'cora-wake-word-enabled';
  // Wake phrases — any of these greetings followed by "Cora" (with the
  // STT-misrecognition variants we've seen in practice: core/cory/coda/
  // corra). The greetings are: hey | hi | hello | good morning |
  // good afternoon | good evening. Adding more is a one-line edit —
  // append into the (?: ... ) group.
  const WAKE_RE = /\b(?:hey|hi|hello|good\s+(?:morning|afternoon|evening))\s+(cora|core|cory|coda|corra)\b/i;

  // Browser support detection. Chrome / Edge expose webkitSpeechRecognition;
  // some Chromium-on-Linux builds expose plain SpeechRecognition.
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

  // ---- Settings UI wiring ----
  const cb     = document.getElementById('wakeWordEnabledInput');
  const status = document.getElementById('wakeWordStatus');
  function setStatusText(msg, kind) {
    if (!status) return;
    status.textContent = msg || '';
    status.style.color = (kind === 'error')
      ? '#ff8a80'
      : (kind === 'on' ? 'rgba(120, 220, 150, 0.95)' : 'var(--text-mid)');
  }
  function readEnabled() {
    try { return localStorage.getItem(STORAGE_KEY) === 'true'; }
    catch (_) { return false; }
  }
  function writeEnabled(on) {
    try { localStorage.setItem(STORAGE_KEY, on ? 'true' : 'false'); }
    catch (_) {}
  }
  if (cb) cb.checked = readEnabled();

  if (!SR) {
    // No browser support — disable the toggle and explain why.
    if (cb) { cb.checked = false; cb.disabled = true; }
    setStatusText("Wake word needs Chrome or Edge — your browser doesn't support SpeechRecognition.", 'error');
    return;
  }

  // ---- Recogniser lifecycle ----
  let recog = null;
  let armed = false;        // user wants it on (setting checked)
  let running = false;      // recogniser is currently listening
  let voiceActive = false;  // a WebRTC session is active — pause us
  let restartTimer = null;

  function onResult(ev) {
    // Sweep both interim + final results. Wake word should fire fast.
    let text = '';
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      text += ev.results[i][0].transcript + ' ';
    }
    const match = text.match(WAKE_RE);
    if (!match) return;
    if (window.__coraListening) return;  // already in a voice session
    setStatusText(`Heard "${match[0]}" — opening voice session.`, 'on');
    // Hand off to the existing mic-toggle path. setupVoice listens on
    // this and runs connect(); the level loop, orb morph, etc. all
    // light up the same way as a manual mic click.
    if (typeof setListening === 'function') {
      setListening(true);
    } else {
      window.dispatchEvent(new CustomEvent('cora:mic-toggle', { detail: { listening: true } }));
    }
  }

  function onError(ev) {
    // 'no-speech' / 'aborted' are normal lifecycle events; quietly ignore.
    if (ev.error === 'no-speech' || ev.error === 'aborted') return;
    if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') {
      setStatusText('Mic permission denied. Enable mic for this site to use the wake word.', 'error');
      armed = false;
      if (cb) cb.checked = false;
      writeEnabled(false);
      return;
    }
    if (ev.error === 'network') {
      // SpeechRecognition needs network; transient blips are common.
      setStatusText('Network blip — wake word will retry.', 'error');
      return;
    }
    setStatusText('Wake-word recogniser error: ' + ev.error, 'error');
  }

  function onEnd() {
    running = false;
    // Auto-restart while armed, unless paused for an active voice session.
    if (armed && !voiceActive) {
      // Small backoff so we don't busy-loop on repeated immediate ends.
      if (restartTimer) clearTimeout(restartTimer);
      restartTimer = setTimeout(() => { restartTimer = null; start(); }, 250);
    }
  }

  function start() {
    if (running || !armed || voiceActive) return;
    if (!recog) {
      recog = new SR();
      recog.continuous     = true;
      recog.interimResults = true;
      recog.lang           = 'en-US';
      recog.addEventListener('result', onResult);
      recog.addEventListener('error',  onError);
      recog.addEventListener('end',    onEnd);
    }
    try {
      recog.start();
      running = true;
      setStatusText('Listening for "Hey Cora"...', 'on');
    } catch (err) {
      // Some browsers throw InvalidStateError if start() is called while
      // a previous instance is still ending. The onEnd timer will retry.
      running = false;
    }
  }

  function stop() {
    armed = false;
    if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
    if (recog && running) {
      try { recog.stop(); } catch (_) {}
    }
    setStatusText('Wake word off.');
  }

  // Pause while a voice session is active.
  window.addEventListener('cora:mic-toggle', (e) => {
    voiceActive = !!e.detail.listening;
    if (voiceActive) {
      if (recog && running) {
        try { recog.stop(); } catch (_) {}
        running = false;
        setStatusText('Paused — voice session active.');
      }
    } else if (armed) {
      // Voice session ended — resume listening for the next "Hey Cora".
      setTimeout(start, 400);
    }
  });

  if (cb) {
    cb.addEventListener('change', () => {
      writeEnabled(cb.checked);
      if (cb.checked) {
        armed = true;
        start();
      } else {
        stop();
      }
    });
  }

  // Auto-arm on page load if the user previously enabled it.
  if (readEnabled()) {
    armed = true;
    // Defer so the rest of the page (mic button, voice IIFE) is wired
    // before we touch the mic.
    setTimeout(start, 600);
  }

  // Public hook for debug / future callers.
  window.coraWakeWord = {
    isArmed:     () => armed,
    isRunning:   () => running,
    isVoiceBusy: () => voiceActive,
    enable() { if (cb) { cb.checked = true; cb.dispatchEvent(new Event('change')); } },
    disable() { if (cb) { cb.checked = false; cb.dispatchEvent(new Event('change')); } },
  };
})();

// ----- SIGNAL Gmail connect/disconnect (Settings → Agents) ----------
// Polls /api/signal/auth/status on settings open and after the OAuth
// popup closes. Connect opens a small popup that runs the OAuth flow;
// the popup posts a `cora-signal-connected` message back to us via
// window.opener.postMessage and then closes itself.
(function setupSignalAuth() {
  const statusEl     = document.getElementById('signalAuthStatus');
  const connectBtn   = document.getElementById('signalConnectBtn');
  const disconnectBtn= document.getElementById('signalDisconnectBtn');
  const hintEl       = document.getElementById('signalAuthHint');
  if (!statusEl || !connectBtn || !disconnectBtn) return;

  async function refresh() {
    try {
      const r = await fetch('/api/signal/auth/status', { credentials: 'same-origin' });
      const d = await r.json();
      if (!d.configured) {
        statusEl.textContent = 'Not configured: add SIGNAL_GMAIL_CLIENT_ID + SIGNAL_GMAIL_CLIENT_SECRET to .env, then restart.';
        connectBtn.hidden = true; disconnectBtn.hidden = true;
        hintEl.hidden = true;
        return;
      }
      if (!d.connected) {
        statusEl.textContent = 'Not connected.';
        connectBtn.hidden = false; disconnectBtn.hidden = true;
        hintEl.hidden = false;
        hintEl.textContent = 'Click Connect to start the Google OAuth flow in a new window.';
        return;
      }
      statusEl.textContent = 'Connected as ' + (d.email || '?');
      connectBtn.hidden = true; disconnectBtn.hidden = false;
      hintEl.hidden = true;
    } catch (err) {
      statusEl.textContent = 'Status check failed: ' + (err.message || err);
    }
  }

  connectBtn.addEventListener('click', () => {
    // Open the OAuth start in a popup. /api/signal/auth/start 302s to
    // Google's consent. The callback's HTML closes the window via
    // window.close() and posts to window.opener.
    window.open('/api/signal/auth/start', 'signal-oauth',
      'popup=1,width=480,height=640');
  });

  disconnectBtn.addEventListener('click', async () => {
    if (!confirm('Disconnect Gmail? Cora will lose access until you reconnect.')) return;
    try {
      await fetch('/api/signal/auth/disconnect', {
        method: 'DELETE', credentials: 'same-origin',
      });
    } catch (_) {}
    refresh();
  });

  // Listen for the OAuth popup's success message.
  window.addEventListener('message', (ev) => {
    if (!ev.data || ev.data.type !== 'cora-signal-connected') return;
    refresh();
  });

  // Refresh status whenever the agents tab becomes visible.
  document.addEventListener('click', (e) => {
    if (e.target.closest('.settings-tab[data-tab="agents"]')) {
      setTimeout(refresh, 80);
    }
  });

  // First load.
  refresh();
})();

// ----- Voice auto-close input (Main Settings → Voice tab) ---------
// Surfaces the localStorage['cora-voice-auto-close-ms'] knob as a
// numeric input in seconds. The setupVoice IIFE reads the same key
// every time it schedules a close, so saving here takes effect on the
// NEXT auto-close arming (no reconnect needed).
(function setupVoiceAutoCloseInput() {
  const STORAGE_KEY = 'cora-voice-auto-close-ms';
  const DEFAULT_MS  = 3000;
  const input = document.getElementById('voiceAutoCloseInput');
  if (!input) return;

  function readSeconds() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw === null) return DEFAULT_MS / 1000;
      const ms = parseInt(raw, 10);
      return Number.isFinite(ms) && ms >= 0 ? ms / 1000 : DEFAULT_MS / 1000;
    } catch (_) {
      return DEFAULT_MS / 1000;
    }
  }

  function applyToInput() { input.value = String(readSeconds()); }
  applyToInput();

  // Persist on every change. We accept any non-negative integer
  // up to 60s (matches the input's max). Anything outside that
  // is clamped to the default rather than throwing.
  input.addEventListener('change', () => {
    const seconds = parseInt(input.value, 10);
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > 60) {
      try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
      applyToInput();
      return;
    }
    try {
      localStorage.setItem(STORAGE_KEY, String(seconds * 1000));
    } catch (_) {}
  });
})();

// ----- Agents tab (Main Settings → Agents) ------------------------
// Drives PULSE runtime settings (poll cadence, digest time) and the
// agent_configs editor (provider/model/prompt + version-switcher).
//
// Backend endpoints used:
//   GET  /api/agents/list                          dropdown source
//   GET  /api/agents/{agent}/settings              PULSE knobs
//   PUT  /api/agents/{agent}/settings              save PULSE knobs
//   POST /api/agents/pulse/poll-now                manual poll
//   POST /api/agents/pulse/digest-now              manual digest
//   GET  /api/agents/{agent}/configs               version list
//   GET  /api/agents/{agent}/configs/active        active version
//   POST /api/agents/{agent}/configs               new version
//   POST /api/agents/{agent}/configs/{id}/activate atomic version swap
//   GET  /api/common-data?category=agent_provider  provider radio buttons
(function setupAgentsTab() {
  const modal = document.getElementById('mainSettingsModal');
  if (!modal) return;
  const tabBtn = modal.querySelector('.settings-tab[data-tab="agents"]');
  if (!tabBtn) return;

  // ---- DOM refs ----
  const $ = (id) => document.getElementById(id);
  const visibilityList     = $('agentVisibilityList');
  const popoverTimeoutIn   = $('agentPopoverTimeoutInput');
  const personaList        = $('personaNotesList');
  const personaClearBtn    = $('personaClearBtn');
  const personaStatus      = $('personaStatus');
  const pulseEnabled       = $('pulseEnabledInput');
  const pulsePollInterval  = $('pulsePollIntervalInput');
  const pulseNewsWindow    = $('pulseNewsWindowInput');
  const pulseMaxItems      = $('pulseMaxItemsInput');
  const pulseDigestEnabled = $('pulseDigestEnabledInput');
  const pulseDigestTime    = $('pulseDigestTimeInput');
  const pulseFeatured      = $('pulseFeaturedCountInput');
  const pulseRateLimit     = $('pulseRateLimitInput');
  const pulseSaveBtn       = $('pulseSaveBtn');
  const pulsePollNowBtn    = $('pulsePollNowBtn');
  const pulseDigestNowBtn  = $('pulseDigestNowBtn');
  const pulseStatusText    = $('pulseStatusText');

  const agentPicker        = $('agentConfigPickerSelect');
  const agentPanel         = $('agentConfigPanel');
  const activeLabel        = $('agentConfigActiveLabel');
  const providerHost       = $('agentConfigProvider');
  const modelInput         = $('agentConfigModelInput');
  const endpointInput      = $('agentConfigEndpointInput');
  const keyEnvInput        = $('agentConfigKeyEnvInput');
  const maxTokensInput     = $('agentConfigMaxTokensInput');
  const numCtxInput        = $('agentConfigNumCtxInput');
  const temperatureInput   = $('agentConfigTemperatureInput');
  const promptTextarea     = $('agentConfigPromptTextarea');
  const descriptionInput   = $('agentConfigDescriptionInput');
  const cfgSaveBtn         = $('agentConfigSaveBtn');
  const cfgSaveActivateBtn = $('agentConfigSaveActivateBtn');
  const cfgStatusText      = $('agentConfigStatusText');
  const versionsSelect     = $('agentConfigVersionsSelect');
  const cfgActivateBtn     = $('agentConfigActivateBtn');

  let providers = [];      // common_data agent_provider rows
  let currentAgent = '';   // currently picked agent_configs.agent
  let currentActive = null; // active config row for currentAgent

  function setStatus(el, msg, state = '') {
    if (!el) return;
    el.textContent = msg || '';
    if (state) el.dataset.state = state;
    else delete el.dataset.state;
  }

  async function fetchJson(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
    return r.json();
  }

  // ---- PULSE runtime settings ----
  async function loadPulseSettings() {
    try {
      const s = await fetchJson('/api/agents/pulse/settings');
      pulseEnabled.checked       = !!s.enabled;
      pulsePollInterval.value    = s.poll_interval_minutes ?? 30;
      pulseNewsWindow.value      = s.news_window_hours ?? 24;
      pulseMaxItems.value        = s.max_items_per_source ?? 25;
      pulseDigestEnabled.checked = !!s.digest_enabled;
      pulseDigestTime.value      = s.digest_time_local || '07:15';
      pulseFeatured.value        = s.digest_featured_count ?? 8;
      pulseRateLimit.value       = s.summary_rate_limit ?? 5;
    } catch (err) {
      setStatus(pulseStatusText, `Failed to load PULSE settings: ${err.message || err}`, 'error');
    }
  }

  async function savePulseSettings() {
    const body = {
      settings: {
        enabled:               !!pulseEnabled.checked,
        poll_interval_minutes: Number(pulsePollInterval.value) || 30,
        news_window_hours:     Number(pulseNewsWindow.value) || 24,
        max_items_per_source:  Number(pulseMaxItems.value) || 25,
        digest_enabled:        !!pulseDigestEnabled.checked,
        digest_time_local:     pulseDigestTime.value || '07:15',
        digest_featured_count: Number(pulseFeatured.value) || 8,
        summary_rate_limit:    Number(pulseRateLimit.value) || 5,
        // digest_catchup_hours is server-side default (not exposed in UI yet)
      },
    };
    try {
      setStatus(pulseStatusText, 'Saving...', 'busy');
      await fetchJson('/api/agents/pulse/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      setStatus(pulseStatusText, 'Saved. Scheduler picks up changes on next tick.', 'ok');
    } catch (err) {
      setStatus(pulseStatusText, `Save failed: ${err.message || err}`, 'error');
    }
  }

  async function pollNow() {
    // Fire-and-forget: server returns 202 immediately; the actual fetch
    // runs in a background asyncio task. We surface the kickoff status
    // here; completion shows up in the activity panel under PULSE News.
    try {
      setStatus(pulseStatusText, 'Starting poll in background...', 'busy');
      await fetchJson('/api/agents/pulse/poll-now', { method: 'POST' });
      setStatus(pulseStatusText, 'Poll started. Result lands in the activity panel when done.', 'ok');
    } catch (err) {
      setStatus(pulseStatusText, `Poll failed to start: ${err.message || err}`, 'error');
    }
  }

  async function digestNow() {
    // Fire-and-forget: server returns 202 immediately. Digest synthesis
    // runs in the background; completion updates the dashboard PULSE card
    // and posts a row to the activity panel.
    try {
      setStatus(pulseStatusText, 'Starting digest in background...', 'busy');
      await fetchJson('/api/agents/pulse/digest-now', { method: 'POST' });
      setStatus(pulseStatusText, 'Digest started. The PULSE card will update when done.', 'ok');
    } catch (err) {
      setStatus(pulseStatusText, `Digest failed to start: ${err.message || err}`, 'error');
    }
  }

  // ---- Provider radios ----
  function renderProviders(selected) {
    if (!providerHost) return;
    providerHost.innerHTML = '';
    for (const p of providers) {
      const wrap = document.createElement('label');
      wrap.className = 'segment-option';
      const input = document.createElement('input');
      input.type  = 'radio';
      input.name  = 'agentConfigProvider';
      input.value = p.value;
      if (p.value === selected) input.checked = true;
      const span = document.createElement('span');
      span.textContent = p.label || p.value;
      span.title = p.description || '';
      wrap.appendChild(input);
      wrap.appendChild(span);
      providerHost.appendChild(wrap);
    }
  }

  function selectedProvider() {
    const checked = providerHost.querySelector('input[name="agentConfigProvider"]:checked');
    return checked ? checked.value : (providers[0] && providers[0].value) || 'local_ollama';
  }

  // ---- Agent picker ----
  async function loadAgents() {
    try {
      const list = await fetchJson('/api/agents/list');
      agentPicker.innerHTML = '<option value="">— pick an agent —</option>';
      for (const row of list) {
        const opt = document.createElement('option');
        opt.value = row.agent;
        const ver = row.active_version ? ` (v${row.active_version})` : '';
        const prov = row.active_provider ? ` · ${row.active_provider.replace('local_', '').replace('cloud_', '☁ ')}` : '';
        opt.textContent = `${row.agent}${ver}${prov}`;
        agentPicker.appendChild(opt);
      }
    } catch (err) {
      agentPicker.innerHTML = `<option value="">Failed to load: ${err.message || err}</option>`;
    }
  }

  async function loadAgentDetail(agent) {
    if (!agent) {
      agentPanel.hidden = true;
      currentAgent = '';
      currentActive = null;
      return;
    }
    currentAgent = agent;
    agentPanel.hidden = false;
    setStatus(cfgStatusText, 'Loading...', 'busy');
    try {
      const [active, allVersions] = await Promise.all([
        fetchJson(`/api/agents/${encodeURIComponent(agent)}/configs/active`),
        fetchJson(`/api/agents/${encodeURIComponent(agent)}/configs`),
      ]);
      currentActive = active;
      activeLabel.textContent = `v${active.version} · ${active.provider} · ${active.model}` +
        (active.description ? ` — ${active.description}` : '');
      renderProviders(active.provider);
      modelInput.value       = active.model || '';
      endpointInput.value    = active.endpoint || '';
      keyEnvInput.value      = active.api_key_env || '';
      maxTokensInput.value   = active.max_tokens ?? 1024;
      numCtxInput.value      = active.num_ctx ?? '';
      temperatureInput.value = active.temperature ?? 0.7;
      promptTextarea.value   = active.system_prompt || '';
      descriptionInput.value = '';

      versionsSelect.innerHTML = '';
      for (const v of allVersions) {
        const opt = document.createElement('option');
        opt.value = String(v.id);
        const star = v.active ? '★ ' : '';
        const desc = v.description ? ` — ${v.description}` : '';
        opt.textContent = `${star}v${v.version} · ${v.provider} · ${v.model}${desc}`;
        if (v.active) opt.selected = true;
        versionsSelect.appendChild(opt);
      }
      setStatus(cfgStatusText, '');
    } catch (err) {
      setStatus(cfgStatusText, `Failed: ${err.message || err}`, 'error');
    }
  }

  function readFormConfig() {
    return {
      provider:      selectedProvider(),
      model:         modelInput.value.trim(),
      endpoint:      endpointInput.value.trim() || null,
      api_key_env:   keyEnvInput.value.trim() || null,
      max_tokens:    Number(maxTokensInput.value) || 1024,
      num_ctx:       numCtxInput.value ? Number(numCtxInput.value) : null,
      temperature:   temperatureInput.value === '' ? null : Number(temperatureInput.value),
      system_prompt: promptTextarea.value,
      description:   descriptionInput.value.trim() || null,
    };
  }

  async function saveNewVersion(activate) {
    if (!currentAgent) return;
    const body = { ...readFormConfig(), activate: !!activate };
    if (!body.model) {
      setStatus(cfgStatusText, 'Model is required.', 'error');
      return;
    }
    if (!body.system_prompt) {
      setStatus(cfgStatusText, 'System prompt is required.', 'error');
      return;
    }
    try {
      setStatus(cfgStatusText, 'Saving...', 'busy');
      await fetchJson(`/api/agents/${encodeURIComponent(currentAgent)}/configs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      setStatus(cfgStatusText, activate ? 'Saved & activated.' : 'Saved as new version (not activated).', 'ok');
      await loadAgentDetail(currentAgent);
    } catch (err) {
      setStatus(cfgStatusText, `Save failed: ${err.message || err}`, 'error');
    }
  }

  async function activateSelected() {
    if (!currentAgent) return;
    const id = versionsSelect.value;
    if (!id) return;
    try {
      setStatus(cfgStatusText, 'Activating...', 'busy');
      await fetchJson(`/api/agents/${encodeURIComponent(currentAgent)}/configs/${id}/activate`, {
        method: 'POST',
      });
      setStatus(cfgStatusText, 'Activated.', 'ok');
      await loadAgentDetail(currentAgent);
    } catch (err) {
      setStatus(cfgStatusText, `Activation failed: ${err.message || err}`, 'error');
    }
  }

  // ---- Wiring ----
  if (pulseSaveBtn)      pulseSaveBtn.addEventListener('click', savePulseSettings);
  if (pulsePollNowBtn)   pulsePollNowBtn.addEventListener('click', pollNow);
  if (pulseDigestNowBtn) pulseDigestNowBtn.addEventListener('click', digestNow);
  if (cfgSaveBtn)         cfgSaveBtn.addEventListener('click', () => saveNewVersion(false));
  if (cfgSaveActivateBtn) cfgSaveActivateBtn.addEventListener('click', () => saveNewVersion(true));
  if (cfgActivateBtn)     cfgActivateBtn.addEventListener('click', activateSelected);
  if (agentPicker)        agentPicker.addEventListener('change', () => loadAgentDetail(agentPicker.value));

  // ---- Auto-close timeout ----
  // Same localStorage key the agent island reads. Saved on input change
  // — no Save button — so the next popover opening uses the new value.
  const POPOVER_TIMEOUT_KEY = 'cora-agent-popover-timeout';
  function loadPopoverTimeout() {
    if (!popoverTimeoutIn) return;
    let val = 10;
    try {
      const raw = localStorage.getItem(POPOVER_TIMEOUT_KEY);
      if (raw !== null) {
        const n = Number(raw);
        if (Number.isFinite(n) && n >= 0) val = n;
      }
    } catch (_) {}
    popoverTimeoutIn.value = String(val);
  }
  function savePopoverTimeout() {
    if (!popoverTimeoutIn) return;
    let n = Number(popoverTimeoutIn.value);
    if (!Number.isFinite(n) || n < 0) n = 0;
    if (n > 300) n = 300;
    popoverTimeoutIn.value = String(n);
    try { localStorage.setItem(POPOVER_TIMEOUT_KEY, String(n)); } catch (_) {}
  }
  if (popoverTimeoutIn) popoverTimeoutIn.addEventListener('change', savePopoverTimeout);

  // ---- Cora's remembered preferences (persona notes) ----
  // Mirrors the GET /api/persona shape: { notes: [{text, added_at}, ...] }.
  // Cora persists notes via the persona_add cora-action; this section
  // shows them and lets the user delete one or wipe all.
  function setPersonaStatus(msg, kind) {
    if (!personaStatus) return;
    personaStatus.textContent = msg || '';
    personaStatus.style.color = (kind === 'error')
      ? '#ff8a80'
      : (kind === 'ok' ? 'rgba(120, 220, 150, 0.95)' : 'var(--text-mid)');
  }

  async function loadPersonaNotes() {
    if (!personaList) return;
    try {
      const r = await fetch('/api/persona', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
      const { notes } = await r.json();
      renderPersonaNotes(notes || []);
    } catch (err) {
      personaList.innerHTML = '<p class="persona-notes-empty">Failed to load: ' + (err.message || err) + '</p>';
    }
  }

  function renderPersonaNotes(notes) {
    personaList.innerHTML = '';
    if (!notes.length) {
      const p = document.createElement('p');
      p.className = 'persona-notes-empty';
      p.textContent = 'Nothing saved yet. Tell Cora to remember something — '
                    + 'e.g. "be more concise" — and it shows up here.';
      personaList.appendChild(p);
      return;
    }
    notes.forEach((n, idx) => {
      const row = document.createElement('div');
      row.className = 'persona-note';
      const text = document.createElement('span');
      text.className = 'persona-note-text';
      text.textContent = n.text || '';
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'persona-note-remove';
      rm.setAttribute('aria-label', 'Remove preference');
      rm.title = 'Remove this preference';
      rm.textContent = '×';
      rm.addEventListener('click', async () => {
        try {
          const r = await fetch('/api/persona/' + idx, { method: 'DELETE' });
          if (!r.ok && r.status !== 204) throw new Error(r.status + '');
          await loadPersonaNotes();
          setPersonaStatus('Removed.', 'ok');
        } catch (err) {
          setPersonaStatus('Failed to remove: ' + (err.message || err), 'error');
        }
      });
      row.appendChild(text);
      row.appendChild(rm);
      personaList.appendChild(row);
    });
  }

  if (personaClearBtn) {
    personaClearBtn.addEventListener('click', async () => {
      try {
        setPersonaStatus('Clearing...');
        const r = await fetch('/api/persona', { method: 'DELETE' });
        if (!r.ok && r.status !== 204) throw new Error(r.status + '');
        await loadPersonaNotes();
        setPersonaStatus('All preferences cleared.', 'ok');
      } catch (err) {
        setPersonaStatus('Failed: ' + (err.message || err), 'error');
      }
    });
  }

  // Cora may persist a note via cora-action mid-conversation; refresh the
  // list when that happens so the user sees it appear immediately.
  window.addEventListener('cora:persona-changed', () => {
    if (loaded) loadPersonaNotes();
  });

  // ---- Visible-tabs checkboxes ----
  // Mirrors the agent strip at the top of the page. Reading from
  // localStorage['cora-agent-visibility'] (object keyed by slug → bool).
  // Default: every agent visible. Toggling re-renders the strip live via
  // window.coraRefreshAgentTabs (set by setupAgentIsland).
  const VISIBILITY_KEY = 'cora-agent-visibility';
  function readVisibilityMap() {
    try {
      const raw = localStorage.getItem(VISIBILITY_KEY);
      if (!raw) return {};
      const obj = JSON.parse(raw);
      return obj && typeof obj === 'object' ? obj : {};
    } catch (_) { return {}; }
  }
  function writeVisibilityMap(map) {
    try { localStorage.setItem(VISIBILITY_KEY, JSON.stringify(map)); } catch (_) {}
  }

  function renderVisibilityList() {
    if (!visibilityList) return;
    const defs = (window.CORA_AGENT_DEFS && window.CORA_AGENT_DEFS.all) || [];
    const info = (window.CORA_AGENT_DEFS && window.CORA_AGENT_DEFS.info) || {};
    const visMap = readVisibilityMap();

    visibilityList.innerHTML = '';
    for (const slug of defs) {
      const meta = info[slug] || { name: slug.toUpperCase(), role: '', glyph: '?', color: '#888' };
      const checked = visMap[slug] !== false;            // default true
      const wrap = document.createElement('label');
      wrap.className = 'agent-visibility-row';
      wrap.style.setProperty('--agent-color', meta.color);
      wrap.innerHTML = `
        <input type="checkbox" data-agent-slug="${slug}" ${checked ? 'checked' : ''}/>
        <span class="agent-visibility-glyph">${meta.glyph}</span>
        <span class="agent-visibility-text">
          <span class="agent-visibility-name">${meta.name}</span>
          <span class="agent-visibility-role">${meta.role}</span>
        </span>`;
      const cb = wrap.querySelector('input[type="checkbox"]');
      cb.addEventListener('change', () => {
        const map = readVisibilityMap();
        map[slug] = cb.checked;
        writeVisibilityMap(map);
        if (typeof window.coraRefreshAgentTabs === 'function') {
          window.coraRefreshAgentTabs();
        }
      });
      visibilityList.appendChild(wrap);
    }
  }

  // Lazy load on first open of the Agents tab — saves work for users who
  // never click into it. We re-check `loaded` so subsequent re-opens are
  // free unless the modal was hard-refreshed.
  let loaded = false;
  async function loadAll() {
    if (loaded) return;
    loaded = true;
    try {
      providers = await fetchJson('/api/common-data?category=agent_provider');
    } catch (err) {
      providers = [{ value: 'local_ollama', label: 'Local — Ollama' }];
    }
    renderVisibilityList();
    loadPopoverTimeout();
    loadPersonaNotes();
    await Promise.all([loadPulseSettings(), loadAgents()]);
  }

  tabBtn.addEventListener('click', () => { loadAll(); });

  // Refresh PULSE settings when the modal opens via gear menu (covers
  // the case where the Agents tab is the selected tab on open).
  modal.addEventListener('cora:opened', () => {
    if (tabBtn.classList.contains('active')) loadAll();
  });
})();

// ----- Agent island: floating dock for in-flight agent runs -------
// Renders one chip per row in agent_runs WHERE status IN ('queued','running').
// SSE-driven: bootstrap.js dispatches `cora:agent-runs-change` when a NOTIFY
// arrives; this listener debounces + re-fetches + re-renders. Click a chip
// to expand a popover with the run's task + status + start time. When the
// run finishes (status leaves the in-flight set) the chip vanishes; the
// island hides itself when no chips remain.
(function setupAgentIsland() {
  const island   = document.getElementById('agentIsland');
  const chipsBox = document.getElementById('agentIslandChips');
  const popover  = document.getElementById('agentIslandPopover');
  if (!island || !chipsBox || !popover) return;

  // Per-agent appearance + popover content. Keyed by the umbrella
  // identity (the seven canonical agents). Sub-prompt slugs in
  // agent_runs (pulse_summarize, pulse_digest, ...) get rolled up to
  // an umbrella via umbrellaAgent() so the avatar represents the
  // agent as a whole, not a specific prompt.
  //   name   — bold display name in the popover
  //   role   — uppercase tag below the name
  //   color  — drives both the avatar gradient and the selection outline
  //   glyph  — single character / short label rendered inside the avatar
  // Glyphs use periodic-table element style: first letter capitalised,
  // second letter lowercase. Reads like a chemistry symbol — Co, At,
  // Sc, Fo, Pu, Si, Ch — and gives every tab an instantly recognisable
  // two-character mark.
  const AGENT_INFO = {
    cora:    { name: 'CORA',    role: 'MASTER',       color: '#5aa8ff', glyph: 'Co' },
    atlas:   { name: 'ATLAS',   role: 'ORCHESTRATOR', color: '#a86eff', glyph: 'At' },
    scribe:  { name: 'SCRIBE',  role: 'WIKI',         color: '#ffb84d', glyph: 'Sc' },
    forge:   { name: 'FORGE',   role: 'CODING',       color: '#ff7a4d', glyph: 'Fo' },
    pulse:   { name: 'PULSE',   role: 'AI NEWS',      color: '#3df0a8', glyph: 'Pu' },
    signal:  { name: 'SIGNAL',  role: 'EMAIL',        color: '#5ee0d8', glyph: 'Si' },
    chronos: { name: 'CHRONOS', role: 'CALENDAR',     color: '#c97aff', glyph: 'Ch' },
  };
  // Display order across the top — master first, then specialists.
  const ALL_AGENTS = ['cora', 'atlas', 'scribe', 'forge', 'pulse', 'signal', 'chronos'];

  // Expose for setupAgentsTab (Main settings → Agents → Visible tabs).
  // Done lazily here so AGENT_INFO + ALL_AGENTS stay defined in one place
  // but other IIFEs can read them without duplication.
  window.CORA_AGENT_DEFS = { all: ALL_AGENTS, info: AGENT_INFO };

  // Per-user visibility toggle, set from the Main settings → Agents tab.
  // Default: every agent visible. Stored as a JSON object slug → bool.
  const VISIBILITY_KEY = 'cora-agent-visibility';
  function readVisibility() {
    try {
      const raw = localStorage.getItem(VISIBILITY_KEY);
      if (!raw) return null;
      const obj = JSON.parse(raw);
      return obj && typeof obj === 'object' ? obj : null;
    } catch (_) { return null; }
  }
  function isVisible(slug) {
    const v = readVisibility();
    if (!v) return true;                  // default: shown
    return v[slug] !== false;             // hidden only if explicitly false
  }

  // Map any agent_runs.agent slug to its umbrella identity.
  // pulse_summarize / pulse_digest / pulse_poll all roll up to 'pulse'.
  function umbrellaAgent(slug) {
    if (!slug) return slug;
    if (slug.startsWith('pulse')) return 'pulse';
    return slug;
  }

  function infoFor(slug) {
    return AGENT_INFO[slug] || { name: String(slug).toUpperCase(), role: '', color: '#888', glyph: String(slug).slice(0, 2).toUpperCase() };
  }

  // With persistent tabs, every umbrella avatar is always rendered.
  // Active runs (any sub-prompt) make the avatar pulse via .active.
  // Hidden-agent filter is no longer needed — all in-flight runs
  // contribute to the umbrella's "active" state, including pulse_poll.

  let runs = [];               // current in-flight rows from /api/agents/runs
  let selectedSlug = null;     // umbrella slug of the currently-open popover (or null)

  function fmtRelTime(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return '';
    const dt = (Date.now() - t) / 1000;
    if (dt < 1)   return 'just now';
    if (dt < 60)  return Math.round(dt) + 's ago';
    if (dt < 3600) return Math.round(dt / 60) + 'm ago';
    return Math.round(dt / 3600) + 'h ago';
  }

  // Build {umbrella -> most-recent active run} from the in-flight list.
  function activeRunByUmbrella() {
    const m = {};
    for (const run of runs) {
      const u = umbrellaAgent(run.agent);
      const existing = m[u];
      if (!existing) { m[u] = run; continue; }
      const a = new Date(run.started_at || run.created_at).getTime();
      const b = new Date(existing.started_at || existing.created_at).getTime();
      if (a > b) m[u] = run;
    }
    return m;
  }

  // Auto-close timer: dismisses the popover after N seconds of inactivity.
  // Configurable via Main settings → Agents → "Auto-close popover";
  // stored in localStorage. Default: 10s. Set to 0 to disable.
  // Pauses while the cursor hovers the island so reading isn't cut short.
  const POPOVER_TIMEOUT_KEY = 'cora-agent-popover-timeout';
  function readPopoverTimeoutSeconds() {
    try {
      const raw = localStorage.getItem(POPOVER_TIMEOUT_KEY);
      if (raw === null) return 10;
      const n = Number(raw);
      return Number.isFinite(n) && n >= 0 ? n : 10;
    } catch (_) { return 10; }
  }
  let popoverTimer = null;
  function clearPopoverTimer() {
    if (popoverTimer !== null) { clearTimeout(popoverTimer); popoverTimer = null; }
  }
  function schedulePopoverAutoClose() {
    clearPopoverTimer();
    const sec = readPopoverTimeoutSeconds();
    if (sec <= 0) return;                  // disabled — stay open indefinitely
    popoverTimer = setTimeout(() => {
      popoverTimer = null;
      selectedSlug = null;
      closePopover();
      for (const c of chipsBox.querySelectorAll('.agent-chip')) {
        c.setAttribute('aria-pressed', 'false');
      }
    }, sec * 1000);
  }

  function openPopover() {
    popover.classList.add('open');
    popover.setAttribute('aria-hidden', 'false');
    schedulePopoverAutoClose();
  }
  function closePopover() {
    popover.classList.remove('open');
    popover.setAttribute('aria-hidden', 'true');
    clearPopoverTimer();
  }
  function isPopoverOpen() {
    return popover.classList.contains('open');
  }

  // Hover the island/popover → pause the auto-close timer; leave → resume.
  // Means a user reading slowly won't get the popover yanked away.
  island.addEventListener('mouseenter', clearPopoverTimer);
  island.addEventListener('mouseleave', () => {
    if (isPopoverOpen()) schedulePopoverAutoClose();
  });

  function renderChips() {
    chipsBox.innerHTML = '';
    island.hidden = false;                    // persistent tabs: always shown
    const active = activeRunByUmbrella();

    const visibleAgents = ALL_AGENTS.filter(isVisible);
    for (const slug of visibleAgents) {
      const info = infoFor(slug);
      const activeRun = active[slug];
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'agent-chip';
      if (activeRun) btn.classList.add('active');   // drives the pulsing ring
      btn.dataset.agentSlug = slug;
      btn.setAttribute('aria-pressed', selectedSlug === slug ? 'true' : 'false');
      btn.title = `${info.name} · ${info.role}` + (activeRun ? ` — ${activeRun.task}` : '');
      btn.style.setProperty('--agent-color', info.color);

      const avatar = document.createElement('span');
      avatar.className = 'agent-avatar';
      avatar.style.setProperty('--agent-color', info.color);
      avatar.textContent = info.glyph;
      avatar.setAttribute('aria-hidden', 'true');
      btn.appendChild(avatar);

      btn.addEventListener('click', () => {
        if (selectedSlug === slug) {
          selectedSlug = null;
          closePopover();
        } else {
          selectedSlug = slug;
          renderPopover(slug, activeRunByUmbrella()[slug]);
          openPopover();
        }
        for (const c of chipsBox.querySelectorAll('.agent-chip')) {
          c.setAttribute('aria-pressed', String(c.dataset.agentSlug === selectedSlug));
        }
      });
      chipsBox.appendChild(btn);
    }

    // If the selected tab was just hidden, drop the popover.
    if (selectedSlug && !visibleAgents.includes(selectedSlug)) {
      selectedSlug = null;
      closePopover();
    }
    // Re-render the popover content if it's open (status may have changed).
    if (selectedSlug && isPopoverOpen()) {
      renderPopover(selectedSlug, active[selectedSlug]);
    }
  }

  // Public hook so the Visible-tabs checkboxes can trigger a re-render
  // immediately on toggle, instead of waiting for the next SSE refresh.
  window.coraRefreshAgentTabs = renderChips;

  function escape(s) {
    const d = document.createElement('div');
    d.textContent = String(s == null ? '' : s);
    return d.innerHTML;
  }

  function renderPopover(slug, activeRun) {
    const info = infoFor(slug);
    popover.style.setProperty('--agent-color', info.color);
    const taskLine = activeRun
      ? `<p class="agent-pop-task">${escape(activeRun.task || 'Working...')}</p>`
      : `<p class="agent-pop-task agent-pop-idle">Idle</p>`;
    popover.innerHTML = `
      <p class="agent-pop-name">${escape(info.name)}</p>
      <p class="agent-pop-role">${escape(info.role)}</p>
      ${taskLine}
    `;
    // Anchor the popover horizontally to the clicked tab so it appears
    // directly under that tab (instead of centred across the whole strip).
    // The transform: translateX(-50%) in CSS centres on this `left` value.
    const btn = chipsBox.querySelector(
      `.agent-chip[data-agent-slug="${CSS.escape(slug)}"]`
    );
    if (btn) {
      const islandRect = island.getBoundingClientRect();
      const btnRect    = btn.getBoundingClientRect();
      const centerX    = (btnRect.left + btnRect.width / 2) - islandRect.left;
      popover.style.left = centerX + 'px';
    }
  }

  let inFlightFetch = null;
  async function refresh() {
    // Coalesce rapid SSE bursts into one in-flight request.
    if (inFlightFetch) return inFlightFetch;
    const promise = (async () => {
      try {
        const r = await fetch('/api/agents/runs?status=queued,running&limit=20', { cache: 'no-store' });
        if (!r.ok) return;
        runs = await r.json();
        renderChips();
      } catch (_) {
        // Network blip — leave existing chips up; the next SSE event will retry.
      } finally {
        inFlightFetch = null;
      }
    })();
    inFlightFetch = promise;
    return promise;
  }

  // Debounced re-fetch on each SSE event (rapid-fire when many summaries
  // come back in a row).
  let timer = null;
  function scheduleRefresh() {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => { timer = null; refresh(); }, 200);
  }

  window.addEventListener('cora:agent-runs-change', scheduleRefresh);

  // Click outside the island closes the popover.
  document.addEventListener('click', (ev) => {
    if (!isPopoverOpen()) return;
    if (island.contains(ev.target)) return;
    selectedSlug = null;
    closePopover();
    for (const c of chipsBox.querySelectorAll('.agent-chip')) {
      c.setAttribute('aria-pressed', 'false');
    }
  });

  // Render the persistent tabs immediately so they appear on first paint
  // even before the runs fetch resolves. refresh() will then fold in the
  // .active state for any in-flight runs.
  renderChips();

  // Initial fetch — covers the case where runs are already in flight when
  // the page loads (e.g. user refreshes during a poll).
  refresh();

  // Periodic safety-net poll (every 30s) in case we miss an SSE event.
  // The whole rendering is idempotent so an extra fetch is benign.
  setInterval(refresh, 30_000);
})();

// ----- Settings gear button (opens Main Settings directly) ---------
// Previously a dropdown popover with "Orb" / "Main" choices; the orb
// modal has been folded into a tab inside Main Settings, so the gear
// goes straight to the unified modal.
if (settingsBtn) {
  settingsBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    openMainSettingsModal();
  });
}

// ----- Chat: text in, streamed reply from /api/chat ----------------
// The server proxies to a local OpenAI-compatible LLM (vLLM by default).
// We render the conversation into #chatSurface as it streams, and own
// the message history client-side (no server-side persistence yet).
(function setupChat() {
  const surface     = document.getElementById('chatSurface');
  const form        = document.getElementById('chatForm');
  const input       = document.getElementById('chatInput');
  const sendBtn     = document.getElementById('chatSend');
  const toggleBtn   = document.getElementById('chatBtn');
  const attachBtn   = document.getElementById('chatAttachBtn');
  const attachInput = document.getElementById('chatAttachInput');
  const chipsHost   = document.getElementById('chatAttachments');
  if (!surface || !form || !input || !sendBtn) return;

  // ----- File attachments -----
  // Two ways to feed Cora a file:
  //   - Drag-drop / paperclip — browser reads the file with FileReader,
  //     no server hop; works for any text file the user can pick.
  //   - @path mention in the input — at send time we extract every
  //     /\B@([\w./-]+)\b/ token and fetch each via GET /api/file (server-
  //     side allowlist). Lets Cora see project files without picking.
  // Both paths land in the same `attachedFiles` array, which gets folded
  // into a developer message before the user's prompt for that turn only.
  const ATTACH_MAX_BYTES_PER_FILE = 64 * 1024;       // 64 KB per file
  const ATTACH_MAX_TOTAL_BYTES    = 192 * 1024;      // 192 KB per turn
  const ATTACH_PATH_RE            = /(?:^|\s)@([\w][\w./-]{0,200})(?=\s|$|[.,;:!?])/g;

  let attachedFiles = [];   // [{ name, path, content, size, source, truncated, error }]

  function attachedTotalBytes() {
    return attachedFiles.reduce((n, f) => n + (f.content?.length || 0), 0);
  }

  function renderAttachmentChips() {
    if (!chipsHost) return;
    if (attachedFiles.length === 0) {
      chipsHost.innerHTML = '';
      chipsHost.hidden = true;
      return;
    }
    chipsHost.hidden = false;
    chipsHost.innerHTML = '';
    attachedFiles.forEach((f, idx) => {
      const chip = document.createElement('span');
      chip.className = 'chat-file-chip' + (f.error ? ' chat-file-chip-error' : '');
      const label = document.createElement('span');
      label.className = 'chat-file-chip-name';
      label.textContent = f.name;
      const meta = document.createElement('span');
      meta.className = 'chat-file-chip-meta';
      if (f.error) {
        meta.textContent = f.error;
      } else {
        const kb = (f.size / 1024).toFixed(f.size < 1024 ? 2 : 1);
        meta.textContent = (f.source === 'mention' ? '@ ' : '') + `${kb} KB${f.truncated ? ' · truncated' : ''}`;
      }
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'chat-file-chip-remove';
      rm.setAttribute('aria-label', `Remove ${f.name}`);
      rm.textContent = '×';
      rm.addEventListener('click', () => {
        attachedFiles.splice(idx, 1);
        renderAttachmentChips();
      });
      chip.appendChild(label);
      chip.appendChild(meta);
      chip.appendChild(rm);
      chipsHost.appendChild(chip);
    });
  }

  function readBrowserFile(file) {
    return new Promise((resolve) => {
      // Refuse oversize early — FileReader would still work but it'd
      // bust the per-turn budget.
      if (file.size > ATTACH_MAX_BYTES_PER_FILE * 4) {
        resolve({ name: file.name, path: file.name, error: `too big (${(file.size/1024).toFixed(0)} KB)`, size: file.size });
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        let content = String(reader.result || '');
        let truncated = false;
        if (content.length > ATTACH_MAX_BYTES_PER_FILE) {
          content = content.slice(0, ATTACH_MAX_BYTES_PER_FILE);
          truncated = true;
        }
        resolve({
          name: file.name,
          path: file.name,
          content,
          size: file.size,
          truncated,
          source: 'attached',
        });
      };
      reader.onerror = () => resolve({ name: file.name, path: file.name, error: 'read failed', size: file.size });
      reader.readAsText(file);
    });
  }

  async function addBrowserFiles(fileList) {
    const incoming = Array.from(fileList || []);
    if (incoming.length === 0) return;
    const reads = await Promise.all(incoming.map(readBrowserFile));
    for (const f of reads) {
      if (!f.error && attachedTotalBytes() + (f.content?.length || 0) > ATTACH_MAX_TOTAL_BYTES) {
        f.error = 'exceeds turn budget';
      }
      attachedFiles.push(f);
    }
    renderAttachmentChips();
  }

  async function fetchMentionedPaths(text) {
    const seen = new Set();
    const paths = [];
    let m;
    ATTACH_PATH_RE.lastIndex = 0;
    while ((m = ATTACH_PATH_RE.exec(text)) !== null) {
      const p = m[1];
      if (!seen.has(p)) { seen.add(p); paths.push(p); }
    }
    if (paths.length === 0) return [];
    const out = [];
    for (const p of paths) {
      try {
        const r = await fetch('/api/file?path=' + encodeURIComponent(p), { cache: 'no-store' });
        if (!r.ok) {
          const detail = await r.text().catch(() => '');
          out.push({ name: p, path: p, error: `${r.status} ${detail.slice(0, 80) || r.statusText}`, source: 'mention', size: 0 });
          continue;
        }
        const data = await r.json();
        out.push({
          name: data.path || p,
          path: data.path || p,
          content: data.content || '',
          size: data.size || (data.content?.length ?? 0),
          truncated: !!data.truncated,
          source: 'mention',
        });
      } catch (err) {
        out.push({ name: p, path: p, error: String(err.message || err), source: 'mention', size: 0 });
      }
    }
    return out;
  }

  function buildAttachmentsDeveloperMessage(list) {
    const ok = list.filter((f) => !f.error && f.content);
    if (ok.length === 0) return null;
    const parts = ['## Attached files'];
    parts.push(
      'Files Freddie attached or referenced in this turn. Use them as ' +
      'ground truth for this question; cite by path when answering.'
    );
    parts.push('');
    for (const f of ok) {
      const trunc = f.truncated ? ', truncated' : '';
      parts.push(`### ${f.path} (${f.size} bytes${trunc})`);
      parts.push('```');
      parts.push(f.content);
      parts.push('```');
      parts.push('');
    }
    return { role: 'developer', content: parts.join('\n') };
  }

  // ----- Wire up paperclip + drag-drop -----
  if (attachBtn && attachInput) {
    attachBtn.addEventListener('click', () => attachInput.click());
    attachInput.addEventListener('change', () => {
      addBrowserFiles(attachInput.files);
      attachInput.value = '';                         // allow re-attaching the same file
    });
  }
  // Drag-drop. Listen on the whole chat surface AND the mic-bar so
  // dropping anywhere in the chat region works regardless of whether
  // chat-surface is currently visible.
  const dropTargets = [surface, document.querySelector('.mic-bar')].filter(Boolean);
  let dragDepth = 0;
  function dragOn() {
    dragDepth++;
    document.documentElement.dataset.chatDrag = 'on';
  }
  function dragOff() {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) delete document.documentElement.dataset.chatDrag;
  }
  for (const el of dropTargets) {
    el.addEventListener('dragenter', (e) => { e.preventDefault(); dragOn(); });
    el.addEventListener('dragover',  (e) => { e.preventDefault(); });
    el.addEventListener('dragleave', (e) => { e.preventDefault(); dragOff(); });
    el.addEventListener('drop',      (e) => {
      e.preventDefault();
      dragDepth = 0;
      delete document.documentElement.dataset.chatDrag;
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        // Open the chat panel if it's not already, so the user can see
        // the chip + their next prompt.
        setChatOpen(true);
        addBrowserFiles(e.dataTransfer.files);
      }
    });
  }

  function setSending(busy) {
    sendBtn.dataset.state = busy ? 'sending' : 'idle';
    sendBtn.setAttribute('aria-label', busy ? 'Stop generating' : 'Send message');
    sendBtn.title = busy ? 'Stop' : 'Send';
  }
  setSending(false);

  // Chat panel visibility — toggled by the chat button next to the mic.
  // CSS reads `data-chat` on <html>; default closed, no localStorage so
  // each page load starts collapsed (per current task requirement).
  function setChatOpen(open) {
    // Voice/text are mutually exclusive: opening the chat returns the
    // mic to its idle state. (The reverse path — mic-on closing the
    // chat — is wired below via the cora:mic-toggle listener.)
    if (open && typeof listening !== 'undefined' && listening
        && typeof setListening === 'function') {
      setListening(false);
    }
    document.documentElement.dataset.chat = open ? 'open' : 'closed';
    if (toggleBtn) {
      toggleBtn.dataset.state = open ? 'open' : 'closed';
      toggleBtn.setAttribute('aria-pressed', String(open));
      toggleBtn.setAttribute('aria-label', open ? 'Hide chat input' : 'Show chat input');
    }
    if (open) {
      // Defer focus so the layout has settled and the keyboard / IME
      // doesn't argue with the transition.
      requestAnimationFrame(() => input.focus());
    } else if (document.activeElement === input) {
      input.blur();
    }
  }
  setChatOpen(false);

  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      const open = document.documentElement.dataset.chat === 'open';
      setChatOpen(!open);
    });
  }

  // Pressing the mic to start listening collapses the chat panel —
  // voice and text are mutually exclusive surfaces. Stopping the mic
  // does not reopen the chat; the user re-opens it explicitly.
  window.addEventListener('cora:mic-toggle', (e) => {
    if (e.detail && e.detail.listening) setChatOpen(false);
  });

  // OpenAI message format: {role, content}. Trimmed to keep request
  // size sane against vLLM's 16384-token context.
  const history = [];
  const HISTORY_CAP = 20;

  // Assistant replies often contain Markdown (tables, lists, bold). Render
  // them through `marked` if it's loaded; fall back to text if not (e.g.
  // unpkg unreachable). User messages stay literal text — they're echoes
  // of what the user typed and shouldn't be reinterpreted.
  function renderMarkdown(text) {
    if (window.marked && typeof window.marked.parse === 'function') {
      // marked escapes raw HTML by default, so LLM output can't inject
      // <script> tags. GFM tables/lists are on by default in v14+.
      return window.marked.parse(text, { gfm: true, breaks: true });
    }
    return null;  // signal: caller should fall back to textContent
  }

  // Cora can emit hidden command blocks for the UI to act on:
  //   ```cora-action
  //   {"action":"open_agent_tab","agent":"pulse"}
  //   ```
  // These are stripped from the rendered chat and dispatched once each.
  //
  // We try several formats in order of strictness so a slightly-malformed
  // block doesn't silently disappear:
  //   1. The canonical ```cora-action ... ``` fenced form.
  //   2. ANY ``` fenced block whose content is JSON with an "action" key
  //      (catches the model labelling the fence `json` instead).
  //   3. Bare {"action": "..."} JSON anywhere in the prose, in case the
  //      model forgot the fence entirely.
  //
  // Each regex tracks already-executed substrings so the same block in
  // a streaming delta only fires once.
  const _coraActionFenceRe = /```cora-action[ \t]*\r?\n?([\s\S]*?)\s*```/g;
  const _anyFenceJsonRe    = /```(?:json|cora_action|cora\.action|action|)[ \t]*\r?\n?(\{[\s\S]*?"action"[\s\S]*?\})\s*```/g;
  const _bareJsonRe        = /(\{[^{}]*?"action"\s*:\s*"[a-z_]+"[^{}]*\})/g;

  function extractCoraActions(text, alreadyExecuted) {
    const fresh = [];
    const tryParse = (raw, label) => {
      const inner = (raw || '').trim();
      try {
        const obj = JSON.parse(inner);
        if (obj && typeof obj === 'object' && typeof obj.action === 'string') {
          fresh.push(obj);
          console.info('[cora-action] parsed via ' + label + ':', obj);
          return true;
        }
      } catch (err) {
        // Quiet on streaming partial blocks; only warn if we expected JSON.
        if (label === 'fence') console.warn('[cora-action] failed to parse JSON:', inner, err);
      }
      return false;
    };

    // Pass 1: canonical fence.
    let m;
    _coraActionFenceRe.lastIndex = 0;
    while ((m = _coraActionFenceRe.exec(text)) !== null) {
      const key = 'F1:' + m[0];
      if (!alreadyExecuted.has(key)) {
        alreadyExecuted.add(key);
        tryParse(m[1], 'fence');
      }
    }
    // Pass 2: any fence containing JSON with an "action" key.
    _anyFenceJsonRe.lastIndex = 0;
    while ((m = _anyFenceJsonRe.exec(text)) !== null) {
      const key = 'F2:' + m[0];
      if (!alreadyExecuted.has(key)) {
        alreadyExecuted.add(key);
        tryParse(m[1], 'any-fence');
      }
    }
    // Pass 3: bare JSON in prose.
    _bareJsonRe.lastIndex = 0;
    while ((m = _bareJsonRe.exec(text)) !== null) {
      const key = 'F3:' + m[0];
      if (!alreadyExecuted.has(key)) {
        alreadyExecuted.add(key);
        tryParse(m[1], 'bare');
      }
    }

    // Strip every match so none of them render to the user.
    let cleanText = text
      .replace(_coraActionFenceRe, '')
      .replace(_anyFenceJsonRe, '')
      .replace(_bareJsonRe, '')
      .trimEnd();
    return { cleanText, actions: fresh };
  }

  function executeCoraAction(action) {
    if (!action || typeof action !== 'object') return;
    console.info('[cora-action] executing:', action);
    if (action.action === 'open_agent_tab' && typeof action.agent === 'string') {
      const tab = document.querySelector(
        `.agent-chip[data-agent-slug="${CSS.escape(action.agent)}"]`
      );
      if (tab) tab.click();
      else console.warn('[cora-action] open_agent_tab: no tab for', action.agent);
      return;
    }
    if (action.action === 'focus_skill_section' && typeof action.category === 'string') {
      // Open SKILLS panel, expand the named category section, scroll
      // it into view, brief highlight. For "show me my coding tools" /
      // "help me with research" — when the user wants the menu of
      // options, not a specific skill click.
      const skillsPanel = document.getElementById('skillsPanel');
      const wasCollapsed = skillsPanel?.classList.contains('collapsed');
      if (wasCollapsed) {
        skillsPanel.classList.remove('collapsed');
        try { localStorage.setItem('cora-skills-panel-open', 'true'); } catch (_) {}
      }
      const apply = () => {
        const section = document.querySelector(
          `.section[data-skill-category="${CSS.escape(action.category)}"]`
        );
        if (!section) {
          console.warn('[cora-action] focus_skill_section: no category', action.category);
          return;
        }
        if (section.classList.contains('collapsed')) {
          const header = section.querySelector('.section-header');
          if (header) header.click();
        }
        section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        section.classList.remove('cora-updated');
        void section.offsetWidth;
        section.classList.add('cora-updated');
        setTimeout(() => section.classList.remove('cora-updated'), 1400);
      };
      setTimeout(apply, wasCollapsed ? 360 : 0);
      return;
    }
    if (action.action === 'invoke_skill' && typeof action.slug === 'string') {
      // Open the SKILLS panel if it's collapsed (default), then click the
      // matching skill row. The existing document-level delegated click
      // handler in bootstrap.js fires the POST /api/skills/{slug}/invoke
      // and flashes the row.
      const skillsPanel = document.getElementById('skillsPanel');
      const wasCollapsed = skillsPanel?.classList.contains('collapsed');
      if (wasCollapsed) {
        skillsPanel.classList.remove('collapsed');
        try { localStorage.setItem('cora-skills-panel-open', 'true'); } catch (_) {}
      }
      const tryClick = () => {
        const btn = document.querySelector(
          `.skill-item[data-skill-slug="${CSS.escape(action.slug)}"]`
        );
        if (btn) {
          // Make sure the section containing this skill is expanded.
          const section = btn.closest('.section');
          if (section && section.classList.contains('collapsed')) {
            const header = section.querySelector('.section-header');
            if (header) header.click();
          }
          btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
          btn.click();
        }
      };
      // Wait for the panel-open transition to start so the click lands
      // on a panel that's actually visible.
      setTimeout(tryClick, wasCollapsed ? 360 : 0);
      return;
    }
    if (action.action === 'focus_activity_lane' && typeof action.lane === 'string') {
      // Expand the activity panel if it's collapsed, expand the lane
      // section if it's collapsed, scroll it into view, brief highlight.
      const panel = document.getElementById('activityPanel');
      if (panel?.classList.contains('collapsed')) {
        panel.classList.remove('collapsed');
      }
      const apply = () => {
        const section = document.querySelector(
          `.section[data-lane="${CSS.escape(action.lane)}"]`
        );
        if (!section) return;
        if (section.classList.contains('collapsed')) {
          const header = section.querySelector('.section-header');
          if (header) header.click();
        }
        section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        section.classList.remove('cora-updated');
        // restart the highlight keyframe
        void section.offsetWidth;
        section.classList.add('cora-updated');
        setTimeout(() => section.classList.remove('cora-updated'), 1400);
      };
      setTimeout(apply, 200);
      return;
    }
    if (action.action === 'open_panel' && typeof action.panel === 'string') {
      const def = ({
        skills:   { id: 'skillsPanel',   storageKey: 'cora-skills-panel-open' },
        activity: { id: 'activityPanel', storageKey: null                     },
      })[action.panel];
      if (!def) return;
      const el = document.getElementById(def.id);
      if (el && el.classList.contains('collapsed')) {
        el.classList.remove('collapsed');
        if (def.storageKey) {
          try { localStorage.setItem(def.storageKey, 'true'); } catch (_) {}
        }
      }
      return;
    }
    if (action.action === 'persona_add' && typeof action.text === 'string') {
      // Persist a one-line preference. Fire-and-forget; if it fails we
      // don't bother the user — Cora's reply already acknowledged.
      // Notify the settings UI so an open list refreshes.
      fetch('/api/persona/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: action.text }),
      }).then((r) => {
        if (r.ok) {
          window.dispatchEvent(new CustomEvent('cora:persona-changed'));
        }
      }).catch(() => { /* swallow */ });
      return;
    }
    if (action.action === 'persona_clear') {
      fetch('/api/persona', { method: 'DELETE' }).then((r) => {
        if (r.ok) window.dispatchEvent(new CustomEvent('cora:persona-changed'));
      }).catch(() => {});
      return;
    }

    // ---- Level 1 UI primitives ----
    if (action.action === 'click_card' && typeof action.slug === 'string') {
      // Opens a response card's modal (clicks the .card-shell, which
      // shell.js's existing modal pipeline handles).
      const card = document.querySelector(`.card-shell.${CSS.escape(action.slug)}`);
      if (card) card.click();
      else console.warn('[cora-action] click_card: no card with slug', action.slug);
      return;
    }
    if (action.action === 'set_view_mode' && typeof action.mode === 'string') {
      // Routes through the existing view-menu button so localStorage
      // persistence + ARIA state stay in sync. Falls back to writing
      // the data attribute directly if the button isn't found.
      const btn = document.querySelector(`[data-view-mode="${CSS.escape(action.mode)}"]`);
      if (btn) {
        btn.click();
      } else if (['cards','bubbles','both','none'].includes(action.mode)) {
        document.documentElement.dataset.view = action.mode;
      } else {
        console.warn('[cora-action] set_view_mode: unknown mode', action.mode);
      }
      return;
    }
    if (action.action === 'open_settings') {
      // Opens the unified Main Settings modal and (optionally) selects a
      // specific tab. The historical `target='orb'` shape is honoured by
      // mapping it to the new "orb" tab inside Main Settings.
      const target = (action.target || 'main');         // 'main' | 'orb' (legacy)
      let   tab    = action.tab;                         // optional tab data-tab
      if (target === 'orb' && !tab) tab = 'orb';
      const settingsBtn = document.getElementById('settingsBtn');
      if (settingsBtn) settingsBtn.click();
      if (tab) {
        setTimeout(() => {
          const tabBtn = document.querySelector(`.settings-tab[data-tab="${CSS.escape(tab)}"]`);
          if (tabBtn) tabBtn.click();
        }, 120);
      }
      return;
    }
    if (action.action === 'close_settings') {
      // Clicks the close button on the Main Settings modal if it's open.
      // (The standalone orb modal has been folded into a tab here.)
      const msm = document.getElementById('mainSettingsModal');
      if (msm && !msm.hidden) {
        msm.querySelector('[data-main-settings-close]')?.click();
      }
      return;
    }
    if (action.action === 'close_modal') {
      // Closes any open card/bubble dialog. The existing close handler
      // runs the full FLIP-out animation.
      const modalRoot = document.getElementById('modalRoot');
      if (modalRoot && !modalRoot.hidden) {
        modalRoot.querySelector('.modal-close')?.click();
      }
      return;
    }
    // ---- SIGNAL (Gmail) actions ----------------------------------
    if (action.action === 'signal_inbox') {
      const params = new URLSearchParams();
      if (action.limit) params.set('limit', String(action.limit));
      if (action.unread_only) params.set('unread_only', 'true');
      const qs = params.toString() ? '?' + params.toString() : '';
      fetch('/api/signal/inbox' + qs, { credentials: 'same-origin' })
        .then((r) => r.ok ? r.json() : r.json().then((d) => Promise.reject(d.detail || r.status)))
        .then((d) => renderSignalInbox(d.threads || []))
        .catch((err) => renderSignalInbox([], String(err)));
      return;
    }
    if (action.action === 'signal_read_thread' && typeof action.thread_id === 'string') {
      fetch('/api/signal/threads/' + encodeURIComponent(action.thread_id), { credentials: 'same-origin' })
        .then((r) => r.ok ? r.json() : r.json().then((d) => Promise.reject(d.detail || r.status)))
        .then((d) => renderSignalThread(d))
        .catch((err) => renderSignalThread(null, String(err)));
      return;
    }
    if (action.action === 'signal_draft_reply' && action.to && typeof action.body === 'string') {
      fetch('/api/signal/drafts', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          thread_id:   action.thread_id || null,
          in_reply_to: action.in_reply_to || null,
          to:      action.to,
          subject: action.subject || '',
          body:    action.body,
        }),
      }).then((r) => r.ok ? r.json() : r.json().then((d) => Promise.reject(d.detail || r.status)))
        .then((d) => renderSignalDraft(d))
        .catch((err) => renderSignalDraft(null, String(err)));
      return;
    }
    if (action.action === 'signal_search' && typeof action.query === 'string') {
      const params = new URLSearchParams({ q: action.query });
      if (action.limit) params.set('limit', String(action.limit));
      fetch('/api/signal/search?' + params.toString(), { credentials: 'same-origin' })
        .then((r) => r.ok ? r.json() : r.json().then((d) => Promise.reject(d.detail || r.status)))
        .then((d) => renderSignalInbox(d.threads || [], null, action.query))
        .catch((err) => renderSignalInbox([], String(err), action.query));
      return;
    }

    if (action.action === 'scribe_save' && typeof action.body === 'string') {
      // POST a new SCRIBE memory. Fire-and-forget — Cora's reply
      // already acknowledges the save. Live-update fires on the next
      // SSE notify so the Memory panel refreshes when one's open.
      fetch('/api/scribe/entries', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          body:       action.body,
          title:      action.title || null,
          kind:       action.kind  || 'note',
          tags:       Array.isArray(action.tags) ? action.tags : [],
          importance: typeof action.importance === 'number' ? action.importance : 5,
          source:     'user_request',
        }),
      }).then((r) => {
        if (r.ok) {
          window.dispatchEvent(new CustomEvent('cora:scribe-changed'));
        } else {
          console.warn('[cora-action] scribe_save: HTTP', r.status);
        }
      }).catch((err) => console.warn('[cora-action] scribe_save failed:', err));
      return;
    }
    if (action.action === 'scribe_recall' && typeof action.query === 'string') {
      // Renders the recall result inline in chat as a small list — gives
      // Cora visible feedback (and the user) that the search ran. The
      // model already used its own context to formulate the answer; this
      // is auxiliary UI.
      const params = new URLSearchParams({ q: action.query });
      if (action.tag)   params.set('tag', action.tag);
      if (action.limit) params.set('limit', String(action.limit));
      fetch('/api/scribe/entries?' + params.toString())
        .then((r) => r.ok ? r.json() : Promise.reject(r.status))
        .then((d) => renderScribeRecallResult(action.query, d.entries || []))
        .catch((err) => {
          renderScribeRecallResult(action.query, [], String(err));
        });
      return;
    }
    if (action.action === 'toggle_panel' && typeof action.panel === 'string') {
      // panel: 'skills' | 'activity'
      // open: true (force open), false (force close), undefined (toggle)
      const id = action.panel === 'skills' ? 'skillsPanel' : 'activityPanel';
      const el = document.getElementById(id);
      if (!el) return;
      if (action.open === true)       el.classList.remove('collapsed');
      else if (action.open === false) el.classList.add('collapsed');
      else                            el.classList.toggle('collapsed');
      if (id === 'skillsPanel') {
        try {
          localStorage.setItem('cora-skills-panel-open',
            el.classList.contains('collapsed') ? 'false' : 'true');
        } catch (_) {}
      }
      return;
    }
    if (action.action === 'click_selector' && typeof action.selector === 'string') {
      // Power-user escape hatch — clicks any element matching a CSS
      // selector. Use sparingly; the structured actions above are
      // safer because they go through their natural handlers.
      try {
        const el = document.querySelector(action.selector);
        if (el && typeof el.click === 'function') {
          el.click();
        } else {
          console.warn('[cora-action] click_selector: no match for', action.selector);
        }
      } catch (err) {
        console.warn('[cora-action] click_selector failed:', err);
      }
      return;
    }
    if (action.action === 'open_url' && typeof action.url === 'string') {
      // Opens a URL — defaults to a new tab so the chat doesn't lose
      // its place. Same-tab navigation can be requested with
      // {target:"_self"} but discouraged.
      const target = action.target || '_blank';
      try {
        window.open(action.url, target);
      } catch (err) {
        console.warn('[cora-action] open_url failed:', err);
      }
      return;
    }

    // ---- browser_* actions: drive the Playwright Chromium ----
    // Each call POSTs to /api/browser/<verb>, gets a {ok, url, title,
    // img_url} response, and renders the screenshot inline in chat as
    // an <img>. The screenshot is the visual feedback Cora needs on
    // her NEXT turn to decide what to click.
    const browserVerbs = {
      browser_open:       { path: '/api/browser/navigate',    body: (a) => ({ url: a.url }) },
      browser_click:      { path: '/api/browser/click',       body: (a) => ({ selector: a.selector }) },
      browser_type:       { path: '/api/browser/type',        body: (a) => ({ selector: a.selector, text: a.text || '', submit: !!a.submit }) },
      browser_extract:    { path: '/api/browser/extract',     body: (a) => ({ selector: a.selector }) },
      browser_screenshot: { path: '/api/browser/screenshot',  body: (_) => ({}) },
      browser_back:       { path: '/api/browser/back',        body: (_) => ({}) },
      browser_forward:    { path: '/api/browser/forward',     body: (_) => ({}) },
      browser_close:      { path: '/api/browser/close',       body: (_) => ({}), method: 'POST' },
    };
    if (browserVerbs[action.action]) {
      const def = browserVerbs[action.action];
      (async () => {
        try {
          const r = await fetch(def.path, {
            method: def.method || 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(def.body(action)),
          });
          if (action.action === 'browser_close') {
            renderBrowserResult({ ok: r.ok, closed: true });
            return;
          }
          if (!r.ok) {
            const err = await r.text().catch(() => r.statusText);
            renderBrowserResult({ ok: false, error: r.status + ' ' + (err || '').slice(0, 200) });
            return;
          }
          const data = await r.json();
          renderBrowserResult({ ...data, action: action.action });
        } catch (err) {
          renderBrowserResult({ ok: false, error: String(err.message || err) });
        }
      })();
      return;
    }

    console.warn('[cora-action] unknown action:', action.action);
  }

  // Expose for the voice IIFE: when Cora-on-Spark fires a tool over the
  // WebRTC data channel, the voice handler delegates back here so the
  // full action vocabulary (including browser_*) works the same in
  // chat and voice modes.
  window.coraExecuteAction = executeCoraAction;

  // ---- SIGNAL (Gmail) result renderers --------------------------
  // Each one drops a styled card into the chat so the user can SEE
  // the response (Cora's prose alone wouldn't show the actual emails).
  function renderSignalInbox(threads, errorText, query) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-msg chat-msg-signal';
    const head = document.createElement('div');
    head.className = 'chat-msg-body chat-signal-head';
    head.textContent = errorText
      ? `SIGNAL: ${errorText}`
      : query
        ? `SIGNAL search "${query}" — ${threads.length} thread${threads.length === 1 ? '' : 's'}`
        : `SIGNAL inbox — ${threads.length} thread${threads.length === 1 ? '' : 's'}`;
    wrap.appendChild(head);
    if (threads.length) {
      const list = document.createElement('ul');
      list.className = 'chat-signal-list';
      for (const t of threads) {
        const li = document.createElement('li');
        if (t.unread) li.classList.add('unread');
        const subj = document.createElement('div');
        subj.className = 'chat-signal-subject';
        subj.textContent = t.subject || '(no subject)';
        li.appendChild(subj);
        const meta = document.createElement('div');
        meta.className = 'chat-signal-meta';
        meta.textContent = (t.from || '?').replace(/<[^>]+>/, '').trim() + (t.date ? ' · ' + t.date : '');
        li.appendChild(meta);
        if (t.snippet) {
          const snip = document.createElement('div');
          snip.className = 'chat-signal-snippet';
          snip.textContent = t.snippet;
          li.appendChild(snip);
        }
        list.appendChild(li);
      }
      wrap.appendChild(list);
    }
    surface.appendChild(wrap);
    surface.scrollTop = surface.scrollHeight;
  }

  function renderSignalThread(data, errorText) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-msg chat-msg-signal';
    if (errorText) {
      wrap.innerHTML = `<div class="chat-signal-head chat-signal-error">SIGNAL: ${errorText}</div>`;
      surface.appendChild(wrap);
      return;
    }
    const head = document.createElement('div');
    head.className = 'chat-signal-head';
    head.textContent = data.subject || '(no subject)';
    wrap.appendChild(head);
    for (const m of (data.messages || [])) {
      const card = document.createElement('div');
      card.className = 'chat-signal-msg';
      const meta = document.createElement('div');
      meta.className = 'chat-signal-meta';
      meta.textContent = (m.from || '?').replace(/<[^>]+>/, '').trim() + (m.date ? ' · ' + m.date : '');
      card.appendChild(meta);
      const body = document.createElement('div');
      body.className = 'chat-signal-body';
      body.textContent = m.body || '(empty)';
      card.appendChild(body);
      wrap.appendChild(card);
    }
    surface.appendChild(wrap);
    surface.scrollTop = surface.scrollHeight;
  }

  function renderSignalDraft(data, errorText) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-msg chat-msg-signal';
    if (errorText) {
      wrap.innerHTML = `<div class="chat-signal-head chat-signal-error">SIGNAL draft failed: ${errorText}</div>`;
    } else {
      wrap.innerHTML =
        `<div class="chat-signal-head">Draft saved to Gmail</div>` +
        `<div class="chat-signal-meta">Open Gmail → Drafts to review and send. Draft id: ${data.draft_id}</div>`;
    }
    surface.appendChild(wrap);
    surface.scrollTop = surface.scrollHeight;
  }

  // Append a SCRIBE recall result to the chat: query + matches list.
  // Visible to both user and Cora's next turn (Cora sees the rendered
  // markdown when she scrolls back). One row per match with title /
  // body snippet / tag chips.
  function renderScribeRecallResult(query, entries, errorText) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-msg chat-msg-scribe';
    const head = document.createElement('div');
    head.className = 'chat-msg-body chat-scribe-head';
    head.textContent = errorText
      ? `SCRIBE recall failed: ${errorText}`
      : `SCRIBE recall — "${query}" — ${entries.length} match${entries.length === 1 ? '' : 'es'}`;
    wrap.appendChild(head);
    if (entries.length) {
      const list = document.createElement('ul');
      list.className = 'chat-scribe-list';
      for (const e of entries) {
        const li = document.createElement('li');
        const title = document.createElement('div');
        title.className = 'chat-scribe-title';
        title.textContent = e.title || (e.body || '').slice(0, 80) + ((e.body || '').length > 80 ? '…' : '');
        li.appendChild(title);
        if (e.title && e.body && e.body !== e.title) {
          const body = document.createElement('div');
          body.className = 'chat-scribe-body';
          body.textContent = e.body;
          li.appendChild(body);
        }
        const meta = document.createElement('div');
        meta.className = 'chat-scribe-meta';
        const bits = [`#${e.id}`, e.kind, `imp ${e.importance}`];
        if (Array.isArray(e.tags) && e.tags.length) bits.push(e.tags.map((t) => '#' + t).join(' '));
        meta.textContent = bits.join(' · ');
        li.appendChild(meta);
        list.appendChild(li);
      }
      wrap.appendChild(list);
    }
    surface.appendChild(wrap);
    surface.scrollTop = surface.scrollHeight;
  }

  // Append a small browser-result card to the chat: status + URL + the
  // screenshot inline. Visible to the user; serves as Cora's feedback
  // loop on her next turn (she sees what changed visually).
  function renderBrowserResult(result) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-msg chat-msg-browser';
    if (result.closed) {
      wrap.innerHTML = '<div class="chat-msg-body chat-browser-meta">Browser closed.</div>';
      surface.appendChild(wrap);
      surface.scrollTop = surface.scrollHeight;
      return;
    }
    if (!result.ok) {
      wrap.innerHTML = '<div class="chat-msg-body chat-browser-meta chat-browser-error">Browser action failed: ' +
                       String(result.error || 'unknown').replace(/[<>&]/g, '') + '</div>';
      surface.appendChild(wrap);
      surface.scrollTop = surface.scrollHeight;
      return;
    }
    const meta = document.createElement('div');
    meta.className = 'chat-msg-body chat-browser-meta';
    const urlText = result.url ? new URL(result.url).hostname + (result.url.length > 80 ? '...' : '') : '';
    meta.textContent = (result.title ? '"' + result.title + '" — ' : '') + urlText;
    wrap.appendChild(meta);
    if (result.text) {
      const t = document.createElement('div');
      t.className = 'chat-msg-body chat-browser-extracted';
      t.textContent = result.text.length > 600 ? result.text.slice(0, 600) + '...' : result.text;
      wrap.appendChild(t);
    }
    if (result.img_url) {
      const img = document.createElement('img');
      img.className = 'chat-browser-shot';
      img.src   = result.img_url;
      img.alt   = 'Browser screenshot of ' + (result.url || '');
      img.loading = 'lazy';
      wrap.appendChild(img);
    }
    surface.appendChild(wrap);
    surface.scrollTop = surface.scrollHeight;
  }

  function setAssistantBody(body, text) {
    // Strip any cora-action / fenced-JSON / bare-action JSON from the
    // rendered text so the user only sees the prose. Action execution
    // happens in the streaming loop where we track which blocks have
    // already fired.
    const visible = text
      .replace(_coraActionFenceRe, '')
      .replace(_anyFenceJsonRe,    '')
      .replace(_bareJsonRe,        '')
      .trimEnd();
    const html = renderMarkdown(visible);
    if (html != null) body.innerHTML = html;
    else body.textContent = visible;
  }

  function appendMessage(role, text, attachments) {
    const wrap = document.createElement('div');
    wrap.className = `chat-msg chat-msg-${role}`;
    const body = document.createElement('div');
    body.className = 'chat-msg-body';
    if (role === 'assistant' && text) setAssistantBody(body, text);
    else body.textContent = text;
    wrap.appendChild(body);
    // Attachments rendered as compact chips at the bottom of the bubble.
    if (attachments && attachments.length) {
      const row = document.createElement('div');
      row.className = 'chat-msg-attachments';
      for (const a of attachments) {
        const c = document.createElement('span');
        c.className = 'chat-msg-attachment' + (a.error ? ' chat-msg-attachment-error' : '');
        const tag = a.source === 'mention' ? '@' : '📎';
        const meta = a.error
          ? a.error
          : `${(a.size / 1024).toFixed(a.size < 1024 ? 2 : 1)} KB${a.truncated ? ' · truncated' : ''}`;
        c.innerHTML = `<span class="chat-msg-attachment-tag">${tag}</span><span class="chat-msg-attachment-name"></span><span class="chat-msg-attachment-meta"></span>`;
        c.querySelector('.chat-msg-attachment-name').textContent = a.path || a.name;
        c.querySelector('.chat-msg-attachment-meta').textContent = meta;
        row.appendChild(c);
      }
      wrap.appendChild(row);
    }
    surface.appendChild(wrap);
    surface.hidden = false;
    surface.scrollTop = surface.scrollHeight;
    return body;  // caller streams text into this node
  }

  let inFlight = null;  // AbortController of the active stream, if any

  async function send(text) {
    if (!text.trim() || inFlight) return;

    // Fetch any @path mentions in the prompt + carry forward any files
    // the user already attached via drag-drop / paperclip. Both flows
    // land in the same per-turn `turnAttachments` list.
    let turnAttachments = attachedFiles.slice();
    const mentioned = await fetchMentionedPaths(text);
    if (mentioned.length) turnAttachments = turnAttachments.concat(mentioned);

    // Render the user message with attachment chips so the conversation
    // shows what was sent. Clears the input-side chips after.
    appendMessage('user', text, turnAttachments);
    attachedFiles = [];
    renderAttachmentChips();

    // Inject a developer message with all the file contents BEFORE the
    // user turn, then push the user turn. History grows even if the
    // developer message is empty; that's fine.
    const devMsg = buildAttachmentsDeveloperMessage(turnAttachments);
    if (devMsg) history.push(devMsg);
    history.push({ role: 'user', content: text });
    while (history.length > HISTORY_CAP) history.shift();

    const assistantBody = appendMessage('assistant', '');
    assistantBody.classList.add('streaming');
    let assistantText = '';
    const executedActions = new Set();
    // Tool-call accumulator. OpenAI-streaming sends tool_calls in
    // pieces — keyed by the per-call `index`, function.name lands in
    // one chunk, function.arguments is a string built across N chunks.
    // We dispatch each call as soon as its arguments parse; otherwise
    // catch-all on stream end.
    const pendingToolCalls = {};
    function flushToolCall(idx) {
      const tc = pendingToolCalls[idx];
      if (!tc || tc.dispatched) return false;
      const name = tc.name || '';
      const argsStr = tc.args || '';
      if (!name) return false;
      let argsObj = {};
      if (argsStr.trim()) {
        try { argsObj = JSON.parse(argsStr); }
        catch (_) { return false; }   // not yet complete
      }
      const action = { action: name, ...argsObj };
      tc.dispatched = true;
      console.info('[cora-action] tool_call:', action);
      executeCoraAction(action);
      return true;
    }

    inFlight = new AbortController();
    setSending(true);
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        cache: 'no-store',
        body: JSON.stringify({ messages: history }),
        signal: inFlight.signal,
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => '');
        assistantBody.textContent = `[error ${res.status}] ${detail || res.statusText}`;
        assistantBody.classList.add('error');
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let done = false;
      while (!done) {
        const { value, done: streamDone } = await reader.read();
        done = streamDone;
        if (value) buffer += decoder.decode(value, { stream: true });

        // Pull complete SSE frames out of the buffer; keep any partial tail.
        const frames = buffer.split('\n\n');
        buffer = frames.pop() ?? '';
        for (const frame of frames) {
          if (!frame.trim()) continue;
          let event = 'message';
          const dataLines = [];
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
          }
          const data = dataLines.join('\n');
          if (!data) continue;

          if (event === 'error') {
            try {
              const obj = JSON.parse(data);
              assistantBody.textContent = `[upstream error] ${obj.detail || data}`;
            } catch {
              assistantBody.textContent = `[upstream error] ${data}`;
            }
            assistantBody.classList.add('error');
            done = true;
            break;
          }

          if (data === '[DONE]') { done = true; break; }
          try {
            const obj = JSON.parse(data);
            const choice = obj.choices?.[0] || {};
            const delta  = choice.delta || {};

            // Prose content stream — same as before.
            if (typeof delta.content === 'string' && delta.content) {
              assistantText += delta.content;
              // Legacy fallback: parse fenced cora-action / bare JSON blocks
              // from the prose. Stays in place so the frontend works whether
              // the model uses tool_calls (preferred) or text fences.
              const { actions } = extractCoraActions(assistantText, executedActions);
              for (const a of actions) executeCoraAction(a);
              setAssistantBody(assistantBody, assistantText);
              surface.scrollTop = surface.scrollHeight;
            }

            // Tool-call stream — accumulate by index, dispatch as soon
            // as arguments parse. OpenAI-streaming format: each delta
            // tool_calls[].function.arguments is a string fragment.
            if (Array.isArray(delta.tool_calls)) {
              for (const tc of delta.tool_calls) {
                const idx = tc.index;
                if (typeof idx !== 'number') continue;
                if (!pendingToolCalls[idx]) {
                  pendingToolCalls[idx] = { id: tc.id || '', name: '', args: '', dispatched: false };
                }
                const slot = pendingToolCalls[idx];
                if (tc.id) slot.id = tc.id;
                if (tc.function && tc.function.name) {
                  slot.name = tc.function.name;
                }
                if (tc.function && typeof tc.function.arguments === 'string') {
                  slot.args += tc.function.arguments;
                }
                // Try to dispatch incrementally — args may already be
                // complete after this delta.
                flushToolCall(idx);
              }
            }

            // finish_reason 'tool_calls' / 'stop' — make a final
            // attempt to flush anything we couldn't dispatch above.
            if (choice.finish_reason) {
              for (const idx of Object.keys(pendingToolCalls)) {
                flushToolCall(idx);
              }
            }
          } catch {
            // Ignore unparseable frames (vLLM keepalives, etc.).
          }
        }
      }

      // Final flush — some servers don't send a finish_reason chunk at
      // the very end, so a tool call could be sitting fully accumulated
      // but undispatched. Catch them here.
      for (const idx of Object.keys(pendingToolCalls)) {
        flushToolCall(idx);
      }

      if (assistantText || Object.keys(pendingToolCalls).length) {
        // Diagnostic: log the FULL response so the user can debug "why
        // didn't Cora click X" via DevTools → Console.
        console.groupCollapsed('[cora-chat] full response (' + assistantText.length + ' chars)');
        console.log(assistantText || '(no prose content)');
        const hadFence = /```cora-action/i.test(assistantText);
        const hadJson  = /\{\s*"action"\s*:/i.test(assistantText);
        const toolCallNames = Object.values(pendingToolCalls).map((tc) => tc.name).filter(Boolean);
        console.info('[cora-chat] tool_calls:', toolCallNames,
                     '| fence present:', hadFence,
                     '| bare JSON present:', hadJson);
        console.groupEnd();

        // Strip cora-action / fenced-JSON / bare-JSON command blocks from
        // history too — keeps the LLM's own previous turns clean of the
        // side-channel JSON when they get re-sent on the next request.
        const cleanHistory = assistantText
          .replace(_coraActionFenceRe, '')
          .replace(_anyFenceJsonRe, '')
          .replace(_bareJsonRe, '')
          .trimEnd();
        // Tool-call-only responses (no prose) get a short placeholder so
        // the user sees something happened. Otherwise the bubble is empty
        // and the page just appears to react with no acknowledgement.
        if (!cleanHistory && Object.keys(pendingToolCalls).length) {
          const labels = {
            open_agent_tab:      'Opened the agent tab.',
            invoke_skill:        'Triggered the skill.',
            focus_skill_section: 'Opened the skill category.',
            focus_activity_lane: 'Opened the activity lane.',
            open_panel:          'Opened the panel.',
            persona_add:         'Got it.',
            persona_clear:       'Preferences cleared.',
          };
          const firstName = Object.values(pendingToolCalls)[0]?.name;
          const filler = labels[firstName] || 'Done.';
          assistantBody.textContent = filler;
          history.push({ role: 'assistant', content: filler });
        } else if (cleanHistory) {
          history.push({ role: 'assistant', content: cleanHistory });
        }
      } else if (!assistantBody.classList.contains('error')
                 && Object.keys(pendingToolCalls).length === 0) {
        assistantBody.textContent = '[empty response]';
        assistantBody.classList.add('error');
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        if (!assistantText) assistantBody.textContent = '[cancelled]';
      } else {
        assistantBody.textContent = `[network error] ${e.message || e}`;
        assistantBody.classList.add('error');
      }
    } finally {
      assistantBody.classList.remove('streaming');
      inFlight = null;
      setSending(false);
    }
  }

  function stop() {
    if (inFlight) inFlight.abort();
  }

  // While a stream is in flight the send button acts as a stop button.
  // The form's normal submit path still runs for new messages.
  sendBtn.addEventListener('click', (e) => {
    if (sendBtn.dataset.state === 'sending') {
      e.preventDefault();
      stop();
    }
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (inFlight) { stop(); return; }
    const text = input.value;
    input.value = '';
    send(text);
  });

  // Expose for devtools / future voice→text wiring.
  window.coraChat = { send, stop, history, setChatOpen };
})();

// ----- Voice: WebRTC bridge to the Spark voice backend ------------
// When the user taps the mic, we open a WebRTC peer connection to the
// Cora voice service running on the DGX Spark and bridge the browser's
// mic in / Cora's audio out. Spark is reachable over the user's tailnet
// at the URL stored in localStorage['cora-voice-endpoint'].
//
// Same offer/answer + ICE-candidate-queueing dance as voice/index.html.
// Keeping the UI side here means the existing orb / mic / status-dot
// reactivity (driven by `data-focus="speech"` and the cora:mic-toggle
// event) just works — no new visuals, no new buttons.
(function setupVoice() {
  const audio = document.getElementById('cora-voice-audio');
  if (!audio) return;

  const ENDPOINT_KEY = 'cora-voice-endpoint';
  const DEFAULT_ENDPOINT = 'https://spark-a84c.tail343b33.ts.net';
  function getEndpoint() {
    return (localStorage.getItem(ENDPOINT_KEY) || DEFAULT_ENDPOINT)
      .replace(/\/$/, '');
  }

  let pc = null;
  // Generation counter — every connect() bumps it, every disconnect()
  // bumps it. If the user toggles the mic mid-handshake we can detect
  // that the response we're about to apply belongs to a stale attempt
  // and bail without contaminating the next connection.
  let gen = 0;
  let micStream = null;

  // Live-audio analyser — drives the orb + plexus when Cora is
  // speaking. Created the moment Cora's track lands on the peer
  // connection (pc.ontrack); torn down with the rest in teardown().
  let audioCtx = null;
  let analyser = null;
  let levelBuf = null;
  let levelRafId = 0;

  // Auto-close: end the voice session N ms after Cora finishes
  // speaking, so the user has to wake-word again for the next turn.
  // Default 3 seconds; tunable via localStorage['cora-voice-auto-close-ms']
  // (set to 0 to disable). Timer is started after Cora's FIRST speech
  // burst ends — never fires while she's mid-thinking before her first
  // response, even if processing takes >3s.
  const AUTO_CLOSE_DEFAULT_MS = 3000;
  let autoCloseTimer = null;
  let hasSpokenOnce  = false;

  // cora-ui2 addition: the auto-close above only arms AFTER Cora's first
  // reply — if she never says anything (pipeline down, or the user goes
  // quiet), the session listened forever. This guard closes a session
  // that has produced no reply audio within IDLE_GUARD_MS of (re)arming.
  const IDLE_GUARD_MS = 30000;
  let idleGuardTimer = null;

  function clearIdleGuard() {
    if (idleGuardTimer) {
      clearTimeout(idleGuardTimer);
      idleGuardTimer = null;
    }
  }

  function armIdleGuard() {
    clearIdleGuard();
    idleGuardTimer = setTimeout(() => {
      idleGuardTimer = null;
      if (typeof setListening === 'function' && listening && !hasSpokenOnce) {
        console.log(`[voice] idle guard: no reply audio within ${IDLE_GUARD_MS}ms — closing`);
        setListening(false);
      }
    }, IDLE_GUARD_MS);
  }

  function getAutoCloseMs() {
    try {
      const raw = localStorage.getItem('cora-voice-auto-close-ms');
      if (raw === null) return AUTO_CLOSE_DEFAULT_MS;
      const n = parseInt(raw, 10);
      return Number.isFinite(n) && n >= 0 ? n : AUTO_CLOSE_DEFAULT_MS;
    } catch (_) {
      return AUTO_CLOSE_DEFAULT_MS;
    }
  }

  function clearAutoClose() {
    if (autoCloseTimer) {
      clearTimeout(autoCloseTimer);
      autoCloseTimer = null;
    }
  }

  function scheduleAutoClose() {
    const ms = getAutoCloseMs();
    if (ms <= 0) return;     // disabled
    clearAutoClose();
    autoCloseTimer = setTimeout(() => {
      autoCloseTimer = null;
      if (typeof setListening === 'function' && listening) {
        console.log(`[voice] auto-close after ${ms}ms of silence`);
        setListening(false);
      }
    }, ms);
  }

  function startLevelLoop(stream) {
    if (audioCtx) return;
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch (err) {
      console.warn('[voice] AudioContext unavailable; orb will not react to speech', err);
      return;
    }
    const src = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.4;
    src.connect(analyser);
    // Note: NOT connecting analyser to audioCtx.destination — playback
    // is via the <audio> element's srcObject (auto via WebRTC), and
    // wiring the AudioContext to destination would double the audio.

    levelBuf = new Float32Array(analyser.fftSize);
    let lastSpeakAt = 0;

    function frame(now) {
      if (!analyser) return;
      analyser.getFloatTimeDomainData(levelBuf);
      let sum = 0;
      for (let i = 0; i < levelBuf.length; i++) {
        const v = levelBuf[i];
        sum += v * v;
      }
      // RMS for typical speech runs ~0.03-0.20; multiply for a healthy
      // 0..1 range while clamping the loud end so shouts don't peg.
      const rms = Math.sqrt(sum / levelBuf.length);
      const level = Math.min(1, rms * 6);

      if (window.CORA) window.CORA.voiceLevel = level;

      // Speaking detector: above threshold for any frame keeps us in
      // "speaking" state; 250 ms of silence drops back to listening.
      const SPEAK_THRESH = 0.04;
      if (level > SPEAK_THRESH) {
        lastSpeakAt = now;
        if (!hasSpokenOnce) { hasSpokenOnce = true; clearIdleGuard(); }
        document.documentElement.dataset.voiceState = 'speaking';
        // Cora is talking — cancel any pending auto-close.
        clearAutoClose();
      } else if (now - lastSpeakAt > 250) {
        document.documentElement.dataset.voiceState = listening ? 'listening' : 'idle';
        // Schedule auto-close once we've heard Cora speak at least
        // once and she's now silent. Re-arming each silence transition
        // is fine — scheduleAutoClose clears the previous one first.
        if (hasSpokenOnce && listening && !autoCloseTimer) {
          scheduleAutoClose();
        }
      }

      levelRafId = requestAnimationFrame(frame);
    }
    levelRafId = requestAnimationFrame(frame);
    armIdleGuard();
  }

  function stopLevelLoop() {
    clearIdleGuard();
    if (levelRafId) cancelAnimationFrame(levelRafId);
    levelRafId = 0;
    if (audioCtx) {
      try { audioCtx.close(); } catch {}
      audioCtx = null;
      analyser = null;
      levelBuf = null;
    }
    if (window.CORA) window.CORA.voiceLevel = 0;
    delete document.documentElement.dataset.voiceState;
  }

  async function sendIce(peer, candidate) {
    await fetch(`${getEndpoint()}/api/offer`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pc_id: peer.pc_id,
        candidates: [{
          candidate:        candidate.candidate,
          sdp_mid:          candidate.sdpMid,
          sdp_mline_index:  candidate.sdpMLineIndex,
        }],
      }),
    }).catch((err) => console.warn('[voice] ICE PATCH failed', err));
  }

  async function connect() {
    const myGen = ++gen;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      console.error('[voice] mic permission denied or unavailable', err);
      throw err;
    }
    if (myGen !== gen) {  // user already toggled off
      micStream.getTracks().forEach((t) => t.stop());
      micStream = null;
      return;
    }

    pc = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
    });
    pc.pendingIceCandidates = [];
    pc.canSendIceCandidates = false;
    pc.ontrack = (e) => {
      audio.srcObject = e.streams[0];
      startLevelLoop(e.streams[0]);
    };

    // Data channel for Cora → browser action messages. Created BEFORE
    // setLocalDescription so the offer SDP includes the data channel
    // m-line; without this, Pipecat warns "Data channel not established"
    // and tools can't fire. Both ends can send via this single channel.
    const dc = pc.createDataChannel('cora', { ordered: true });
    dc.onopen    = () => console.log('[voice] data channel open');
    dc.onclose   = () => console.log('[voice] data channel closed');
    dc.onmessage = (ev) => {
      try {
        handleCoraAction(JSON.parse(ev.data));
      } catch (err) {
        console.warn('[voice] bad action message', ev.data, err);
      }
    };
    // Defensively also handle channels created by Spark (in case the
    // role flips in some Pipecat update).
    pc.ondatachannel = (ev) => {
      ev.channel.onmessage = (msg) => {
        try { handleCoraAction(JSON.parse(msg.data)); } catch {}
      };
    };
    pc.onicecandidate = async (e) => {
      if (!e.candidate) return;
      if (pc.canSendIceCandidates && pc.pc_id) {
        await sendIce(pc, e.candidate);
      } else {
        pc.pendingIceCandidates.push(e.candidate);
      }
    };
    pc.onconnectionstatechange = () => {
      const state = pc?.connectionState;
      console.log('[voice] pc state:', state);
      if (state === 'failed' || state === 'closed') {
        // Roll the UI back to idle so the orb stops pulsing
        if (typeof setListening === 'function' && listening) {
          setListening(false);
        }
      }
    };

    pc.addTransceiver(micStream.getAudioTracks()[0], { direction: 'sendrecv' });
    // SmallWebRTCTransport on the Spark side expects both transceivers
    pc.addTransceiver('video', { direction: 'sendrecv' });

    await pc.setLocalDescription(await pc.createOffer());
    if (myGen !== gen) { teardown(); return; }

    // Voice override: if the user picked a non-default voice via the
    // Main settings → Voice tab, pass it through to Spark as ?voice=&
    // ?lang_code= query params. server.py reads them and forwards to
    // run_bot() which constructs CoraKokoroTTS for this session only.
    let offerUrl = `${getEndpoint()}/api/offer`;
    try {
      const saved = JSON.parse(localStorage.getItem('cora-voice-settings') || '{}');
      if (saved.voice) {
        const params = new URLSearchParams({ voice: saved.voice });
        if (saved.langCode) params.set('lang_code', saved.langCode);
        offerUrl += '?' + params.toString();
      }
    } catch (_) { /* no saved override; fall through */ }

    let res;
    try {
      res = await fetch(offerUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sdp:  pc.localDescription.sdp,
          type: pc.localDescription.type,
        }),
      });
    } catch (err) {
      console.error('[voice] offer POST network error', err);
      teardown();
      throw err;
    }
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      console.error(`[voice] offer rejected ${res.status}: ${detail}`);
      teardown();
      throw new Error(`offer ${res.status}`);
    }
    if (myGen !== gen) { teardown(); return; }

    const answer = await res.json();
    pc.pc_id = answer.pc_id;
    await pc.setRemoteDescription(answer);
    pc.canSendIceCandidates = true;
    for (const c of pc.pendingIceCandidates) await sendIce(pc, c);
    pc.pendingIceCandidates = [];
  }

  // Action messages from Cora over the WebRTC data channel. Format:
  //   { type: 'cora_action', action: 'open_card', slug: 'c4' }
  //   { type: 'cora_action', action: 'browser_open', url: '...' }
  // We handle open_card inline (legacy click → existing modal pipeline)
  // and forward EVERYTHING ELSE to the chat-side executeCoraAction so
  // voice gets the full vocabulary (browser_*, focus_skill_section,
  // open_panel, etc.) without duplicating dispatch logic.
  function handleCoraAction(msg) {
    if (!msg || msg.type !== 'cora_action') return;
    console.log('[voice] action', msg);
    if (msg.action === 'open_card' && msg.slug) {
      // Cards put their slug as a CSS class (e.g. `.card-shell.c4`),
      // not as a data attribute — see bootstrap.js renderCard().
      // Click synthesises the existing modal pipeline (FLIP transition,
      // hue propagation, modal lifecycle) without us reimplementing it.
      const sel = `.card-shell.${CSS.escape(msg.slug)}`;
      const card = document.querySelector(sel);
      if (!card) {
        console.warn('[voice] open_card: no card with slug', msg.slug);
        return;
      }
      card.click();
      return;
    }
    if (typeof window.coraExecuteAction === 'function') {
      window.coraExecuteAction(msg);
    } else {
      console.warn('[voice] coraExecuteAction not ready; dropped', msg);
    }
  }

  function teardown() {
    gen++;  // any in-flight connect() will see the bump and bail
    stopLevelLoop();
    clearAutoClose();
    hasSpokenOnce = false;
    if (pc) {
      try { pc.close(); } catch {}
      pc = null;
    }
    if (micStream) {
      micStream.getTracks().forEach((t) => t.stop());
      micStream = null;
    }
    audio.srcObject = null;
  }

  // ---- Persistent-session optimisation ------------------------------
  // Old behaviour: every mic-off tore the WebRTC connection down, so
  // the next wake paid the full ~1-2s handshake cost. New behaviour:
  // mute the local mic but keep `pc` and the data channel alive.
  // Re-arming the next wake just flips track.enabled back on (~ms).
  // Real teardown still happens on pc 'failed' / 'closed' (network
  // dropped) or browser tab close (handled by the platform).
  function setMicEnabled(on) {
    if (!micStream) return;
    micStream.getAudioTracks().forEach((t) => { t.enabled = !!on; });
  }

  function enterIdle() {
    // Cut off Cora's TTS immediately if she's mid-speech when the user
    // toggles off. Without this the buffered audio keeps playing for
    // a few seconds after the mic goes idle — feels like she's ignoring
    // the interrupt. We pause the <audio> element AND drop the source
    // stream; Pipecat's barge-in (PipelineParams.allow_interruptions)
    // handles the server-side cancellation when the user starts a new
    // utterance, but for a manual click we cut the playback locally.
    try { audio.pause(); } catch (_) {}
    setMicEnabled(false);
    clearAutoClose();
    clearIdleGuard();
    hasSpokenOnce = false;
    document.documentElement.dataset.voiceState = 'idle';
  }

  function exitIdle() {
    setMicEnabled(true);
    hasSpokenOnce = false;
    armIdleGuard();
    document.documentElement.dataset.voiceState = 'listening';
    // Resume playback on the inbound audio element — enterIdle paused
    // it to cut off any in-flight TTS, so we need to un-pause for the
    // next response to be audible. play() is wrapped because the
    // browser's autoplay policy will reject it if it can't trace back
    // to a user gesture; in that case the next ontrack fires a fresh
    // play attempt anyway.
    try { audio.play().catch(() => {}); } catch (_) {}
  }

  function isPcAlive() {
    return pc && (pc.connectionState === 'connected' ||
                  pc.connectionState === 'connecting' ||
                  pc.connectionState === 'new');
  }

  // Pre-warm: open the WebRTC connection now and immediately enter
  // idle (mic muted). Pays the ~1-2s handshake at page-load time so
  // the first wake feels instant. Triggered when wake-word is enabled.
  async function preWarmIfNeeded() {
    if (pc) return;
    if (localStorage.getItem('cora-wake-word-enabled') !== 'true') return;
    try {
      await connect();
      enterIdle();
      console.log('[voice] pre-warmed and idle, ready for wake word');
    } catch (err) {
      console.warn('[voice] pre-warm failed (will retry on first wake):', err);
      teardown();
    }
  }

  window.addEventListener('cora:mic-toggle', async (e) => {
    if (e.detail.listening) {
      // Pre-seed the voice-state so the orb has *something* to read
      // even before Cora speaks. Updated to "speaking" by the level
      // loop the moment Cora's track starts producing audio.
      document.documentElement.dataset.voiceState = 'listening';
      if (isPcAlive() && micStream) {
        // Persistent-session fast path: just unmute the existing
        // connection. Sub-100ms vs. ~1500ms full handshake.
        exitIdle();
        return;
      }
      try {
        await connect();
      } catch (err) {
        // Roll the UI back so the orb stops glowing as if connected
        if (typeof setListening === 'function') setListening(false);
      }
    } else {
      // Don't tear down — go idle. The WebRTC + Pipecat session stays
      // up for the next wake. teardown() still fires on 'failed' /
      // 'closed' (see pc.onconnectionstatechange in connect()).
      if (isPcAlive() && micStream) {
        enterIdle();
      } else {
        teardown();
      }
    }
  });

  // Once the page is settled, pre-warm if wake-word is on. setTimeout
  // gives bootstrap.js time to render cards / activity (we don't want
  // to compete for main-thread time at page-paint).
  setTimeout(preWarmIfNeeded, 1500);

  window.coraVoice = {
    getEndpoint,
    setEndpoint(url) { localStorage.setItem(ENDPOINT_KEY, url); },
    isConnected: () => pc?.connectionState === 'connected',
  };
})();
