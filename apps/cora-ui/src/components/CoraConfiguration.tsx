import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { getAgentConfig, sendAgentChat } from "../api";
import type { AgentRuntimeConfig, AgentRunResponse, AgentRunStep } from "../types";

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

export function CoraConfiguration() {
  const [config, setConfig] = useState<AgentRuntimeConfig | null>(null);
  const [cfgErr, setCfgErr] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<AgentRunResponse | null>(null);
  const [runErr, setRunErr] = useState<string | null>(null);

  useEffect(() => {
    getAgentConfig()
      .then(setConfig)
      .catch((e) =>
        setCfgErr(e instanceof Error ? e.message : "Failed to load config"),
      );
  }, []);

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
    <div className="admin">
      <header className="admin__header">
        <h1>Cora Configuration</h1>
        <p className="admin__subtitle">
          <strong>Agent Runtime</strong> — the model-driven agent loop that can
          use tools, delegate to specialists, and stage review-only drafts.
        </p>
      </header>

      <section className="agent-cfg__status">
        {cfgErr && <div className="chat__error">{cfgErr}</div>}
        {config && (
          <>
            <div className="agent-cfg__pills">
              <StatusPill label="Runtime" on={config.runtime_enabled} />
              <StatusPill label="Delegation" on={config.delegation_enabled} />
              <StatusPill label="Write / staging" on={config.write_enabled} />
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
          </div>
        )}
      </section>
    </div>
  );
}
