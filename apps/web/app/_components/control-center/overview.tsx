import { agentForNode, attentionNodes, eventMessage, eventType, focusNode, weightedProgress } from "./helpers";
import type { ControlData, GraphNode, MissionTab, View } from "./types";
import { EmptyState, Icon, Panel, SectionHeading, StatusBadge, formatTime, relativeTime } from "./ui";

interface Props {
  data: ControlData; loading: boolean;
  onNavigate: (view: View, tab?: MissionTab) => void;
  onInspectTask: (taskId: string) => void;
}

function ProgressRing({ value }: { value: number }) {
  return <div className="relative grid size-24 shrink-0 place-items-center"><svg viewBox="0 0 100 100" className="absolute inset-0 -rotate-90" aria-hidden="true"><circle cx="50" cy="50" r="43" fill="none" stroke="rgba(255,255,255,.06)" strokeWidth="7"/><circle cx="50" cy="50" r="43" fill="none" stroke="url(#progress-gradient)" strokeWidth="7" strokeLinecap="round" pathLength="100" strokeDasharray={`${value} 100`}/><defs><linearGradient id="progress-gradient"><stop stopColor="#64dca3"/><stop offset="1" stopColor="#60a5fa"/></linearGradient></defs></svg><div className="text-center"><p className="font-mono text-2xl font-semibold text-white">{value}%</p><p className="text-[9px] uppercase tracking-[.14em] text-slate-600">progress</p></div></div>;
}

function GraphPreview({ nodes, onOpen }: { nodes: GraphNode[]; onOpen: () => void }) {
  if (!nodes.length) return <EmptyState icon="tasks" title="No execution graph yet" body="Plan this mission to create its deterministic task graph." />;
  return <div className="space-y-2.5">{nodes.slice(0, 5).map((node, index) => <button key={node.id} onClick={onOpen} className="group flex w-full items-center gap-3 text-left"><span className={`grid size-7 shrink-0 place-items-center rounded-full border font-mono text-[10px] ${node.status === "DONE" ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-300" : node.status === "IN_PROGRESS" ? "border-sky-400/30 bg-sky-400/10 text-sky-300 shadow-[0_0_18px_rgba(56,189,248,.1)]" : "border-white/8 bg-white/[.02] text-slate-600"}`}>{node.status === "DONE" ? <Icon name="check" className="size-3.5"/> : index + 1}</span><span className="min-w-0 flex-1"><span className="block truncate text-xs text-slate-300 group-hover:text-white">{node.title}</span><span className="mt-0.5 block text-[10px] text-slate-600">{node.blocked_reason || node.state?.replaceAll("_", " ") || "Task"}</span></span><StatusBadge status={node.status} dot={false}/></button>)}</div>;
}

export function Overview({ data, loading, onNavigate, onInspectTask }: Props) {
  const { mission, graph, status, agents, focusTask } = data;
  const progress = weightedProgress(mission, graph);
  const focus = focusNode(graph);
  const agent = agentForNode(focus, agents);
  const attention = attentionNodes(mission, graph);
  const recentRun = status?.recentAgentRuns.find((run) => run.task_title === focus?.title);
  const currentTool = focus?.tool_calls?.at(-1) ?? null;
  const completed = graph?.nodes.filter((node) => node.status === "DONE").length ?? 0;

  if (loading && !mission) return <div className="grid gap-4 xl:grid-cols-12"><div className="skeleton h-64 xl:col-span-7"/><div className="skeleton h-64 xl:col-span-5"/><div className="skeleton h-72 xl:col-span-4"/><div className="skeleton h-72 xl:col-span-5"/><div className="skeleton h-72 xl:col-span-3"/></div>;

  return <div className="space-y-4">
    <div className="mb-1 flex flex-wrap items-end justify-between gap-3"><div><p className="eyebrow">Control center</p><h1 className="page-title">Company operations at a glance</h1></div><p className="hidden text-xs text-slate-600 sm:block">Live signal · updated {relativeTime(status?.checkedAt)}</p></div>
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4 xl:grid-cols-12">
      <Panel elevated className="mission-hero relative overflow-hidden p-5 sm:p-6 xl:col-span-7">
        <div className="pointer-events-none absolute -right-20 -top-28 size-72 rounded-full bg-emerald-400/[.06] blur-3xl"/>
        {mission ? <div className="relative flex h-full flex-col justify-between gap-5"><div className="flex items-start justify-between gap-4"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><StatusBadge status={mission.status}/><span className="text-[10px] uppercase tracking-[.14em] text-slate-600">Active mission</span></div><h2 className="mt-4 max-w-xl text-2xl font-semibold tracking-[-.035em] text-white sm:text-3xl">{mission.title}</h2><p className="mt-2 line-clamp-2 max-w-xl text-sm leading-6 text-slate-500">{mission.goal}</p></div><ProgressRing value={progress}/></div><div><div className="mb-2 flex items-center justify-between text-[10px]"><span className="text-slate-500">{completed} of {graph?.nodes.length ?? mission.progress.total} tasks complete</span><span className="text-slate-600">Weighted lifecycle estimate</span></div><div className="h-1.5 overflow-hidden rounded-full bg-white/[.06]"><div className="h-full rounded-full bg-gradient-to-r from-emerald-400 to-sky-400 transition-[width] duration-700" style={{ width: `${progress}%` }}/></div><button onClick={() => onNavigate("missions", "overview")} className="mt-4 inline-flex items-center gap-2 text-xs font-medium text-emerald-300 hover:text-emerald-200">Open mission workspace <Icon name="arrow" className="size-3.5"/></button></div></div> : <EmptyState icon="missions" title="No active mission" body="Create a mission to turn a business goal into a reviewed execution plan."/>}
      </Panel>

      <Panel className="p-5 sm:p-6 xl:col-span-5">
        <SectionHeading eyebrow="Current execution" title={focus?.status === "IN_PROGRESS" ? "Work in progress" : "Next operational state"} action={focus && <StatusBadge status={focus.status}/>}/>
        {focus ? <div className="flex h-[calc(100%-3rem)] flex-col justify-between gap-4"><div><p className="text-lg font-medium tracking-[-.02em] text-slate-100">{focus.title}</p><div className="mt-4 grid grid-cols-2 gap-2"><div className="mini-stat"><span>Agent</span><strong>{agent?.name ?? "Unassigned"}</strong></div><div className="mini-stat"><span>Iteration</span><strong>{focusTask?.iteration ?? 0} / {focusTask?.max_iterations ?? "—"}</strong></div><div className="mini-stat"><span>Model</span><strong>{recentRun?.model_alias ?? agent?.model_alias ?? "Waiting"}</strong></div><div className="mini-stat"><span>Latest step</span><strong>{currentTool?.tool_name?.replace("filesystem.", "") ?? "No tool activity"}</strong></div></div></div><div className="flex items-center gap-3 border-t border-white/[.06] pt-4"><span className={`signal ${focus.status === "IN_PROGRESS" ? "signal-live" : ""}`}/><div><p className="text-xs text-slate-300">{focus.status === "REVIEW" ? "Waiting for human approval" : focus.status === "QUEUED" && !status?.orchestrator.autonomy_enabled ? "Ready work · autonomy paused" : focus.status === "IN_PROGRESS" ? "Agent is executing this task" : `Task is ${focus.status.replaceAll("_", " ").toLowerCase()}`}</p><p className="mt-0.5 text-[10px] text-slate-600">{focusTask?.started_at ? `Started ${formatTime(focusTask.started_at)}` : "Durable state is synchronized"}</p></div></div></div> : <EmptyState icon="pulse" title="Waiting for ready work" body="Forge will surface the next executable task here."/>}
      </Panel>

      <Panel className="p-5 xl:col-span-4">
        <SectionHeading eyebrow="Human checkpoints" title="Needs attention" action={<span className={`attention-count ${attention.count ? "attention-count-active" : ""}`}>{attention.count}</span>}/>
        {!attention.count ? <EmptyState icon="check" title="Nothing needs intervention" body="Planning and execution are moving within current controls."/> : <div className="space-y-2">{attention.planReady && <button onClick={() => onNavigate("missions", "plan")} className="attention-row"><span className="attention-icon"><Icon name="spark"/></span><span className="min-w-0 flex-1"><strong>Plan ready to activate</strong><small>Review the team and task graph</small></span><Icon name="arrow" className="size-4 text-slate-600"/></button>}{attention.nodes.slice(0, 4).map((node) => <button key={node.id} onClick={() => node.status === "FAILED" ? onInspectTask(node.id) : onNavigate("missions", "tasks")} className="attention-row"><span className="attention-icon"><Icon name="alert"/></span><span className="min-w-0 flex-1"><strong className="truncate">{node.status === "FAILED" ? "Development failed" : node.title}</strong><small>{node.status === "FAILED" ? `${node.title} · ${node.qa_results.at(-1)?.summary ?? "Inspect durable failure evidence"}` : `${node.status.replaceAll("_", " ")}${node.blocked_reason ? ` · ${node.blocked_reason}` : ""}`}</small></span><Icon name="arrow" className="size-4 text-slate-600"/></button>)}</div>}
      </Panel>

      <Panel className="p-5 xl:col-span-5">
        <SectionHeading eyebrow="Execution map" title="Mission graph" action={<button onClick={() => onNavigate("missions", "plan")} className="text-link">View plan</button>}/>
        <GraphPreview nodes={graph?.nodes ?? []} onOpen={() => onNavigate("missions", "tasks")}/>
      </Panel>

      <Panel className="p-5 xl:col-span-3">
        <SectionHeading eyebrow="Realtime" title="Live activity" action={<span className="live-label"><span/>Live</span>}/>
        {!status?.recentEvents.length ? <EmptyState icon="activity" title="Waiting for activity" body="Operational events will appear here in real time."/> : <div className="activity-list">{status.recentEvents.slice(0, 5).map((event) => <button key={event.event_id ?? event.id} onClick={() => onNavigate("activity")} className="activity-item"><span className="activity-rail"/><span className="min-w-0"><strong>{eventMessage(event)}</strong><small>{relativeTime(event.occurred_at ?? event.created_at)} · {eventType(event)}</small></span></button>)}</div>}
      </Panel>
    </div>
  </div>;
}
