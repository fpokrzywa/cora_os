import { useCallback, useEffect, useState } from "react";
import {
  listExecutionApprovals,
  runFinalSafetyCheck,
  listExecutionAdapters,
  simulateAdapterPayload,
  runBlockedExecutionCheck,
} from "../api";
import type {
  ExecutionApprovalListItem,
  FinalInterlockResult,
  FinalInterlockStatus,
  ExecutionAdapterInfo,
  AdapterSimulationResult,
  AdapterBlockedResult,
} from "../types";

const STATUS_LABEL: Record<FinalInterlockStatus, string> = {
  blocked_by_final_interlock: "Blocked by Final Interlock",
  ready_but_execution_disabled: "Ready — Execution Disabled",
  missing_approval: "Missing Approval",
  provider_not_ready: "Provider Not Ready",
  payload_mismatch: "Payload Mismatch",
};

function check(ok: boolean) {
  return <span>{ok ? "✓ pass" : "✗ fail"}</span>;
}

export function ExecutionRunbook() {
  const [items, setItems] = useState<ExecutionApprovalListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [result, setResult] = useState<FinalInterlockResult | null>(null);
  const [adapters, setAdapters] = useState<ExecutionAdapterInfo[]>([]);
  const [adapterSim, setAdapterSim] = useState<{
    sim?: AdapterSimulationResult;
    blocked?: AdapterBlockedResult;
    mode: "validate" | "simulate" | "blocked";
  } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [rows, ad] = await Promise.all([
        listExecutionApprovals(),
        listExecutionAdapters().catch(() => null),
      ]);
      setItems(rows);
      if (ad) setAdapters(ad.adapters);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load intents");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const runCheck = async (intentId: string) => {
    setBusyId(intentId);
    setError(null);
    try {
      setResult(await runFinalSafetyCheck(intentId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run safety check");
    } finally {
      setBusyId(null);
    }
  };

  const runAdapter = async (
    intentId: string,
    mode: "validate" | "simulate" | "blocked",
  ) => {
    setBusyId(intentId);
    setError(null);
    try {
      if (mode === "blocked") {
        setAdapterSim({ blocked: await runBlockedExecutionCheck(intentId), mode });
      } else {
        setAdapterSim({ sim: await simulateAdapterPayload(intentId), mode });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Adapter check failed");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="admin__section">
      <div className="admin__section-head">
        <h2>Execution Runbook</h2>
        <button className="btn btn--ghost btn--sm" onClick={load} disabled={loading}>
          ↻ Refresh
        </button>
      </div>

      <div
        className="admin__error"
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        🔒 <strong>External execution is disabled by the global safety guard.</strong>{" "}
        The final interlock is diagnostic only — it calls no provider API, never
        clears dry-run, and never enables execution.
      </div>

      {error && <div className="admin__error">{error}</div>}

      {items.length === 0 ? (
        <div className="admin__hint">No integration intents to check.</div>
      ) : (
        <table className="admin__table">
          <thead>
            <tr>
              <th>Source</th>
              <th>Provider</th>
              <th>Action</th>
              <th>Approval state</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={it.intent_id}>
                <td className="muted">{it.source_type}</td>
                <td>
                  {it.provider_type}
                  {it.provider_name ? ` · ${it.provider_name}` : ""}
                </td>
                <td className="muted">{it.action_type}</td>
                <td className="muted">{it.approval_state}</td>
                <td>
                  <div className="admin__row-actions" style={{ flexWrap: "wrap", gap: 4 }}>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => runCheck(it.intent_id)}
                      disabled={busyId === it.intent_id}
                      title="Run the full final safety checklist (executes nothing)"
                    >
                      Run Final Safety Check
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => runAdapter(it.intent_id, "validate")}
                      disabled={busyId === it.intent_id}
                      title="Validate the provider adapter payload (no provider call)"
                    >
                      Validate Adapter
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => runAdapter(it.intent_id, "simulate")}
                      disabled={busyId === it.intent_id}
                      title="Simulate the provider-shaped payload (no provider call)"
                    >
                      Simulate Adapter Payload
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => runAdapter(it.intent_id, "blocked")}
                      disabled={busyId === it.intent_id}
                      title="Run the adapter execute path — always blocked by governance"
                    >
                      Run Blocked Execution Check
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3 className="admin__vt-h3" style={{ marginTop: "16px" }}>
        Adapter Readiness
      </h3>
      <p className="admin__hint" style={{ marginTop: 0 }}>
        Registered execution adapters. <strong>real_execution is False</strong> for
        all — these define how a future executor would format the request; none can
        call a provider in this phase.
      </p>
      <table className="admin__table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Type</th>
            <th>Supported actions</th>
            <th>Real execution</th>
          </tr>
        </thead>
        <tbody>
          {adapters.map((a) => (
            <tr key={a.provider_name}>
              <td>{a.provider_name}</td>
              <td className="muted">{a.provider_type}</td>
              <td className="muted">{a.supported_action_types.join(", ")}</td>
              <td>{a.real_execution ? "enabled" : "disabled"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {adapterSim && (
        <div className="modal-backdrop" onClick={() => setAdapterSim(null)}>
          <div
            className="modal"
            style={{ maxWidth: "680px" }}
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 className="modal__title">Adapter Readiness</h3>
            </div>
            {adapterSim.blocked ? (
              <>
                <div className="admin__error" style={{ display: "flex", gap: 8 }}>
                  🔒 <strong>
                    Execution blocked: {adapterSim.blocked.status} ·{" "}
                    {adapterSim.blocked.reason}
                  </strong>
                </div>
                <table className="admin__table">
                  <tbody>
                    <tr><td>Provider</td><td className="muted">{adapterSim.blocked.provider_name ?? "—"}</td></tr>
                    <tr><td>Action</td><td className="muted">{adapterSim.blocked.action_type}</td></tr>
                    <tr><td>Real execution performed</td><td>{adapterSim.blocked.real_execution_performed ? "yes" : "no"}</td></tr>
                    <tr><td>Interlock status</td><td className="muted">{adapterSim.blocked.interlock_status ?? "—"}</td></tr>
                  </tbody>
                </table>
                <p className="admin__hint">{adapterSim.blocked.note}</p>
              </>
            ) : adapterSim.sim ? (
              <>
                <table className="admin__table">
                  <tbody>
                    <tr><td>Provider</td><td className="muted">{adapterSim.sim.provider_name ?? "—"}</td></tr>
                    <tr><td>Action</td><td className="muted">{adapterSim.sim.action_type}</td></tr>
                    <tr><td>Adapter resolved</td><td>{adapterSim.sim.resolved ? "✓ yes" : "✗ no (fail-closed)"}</td></tr>
                    <tr><td>Supported action</td><td>{adapterSim.sim.supported_action ? "✓ yes" : "✗ no"}</td></tr>
                    <tr>
                      <td>Payload validation</td>
                      <td>
                        {adapterSim.sim.payload_ready
                          ? "✓ valid"
                          : `✗ ${(adapterSim.sim.validation_errors ?? []).join("; ") || "invalid"}`}
                      </td>
                    </tr>
                    <tr><td>External action performed</td><td>{adapterSim.sim.external_action_performed ? "yes" : "no"}</td></tr>
                  </tbody>
                </table>
                {adapterSim.mode === "simulate" && adapterSim.sim.simulation && (
                  <>
                    <h4 className="admin__vt-h3">
                      Simulation result · {adapterSim.sim.simulation.provider_request.api_method}{" "}
                      (would_send={String(adapterSim.sim.simulation.provider_request.would_send)})
                    </h4>
                    <pre
                      style={{
                        background: "var(--surface-2, #16161e)",
                        padding: "10px",
                        borderRadius: 6,
                        overflowX: "auto",
                        fontSize: "12px",
                      }}
                    >
                      {JSON.stringify(adapterSim.sim.simulation.provider_request.request, null, 2)}
                    </pre>
                    <p className="admin__hint">{adapterSim.sim.simulation.note}</p>
                  </>
                )}
                {adapterSim.sim.status === "no_adapter" && (
                  <div className="admin__error">{adapterSim.sim.reason}</div>
                )}
              </>
            ) : null}
            <div className="modal__footer">
              <button className="btn btn--ghost btn--sm" onClick={() => setAdapterSim(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {result && (
        <div className="modal-backdrop" onClick={() => setResult(null)}>
          <div
            className="modal"
            style={{ maxWidth: "720px" }}
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal__header">
              <h3 className="modal__title">
                Final Safety Check ·{" "}
                {STATUS_LABEL[result.status] ?? result.status}
              </h3>
            </div>

            <div
              className="admin__error"
              style={{ display: "flex", alignItems: "center", gap: 8 }}
            >
              🔒 <strong>
                Real execution allowed: {result.real_execution_allowed ? "yes" : "NO"}
              </strong>{" "}
              · execution_enabled={String(result.execution_enabled)} · dry_run_only=
              {String(result.dry_run_only)}
            </div>

            {result.block_reasons.length > 0 && (
              <>
                <h4 className="admin__vt-h3">Block reason</h4>
                <ul className="muted" style={{ marginTop: 0 }}>
                  {result.block_reasons.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              </>
            )}

            <h4 className="admin__vt-h3">Final safety checklist</h4>
            <table className="admin__table">
              <tbody>
                {Object.entries(result.checks).map(([k, v]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td>{check(v)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <h4 className="admin__vt-h3">Approval evidence</h4>
            <table className="admin__table">
              <tbody>
                <tr><td>Approved</td><td>{check(result.approval_evidence.approved)}</td></tr>
                <tr><td>Approver</td><td className="muted">{result.approval_evidence.approver_id ?? "—"}</td></tr>
                <tr><td>Approved at</td><td className="muted">{result.approval_evidence.approved_at ?? "—"}</td></tr>
                <tr><td>Latest decision</td><td className="muted">{result.approval_evidence.latest_decision ?? "—"}</td></tr>
              </tbody>
            </table>

            <h4 className="admin__vt-h3">Provider readiness evidence</h4>
            <table className="admin__table">
              <tbody>
                <tr><td>Provider connected</td><td>{check(result.provider_readiness.provider_connected)}</td></tr>
                <tr><td>Token valid or refreshable</td><td>{check(result.provider_readiness.token_valid_or_refreshable)}</td></tr>
                <tr><td>Required scopes present</td><td>{check(result.provider_readiness.required_scopes_present)}</td></tr>
                <tr><td>Provider supports action</td><td>{check(result.provider_readiness.provider_supports_action)}</td></tr>
                {result.provider_readiness.missing_scopes.length > 0 && (
                  <tr><td>Missing scopes</td><td className="muted">{result.provider_readiness.missing_scopes.join(", ")}</td></tr>
                )}
              </tbody>
            </table>

            <h4 className="admin__vt-h3">Payload hash</h4>
            <table className="admin__table">
              <tbody>
                <tr><td>Current</td><td className="muted"><code>{result.payload_hash.slice(0, 24)}…</code></td></tr>
                <tr><td>Approved</td><td className="muted"><code>{result.approved_payload_hash ? `${result.approved_payload_hash.slice(0, 24)}…` : "—"}</code></td></tr>
                <tr><td>Matches</td><td>{check(result.payload_matches)}</td></tr>
              </tbody>
            </table>

            <p className="admin__hint">{result.note}</p>
            <div className="modal__footer">
              <button className="btn btn--ghost btn--sm" onClick={() => setResult(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
