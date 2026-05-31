import { useCallback, useEffect, useMemo, useState } from "react";
import {
  adminActivateAgentVersion,
  adminArchiveAgentVersion,
  adminCreateAgent,
  adminCreateAgentVersion,
  adminGetAgent,
  adminListAgents,
  testAgentResponse,
  testAgentRouting,
} from "../api";
import type {
  Agent,
  AgentDetail,
  AgentType,
  AgentVersion,
  ResponseTestResult,
  RoutingTestResult,
} from "../types";

const AGENT_TYPES: AgentType[] = [
  "orchestrator",
  "subagent",
  "memory",
  "tool_agent",
];

export function AgentAdmin() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [loadingList, setLoadingList] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshList = useCallback(async () => {
    setLoadingList(true);
    setError(null);
    try {
      setAgents(await adminListAgents());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoadingList(false);
    }
  }, []);

  const refreshDetail = useCallback(async (name: string) => {
    setLoadingDetail(true);
    setError(null);
    try {
      setDetail(await adminGetAgent(name));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
      setDetail(null);
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  useEffect(() => {
    refreshList();
  }, [refreshList]);

  useEffect(() => {
    if (selectedName) refreshDetail(selectedName);
    else setDetail(null);
  }, [selectedName, refreshDetail]);

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Agent Admin</h1>
        <p className="admin__subtitle">
          Version-controlled prompts and routing for ATLAS, SCRIBE, FORGE,
          PULSE, SIGNAL, CHRONOS and future subagents. Runtime falls back to
          Python constants if the active version cannot be loaded.
        </p>
      </header>

      {error && <div className="admin__error">{error}</div>}

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Registered agents</h2>
          <button
            className="btn btn--ghost btn--sm"
            onClick={refreshList}
            disabled={loadingList}
          >
            ↻ Refresh
          </button>
        </div>

        <table className="admin__table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Display</th>
              <th>Type</th>
              <th>Status</th>
              <th>Active version</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => {
              const isSelected = a.name === selectedName;
              return (
                <tr
                  key={a.id}
                  className={isSelected ? "admin__row--selected" : ""}
                >
                  <td className="mono">{a.name}</td>
                  <td>{a.display_name}</td>
                  <td>
                    <span className={`scope-chip scope-chip--${a.agent_type}`}>
                      {a.agent_type}
                    </span>
                  </td>
                  <td>
                    <span
                      className={`role-chip ${a.enabled ? "role-chip--admin" : "role-chip--user"}`}
                    >
                      {a.enabled ? "enabled" : "disabled"}
                    </span>
                  </td>
                  <td className="muted">
                    {a.current_version_number
                      ? `v${a.current_version_number}`
                      : "— none —"}
                  </td>
                  <td>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => setSelectedName(a.name)}
                    >
                      {isSelected ? "Selected" : "Inspect"}
                    </button>
                  </td>
                </tr>
              );
            })}
            {!loadingList && agents.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  No agents registered.
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <CreateAgentForm onCreated={refreshList} />
      </section>

      <AgentTestHarness agents={agents} />

      {detail && (
        <AgentDetailSection
          detail={detail}
          loading={loadingDetail}
          onChanged={() => refreshDetail(detail.name).then(() => refreshList())}
        />
      )}
    </main>
  );
}

function CreateAgentForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [agentType, setAgentType] = useState<AgentType>("subagent");
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setMsg(null);
    try {
      await adminCreateAgent({
        name: name.trim(),
        display_name: displayName.trim(),
        description: description.trim() || null,
        agent_type: agentType,
      });
      setMsg(`Created ${name.trim()}`);
      setName("");
      setDisplayName("");
      setDescription("");
      onCreated();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="admin__form" onSubmit={submit}>
      <h3>Create agent</h3>
      <div className="admin__form-row">
        <label>
          <span>Name (slug)</span>
          <input
            type="text"
            pattern="^[A-Za-z][A-Za-z0-9_]*$"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={submitting}
          />
        </label>
        <label>
          <span>Display name</span>
          <input
            type="text"
            required
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            disabled={submitting}
          />
        </label>
        <label>
          <span>Type</span>
          <select
            value={agentType}
            onChange={(e) => setAgentType(e.target.value as AgentType)}
            disabled={submitting}
          >
            {AGENT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="admin__field-wide">
        <span>Description</span>
        <textarea
          rows={2}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          disabled={submitting}
        />
      </label>
      <div className="admin__form-row">
        <button
          type="submit"
          className="btn btn--primary"
          disabled={submitting}
        >
          {submitting ? "…" : "Create agent"}
        </button>
        {msg && <span className="admin__hint">{msg}</span>}
      </div>
    </form>
  );
}

function AgentDetailSection({
  detail,
  loading,
  onChanged,
}: {
  detail: AgentDetail;
  loading: boolean;
  onChanged: () => void;
}) {
  const activeVersion = useMemo(
    () => detail.versions.find((v) => v.status === "active") ?? null,
    [detail],
  );

  const [editVersion, setEditVersion] = useState<AgentVersion | null>(null);
  // When detail changes, default to showing the active version
  useEffect(() => {
    setEditVersion(activeVersion);
  }, [activeVersion, detail.id]);

  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  const activate = useCallback(
    async (version: AgentVersion) => {
      if (acting) return;
      setActing(true);
      setActionMsg(null);
      try {
        await adminActivateAgentVersion(detail.name, version.id);
        setActionMsg(`Activated v${version.version_number}`);
        onChanged();
      } catch (err) {
        setActionMsg(err instanceof Error ? err.message : "Failed");
      } finally {
        setActing(false);
      }
    },
    [detail.name, acting, onChanged],
  );

  const archive = useCallback(
    async (version: AgentVersion) => {
      if (acting) return;
      setActing(true);
      setActionMsg(null);
      try {
        await adminArchiveAgentVersion(detail.name, version.id);
        setActionMsg(`Archived v${version.version_number}`);
        onChanged();
      } catch (err) {
        setActionMsg(err instanceof Error ? err.message : "Failed");
      } finally {
        setActing(false);
      }
    },
    [detail.name, acting, onChanged],
  );

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>
          {detail.display_name}{" "}
          <span className="mono muted">({detail.name})</span>
        </h2>
        {loading && <span className="admin__hint">Loading…</span>}
      </div>

      {actionMsg && <div className="admin__hint">{actionMsg}</div>}

      <h3 className="admin__vt-h3">Version history</h3>
      <table className="admin__table">
        <thead>
          <tr>
            <th>#</th>
            <th>Status</th>
            <th>Notes</th>
            <th>Created</th>
            <th>Activated</th>
            <th>Archived</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {detail.versions.map((v) => {
            const isEditing = editVersion?.id === v.id;
            return (
              <tr
                key={v.id}
                className={isEditing ? "admin__row--selected" : ""}
              >
                <td>v{v.version_number}</td>
                <td>
                  <span className={`status-chip status-chip--${v.status}`}>
                    {v.status}
                  </span>
                </td>
                <td className="muted">{v.notes || "—"}</td>
                <td className="muted">
                  {new Date(v.created_at).toLocaleString()}
                </td>
                <td className="muted">
                  {v.activated_at
                    ? new Date(v.activated_at).toLocaleString()
                    : "—"}
                </td>
                <td className="muted">
                  {v.archived_at
                    ? new Date(v.archived_at).toLocaleString()
                    : "—"}
                </td>
                <td>
                  <div className="admin__row-actions">
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => setEditVersion(v)}
                    >
                      View
                    </button>
                    {v.status !== "active" && v.status !== "archived" && (
                      <button
                        className="btn btn--primary btn--sm"
                        onClick={() => activate(v)}
                        disabled={acting}
                      >
                        Activate
                      </button>
                    )}
                    {v.status === "active" && (
                      <span className="admin__hint">current</span>
                    )}
                    {v.status !== "archived" && (
                      <button
                        className="btn btn--ghost btn--sm"
                        onClick={() => archive(v)}
                        disabled={acting}
                      >
                        Archive
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {editVersion && (
        <VersionViewer
          agentName={detail.name}
          version={editVersion}
          activeVersion={activeVersion}
          onDraftCreated={onChanged}
        />
      )}
    </section>
  );
}

function VersionViewer({
  agentName,
  version,
  activeVersion,
  onDraftCreated,
}: {
  agentName: string;
  version: AgentVersion;
  activeVersion: AgentVersion | null;
  onDraftCreated: () => void;
}) {
  // Routing keywords prefer DB metadata.routing_keywords, else the column.
  const seedKeywords = (v: AgentVersion) =>
    (v.metadata?.routing_keywords && v.metadata.routing_keywords.length > 0
      ? v.metadata.routing_keywords
      : v.routing_keywords
    ).join(", ");

  const [systemPrompt, setSystemPrompt] = useState(version.system_prompt);
  const [routingKeywords, setRoutingKeywords] = useState(seedKeywords(version));
  const [allowedTools, setAllowedTools] = useState(
    version.allowed_tools.join(", "),
  );
  const [modelName, setModelName] = useState(version.model_name ?? "");
  const [temperature, setTemperature] = useState(version.temperature);
  const [maxPromptChars, setMaxPromptChars] = useState(version.max_prompt_chars);
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const loadFrom = (v: AgentVersion) => {
    setSystemPrompt(v.system_prompt);
    setRoutingKeywords(seedKeywords(v));
    setAllowedTools(v.allowed_tools.join(", "));
    setModelName(v.model_name ?? "");
    setTemperature(v.temperature);
    setMaxPromptChars(v.max_prompt_chars);
    setNotes("");
    setMsg(null);
  };

  // Reseed the editor when the inspected version changes.
  useEffect(() => {
    loadFrom(version);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version.id]);

  const save = async (activate: boolean) => {
    if (submitting) return;
    if (!notes.trim()) {
      setMsg("Notes are required (why this version changed)");
      return;
    }
    setSubmitting(true);
    setMsg(null);
    const keywords = routingKeywords
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    try {
      const v = await adminCreateAgentVersion(agentName, {
        system_prompt: systemPrompt,
        routing_keywords: keywords,
        allowed_tools: allowedTools
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        model_name: modelName.trim() || null,
        temperature,
        max_prompt_chars: maxPromptChars,
        notes: notes.trim(),
        metadata: {
          routing_keywords: keywords,
          change_summary: notes.trim(),
        },
        activate,
      });
      setMsg(
        activate
          ? `Created and activated v${v.version_number}`
          : `Created v${v.version_number} (${v.status})`,
      );
      onDraftCreated();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  const fallbackKeywords =
    activeVersion &&
    (!activeVersion.metadata?.routing_keywords ||
      activeVersion.metadata.routing_keywords.length === 0)
      ? activeVersion.routing_keywords
      : [];

  return (
    <form className="admin__form" onSubmit={(e) => e.preventDefault()}>
      <h3>
        Editing from v{version.version_number}{" "}
        <span className={`status-chip status-chip--${version.status}`}>
          {version.status}
        </span>
      </h3>
      <p className="admin__warn">
        ⚠ Saving creates a new version. Active versions are never overwritten.
      </p>

      <label className="admin__field-wide">
        <span>System prompt</span>
        <textarea
          rows={12}
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          disabled={submitting}
        />
      </label>

      <h4 className="admin__vt-h3">Routing</h4>
      <label className="admin__field-wide">
        <span>Routing keywords (comma-separated → metadata.routing_keywords)</span>
        <input
          type="text"
          value={routingKeywords}
          onChange={(e) => setRoutingKeywords(e.target.value)}
          disabled={submitting}
        />
      </label>
      {fallbackKeywords.length > 0 && (
        <p className="admin__hint">
          Active version has no metadata keywords; runtime currently falls back
          to: <span className="mono">{fallbackKeywords.join(", ")}</span>
        </p>
      )}
      <label className="admin__field-wide">
        <span>Allowed tools (comma-separated)</span>
        <input
          type="text"
          value={allowedTools}
          onChange={(e) => setAllowedTools(e.target.value)}
          disabled={submitting}
        />
      </label>
      <div className="admin__form-row">
        <label>
          <span>Model</span>
          <input
            type="text"
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            placeholder="(inherits DGX_MODEL_NAME)"
            disabled={submitting}
          />
        </label>
        <label>
          <span>Temperature</span>
          <input
            type="number"
            step="0.1"
            min={0}
            max={2}
            value={temperature}
            onChange={(e) => setTemperature(Number(e.target.value))}
            disabled={submitting}
          />
        </label>
        <label>
          <span>Max prompt chars</span>
          <input
            type="number"
            min={500}
            max={200000}
            value={maxPromptChars}
            onChange={(e) => setMaxPromptChars(Number(e.target.value))}
            disabled={submitting}
          />
        </label>
      </div>
      <label className="admin__field-wide">
        <span>Version notes / change summary — required</span>
        <input
          type="text"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          disabled={submitting}
        />
      </label>
      <div className="admin__form-row">
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => save(false)}
          disabled={submitting}
        >
          {submitting ? "…" : "Save as New Version"}
        </button>
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => save(true)}
          disabled={submitting}
        >
          {submitting ? "…" : "Save and Activate"}
        </button>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => activeVersion && loadFrom(activeVersion)}
          disabled={submitting || !activeVersion}
          title="Discard unsaved edits and reload the active version"
        >
          Reset to Active
        </button>
        {msg && <span className="admin__hint">{msg}</span>}
      </div>
    </form>
  );
}

function AgentTestHarness({ agents }: { agents: Agent[] }) {
  const [message, setMessage] = useState("");
  const [agentName, setAgentName] = useState(""); // "" = auto route
  const [workspaceId, setWorkspaceId] = useState("");
  const [includeMemory, setIncludeMemory] = useState(false);
  const [busy, setBusy] = useState<null | "routing" | "response">(null);
  const [error, setError] = useState<string | null>(null);
  const [routing, setRouting] = useState<RoutingTestResult | null>(null);
  const [resp, setResp] = useState<ResponseTestResult | null>(null);

  const wid = workspaceId.trim() || undefined;

  const runRouting = async () => {
    if (!message.trim() || busy) return;
    setBusy("routing");
    setError(null);
    try {
      setRouting(
        await testAgentRouting({
          message: message.trim(),
          workspace_id: wid,
          include_prompt_preview: true,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(null);
    }
  };

  const runResponse = async () => {
    if (!message.trim() || busy) return;
    setBusy("response");
    setError(null);
    try {
      setResp(
        await testAgentResponse({
          message: message.trim(),
          workspace_id: wid,
          agent_name: agentName || undefined,
          include_memory: includeMemory,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="admin__section agent-test-harness">
      <div className="admin__section-head">
        <h2>Agent Test Harness</h2>
      </div>
      <p className="admin__hint">
        Test routing, prompt source, and one-off responses without touching live
        chat. Nothing here is saved as a conversation.
      </p>

      <div className="agent-test-form">
        <label className="agent-test-message">
          <span>Test message</span>
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="e.g. Draft a stakeholder update about the news ingestion feature."
            disabled={busy !== null}
          />
        </label>

        <div className="agent-test-controls">
          <label className="agent-test-field">
            <span>Agent (response)</span>
            <select
              value={agentName}
              onChange={(e) => setAgentName(e.target.value)}
              disabled={busy !== null}
            >
              <option value="">Auto route</option>
              {agents.map((a) => (
                <option key={a.id} value={a.name}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>
          <label className="agent-test-field">
            <span>Workspace ID (optional)</span>
            <input
              type="text"
              value={workspaceId}
              onChange={(e) => setWorkspaceId(e.target.value)}
              placeholder="(none)"
              disabled={busy !== null}
            />
          </label>
          <label className="agent-test-field agent-test-checkbox">
            <span>Include memory</span>
            <span className="agent-test-checkbox__row">
              <input
                type="checkbox"
                checked={includeMemory}
                onChange={(e) => setIncludeMemory(e.target.checked)}
                disabled={busy !== null}
              />
              <span>Inject workspace memory into the response</span>
            </span>
          </label>
        </div>

        <div className="agent-test-actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={runRouting}
            disabled={busy !== null || !message.trim()}
          >
            {busy === "routing" ? "Testing…" : "Test Routing"}
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={runResponse}
            disabled={busy !== null || !message.trim()}
          >
            {busy === "response" ? "Running…" : "Run Test Response"}
          </button>
        </div>
      </div>

      {error && <div className="admin__error">{error}</div>}

      {(routing || resp) && (
        <div className="agent-test-results">
          {routing && (
            <div className="agent-test-result-card">
              <h3>Routing result</h3>
              <div className="admin__hint">
                Selected:{" "}
                <span
                  className={`agent-badge agent-badge--${routing.selected_agent.toLowerCase()}`}
                >
                  {routing.selected_agent}
                </span>
                {" · "}prompt source:{" "}
                <span className="mono">{routing.prompt_source}</span>
                {routing.active_version != null && (
                  <> {" · "}active v{routing.active_version}</>
                )}
                {routing.tie_break_applied && (
                  <>
                    {" · "}
                    <span className="status-chip status-chip--draft">
                      tie-break applied
                    </span>
                  </>
                )}
              </div>
              {routing.would_delegate && (
                <div className="admin__hint">
                  Would delegate: {routing.delegation_from} →{" "}
                  {routing.delegation_to}
                </div>
              )}
              <table className="admin__table">
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>Score</th>
                    <th>Matched keywords</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.keys(routing.scores).map((name) => (
                    <tr key={name}>
                      <td className="mono">{name}</td>
                      <td>{routing.scores[name]}</td>
                      <td className="muted">
                        {(routing.matched_keywords[name] || []).join(", ") ||
                          "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {routing.prompt_preview && (
                <details>
                  <summary className="admin__hint">
                    Prompt preview (first 500 chars)
                  </summary>
                  <pre className="trace-json">{routing.prompt_preview}</pre>
                </details>
              )}
            </div>
          )}

          {resp && (
            <div className="agent-test-result-card">
              <h3>Test response</h3>
              <p className="admin__warn">
                ⚠ Test response only. Not saved as a chat.
              </p>
              <div className="admin__hint">
                Agent:{" "}
                <span
                  className={`agent-badge agent-badge--${resp.selected_agent.toLowerCase()}`}
                >
                  {resp.selected_agent}
                </span>
                {" · "}prompt source:{" "}
                <span className="mono">{resp.prompt_source}</span>
                {resp.active_version != null && (
                  <> {" · "}active v{resp.active_version}</>
                )}
              </div>
              <pre className="trace-json" style={{ whiteSpace: "pre-wrap" }}>
                {resp.response}
              </pre>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
