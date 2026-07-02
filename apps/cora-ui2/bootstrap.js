/**
 * Bootstrap: fetches cards + activity items from /api/* and renders them
 * into the static shell, then dynamically loads shell.js. Also runs the
 * live-update channel: SSE events from Postgres NOTIFY are translated
 * into surgical DOM updates (no page reload).
 *
 * shell.js is intentionally untouched — its DOM queries find the
 * .card-shell / .entry elements as before, just generated from the
 * Postgres-backed API. Surgical updates preserve the wired drag /
 * motion / modal handlers it binds at startup.
 *
 * Note: card.detail_html and card.footer_html are inserted as raw HTML.
 * They come from our own DB seed and are trusted. If you start letting
 * untrusted users author card content, sanitise these fields first.
 */

// ---------------------------------------------------------------------
// Helpers — file-scope so both initial bootstrap AND live-update logic
// can reuse them.
// ---------------------------------------------------------------------
const escapeHTML = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

// Activity lanes used to be hardcoded here; they now come from
// /api/common-data?category=activity_lane. The fallback below is what
// renders if that fetch fails — it matches the seed in migration 007
// so the UI keeps working offline / with a broken API.
let ACTIVITY_LANES = [
  { value: 'inbox_replies', label: 'Inbox Replies' },
  { value: 'scout_tasks',   label: 'Scout Tasks' },
  { value: 'flux_tasks',    label: 'Flux Tasks' },
  { value: 'relay_drafts',  label: 'Relay Drafts' },
];

// Skill categories — same pattern. Loaded from /api/common-data?category=skill_category;
// fallback below mirrors the seed in migration 021.
let SKILL_CATEGORIES = [
  { value: 'communication', label: 'Communication' },
  { value: 'research',      label: 'Research' },
  { value: 'coding',        label: 'Coding' },
  { value: 'calendar',      label: 'Calendar' },
  { value: 'memory',        label: 'Memory' },
];

// Persisted UI state for skills (mirrors the activity-section pattern):
//   cora-skills-collapsed     — JSON array of category values that are
//                               collapsed (section-level fold/unfold).
//   cora-skills-panel-open    — string 'true' | 'false'. Whole-panel
//                               open/closed state. Default: closed.
const SKILLS_COLLAPSED_KEY  = 'cora-skills-collapsed';
const SKILLS_PANEL_OPEN_KEY = 'cora-skills-panel-open';

function readCollapsedSkillCats() {
  try {
    const raw = localStorage.getItem(SKILLS_COLLAPSED_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch (_) { return new Set(); }
}
function writeCollapsedSkillCats(set) {
  try { localStorage.setItem(SKILLS_COLLAPSED_KEY, JSON.stringify(Array.from(set))); }
  catch (_) {}
}

// Collapsed activity sections persist in localStorage so a section
// stays collapsed across reloads and across surgical re-renders driven
// by the live SSE channel. Stored as a JSON array of lane values.
const ACTIVITY_COLLAPSED_KEY = 'cora-activity-collapsed';

function readCollapsedLanes() {
  try {
    const raw = localStorage.getItem(ACTIVITY_COLLAPSED_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch (_) { return new Set(); }
}

function writeCollapsedLanes(set) {
  try {
    localStorage.setItem(ACTIVITY_COLLAPSED_KEY, JSON.stringify(Array.from(set)));
  } catch (_) {}
}

// Chevron rendered at the start of every section header; CSS rotates
// it -90° when the section is collapsed.
const SECTION_CHEVRON = '<svg class="section-chevron" viewBox="0 0 12 8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 1.5l5 5 5-5"/></svg>';

// Motion-pause button — kept identical to the pre-rewire markup so
// shell.js's selectors and CSS keep working.
const MOTION_BTN = `
  <button class="card-motion-btn" type="button" aria-label="Pause motion">
    <svg class="icon-pause" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <rect x="3" y="2" width="3.5" height="12" rx="0.8"/>
      <rect x="9.5" y="2" width="3.5" height="12" rx="0.8"/>
    </svg>
    <svg class="icon-play" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <path d="M4 2.5L13 8L4 13.5V2.5Z"/>
    </svg>
  </button>`;

// Default positions for non-bubble-only cards that have no `position` set
// in the DB. Assigned in render order so each unpositioned card lands in a
// distinct slot — no stacking. Slots avoid the activity panel (right ~25%)
// and the existing seeded positions of c1 (top:18%, left:5.5%), c2
// (bottom:16%, left:7%), and c3 (top:46%, left:30%).
const DEFAULT_CARD_POSITIONS = [
  { top: '14%',    left: '54%' },
  { top: '70%',    left: '58%' },
  { top: '40%',    left: '50%' },
  { top: '24%',    left: '38%' },
  { top: '62%',    left: '36%' },
  { top: '80%',    left: '50%' },
];

function renderCard(card, defaultPosFallback) {
  const shellClasses = ['card-shell', card.slug];
  if (card.is_bubble_only) shellClasses.push('bubble-only');

  const articleClasses = ['response-card'];
  if (card.card_classes) articleClasses.push(card.card_classes);

  // Use the DB's position if present; otherwise (only for visible cards)
  // fall back to the next default slot. Bubble-only cards never need a
  // position because they're display:none in card view and bubbles get
  // their own random placement at view-toggle time.
  const position = card.position
    || (!card.is_bubble_only ? defaultPosFallback : null);

  const styleAttr = position
    ? ` style="${Object.entries(position).map(([k, v]) => `${k}: ${v}`).join('; ')}"`
    : '';

  const motion = card.is_bubble_only ? '' : MOTION_BTN;
  const detail = card.detail_html
    ? `<template class="card-detail">${card.detail_html}</template>`
    : '';
  const footer = card.footer_html
    ? `<div class="card-footer">${card.footer_html}</div>`
    : '';

  return `
    <div class="${shellClasses.join(' ')}"${styleAttr}>
      <article class="${articleClasses.join(' ')}">
        ${motion}
        <div class="card-eyebrow"><span class="pip"></span> ${escapeHTML(card.eyebrow)}</div>
        <div class="card-title">${escapeHTML(card.title)}</div>
        <p class="card-body">${escapeHTML(card.body)}</p>
        ${footer}
      </article>
      ${detail}
    </div>`;
}

function renderSkills(byCat) {
  // Section per category, in the order common_data gives us. Unknown
  // categories from the server are appended with a humanised label.
  const known = new Set(SKILL_CATEGORIES.map((c) => c.value));
  const extra = Object.keys(byCat || {})
    .filter((k) => !known.has(k))
    .map((value) => ({
      value,
      label: value.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
    }));
  const allCats = SKILL_CATEGORIES.concat(extra);
  const collapsed = readCollapsedSkillCats();

  let delay = 60;
  const sections = allCats.map((cat) => {
    const items = byCat[cat.value] || [];
    if (items.length === 0) return ''; // hide empty categories
    const isCollapsed = collapsed.has(cat.value);
    const rows = items.map((sk) => {
      const row = `
        <button type="button" class="skill-item" data-skill-slug="${escapeHTML(sk.slug)}"
                style="animation-delay: ${delay}ms;">
          <span class="skill-text">
            <span class="skill-name">${escapeHTML(sk.name)}</span>
            <span class="skill-desc">${escapeHTML(sk.description || '')}</span>
          </span>
        </button>`;
      delay += 40;
      return row;
    }).join('');
    return `
      <section class="section${isCollapsed ? ' collapsed' : ''}" data-skill-category="${escapeHTML(cat.value)}">
        <div class="section-header" role="button" tabindex="0" aria-expanded="${!isCollapsed}">
          <span class="section-title-wrap">
            ${SECTION_CHEVRON}
            <span>${escapeHTML(cat.label || cat.value)}</span>
          </span>
          <span class="section-count">${items.length}</span>
        </div>
        <div class="section-entries">
          ${rows}
        </div>
      </section>`;
  });
  return sections.join('');
}

function renderActivity(byLane) {
  let delay = 60; // matches the original animation-delay sequence (60..460 step 40)

  // Defensive fallback: if the API returned a lane we don't know about
  // (e.g. a new lane was inserted into common_data after this page
  // loaded), append it after the known lanes with a humanised label so
  // the items still appear instead of getting silently dropped.
  const knownValues = new Set(ACTIVITY_LANES.map((l) => l.value));
  const extraLanes = Object.keys(byLane || {})
    .filter((k) => !knownValues.has(k))
    .map((value) => ({
      value,
      label: value.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
    }));
  const allLanes = ACTIVITY_LANES.concat(extraLanes);

  const collapsed = readCollapsedLanes();

  const sections = allLanes.map((lane) => {
    const items = byLane[lane.value] || [];
    const isCollapsed = collapsed.has(lane.value);
    const entries = items.map((it) => {
      // data-lane / data-lane-label let the modal pipeline in shell.js
      // (collectSourceData) extract context without needing to walk up
      // the DOM. role/tabindex make each entry keyboard-activatable.
      const e = `
        <div class="entry" role="button" tabindex="0"
             data-lane="${escapeHTML(lane.value)}"
             data-lane-label="${escapeHTML(lane.label || lane.value)}"
             style="animation-delay: ${delay}ms;">
          <span class="entry-title">${escapeHTML(it.title)}</span>
          <span class="entry-meta">${escapeHTML(it.meta || '')}</span>
        </div>`;
      delay += 40;
      return e;
    }).join('');
    return `
      <section class="section${isCollapsed ? ' collapsed' : ''}" data-lane="${escapeHTML(lane.value)}">
        <div class="section-header" role="button" tabindex="0" aria-expanded="${!isCollapsed}">
          <span class="section-title-wrap">
            ${SECTION_CHEVRON}
            <span>${escapeHTML(lane.label || lane.value)}</span>
          </span>
          <span class="section-count">${items.length}</span>
        </div>
        <div class="section-entries">
          ${entries}
        </div>
      </section>`;
  });
  return sections.join('');
}

async function fetchJson(url) {
  // cache: 'no-store' tells the browser to bypass its HTTP cache.
  const r = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
  if (!r.ok) throw new Error(`${url} returned ${r.status}`);
  return r.json();
}

function loadShellScript() {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'shell.js';
    s.onload = resolve;
    s.onerror = reject;
    document.body.appendChild(s);
  });
}

function flashUpdated(el) {
  if (!el) return;
  el.classList.remove('cora-updated');   // restart animation if reapplying
  // Force reflow so the class re-add re-triggers the CSS animation.
  void el.offsetWidth;
  el.classList.add('cora-updated');
  setTimeout(() => el.classList.remove('cora-updated'), 1400);
}

// ---------------------------------------------------------------------
// Initial bootstrap: fetch + render + load shell.js.
// ---------------------------------------------------------------------
(async () => {
  try {
    const [cards, activity, lanes, skills, skillCats] = await Promise.all([
      fetchJson('/api/cards'),
      fetchJson('/api/activity?grouped=true'),
      fetchJson('/api/common-data?category=activity_lane').catch(() => null),
      fetchJson('/api/skills?grouped=true').catch(() => null),
      fetchJson('/api/common-data?category=skill_category').catch(() => null),
    ]);

    if (Array.isArray(lanes) && lanes.length > 0) {
      ACTIVITY_LANES = lanes.map((l) => ({ value: l.value, label: l.label || l.value }));
    }
    if (Array.isArray(skillCats) && skillCats.length > 0) {
      SKILL_CATEGORIES = skillCats.map((c) => ({ value: c.value, label: c.label || c.value }));
    }

    const cardLayer    = document.querySelector('.card-layer');
    const panelScroll  = document.querySelector('.activity-panel .panel-scroll');
    const skillsScroll = document.getElementById('skillsPanelScroll');
    if (cardLayer) {
      // Walk the cards in display order; assign the next default slot
      // to each unpositioned visible card so they don't stack at top:0.
      let slot = 0;
      cardLayer.innerHTML = cards.map((card) => {
        const fallback = (!card.position && !card.is_bubble_only)
          ? DEFAULT_CARD_POSITIONS[slot++ % DEFAULT_CARD_POSITIONS.length]
          : null;
        return renderCard(card, fallback);
      }).join('');
    }
    if (panelScroll) panelScroll.innerHTML = renderActivity(activity);
    if (skillsScroll && skills) skillsScroll.innerHTML = renderSkills(skills);

    // Hand off to shell.js — DOM now contains the elements it expects.
    await loadShellScript();
  } catch (err) {
    console.error('[bootstrap] failed to load API data:', err);
    // Still load shell.js so orb / mic / theme keep working without data.
    try { await loadShellScript(); } catch (_) {}
  }
})();

// ---------------------------------------------------------------------
// Activity panel section collapse/expand. Document-level delegation so
// the handler survives surgical re-renders of .panel-scroll (the SSE
// live update replaces innerHTML; a listener on .panel-scroll itself
// would also survive that, but document-level is the simplest answer).
//
// Click anywhere on .section-header → toggle .collapsed on the parent
// .section, persist to localStorage, update aria-expanded.
// Enter / Space on the focused header does the same thing.
// ---------------------------------------------------------------------
function toggleActivitySection(section) {
  if (!section) return;
  const lane = section.dataset.lane;
  if (!lane) return;
  const collapsed = readCollapsedLanes();
  const wasCollapsed = collapsed.has(lane);
  if (wasCollapsed) {
    collapsed.delete(lane);
    section.classList.remove('collapsed');
  } else {
    collapsed.add(lane);
    section.classList.add('collapsed');
  }
  writeCollapsedLanes(collapsed);
  const header = section.querySelector('.section-header');
  if (header) header.setAttribute('aria-expanded', String(wasCollapsed));
}

function toggleSkillSection(section) {
  if (!section) return;
  const cat = section.dataset.skillCategory;
  if (!cat) return;
  const collapsed = readCollapsedSkillCats();
  const wasCollapsed = collapsed.has(cat);
  if (wasCollapsed) {
    collapsed.delete(cat);
    section.classList.remove('collapsed');
  } else {
    collapsed.add(cat);
    section.classList.add('collapsed');
  }
  writeCollapsedSkillCats(collapsed);
  const header = section.querySelector('.section-header');
  if (header) header.setAttribute('aria-expanded', String(wasCollapsed));
}

document.addEventListener('click', (e) => {
  const header = e.target.closest('.section-header');
  if (!header) return;
  // Section can be activity (data-lane) OR skills (data-skill-category).
  const activitySection = header.closest('.section[data-lane]');
  if (activitySection) {
    toggleActivitySection(activitySection);
    return;
  }
  const skillSection = header.closest('.section[data-skill-category]');
  if (skillSection) {
    toggleSkillSection(skillSection);
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const header = e.target.closest && e.target.closest('.section-header');
  if (!header) return;
  e.preventDefault();
  const activitySection = header.closest('.section[data-lane]');
  if (activitySection) {
    toggleActivitySection(activitySection);
    return;
  }
  const skillSection = header.closest('.section[data-skill-category]');
  if (skillSection) {
    toggleSkillSection(skillSection);
  }
});


// ---------------------------------------------------------------------
// Skills panel: clicks on .skill-item POST to /api/skills/{slug}/invoke.
// The invocation lands in skill_invocations and shows up in Cora's
// next-turn screen-context snapshot. Visually we flash the row so the
// user knows the click registered. Document-level delegation so the
// handler survives surgical re-renders.
// ---------------------------------------------------------------------
async function invokeSkill(slug, btn) {
  if (!slug) return;
  if (btn) {
    btn.classList.remove('invoked');
    void btn.offsetWidth; // restart the keyframe
    btn.classList.add('invoked');
  }
  try {
    const r = await fetch('/api/skills/' + encodeURIComponent(slug) + '/invoke', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (!r.ok) {
      console.warn('[skill] invoke failed:', r.status, await r.text());
    }
  } catch (err) {
    console.warn('[skill] invoke error:', err);
  }
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('.skill-item[data-skill-slug]');
  if (!btn) return;
  // Don't double-fire if the click also targeted a section-header (it doesn't,
  // but be defensive).
  e.preventDefault();
  invokeSkill(btn.dataset.skillSlug, btn);
});

// Skills panel toggle. Default: closed (the HTML ships with .collapsed
// already applied to avoid a flash of open-then-close on first paint).
// We honour the user's previous choice via cora-skills-panel-open in
// localStorage — only OPEN it if they explicitly opened it last time.
const skillsPanel       = document.getElementById('skillsPanel');
const skillsPanelToggle = document.getElementById('skillsPanelToggle');
if (skillsPanel) {
  try {
    if (localStorage.getItem(SKILLS_PANEL_OPEN_KEY) === 'true') {
      skillsPanel.classList.remove('collapsed');
    }
  } catch (_) { /* localStorage blocked; stay collapsed */ }
}
if (skillsPanel && skillsPanelToggle) {
  skillsPanelToggle.addEventListener('click', () => {
    skillsPanel.classList.toggle('collapsed');
    try {
      const open = !skillsPanel.classList.contains('collapsed');
      localStorage.setItem(SKILLS_PANEL_OPEN_KEY, open ? 'true' : 'false');
    } catch (_) {}
  });
}

async function liveUpdateSkills() {
  // Re-fetch + re-render — skills/invocations are small, full replacement
  // is fine and keeps the click handlers wired via the document delegate.
  try {
    const skills = await fetchJson('/api/skills?grouped=true');
    const host = document.getElementById('skillsPanelScroll');
    if (host) host.innerHTML = renderSkills(skills);
  } catch (err) {
    console.warn('[skills] live update failed:', err);
  }
}


// ---------------------------------------------------------------------
// Manual refresh + auto-refresh poll. Both call coraRefresh which is a
// full page reload — preserves all localStorage-backed state. The live
// SSE channel below is the path that does NOT reload.
// ---------------------------------------------------------------------
window.coraRefresh = function () {
  window.location.reload();
};

(function setupRefreshTimer() {
  const MAX_SECONDS = 86400; // 24 h
  let timerId = null;

  function readIntervalSeconds() {
    try {
      const raw = localStorage.getItem('cora-main-settings');
      if (!raw) return 0;
      const s = JSON.parse(raw);
      let val = (s && s.refreshIntervalSeconds);
      if (val == null && s && s.refreshIntervalMinutes != null) {
        val = Number(s.refreshIntervalMinutes) * 60;
      }
      const n = Number(val);
      if (!Number.isFinite(n) || n <= 0) return 0;
      return Math.min(MAX_SECONDS, Math.floor(n));
    } catch (_) { return 0; }
  }

  function applyInterval() {
    if (timerId !== null) {
      clearInterval(timerId);
      timerId = null;
    }
    const seconds = readIntervalSeconds();
    if (seconds > 0) {
      timerId = setInterval(() => window.coraRefresh(), seconds * 1000);
    }
  }

  applyInterval();
  window.addEventListener('cora:refresh-interval-changed', applyInterval);
})();


// ---------------------------------------------------------------------
// Surgical live updates driven by /api/events SSE.
//
// Strategy:
//   • activity_items — re-render the entire .panel-scroll from a fresh
//     fetch. Activity items have no wired handlers (just text + an
//     animation-delay style), so a full re-render is safe and handles
//     INSERT, UPDATE and DELETE uniformly.
//   • cards UPDATE — replace the text content of the matching
//     .card-shell in place. Drag, motion-button, and modal-click
//     handlers are wired to the shell at startup and survive because
//     we don't touch the wrapper element.
//   • cards INSERT/DELETE — fall back to coraRefresh() (full reload).
//     Wiring new shells would need shell.js to expose a re-runnable
//     init function; not done yet.
//
// Multi-row updates within the debounce window collapse into a single
// fetch per affected table.
// ---------------------------------------------------------------------
async function liveUpdateActivity() {
  try {
    const grouped = await fetchJson('/api/activity?grouped=true');
    const panelScroll = document.querySelector('.panel-scroll');
    if (!panelScroll) return;
    panelScroll.innerHTML = renderActivity(grouped);
    // Flash all entries briefly so the user sees something happened.
    panelScroll.querySelectorAll('.entry').forEach(flashUpdated);
  } catch (err) {
    console.warn('[live] activity refetch failed:', err);
  }
}

// Compute the bubble `data-meta` value from a card-footer DOM node, mirroring
// the logic of shell.js's collectSourceData(): join the non-`.figure` spans
// with ' · '. Returns '' if footer is null or empty.
function computeBubbleMetaFromFooter(footerEl) {
  if (!footerEl) return '';
  return Array.from(footerEl.querySelectorAll(':scope > span'))
    .filter((s) => !s.classList.contains('figure'))
    .map((s) => s.textContent.trim())
    .filter(Boolean)
    .join(' · ');
}

// If a bubble is currently rendered for this card slug, sync its data-* attrs
// and visible text with the freshly-updated card. No-op when bubbles aren't
// active or this card isn't represented in the current view.
function syncBubbleFor(slug, target, footerEl) {
  const bubble = document.querySelector(`.bubble[data-source-key="${CSS.escape(slug)}"]`);
  if (!bubble) return;

  bubble.dataset.category = target.eyebrow || '';
  bubble.dataset.title    = target.title   || '';
  bubble.dataset.body     = target.body    || '';
  bubble.dataset.meta     = computeBubbleMetaFromFooter(footerEl);

  const bEyebrow = bubble.querySelector('.bubble-eyebrow');
  const bTitle   = bubble.querySelector('.bubble-title');
  const bDesc    = bubble.querySelector('.bubble-desc');
  if (bEyebrow) bEyebrow.textContent = target.eyebrow || '';
  if (bTitle)   bTitle.textContent   = target.title   || '';
  if (bDesc)    bDesc.textContent    = target.body    || '';

  flashUpdated(bubble);
}

async function liveUpdateCards(changes) {
  // changes is an Array of {id, op, slug?, table:'cards'}
  try {
    const cards = await fetchJson('/api/cards');
    const byId = new Map(cards.map((c) => [c.id, c]));
    let needFullReload = false;

    for (const change of changes) {
      const target = byId.get(change.id);

      if (change.op === 'DELETE' || !target) {
        // Row removed — a soft DOM removal is fine, BUT bubbles/modal
        // state may also reference it. Easier and safer: full reload.
        needFullReload = true;
        break;
      }

      const shell = document.querySelector(`.card-shell.${CSS.escape(target.slug)}`);
      if (!shell) {
        // INSERT — no existing shell. shell.js wires handlers at startup
        // and we don't have a re-init entry point for new shells yet.
        needFullReload = true;
        break;
      }

      // Topology change: is_bubble_only flipped. The card needs to move
      // between "card view" and "bubbles only" — that means changing the
      // .bubble-only class, conditionally adding/removing the motion
      // button, AND most importantly re-running shell.js's draggable /
      // bubble-physics setup so the shell behaves correctly in its new
      // role. Falling back to a full reload is the only correct option
      // until shell.js exposes re-init entry points.
      const wasBubbleOnly = shell.classList.contains('bubble-only');
      const nowBubbleOnly = !!target.is_bubble_only;
      if (wasBubbleOnly !== nowBubbleOnly) {
        needFullReload = true;
        break;
      }

      // UPDATE in place — replace text content of the existing shell.
      const eyebrow = shell.querySelector('.card-eyebrow');
      const title   = shell.querySelector('.card-title');
      const body    = shell.querySelector('.card-body');
      const article = shell.querySelector('.response-card');
      const detail  = shell.querySelector('template.card-detail');
      let   footer  = shell.querySelector('.card-footer');

      if (eyebrow) {
        // Eyebrow is "<span class='pip'></span> {text}" — preserve the pip.
        eyebrow.innerHTML = `<span class="pip"></span> ${escapeHTML(target.eyebrow)}`;
      }
      if (title) title.textContent = target.title;
      if (body)  body.textContent  = target.body;

      if (footer && target.footer_html) {
        footer.innerHTML = target.footer_html;
      } else if (footer && !target.footer_html) {
        footer.remove();
        footer = null;
      } else if (!footer && target.footer_html) {
        footer = document.createElement('div');
        footer.className = 'card-footer';
        footer.innerHTML = target.footer_html;
        article.appendChild(footer);
      }

      if (article) {
        article.className = 'response-card' + (target.card_classes ? ` ${target.card_classes}` : '');
      }
      if (detail && target.detail_html) {
        detail.innerHTML = target.detail_html;
      }

      // Note: we deliberately do NOT update card.position here. If the
      // user has dragged the card, their localStorage position takes
      // precedence over the DB's initial position.

      flashUpdated(shell);

      // If a bubble is currently rendered for this card (bubbles or both
      // view), keep its data-* and visible text in sync with the card.
      // Bubbles cache their text in DOM/data-attrs and don't react to
      // their source shell's changes on their own.
      syncBubbleFor(target.slug, target, footer);
    }

    if (needFullReload) window.coraRefresh();
  } catch (err) {
    console.warn('[live] cards refetch failed:', err);
  }
}

(function setupLiveChangeFeed() {
  if (typeof EventSource === 'undefined') return;

  const DEBOUNCE_MS = 250;
  let pendingActivity = false;
  const pendingCards = new Map(); // id → change record (last-write-wins per id)
  let timerId = null;

  function flush() {
    timerId = null;
    if (pendingActivity) {
      pendingActivity = false;
      liveUpdateActivity();
    }
    if (pendingCards.size > 0) {
      const changes = Array.from(pendingCards.values());
      pendingCards.clear();
      liveUpdateCards(changes);
    }
  }

  function schedule() {
    if (timerId !== null) clearTimeout(timerId);
    timerId = setTimeout(flush, DEBOUNCE_MS);
  }

  function onChange(ev) {
    let payload;
    try { payload = JSON.parse(ev.data); }
    catch (_) { return; }

    if (payload.table === 'activity_items') {
      pendingActivity = true;
      schedule();
    } else if (payload.table === 'cards') {
      pendingCards.set(payload.id, payload);
      schedule();
    } else if (payload.table === 'tasks') {
      // tasks table exists in the DB and emits NOTIFY, but there's no
      // frontend UI for it yet — ignore the event silently. When the UI
      // lands, swap this for a tasks-specific surgical update.
    } else if (payload.table === 'agent_runs') {
      // The agent island (rendered by shell.js) listens for this. Doing
      // the dispatch here means we keep one EventSource for the whole
      // page; consumers subscribe via the standard window event API.
      window.dispatchEvent(new CustomEvent('cora:agent-runs-change', { detail: payload }));
    } else if (payload.table === 'agent_configs' || payload.table === 'agent_settings') {
      // Same indirection for the Agents tab — listeners can re-fetch on
      // their own cadence. Not strictly required for Phase A but cheap
      // to wire here so the live UI just works.
      window.dispatchEvent(new CustomEvent('cora:agent-config-change', { detail: payload }));
    } else if (payload.table === 'skills') {
      // Skills metadata change — re-render the SKILLS panel.
      liveUpdateSkills();
    } else if (payload.table === 'skill_invocations') {
      // A click happened (locally or in another tab). The panel itself
      // doesn't change, but other tabs may want to know. Dispatch a
      // CustomEvent so future UI hooks (e.g. a "recently invoked" badge)
      // can pick it up without a second EventSource.
      window.dispatchEvent(new CustomEvent('cora:skill-invocation', { detail: payload }));
    } else {
      // Unknown table — fall back to full reload (covers any future
      // tables added to the trigger before this client knows about them).
      window.coraRefresh();
    }
  }

  function open() {
    let es;
    try {
      es = new EventSource('/api/events');
    } catch (err) {
      console.warn('[live] EventSource init failed:', err);
      return;
    }
    es.addEventListener('change', onChange);
    es.onerror = () => {
      // EventSource auto-reconnects with the `retry: 5000` from the
      // server; log so devtools shows the gap.
      console.warn('[live] /api/events connection lost; reconnecting…');
    };
    // Expose for debugging: window.coraEventSource.close() in devtools
    // suspends live updates for this tab.
    window.coraEventSource = es;
  }
  open();
})();
