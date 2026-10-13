import type { ControlData } from "./types";
import { Icon, Panel, SectionHeading, StatusBadge } from "./ui";

export function SettingsPage({ theme, onToggleTheme, data }: { theme: "dark" | "light"; onToggleTheme: () => void; data: ControlData }) {
  return <div className="settings-page">
    <div className="reference-heading"><h1 className="page-title">Settings</h1><p>Manage your workspace appearance and preferences.</p></div>
    <div className="settings-grid">
      <Panel className="reference-panel"><SectionHeading title="Workspace" /><div className="settings-workspace"><div className="reference-avatar"><Icon name="companies" /></div><div><strong>{data.company?.name ?? "Forge workspace"}</strong><p>{data.project?.name ?? "Your AI company control center"}</p></div></div><div className="settings-field"><span>Current mission</span><strong>{data.mission?.title ?? "No mission selected"}</strong></div><div className="settings-field"><span>Version</span><strong>{data.status?.system.version ?? "—"}</strong></div></Panel>
      <Panel className="reference-panel"><SectionHeading title="Appearance" /><p className="settings-description">Choose the theme for your workspace. Your preference is saved on this device.</p><button className="settings-theme" role="switch" aria-checked={theme === "dark"} onClick={onToggleTheme}><span><strong>Dark mode</strong><small>{theme === "dark" ? "Dark theme is active" : "Light theme is active"}</small></span><span className="reference-toggle" data-on={theme === "dark"}><span /></span></button></Panel>
      <Panel className="reference-panel"><SectionHeading title="Runtime" /><div className="settings-field"><span>Orchestrator</span><StatusBadge status={data.status?.orchestrator.orchestrator_online ? "ONLINE" : "OFFLINE"} /></div><div className="settings-field"><span>Autonomy</span><StatusBadge status={data.status?.orchestrator.autonomy_enabled ? "ACTIVE" : "PAUSED"} /></div><p className="settings-description">Use the autonomy control in the toolbar to pause or resume execution.</p></Panel>
    </div>
  </div>;
}
