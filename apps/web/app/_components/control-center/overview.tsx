import { eventMessage, weightedProgress } from "./helpers";
import type { ControlData, MissionTab, View } from "./types";
import { EmptyState, Icon, Panel, SectionHeading, StatusBadge, formatTime } from "./ui";

interface Props {
  data: ControlData;
  loading: boolean;
  onNavigate: (view: View, tab?: MissionTab) => void;
  onOpenMission: (id: string) => void;
}

export function Overview({ data, loading, onNavigate, onOpenMission }: Props) {
  const { missions, status, agents } = data;
  const active = missions.filter((mission) => !["COMPLETED", "CANCELLED", "FAILED"].includes(mission.status));
  const finished = missions.filter((mission) => ["COMPLETED", "FAILED"].includes(mission.status));
  const success = finished.length ? `${Math.round(finished.filter((mission) => mission.status === "COMPLETED").length / finished.length * 100)}%` : "—";
  const workforce = data.allAgents ?? agents;
  const stats = [
    ["Active missions", active.length],
    ["Mission success rate", success],
    ["AI spend", status ? `$${Number(status.agentRunStats.estimated_cost ?? 0).toFixed(2)}` : "—"],
    ["Agents online", workforce.filter((agent) => !["OFFLINE", "DISABLED", "FAILED"].includes(agent.status)).length],
  ];

  return <div className="reference-overview">
    <div className="reference-heading"><h1 className="page-title">Overview</h1><p>Welcome back. Here’s what’s happening in your company.</p></div>
    <div className="overview-stats">{stats.map(([label, value]) => <div className="overview-stat" key={label}><span>{label}</span><strong>{loading ? "—" : value}</strong></div>)}</div>
    <section className="overview-missions">
      <SectionHeading title="Active missions" action={<button className="text-link" onClick={() => onNavigate("missions")}>All missions <Icon name="arrow" /></button>} />
      {loading ? <div className="skeleton h-40" /> : missions.length ? <div className="overview-mission-list">{missions.slice(0, 3).map((mission) => {
        const progress = weightedProgress(mission, mission.id === data.mission?.id ? data.graph : null);
        return <article className="overview-mission" key={mission.id}>
          <div className="reference-avatar"><Icon name="missions" /></div>
          <div className="overview-mission-copy"><div><h2>{mission.title}</h2><StatusBadge status={mission.status} /></div><p>{mission.goal}</p></div>
          <div className="overview-progress"><div><span>{progress}%</span><span>{mission.progress.done}/{mission.progress.total} tasks</span></div><div className="reference-progress"><span style={{ width: `${progress}%` }} /></div></div>
          <button className="reference-open" onClick={() => onOpenMission(mission.id)}>Open <Icon name="arrow" /></button>
        </article>;
      })}</div> : <Panel><EmptyState icon="missions" title="No active mission" body="Create a mission to turn a business goal into a reviewed execution plan." /></Panel>}
    </section>
    <div className="overview-bottom">
      <Panel className="reference-panel"><SectionHeading title="Agents" action={<button className="text-link" onClick={() => onNavigate("agents")}>All agents <Icon name="arrow" /></button>} />
        {workforce.length ? <div>{workforce.slice(0, 5).map((agent) => <div className="reference-agent" key={agent.id}><span className="reference-agent-dot" /><div><strong>{agent.name}</strong></div><StatusBadge status={agent.status} /></div>)}</div> : <EmptyState icon="agents" title="No agents provisioned" body="Your team will appear when a mission plan is activated." />}
      </Panel>
      <Panel className="reference-panel"><SectionHeading title="Recent activity" action={<button className="text-link" onClick={() => onNavigate("activity")}>View all <Icon name="arrow" /></button>} />
        {status?.recentEvents.length ? <div className="reference-events">{status.recentEvents.slice(0, 5).map((event) => <div key={event.event_id ?? event.id}><time>{formatTime(event.occurred_at ?? event.created_at)}</time><div><strong>{eventMessage(event)}</strong></div></div>)}</div> : <EmptyState icon="activity" title="Waiting for activity" body="Mission updates will appear here in real time." />}
      </Panel>
    </div>
  </div>;
}
