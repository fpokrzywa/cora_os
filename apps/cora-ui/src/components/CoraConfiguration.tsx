import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { setScreenView } from "../screenContext";
import {
  decideAgentRun,
  getAgentConfig,
  getAgentRun,
  listAgentRuns,
  sendAgentChat,
  sendAgentChatAsync,
} from "../api";
import type {
  AgentDelegationNode,
  AgentEvaluation,
  AgentInterrupt,
  AgentRunDetail,
  AgentRunResponse,
  AgentRunStep,
  AgentRunSummary,
  AgentRuntimeConfig,
} from "../types";

function StatusPill({ label, on }: { label: string; on: boolean }) {
  return (
    <span className={`agent-pill agent-pill--${on ? "on" : "off"}`}>
      <span className="agent-pill__dot" aria-hidden />
      {label}: {on ? "on" : "off"}
    </span>
  );
}

function StepRow({ step }: { step: AgentRunStep }) {
  if (step.kind === "tool_call") {
    const isDelegate = step.name === "delegate_to";
    const target = String(step.arguments?.agent ?? "?");
    return (
      <li className="agent-step agent-step--call">
        <span className="agent-step__arrow" aria-hidden>
          →
        </span>
        <span className="agent-step__name">
          {isDelegate ? `delegate → ${target}` : step.name}
        </span>
        {step.arguments && Object.keys(step.arguments).length > 0 && (
          <code className="agent-step__args">{JSON.stringify(step.arguments)}</code>
        )}
      </li>
    );
  }
  if (step.kind === "tool_result") {
    return (
      <li className="agent-step agent-step--result">
        <span className="agent-step__arrow" aria-hidden>
          ←
        </span>
        <span className="agent-step__name">{step.name}</span>
        <span className="agent-step__text">{step.result}</span>
      </li>
    );
  }
  if (step.kind === "error") {
    return <li className="agent-step agent-step--error">⚠ {step.error}</li>;
  }
  return null; // 'final' — the answer is rendered separately below
}

// Status → on/off coloring for the run pills (done/final = good).
function runOk(status: string | null | undefined): boolean {
  return status !== "failed" && status !== "error" && status !== "cancelled";
}

// Statuses still being driven by the worker — keep polling. waiting_user is NOT
// here: it's the human's turn, so polling stops and the approval card shows.
const POLLING_STATUSES = new Set(["pending", "running", "waiting_tool"]);
function isPollable(status: string | null | undefined): boolean {
  return POLLING_STATUSES.has(status ?? "");
}

// Independent evaluator verdict (Phase 6) — advisory, review-only.
function EvaluationCard({ evaluation }: { evaluation: AgentEvaluation }) {
  return (
    <div className="agent-eval">
      <div className="agent-eval__head">
        <span className={`agent-verdict agent-verdict--${evaluation.verdict}`}>
          evaluator: {evaluation.verdict}
        </span>
        {evaluation.model && (
          <span className="agent-eval__model">
            judge <code>{evaluation.model}</code>
          </span>
        )}
      </div>
      {evaluation.summary && (
        <p className="agent-eval__summary">{evaluation.summary}</p>
      )}
      {evaluation.reasons.length > 0 && (
        <ul className="agent-eval__reasons">
          {evaluation.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Confirm-as-interrupt (Phase 7) — a run paused at waiting_user. Approving
// records the decision ONLY; it never sends or writes anything.
function InterruptCard({
  runId,
  interrupt,
  onResolved,
}: {
  runId: string;
  interrupt: AgentInterrupt;
  onResolved?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [decided, setDecided] = useState<string | null>(interrupt.decision);
  const [err, setErr] = useState<string | null>(null);

  const decide = async (decision: "approve" | "reject") => {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      await decideAgentRun(runId, decision);
      setDecided(decision);
      onResolved?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Decision failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="agent-interrupt">
      <div className="agent-interrupt__head">⏸ Awaiting your approval</div>
      <p className="agent-cfg__note">
        This run staged the artifact(s) below. Approving records your decision; if
        calendar execution is enabled it also creates an approved calendar item on
        your real calendar. Email drafts are never sent — they stay in your drafts
        to review and send yourself.
      </p>
      {interrupt.staged.length > 0 && (
        <ul className="agent-interrupt__staged">
          {interrupt.staged.map((s, i) => (
            <li key={i}>
              <code>{s.tool}</code> {s.summary}
              {s.type === "calendar_create" && (
                <span className="agent-deleg__reason">
                  {" "}
                  → would create on {s.provider}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {interrupt.executed && interrupt.executed.length > 0 && (
        <ul className="agent-interrupt__staged">
          {interrupt.executed.map((e, i) => (
            <li key={i}>
              <span className={`agent-pill agent-pill--${e.ok ? "on" : "off"}`}>
                {e.ok ? "fired" : "not fired"}
              </span>{" "}
              {e.ok
                ? `created — ${e.title ?? e.event_id ?? "ok"}`
                : e.reason}
              {e.ok && e.link && (
                <>
                  {" "}
                  <a href={e.link} target="_blank" rel="noreferrer">
                    open
                  </a>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {decided ? (
        <span
          className={`agent-verdict agent-verdict--${
            decided === "approve" ? "pass" : "fail"
          }`}
        >
          {decided}d — recorded
        </span>
      ) : (
        <div className="agent-interrupt__btns">
          <button
            className="btn btn--primary"
            disabled={busy}
            onClick={() => decide("approve")}
          >
            Approve
          </button>
          <button
            className="btn btn--ghost"
            disabled={busy}
            onClick={() => decide("reject")}
          >
            Reject
          </button>
        </div>
      )}
      {err && <div className="chat__error">{err}</div>}
    </div>
  );
}

function when(ts: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function AgentPanel({ config }: { config: AgentRuntimeConfig | null }) {
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<AgentRunResponse | null>(null);
  const [asyncRunId, setAsyncRunId] = useState<string | null>(null);
  const [runErr, setRunErr] = useState<string | null>(null);

  const run = async () => {
    const msg = input.trim();
    if (!msg || sending) return;
    setSending(true);
    setRunErr(null);
    setResult(null);
    setAsyncRunId(null);
    try {
      setResult(await sendAgentChat(msg));
    } catch (e) {
      setRunErr(e instanceof Error ? e.message : "Request failed");
    } finally {
      setSending(false);
    }
  };

  // Background run: enqueue on the worker, return immediately, then poll the
  // run id for live progress. Best for long / delegating runs that would block
  // the synchronous request.
  const runAsync = async () => {
    const msg = input.trim();
    if (!msg || sending) return;
    setSending(true);
    setRunErr(null);
    setResult(null);
    setAsyncRunId(null);
    try {
      const { run_id } = await sendAgentChatAsync(msg);
      setAsyncRunId(run_id);
    } catch (e) {
      setRunErr(e instanceof Error ? e.message : "Request failed");
    } finally {
      setSending(false);
    }
  };

  return (
    <>
      <section className="agent-cfg__status">
        {config && (
          <>
            <div className="agent-cfg__pills">
              <StatusPill label="Runtime" on={config.runtime_enabled} />
              <StatusPill label="Delegation" on={config.delegation_enabled} />
              <StatusPill label="Write / staging" on={config.write_enabled} />
              <StatusPill label="Evaluator" on={config.eval_enabled} />
              <StatusPill label="Interrupt" on={config.interrupt_enabled} />
              <StatusPill label="Execution" on={config.execution_enabled} />
            </div>
            <div className="agent-cfg__meta">
              Model <code>{config.chat_model || "—"}</code> · max steps{" "}
              {config.max_steps} · max parallel {config.max_parallel} · endpoint{" "}
              {config.endpoint_configured ? "configured" : "missing"}
            </div>
            <p className="agent-cfg__note">
              Flags are set via environment (<code>AGENT_*</code>) and need a
              service restart to change — this panel is read-only status.
            </p>
          </>
        )}
      </section>

      <section className="agent-cfg__try">
        <h2 className="agent-cfg__h2">Use the agent</h2>
        {config && !config.runtime_enabled && (
          <div className="chat__hint">
            The agent runtime is currently disabled — set
            <code> AGENT_RUNTIME_ENABLED=true</code> to use it.
          </div>
        )}
        <div className="agent-cfg__composer">
          <textarea
            className="composer__input"
            placeholder="Ask the agent to do something — e.g. “search the web for the latest on X and summarize”…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            rows={2}
            disabled={sending}
          />
          <div className="agent-cfg__run-btns">
            <button
              className="btn btn--primary"
              onClick={run}
              disabled={sending || !input.trim()}
            >
              {sending ? "Running…" : "Run"}
            </button>
            <button
              className="btn btn--ghost"
              onClick={runAsync}
              disabled={sending || !input.trim()}
              title="Enqueue on the worker and poll for progress — best for long runs"
            >
              Run in background
            </button>
          </div>
        </div>
        {runErr && <div className="chat__error">{runErr}</div>}

        {asyncRunId && (
          <>
            <p className="agent-cfg__note">
              Background run <code>{asyncRunId.slice(0, 8)}</code> — polling for
              progress; it also appears in the <strong>Runs</strong> tab.
            </p>
            <RunDetail runId={asyncRunId} poll />
          </>
        )}

        {result && (
          <div className="agent-cfg__result">
            <div className="agent-cfg__result-meta">
              <span
                className={`agent-pill agent-pill--${
                  result.stopped === "error" ? "off" : "on"
                }`}
              >
                {result.stopped}
              </span>
              <span>
                {result.tool_calls} tool call
                {result.tool_calls === 1 ? "" : "s"}
              </span>
              <span>
                model <code>{result.model}</code>
              </span>
            </div>
            {result.steps.length > 0 && (
              <ol className="agent-steps">
                {result.steps.map((s, i) => (
                  <StepRow key={i} step={s} />
                ))}
              </ol>
            )}
            <div className="agent-cfg__answer msg__bubble msg__bubble--md">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {result.answer}
              </ReactMarkdown>
            </div>
            {result.evaluation && (
              <EvaluationCard evaluation={result.evaluation} />
            )}
            {result.status === "waiting_user" &&
              result.interrupt &&
              result.run_id && (
                <InterruptCard
                  runId={result.run_id}
                  interrupt={result.interrupt}
                />
              )}
          </div>
        )}
      </section>
    </>
  );
}

function DelegationNode({ deleg }: { deleg: AgentDelegationNode }) {
  const spoke = deleg.spoke_run;
  return (
    <li className="agent-deleg">
      <div className="agent-deleg__head">
        <span className="agent-run__badge agent-run__badge--spoke">
          {deleg.from_agent} → {deleg.to_agent}
        </span>
        <span className={`agent-pill agent-pill--${runOk(deleg.status) ? "on" : "off"}`}>
          {deleg.status}
        </span>
        {deleg.delegation_reason && (
          <span className="agent-deleg__reason">{deleg.delegation_reason}</span>
        )}
      </div>
      {spoke && spoke.steps.length > 0 && (
        <ol className="agent-steps">
          {spoke.steps.map((s, i) => (
            <StepRow key={i} step={s} />
          ))}
        </ol>
      )}
      {spoke?.answer && (
        <div className="agent-deleg__answer msg__bubble msg__bubble--md">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{spoke.answer}</ReactMarkdown>
        </div>
      )}
    </li>
  );
}

function RunDetail({ runId, poll = false }: { runId: string; poll?: boolean }) {
  const [detail, setDetail] = useState<AgentRunDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const reload = () => setReloadKey((k) => k + 1);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setDetail(null);
    setErr(null);
    const tick = () => {
      getAgentRun(runId)
        .then((d) => {
          if (!active) return;
          setDetail(d);
          // Keep polling while the worker is still driving it (async mode);
          // stop at waiting_user (the human's turn) and terminal states.
          if (poll && isPollable(d.status)) {
            timer = setTimeout(tick, 2000);
          }
        })
        .catch((e) => {
          if (!active) return;
          setErr(e instanceof Error ? e.message : "Failed to load run");
        });
    };
    tick();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [runId, poll, reloadKey]);

  if (err) return <div className="chat__error">{err}</div>;
  if (!detail) return <div className="agent-cfg__note">Loading run…</div>;

  const live = poll && isPollable(detail.status);

  return (
    <div className="agent-cfg__result">
      <div className="agent-cfg__result-meta">
        <span className="agent-run__badge">{detail.agent_name || "ATLAS"}</span>
        <span className={`agent-pill agent-pill--${runOk(detail.status) ? "on" : "off"}`}>
          {detail.status}
          {detail.stopped ? ` · ${detail.stopped}` : ""}
        </span>
        {live && <span className="agent-cfg__live">updating…</span>}
        <span>
          {detail.tool_calls} tool call{detail.tool_calls === 1 ? "" : "s"} ·{" "}
          {detail.step_count}/{detail.max_steps} steps
        </span>
        <span>
          model <code>{detail.model_name || "—"}</code>
        </span>
      </div>
      <p className="agent-cfg__note">Goal: {detail.goal}</p>
      {detail.error_message && (
        <div className="chat__error">{detail.error_message}</div>
      )}
      {detail.steps.length > 0 && (
        <ol className="agent-steps">
          {detail.steps.map((s, i) => (
            <StepRow key={i} step={s} />
          ))}
        </ol>
      )}
      {detail.delegations.length > 0 && (
        <div className="agent-deleg__wrap">
          <h3 className="agent-cfg__h2">Delegations</h3>
          <ul className="agent-deleg__list">
            {detail.delegations.map((d) => (
              <DelegationNode key={d.id} deleg={d} />
            ))}
          </ul>
        </div>
      )}
      {detail.answer && (
        <div className="agent-cfg__answer msg__bubble msg__bubble--md">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{detail.answer}</ReactMarkdown>
        </div>
      )}
      {detail.evaluation && <EvaluationCard evaluation={detail.evaluation} />}
      {detail.status === "waiting_user" && detail.interrupt && (
        <InterruptCard
          runId={detail.id}
          interrupt={detail.interrupt}
          onResolved={reload}
        />
      )}
    </div>
  );
}

function AgentRuns() {
  const [runs, setRuns] = useState<AgentRunSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = () => {
    setLoading(true);
    setErr(null);
    listAgentRuns()
      .then((rows) => {
        setRuns(rows);
        setSelected((cur) => cur ?? (rows[0]?.id ?? null));
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "Failed to load runs"))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  return (
    <section className="agent-runs">
      <div className="agent-runs__bar">
        <h2 className="agent-cfg__h2">Runs</h2>
        <button className="btn btn--ghost" onClick={load} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      {err && <div className="chat__error">{err}</div>}
      {runs && runs.length === 0 && (
        <p className="agent-cfg__note">
          No agent runs yet — use the Agent tab to start one.
        </p>
      )}
      {runs && runs.length > 0 && (
        <div className="agent-runs__split">
          <ul className="agent-runs__list">
            {runs.map((r) => (
              <li key={r.id}>
                <button
                  className={`agent-run-row${
                    selected === r.id ? " agent-run-row--active" : ""
                  }`}
                  onClick={() => setSelected(r.id)}
                >
                  <span className="agent-run-row__top">
                    <span className="agent-run__badge">
                      {r.agent_name || "ATLAS"}
                    </span>
                    <span
                      className={`agent-pill agent-pill--${
                        runOk(r.status) ? "on" : "off"
                      }`}
                    >
                      {r.status}
                    </span>
                  </span>
                  <span className="agent-run-row__goal">{r.goal}</span>
                  <span className="agent-run-row__meta">
                    {r.tool_calls} call{r.tool_calls === 1 ? "" : "s"} ·{" "}
                    {when(r.created_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <div className="agent-runs__detail">
            {selected ? (
              <RunDetail runId={selected} />
            ) : (
              <p className="agent-cfg__note">Select a run to inspect its trace.</p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

// In the Admin Console, AdminConsole's outer bar drives the Agent/Runs sub-tabs
// and passes `sub`. Rendered standalone (the sidebar entry open to every user),
// this owns its own sub-tab row + screen-context reporting.
export function CoraConfiguration({
  sub,
  standalone = false,
}: {
  sub?: string;
  standalone?: boolean;
}) {
  const [config, setConfig] = useState<AgentRuntimeConfig | null>(null);
  const [cfgErr, setCfgErr] = useState<string | null>(null);
  const [localSub, setLocalSub] = useState("agent");

  useEffect(() => {
    getAgentConfig()
      .then(setConfig)
      .catch((e) =>
        setCfgErr(e instanceof Error ? e.message : "Failed to load config"),
      );
  }, []);

  const activeSub = standalone ? localSub : sub;

  // Report the active screen so chat can answer "what am I looking at?". Only in
  // standalone mode — the Admin Console reports its own tab/sub-tab state.
  useEffect(() => {
    if (!standalone) return;
    const isRuns = activeSub === "runs";
    setScreenView(
      "cora-config",
      isRuns ? "cora-config/runs" : "cora-config/agent",
      `Cora Configuration · ${isRuns ? "Runs" : "Agent"}`,
    );
  }, [standalone, activeSub]);

  return (
    <div className="admin">
      <header className="admin__header">
        <h1>Cora Configuration</h1>
        <p className="admin__subtitle">
          <strong>Agent Runtime</strong> — the model-driven agent loop that can
          use tools, delegate to specialists, and stage review-only drafts.
        </p>
      </header>

      {standalone && (
        <nav
          className="admin-console__subtabs"
          aria-label="Cora Configuration sections"
        >
          {[
            { key: "agent", label: "Agent" },
            { key: "runs", label: "Runs" },
          ].map((s) => (
            <button
              key={s.key}
              className={`admin-console__subtab${
                activeSub === s.key ? " admin-console__subtab--active" : ""
              }`}
              onClick={() => setLocalSub(s.key)}
            >
              {s.label}
            </button>
          ))}
        </nav>
      )}

      {cfgErr && <div className="chat__error">{cfgErr}</div>}

      {activeSub === "runs" ? <AgentRuns /> : <AgentPanel config={config} />}
    </div>
  );
}
