import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  getAgentConfig,
  getAgentRun,
  listAgentRuns,
  sendAgentChat,
} from "../api";
import type {
  AgentDelegationNode,
  AgentEvaluation,
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
  const [runErr, setRunErr] = useState<string | null>(null);

  const run = async () => {
    const msg = input.trim();
    if (!msg || sending) return;
    setSending(true);
    setRunErr(null);
    setResult(null);
    try {
      setResult(await sendAgentChat(msg));
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
          <button
            className="btn btn--primary"
            onClick={run}
            disabled={sending || !input.trim()}
          >
            {sending ? "Running…" : "Run"}
          </button>
        </div>
        {runErr && <div className="chat__error">{runErr}</div>}

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

function RunDetail({ runId }: { runId: string }) {
  const [detail, setDetail] = useState<AgentRunDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setErr(null);
    getAgentRun(runId)
      .then(setDetail)
      .catch((e) => setErr(e instanceof Error ? e.message : "Failed to load run"));
  }, [runId]);

  if (err) return <div className="chat__error">{err}</div>;
  if (!detail) return <div className="agent-cfg__note">Loading run…</div>;

  return (
    <div className="agent-cfg__result">
      <div className="agent-cfg__result-meta">
        <span className="agent-run__badge">{detail.agent_name || "ATLAS"}</span>
        <span className={`agent-pill agent-pill--${runOk(detail.status) ? "on" : "off"}`}>
          {detail.status}
          {detail.stopped ? ` · ${detail.stopped}` : ""}
        </span>
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

export function CoraConfiguration({ sub }: { sub?: string }) {
  const [config, setConfig] = useState<AgentRuntimeConfig | null>(null);
  const [cfgErr, setCfgErr] = useState<string | null>(null);

  useEffect(() => {
    getAgentConfig()
      .then(setConfig)
      .catch((e) =>
        setCfgErr(e instanceof Error ? e.message : "Failed to load config"),
      );
  }, []);

  return (
    <div className="admin">
      <header className="admin__header">
        <h1>Cora Configuration</h1>
        <p className="admin__subtitle">
          <strong>Agent Runtime</strong> — the model-driven agent loop that can
          use tools, delegate to specialists, and stage review-only drafts.
        </p>
      </header>

      {cfgErr && <div className="chat__error">{cfgErr}</div>}

      {sub === "runs" ? <AgentRuns /> : <AgentPanel config={config} />}
    </div>
  );
}
