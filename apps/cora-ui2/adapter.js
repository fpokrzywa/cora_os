/**
 * cora-ui2 adapter — serves the Cora_2 shell's `/api/*` contract from the
 * Cora AI OS backend (cora-api).
 *
 * The five shell files (index.html, styles.css, bootstrap.js, shell.js,
 * scene.js) are verbatim copies of /home/owner/Cora_2 and stay pristine so
 * they can be re-synced; EVERY backend difference is absorbed here.
 *
 * How: loaded before bootstrap.js, this file wraps window.fetch (and
 * window.EventSource) and intercepts same-origin `/api/...` requests. Each
 * route is either translated to a cora-api call (JWT auth, different paths
 * and shapes) or answered locally (static lookup data, localStorage-backed
 * persona notes, graceful "not available" stubs for features cora-api does
 * not have: browser automation, Gmail OAuth, PULSE settings, agent config
 * editing). Absolute-URL requests (unpkg, fonts, the Spark voice endpoint)
 * pass through untouched.
 *
 * Auth: cora-api requires a JWT on everything, and the Cora_2 shell has no
 * login UI — so this file also injects a minimal login overlay when there is
 * no stored token, POSTs /auth/login, and reloads.
 */
(function () {
  "use strict";

  const cfg = window.CORA_UI2_CONFIG || {};
  const API = String(cfg.apiBase || "http://api.cora.local.arpa").replace(/\/$/, "");
  const TOKEN_KEY = "cora-ui2-token";
  const SESSION_KEY = "cora-ui2-session";
  const PERSONA_KEY = "cora-ui2-persona";

  const realFetch = window.fetch.bind(window);

  // ---- small helpers ----------------------------------------------------

  function token() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; }
  }
  function setToken(t) {
    try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (_) {}
  }
  function chatSession() {
    try { return sessionStorage.getItem(SESSION_KEY) || ""; } catch (_) { return ""; }
  }
  function setChatSession(id) {
    try { sessionStorage.setItem(SESSION_KEY, id); } catch (_) {}
  }

  function json(obj, status) {
    return new Response(JSON.stringify(obj), {
      status: status || 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  function relTime(iso) {
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }

  const escapeHTML = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  // Authenticated call to cora-api. A 401 drops the stale token and raises
  // the login overlay; callers still receive the response.
  async function api(path, init) {
    init = init || {};
    const headers = Object.assign({}, init.headers || {});
    const t = token();
    if (t) headers["Authorization"] = "Bearer " + t;
    const res = await realFetch(API + path, Object.assign({}, init, { headers }));
    if (res.status === 401) {
      setToken("");
      showLogin();
    }
    return res;
  }

  async function apiJson(path) {
    const res = await api(path);
    if (!res.ok) throw new Error(path + " returned " + res.status);
    return res.json();
  }

  // ---- route handlers ----------------------------------------------------

  async function handleCards() {
    const cards = [{
      id: "welcome",
      slug: "cora-os-welcome",
      eyebrow: "Cora AI OS",
      title: "Voice shell online",
      body: "This UI runs on the Cora AI OS backend. Chat below; recent conversations appear as cards and in the activity panel.",
      is_bubble_only: false,
      position: { top: "18%", left: "6%" },
      detail_html:
        "<p>This is the Cora_2 visual shell wired to cora-api. Chat, memories and agent activity are live; " +
        "browser automation, Gmail OAuth and agent config editing live in the classic UI.</p>",
      footer_html: "",
    }];
    if (!token()) return json(cards);
    try {
      const convos = await apiJson("/conversations");
      convos.slice(0, 5).forEach((c, i) => {
        const title = c.title || "Untitled conversation";
        const when = c.updated_at || c.created_at || "";
        cards.push({
          id: c.session_id,
          slug: "conv-" + String(c.session_id).slice(0, 8),
          eyebrow: "Conversation",
          title,
          body: (when ? relTime(when) + " · " : "") + "session " + String(c.session_id).slice(0, 8),
          is_bubble_only: i >= 3, // first three float as cards, the rest are bubbles
          position: null,
          detail_html:
            "<p><strong>" + escapeHTML(title) + "</strong></p>" +
            "<p>Updated " + escapeHTML(when ? relTime(when) : "recently") +
            ". Open the classic UI to browse the full transcript.</p>",
          footer_html: "",
        });
      });
    } catch (_) { /* unauthenticated / API down — welcome card only */ }
    return json(cards);
  }

  async function handleActivity() {
    const grouped = { conversations: [], agent_runs: [] };
    if (!token()) return json(grouped);
    try {
      const convos = await apiJson("/conversations");
      grouped.conversations = convos.slice(0, 8).map((c) => ({
        title: c.title || "Untitled conversation",
        meta: relTime(c.updated_at || c.created_at || ""),
      }));
    } catch (_) {}
    try {
      const runs = await apiJson("/chat/agent/runs");
      grouped.agent_runs = (Array.isArray(runs) ? runs : []).slice(0, 8).map((r) => ({
        title: (r.goal || "agent run").slice(0, 90),
        meta: (r.agent_name || "ATLAS") + " · " + (r.status || ""),
      }));
    } catch (_) { /* agent runtime disabled or forbidden */ }
    return json(grouped);
  }

  function handleSkills() {
    return json({
      research: [
        { slug: "web-search", name: "Web Search", description: "PULSE live web search (SearXNG)" },
        { slug: "news-briefing", name: "News Briefing", description: "Latest ingested news rundown" },
      ],
      calendar: [
        { slug: "daily-briefing", name: "Daily Briefing", description: "Schedule + inbox + news digest" },
        { slug: "free-slots", name: "Find Free Time", description: "CHRONOS open-slot search" },
      ],
      memory: [
        { slug: "memories", name: "Memories", description: "SCRIBE hybrid recall over saved facts" },
      ],
      communication: [
        { slug: "inbox", name: "Inbox Highlights", description: "Read-only Gmail/Outlook summary" },
      ],
    });
  }

  function handleCommonData(category) {
    switch (category) {
      case "activity_lane":
        return json([
          { value: "conversations", label: "Recent Chats" },
          { value: "agent_runs", label: "Agent Runs" },
        ]);
      case "skill_category":
        return json([
          { value: "communication", label: "Communication" },
          { value: "research", label: "Research" },
          { value: "calendar", label: "Calendar" },
          { value: "memory", label: "Memory" },
        ]);
      case "tts_voice":
        // Voices for the Spark (Pipecat/Kokoro) voice service, if it is
        // running; the picker tests directly against that endpoint.
        return json([
          { value: "af_heart", label: "Heart (US female)", description: "Kokoro default", display_order: 1, is_active: true, metadata: { lang_code: "a" } },
          { value: "af_bella", label: "Bella (US female)", description: "Warm, bright", display_order: 2, is_active: true, metadata: { lang_code: "a" } },
          { value: "am_michael", label: "Michael (US male)", description: "Calm, low", display_order: 3, is_active: true, metadata: { lang_code: "a" } },
          { value: "bf_emma", label: "Emma (UK female)", description: "British accent", display_order: 4, is_active: true, metadata: { lang_code: "b" } },
        ]);
      case "agent_provider":
        return json([
          { value: "local_ollama", label: "Local (Ollama)" },
          { value: "local_vllm", label: "Local (vLLM)" },
        ]);
      default:
        return json([]);
    }
  }

  // POST /api/chat {messages:[...]} → cora-api POST /chat (stream) → re-emit
  // as the OpenAI-style SSE chunks shell.js parses. Session continuity is
  // adapter-held (one cora-api session per tab).
  async function handleChat(init) {
    let payload = {};
    try { payload = JSON.parse(init.body || "{}"); } catch (_) {}
    const msgs = Array.isArray(payload.messages) ? payload.messages : [];
    let last = "";
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i] && msgs[i].role === "user") { last = String(msgs[i].content || ""); break; }
    }
    if (!token()) {
      showLogin();
      return json({ detail: "Sign in first — a login prompt is on screen." }, 401);
    }

    const res = await api("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({
        message: last,
        session_id: chatSession() || undefined,
        stream: true,
      }),
      signal: init.signal,
    });
    if (!res.ok || !res.body) return res;

    // Deterministic handlers (calendar, inbox, briefing, memory commands)
    // short-circuit the pipeline and return plain JSON even when
    // stream:true — synthesize a one-shot stream so the shell's SSE
    // parser still renders the reply.
    const ct = (res.headers.get("Content-Type") || "").toLowerCase();
    if (ct.indexOf("application/json") !== -1) {
      const data = await res.json();
      if (data.session_id) setChatSession(data.session_id);
      const text = String(data.response || "");
      const body =
        "data: " + JSON.stringify({ choices: [{ index: 0, delta: { content: text } }] }) +
        "\n\ndata: [DONE]\n\n";
      return new Response(body, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }

    const enc = new TextEncoder();
    const chunk = (text) =>
      enc.encode("data: " + JSON.stringify({ choices: [{ index: 0, delta: { content: text } }] }) + "\n\n");

    const upstream = res.body.getReader();
    const stream = new ReadableStream({
      async start(controller) {
        const decoder = new TextDecoder();
        let buf = "";
        let streamed = "";
        let closed = false;
        const finish = () => {
          if (closed) return;
          closed = true;
          controller.enqueue(enc.encode("data: [DONE]\n\n"));
          controller.close();
        };
        try {
          for (;;) {
            const { done, value } = await upstream.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            let sep;
            while ((sep = buf.indexOf("\n\n")) !== -1) {
              const frame = buf.slice(0, sep);
              buf = buf.slice(sep + 2);
              const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
              if (!dataLine) continue;
              let evt;
              try { evt = JSON.parse(dataLine.slice(5).trim()); } catch (_) { continue; }
              if (evt.type === "meta") {
                if (evt.session_id) setChatSession(evt.session_id);
              } else if (evt.type === "delta") {
                streamed += evt.text;
                controller.enqueue(chunk(evt.text));
              } else if (evt.type === "done") {
                if (evt.session_id) setChatSession(evt.session_id);
                // The authoritative reply can extend the deltas (drafts /
                // proposals appended server-side) — emit the missing tail.
                const full = String(evt.response || "");
                if (full && full.startsWith(streamed) && full.length > streamed.length) {
                  controller.enqueue(chunk(full.slice(streamed.length)));
                }
                finish();
                return;
              } else if (evt.type === "error") {
                if (closed) return;
                closed = true;
                controller.enqueue(enc.encode(
                  "event: error\ndata: " + JSON.stringify({ detail: evt.detail || "upstream error" }) + "\n\n",
                ));
                controller.close();
                return;
              }
            }
          }
          finish();
        } catch (err) {
          if (!closed) { closed = true; controller.error(err); }
        }
      },
      cancel() {
        try { upstream.cancel(); } catch (_) {}
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  }

  // Persona notes — localStorage-backed. cora-api injects its own memory
  // into every chat turn, so these are a UI-side nicety, not server state.
  function readPersona() {
    try { return JSON.parse(localStorage.getItem(PERSONA_KEY) || "[]"); } catch (_) { return []; }
  }
  function writePersona(notes) {
    try { localStorage.setItem(PERSONA_KEY, JSON.stringify(notes)); } catch (_) {}
  }

  async function handleScribeRecall(u) {
    const q = u.searchParams.get("q") || "";
    if (!q || !token()) return json({ entries: [] });
    try {
      const rows = await apiJson("/memory/search?q=" + encodeURIComponent(q) + "&limit=8");
      return json({
        entries: (Array.isArray(rows) ? rows : []).map((r) => ({
          title: r.title,
          body: r.content_preview || "",
          kind: r.type,
          tags: r.tags || [],
        })),
      });
    } catch (_) {
      return json({ entries: [] });
    }
  }

  const NOT_AVAILABLE =
    "Not available in the Cora AI OS build — use the classic UI for this.";

  // ---- the fetch shim ------------------------------------------------------

  function route(u, init) {
    const p = u.pathname;
    const method = ((init && init.method) || "GET").toUpperCase();

    if (p === "/api/cards") return handleCards();
    if (p === "/api/activity") return handleActivity();
    if (p === "/api/skills") return handleSkills();
    if (p.startsWith("/api/skills/") && p.endsWith("/invoke")) return json({ ok: true });
    if (p === "/api/common-data") return handleCommonData(u.searchParams.get("category") || "");
    if (p === "/api/chat") return handleChat(init);

    if (p === "/api/persona" && method === "GET") return json({ notes: readPersona() });
    if (p === "/api/persona" && method === "DELETE") { writePersona([]); return json({ ok: true }); }
    if (p === "/api/persona/add") {
      let body = {};
      try { body = JSON.parse(init.body || "{}"); } catch (_) {}
      const text = String(body.text || "").trim();
      if (text) {
        const notes = readPersona();
        if (!notes.some((n) => n.text === text)) notes.push({ text, added_at: new Date().toISOString() });
        writePersona(notes);
      }
      return json({ ok: true });
    }
    if (/^\/api\/persona\/\d+$/.test(p) && method === "DELETE") {
      const idx = Number(p.split("/").pop());
      const notes = readPersona();
      if (idx >= 0 && idx < notes.length) notes.splice(idx, 1);
      writePersona(notes);
      return json({ ok: true });
    }

    if (p === "/api/agents/list") {
      return json(["cora", "atlas", "scribe", "forge", "pulse", "signal", "chronos"]
        .map((agent) => ({ agent, active_version: null, active_provider: null })));
    }
    if (p === "/api/agents/runs") {
      return (async () => {
        if (!token()) return json([]);
        try {
          const runs = await apiJson("/chat/agent/runs");
          const live = (Array.isArray(runs) ? runs : []).filter((r) =>
            ["queued", "pending", "running", "waiting_user"].includes(r.status));
          return json(live.map((r) => ({
            agent: String(r.agent_name || "atlas").toLowerCase(),
            task: r.goal || "",
            status: r.status,
            created_at: r.created_at,
            started_at: r.created_at,
          })));
        } catch (_) { return json([]); }
      })();
    }
    if (p === "/api/signal/auth/status") return json({ configured: false });
    if (p === "/api/scribe/entries" && method === "GET") return handleScribeRecall(u);
    if (p.startsWith("/api/browser/")) {
      return json({ ok: false, url: "", title: "", error: NOT_AVAILABLE });
    }
    if (p === "/api/tasks") return json([]);

    // Everything else (agent configs, pulse settings, scribe save, file
    // mentions, …) is honestly not supported by this backend.
    return json({ detail: NOT_AVAILABLE }, 501);
  }

  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const u = new URL(url, window.location.origin);
      if (u.origin === window.location.origin && u.pathname.startsWith("/api/")) {
        return Promise.resolve(route(u, init || (typeof input === "object" ? input : {})));
      }
    } catch (_) { /* fall through to the real fetch */ }
    return realFetch(input, init);
  };

  // The shell opens EventSource('/api/events') for live updates; cora-api has
  // no equivalent push channel, and unknown events trigger full reloads — so
  // serve a silent, permanently-"open" stub instead of letting it 404-loop.
  const RealEventSource = window.EventSource;
  if (RealEventSource) {
    window.EventSource = function (url, opts) {
      if (String(url).indexOf("/api/events") === 0) {
        return {
          url: String(url), readyState: 1, withCredentials: false,
          onopen: null, onmessage: null, onerror: null,
          addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; },
          close() { this.readyState = 2; },
        };
      }
      return new RealEventSource(url, opts);
    };
    window.EventSource.prototype = RealEventSource.prototype;
  }

  // ---- login overlay -------------------------------------------------------

  let overlayUp = false;
  function showLogin() {
    if (overlayUp) return;
    overlayUp = true;
    const build = () => {
      const wrap = document.createElement("div");
      wrap.id = "coraUi2Login";
      wrap.innerHTML =
        '<style>' +
        '#coraUi2Login{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;' +
        'background:rgba(4,8,18,0.72);backdrop-filter:blur(14px);font-family:Inter,system-ui,sans-serif;}' +
        '#coraUi2Login .box{width:min(360px,92vw);padding:28px;border-radius:18px;border:1px solid rgba(255,255,255,0.14);' +
        'background:rgba(16,22,38,0.78);box-shadow:0 24px 80px rgba(0,0,0,0.5);color:#e8ecf6;}' +
        '#coraUi2Login h2{margin:0 0 4px;font-size:19px;font-weight:600;}' +
        '#coraUi2Login p{margin:0 0 18px;font-size:12.5px;color:rgba(232,236,246,0.6);}' +
        '#coraUi2Login input{width:100%;box-sizing:border-box;margin:0 0 10px;padding:10px 12px;border-radius:10px;' +
        'border:1px solid rgba(255,255,255,0.16);background:rgba(255,255,255,0.06);color:#fff;font-size:14px;outline:none;}' +
        '#coraUi2Login button{width:100%;padding:10px 12px;border-radius:10px;border:0;cursor:pointer;' +
        'background:#2563eb;color:#fff;font-size:14px;font-weight:600;}' +
        '#coraUi2Login .err{min-height:16px;margin:8px 0 0;font-size:12px;color:#ff8a80;}' +
        '</style>' +
        '<div class="box"><h2>Sign in to Cora</h2>' +
        '<p>Same account as the classic UI (cora-api).</p>' +
        '<form><input type="email" name="email" placeholder="Email" autocomplete="username" required>' +
        '<input type="password" name="password" placeholder="Password" autocomplete="current-password" required>' +
        '<button type="submit">Sign in</button><div class="err"></div></form></div>';
      document.body.appendChild(wrap);
      const form = wrap.querySelector("form");
      const err = wrap.querySelector(".err");
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        err.textContent = "";
        try {
          const res = await realFetch(API + "/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              email: form.email.value.trim(),
              password: form.password.value,
            }),
          });
          if (!res.ok) {
            let detail = res.statusText;
            try { detail = (await res.json()).detail || detail; } catch (_) {}
            throw new Error(detail);
          }
          const data = await res.json();
          setToken(data.access_token);
          window.location.reload();
        } catch (ex) {
          err.textContent = ex && ex.message ? ex.message : "Login failed";
        }
      });
    };
    if (document.body) build();
    else document.addEventListener("DOMContentLoaded", build, { once: true });
  }

  // ---- settings sync -------------------------------------------------------
  // The shell persists ALL its settings (theme, orb, main settings, voice,
  // wake word, card positions, panel state) in localStorage — which is
  // per-origin and gone after a site-data clear. Mirror those keys to
  // cora-api (PUT /users/me/ui-prefs) whenever the shell writes them, and
  // seed them back at boot, so settings follow the ACCOUNT across origins,
  // devices and logouts. Our own cora-ui2-* keys (token/session) never sync.

  const PREFS_PATH = "/users/me/ui-prefs";
  const syncable = (k) => k.indexOf("cora-") === 0 && k.indexOf("cora-ui2-") !== 0;

  function snapshotPrefs() {
    const out = {};
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && syncable(k)) out[k] = localStorage.getItem(k);
    }
    return out;
  }

  // Pushes are suppressed until the boot-time seed finishes, so a fresh
  // browser's defaults can't overwrite the account's saved settings.
  let prefsSeeded = false;
  let pushPending = false;
  let pushTimer = null;
  function schedulePrefsPush() {
    if (!token()) return;
    if (!prefsSeeded) { pushPending = true; return; }
    if (pushTimer) clearTimeout(pushTimer);
    pushTimer = setTimeout(() => {
      pushTimer = null;
      api(PREFS_PATH, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prefs: snapshotPrefs() }),
      }).catch(() => { /* offline — next change retries */ });
    }, 800);
  }

  const realSetItem = Storage.prototype.setItem;
  const realRemoveItem = Storage.prototype.removeItem;
  Storage.prototype.setItem = function (k, v) {
    realSetItem.call(this, k, v);
    if (this === window.localStorage && syncable(String(k))) schedulePrefsPush();
  };
  Storage.prototype.removeItem = function (k) {
    realRemoveItem.call(this, k);
    if (this === window.localStorage && syncable(String(k))) schedulePrefsPush();
  };

  async function seedPrefsFromServer() {
    if (!token()) return;
    try {
      const res = await api(PREFS_PATH);
      if (!res.ok) return;
      const data = await res.json();
      const prefs = data && data.prefs;
      if (!prefs || typeof prefs !== "object") return;
      let changed = false;
      for (const k of Object.keys(prefs)) {
        const v = prefs[k];
        if (!syncable(k) || typeof v !== "string") continue;
        if (localStorage.getItem(k) !== v) {
          realSetItem.call(localStorage, k, v);
          changed = true;
        }
      }
      // Theme/orb/background were already painted by the head scripts from
      // the OLD local values — one reload applies the account's settings.
      // Can't loop: after seeding, local matches server, so the next pass
      // sees no change.
      if (changed) window.location.reload();
    } catch (_) {
      // API unreachable — keep local-only behavior.
    } finally {
      prefsSeeded = true;
      if (pushPending) { pushPending = false; schedulePrefsPush(); }
    }
  }
  seedPrefsFromServer();

  if (!token()) showLogin();
})();
