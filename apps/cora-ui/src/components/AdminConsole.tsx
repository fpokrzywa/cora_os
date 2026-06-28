import { useEffect, useState } from "react";

import { setScreenView } from "../screenContext";
import { Admin } from "./Admin";
import { Memories } from "./Memories";
import { AgentAdmin } from "./AgentAdmin";
import { McpAdmin } from "./McpAdmin";
import { ToolAdmin } from "./ToolAdmin";
import { Governance } from "./Governance";
import { Traces } from "./Traces";
import { Plans } from "./Plans";
import { Jobs } from "./Jobs";
import { Workspaces } from "./Workspaces";
import { Delegations } from "./Delegations";
import { WorkspaceContext } from "./WorkspaceContext";
import { Knowledge } from "./Knowledge";
import { SignalDrafts } from "./SignalDrafts";
import { ChronosProposals } from "./ChronosProposals";
import { IntegrationReadiness } from "./IntegrationReadiness";
import { IntegrationReadinessQueue } from "./IntegrationReadinessQueue";
import { ExecutionApprovalConsole } from "./ExecutionApprovalConsole";
import { ExecutionRunbook } from "./ExecutionRunbook";
import { ProviderFeatureFlags } from "./ProviderFeatureFlags";
import { ExecutionGovernanceDashboard } from "./ExecutionGovernanceDashboard";
import { ProviderConnectors } from "./ProviderConnectors";
import { IntegrationProviders } from "./IntegrationProviders";
import { CredentialVault } from "./CredentialVault";
import { CoraConfiguration } from "./CoraConfiguration";

// Primary tabs collapse the old per-module sidebar items into one console.
export type AdminTab =
  | "overview"
  | "users"
  | "memories"
  | "agents"
  | "tools"
  | "knowledge"
  | "execution"
  | "workspaces"
  | "cora-config";

interface Props {
  onImpersonate: (userId: string) => void;
  workspaceId: string | null;
  isAdmin: boolean;
  onWorkspacesChanged: () => void;
  initialTab?: AdminTab;
  currentUserId?: string;
}

interface TabDef {
  key: AdminTab;
  label: string;
  icon: string;
  blurb: string;
  subs: { key: string; label: string }[];
}

// Single source of truth for the tab bar, the Overview cards, and the sub-tab
// rows. Tabs with more than one sub render a secondary pill row; single-sub
// tabs render their component directly.
const TABS: TabDef[] = [
  {
    key: "overview",
    label: "Overview",
    icon: "◆",
    blurb: "Jump into any admin area.",
    subs: [],
  },
  {
    key: "users",
    label: "Users",
    icon: "⚙",
    blurb: "Manage users, roles, and impersonation.",
    subs: [{ key: "users", label: "Users" }],
  },
  {
    key: "memories",
    label: "Memories",
    icon: "❖",
    blurb: "Inspect memory by scope, create entries, and preview visibility.",
    subs: [{ key: "memories", label: "Memories" }],
  },
  {
    key: "agents",
    label: "Agents",
    icon: "⌥",
    blurb: "Agent registry, prompts, versions, and SIGNAL/CHRONOS tools.",
    subs: [
      { key: "agents", label: "Agents" },
      { key: "signal-drafts", label: "SIGNAL Drafts" },
      { key: "chronos-proposals", label: "CHRONOS Proposals" },
    ],
  },
  {
    key: "tools",
    label: "Tools",
    icon: "⚒",
    blurb: "Tool registry, governance policies, MCP servers, and integration readiness.",
    subs: [
      { key: "tooling", label: "Tools" },
      { key: "governance", label: "Governance" },
      { key: "mcp", label: "MCP" },
      { key: "integrations", label: "Integration Readiness" },
      { key: "integration-queue", label: "Integration Queue" },
      { key: "approval-console", label: "Approval Console" },
      { key: "execution-runbook", label: "Execution Runbook" },
      { key: "feature-flags", label: "Provider Feature Flags" },
      { key: "execution-governance", label: "Execution Governance" },
      { key: "providers", label: "Providers" },
      { key: "provider-connectors", label: "Provider Connectors" },
      { key: "credentials", label: "Credential Vault" },
    ],
  },
  {
    key: "knowledge",
    label: "Knowledge",
    icon: "✎",
    blurb:
      "Knowledge ingestion (manual, bulk, upload, URL, PDF, and news feeds) " +
      "plus workspace context.",
    subs: [
      { key: "knowledge", label: "Knowledge" },
      { key: "context", label: "Context" },
    ],
  },
  {
    key: "execution",
    label: "Execution",
    icon: "▤",
    blurb: "Plans, jobs, delegations, and runtime traces.",
    subs: [
      { key: "plans", label: "Plans" },
      { key: "jobs", label: "Jobs" },
      { key: "delegations", label: "Delegations" },
      { key: "traces", label: "Traces" },
    ],
  },
  {
    key: "workspaces",
    label: "Workspaces",
    icon: "◫",
    blurb: "Create and manage workspaces.",
    subs: [{ key: "workspaces", label: "Workspaces" }],
  },
  {
    key: "cora-config",
    label: "Cora Configuration",
    icon: "✦",
    blurb: "Agent runtime status, a panel to use the agent, and a runs viewer.",
    subs: [
      { key: "agent", label: "Agent" },
      { key: "runs", label: "Runs" },
    ],
  },
];

function firstSub(tab: AdminTab): string {
  const def = TABS.find((t) => t.key === tab);
  return def && def.subs.length > 0 ? def.subs[0].key : "";
}

export function AdminConsole({
  onImpersonate,
  workspaceId,
  isAdmin,
  onWorkspacesChanged,
  initialTab = "overview",
  currentUserId,
}: Props) {
  const [tab, setTab] = useState<AdminTab>(initialTab);
  const [sub, setSub] = useState<string>(() => firstSub(initialTab));

  const selectTab = (next: AdminTab, nextSub?: string) => {
    setTab(next);
    setSub(nextSub ?? firstSub(next));
  };

  const activeDef = TABS.find((t) => t.key === tab)!;
  const showSubtabs = activeDef.subs.length > 1;

  // Report the active screen so chat can answer "what am I looking at?".
  useEffect(() => {
    const subDef = activeDef.subs.find((s) => s.key === sub);
    const section = sub ? `${tab}/${sub}` : tab;
    const label = subDef
      ? `${activeDef.label} · ${subDef.label}`
      : activeDef.label;
    setScreenView("admin-console", section, label);
  }, [tab, sub, activeDef]);

  const renderSection = () => {
    switch (tab) {
      case "users":
        return <Admin onImpersonate={onImpersonate} />;
      case "memories":
        return <Memories />;
      case "agents":
        if (sub === "signal-drafts")
          return <SignalDrafts workspaceId={workspaceId} isAdmin={isAdmin} />;
        if (sub === "chronos-proposals")
          return <ChronosProposals workspaceId={workspaceId} isAdmin={isAdmin} />;
        return <AgentAdmin />;
      case "tools":
        if (sub === "governance") return <Governance />;
        if (sub === "mcp") return <McpAdmin />;
        if (sub === "integrations")
          return (
            <IntegrationReadiness workspaceId={workspaceId} isAdmin={isAdmin} />
          );
        if (sub === "integration-queue")
          return (
            <IntegrationReadinessQueue
              workspaceId={workspaceId}
              isAdmin={isAdmin}
              onNavigate={(t, s) => selectTab(t as AdminTab, s)}
            />
          );
        if (sub === "approval-console")
          return (
            <ExecutionApprovalConsole
              isAdmin={isAdmin}
              onNavigate={(t, s) => selectTab(t as AdminTab, s)}
            />
          );
        if (sub === "execution-runbook") return <ExecutionRunbook />;
        if (sub === "feature-flags")
          return <ProviderFeatureFlags isAdmin={isAdmin} />;
        if (sub === "execution-governance")
          return <ExecutionGovernanceDashboard />;
        if (sub === "provider-connectors")
          return (
            <ProviderConnectors workspaceId={workspaceId} isAdmin={isAdmin} />
          );
        if (sub === "providers")
          return <IntegrationProviders isAdmin={isAdmin} />;
        if (sub === "credentials")
          return (
            <CredentialVault
              workspaceId={workspaceId}
              isAdmin={isAdmin}
              currentUserId={currentUserId}
            />
          );
        return <ToolAdmin />;
      case "knowledge":
        if (sub === "context")
          return <WorkspaceContext workspaceId={workspaceId} />;
        // News ingestion is unified into the Knowledge tab's news-feed form
        // (POST /knowledge/news → knowledge_sources + memory_entries). The old
        // registration-based News page (/news/*) was removed from navigation.
        return <Knowledge workspaceId={workspaceId} isAdmin={isAdmin} />;
      case "execution":
        if (sub === "jobs") return <Jobs />;
        if (sub === "delegations") return <Delegations />;
        if (sub === "traces") return <Traces />;
        return <Plans />;
      case "workspaces":
        return <Workspaces onWorkspacesChanged={onWorkspacesChanged} />;
      case "cora-config":
        return <CoraConfiguration sub={sub} />;
      case "overview":
      default:
        return (
          <div className="admin">
            <header className="admin__header">
              <h1>Admin Console</h1>
              <p className="admin__subtitle">
                All Cora administration in one place. Pick an area below.
              </p>
            </header>
            <div className="admin-console__overview">
              {TABS.filter((t) => t.key !== "overview").map((t) => (
                <button
                  key={t.key}
                  className="admin-console__card"
                  onClick={() => selectTab(t.key)}
                >
                  <span className="admin-console__card-icon" aria-hidden>
                    {t.icon}
                  </span>
                  <span className="admin-console__card-title">{t.label}</span>
                  <span className="admin-console__card-blurb">{t.blurb}</span>
                  {t.subs.length > 1 && (
                    <span className="admin-console__card-subs">
                      {t.subs.map((s) => s.label).join(" · ")}
                    </span>
                  )}
                </button>
              ))}
            </div>
          </div>
        );
    }
  };

  return (
    <main className="admin-console">
      <div className="admin-console__bar">
        <nav className="admin-console__tabs" role="tablist" aria-label="Admin sections">
          {TABS.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              className={`admin-console__tab${
                tab === t.key ? " admin-console__tab--active" : ""
              }`}
              onClick={() => selectTab(t.key)}
            >
              <span className="admin-console__tab-icon" aria-hidden>
                {t.icon}
              </span>
              {t.label}
            </button>
          ))}
        </nav>
        {showSubtabs && (
          <nav className="admin-console__subtabs" aria-label={`${activeDef.label} sections`}>
            {activeDef.subs.map((s) => (
              <button
                key={s.key}
                className={`admin-console__subtab${
                  sub === s.key ? " admin-console__subtab--active" : ""
                }`}
                onClick={() => setSub(s.key)}
              >
                {s.label}
              </button>
            ))}
          </nav>
        )}
      </div>
      <div className="admin-console__body">{renderSection()}</div>
    </main>
  );
}
