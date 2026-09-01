"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { forge, focusNode } from "./helpers";
import { CreateMission } from "./create-mission";
import { MissionView } from "./mission-view";
import { MissionDirectory } from "./mission-directory";
import { ActivityPage, OperationsView, SystemPage } from "./operations";
import { Overview } from "./overview";
import type {
  Agent, ArtifactMetadata, Company, ControlData, DevelopmentProfile, ForgeEvent,
  Graph, Mission, MissionScope, MissionTab, Plan, Project, StatusPayload,
  TaskDetail, TaskRun, View,
} from "./types";
import { Icon, type IconName } from "./ui";

const navigation: Array<{ id: View; label: string; icon: IconName }> = [
  { id: "overview", label: "Overview", icon: "overview" },
  { id: "missions", label: "Missions", icon: "missions" },
  { id: "companies", label: "Companies", icon: "companies" },
  { id: "projects", label: "Projects", icon: "projects" },
  { id: "agents", label: "Agents", icon: "agents" },
  { id: "tasks", label: "Tasks", icon: "tasks" },
  { id: "activity", label: "Activity", icon: "activity" },
  { id: "system", label: "System", icon: "system" },
];
const emptyData: ControlData = {
  status: null, missions: [], archivedMissions: [], mission: null, plan: null,
  graph: null, company: null, project: null, developmentProfile: null, agents: [],
  focusTask: null, taskRuns: [], artifacts: null, artifactError: null,
  companies: null, projects: null, allAgents: null, allTasks: null, inventoryError: null,
};
const missionPriority = ["ACTIVE", "REVIEW", "PLAN_READY", "PLANNING", "DRAFT", "FAILED", "COMPLETED", "CANCELLED"];
const views = new Set<View>(navigation.map((item) => item.id));
const tabs = new Set<MissionTab>(["overview", "plan", "tasks", "artifacts", "activity", "technical"]);

type StreamState = "connecting" | "connected" | "reconnecting";
type RouteState = { view: View; tab: MissionTab; scope: MissionScope; missionId: string | null; inspect: string | null };

async function statusPayload() {
  const response = await fetch("/api/status", { cache: "no-store" });
  const payload = await response.json() as StatusPayload & { error?: { message?: string } };
  if (!response.ok) throw new Error(payload.error?.message ?? `Status endpoint returned HTTP ${response.status}`);
  return payload;
}
function archivedEvent(event: ForgeEvent, missions: Mission[]) {
  const missionIds = new Set(missions.map((item) => item.id));
  const projectIds = new Set(missions.flatMap((item) => item.project_id ? [item.project_id] : []));
  const payloadMission = typeof event.payload?.mission_id === "string" ? event.payload.mission_id : null;
  return Boolean((event.project_id && projectIds.has(event.project_id)) ||
    (event.correlation_id && missionIds.has(event.correlation_id)) ||
    (payloadMission && missionIds.has(payloadMission)));
}
function readRoute(): RouteState {
  const params = new URLSearchParams(window.location.search);
  const rawView = params.get("view") as View | null;
  const rawTab = params.get("tab") as MissionTab | null;
  return {
    view: rawView && views.has(rawView) ? rawView : "overview",
    tab: rawTab && tabs.has(rawTab) ? rawTab : "overview",
    scope: params.get("scope") === "archived" ? "archived" : "active",
    missionId: params.get("mission"),
    inspect: params.get("inspect"),
  };
}
function writeRoute(route: RouteState, replace = false) {
  const params = new URLSearchParams();
  if (route.view !== "overview") params.set("view", route.view);
  if (route.missionId) params.set("mission", route.missionId);
  if (route.view === "missions" && route.tab !== "overview") params.set("tab", route.tab);
  if (route.scope === "archived") params.set("scope", "archived");
  if (route.inspect) params.set("inspect", route.inspect);
  const url = `${window.location.pathname}${params.size ? `?${params}` : ""}`;
  window.history[replace ? "replaceState" : "pushState"]({}, "", url);
}

export function ControlCenter() {
  const [data, setData] = useState<ControlData>(emptyData);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [view, setView] = useState<View>("overview");
  const [tab, setTab] = useState<MissionTab>("overview");
  const [missionScope, setMissionScope] = useState<MissionScope>("active");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [createStage, setCreateStage] = useState<string | null>(null);
  const [pendingCreate, setPendingCreate] = useState<Mission | null>(null);
  const [controlBusy, setControlBusy] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inspectionTaskId, setInspectionTaskId] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const refreshTimer = useRef<number | null>(null);
  const detailRequest = useRef(0);
  const routeReady = useRef(false);

  const applyRoute = useCallback((route: RouteState) => {
    setView(route.view);
    setTab(route.tab);
    setMissionScope(route.scope);
    setSelectedId(route.missionId);
    setInspectionTaskId(route.inspect);
  }, []);

  const syncRoute = useCallback((updates: Partial<RouteState>, replace = false) => {
    const current: RouteState = { view, tab, scope: missionScope, missionId: selectedId, inspect: inspectionTaskId };
    const next = { ...current, ...updates };
    applyRoute(next);
    writeRoute(next, replace);
  }, [applyRoute, inspectionTaskId, missionScope, selectedId, tab, view]);

  const refreshCore = useCallback(async () => {
    try {
      const [status, missions, archivedMissions] = await Promise.all([
        statusPayload(),
        forge<Mission[]>("missions?archive=active&limit=100"),
        forge<Mission[]>("missions?archive=archived&limit=100"),
      ]);
      const ordered = [...missions].sort((a, b) => missionPriority.indexOf(a.status) - missionPriority.indexOf(b.status));
      const archived = [...archivedMissions].sort((a, b) => Date.parse(b.archived_at ?? "") - Date.parse(a.archived_at ?? ""));
      const operationalStatus = { ...status, recentEvents: status.recentEvents.filter((event) => !archivedEvent(event, archived)) };
      const all = [...ordered, ...archived];
      setData((current) => ({ ...current, status: operationalStatus, missions: ordered, archivedMissions: archived }));
      setSelectedId((current) => {
        if (current && all.some((item) => item.id === current)) return current;
        const next = ordered[0]?.id ?? archived[0]?.id ?? null;
        if (routeReady.current && next) {
          const route = readRoute();
          writeRoute({ ...route, missionId: next }, true);
        }
        return next;
      });
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Forge control data is unavailable.");
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshDetail = useCallback(async () => {
    const requestId = ++detailRequest.current;
    if (!selectedId) {
      setData((current) => ({ ...current, mission: null, plan: null, graph: null, company: null, project: null, developmentProfile: null, agents: [], focusTask: null, taskRuns: [], artifacts: null, artifactError: null }));
      return;
    }
    setData((current) => current.mission?.id === selectedId ? current : ({ ...current, mission: null, plan: null, graph: null, company: null, project: null, developmentProfile: null, agents: [], focusTask: null, taskRuns: [], artifacts: null, artifactError: null }));
    try {
      const mission = await forge<Mission>(`missions/${selectedId}`);
      const [planResult, graphResult, companyResult, projectResult, profileResult, agentsResult, artifactsResult] = await Promise.allSettled([
        mission.planning_attempts > 0 ? forge<Plan>(`missions/${mission.id}/plan`) : Promise.resolve(null),
        mission.project_id ? forge<Graph>(`projects/${mission.project_id}/graph`) : Promise.resolve(null),
        mission.company_id ? forge<Company>(`companies/${mission.company_id}`) : Promise.resolve(null),
        mission.project_id ? forge<Project>(`projects/${mission.project_id}`) : Promise.resolve(null),
        mission.project_id ? forge<DevelopmentProfile>(`projects/${mission.project_id}/development-profile`) : Promise.resolve(null),
        mission.company_id ? forge<Agent[]>(`companies/${mission.company_id}/agents?limit=100`) : Promise.resolve([]),
        mission.project_id ? forge<ArtifactMetadata[]>(`projects/${mission.project_id}/artifacts`) : Promise.resolve([]),
      ]);
      const graph = graphResult.status === "fulfilled" ? graphResult.value : null;
      const focus = focusNode(graph);
      const [taskResult, runsResult] = await Promise.allSettled([
        focus ? forge<TaskDetail>(`tasks/${focus.id}`) : Promise.resolve(null),
        focus ? forge<TaskRun[]>(`tasks/${focus.id}/runs?limit=20`) : Promise.resolve([]),
      ]);
      if (requestId !== detailRequest.current) return;
      setData((current) => ({
        ...current, mission,
        plan: planResult.status === "fulfilled" ? planResult.value : null,
        graph, company: companyResult.status === "fulfilled" ? companyResult.value : null,
        project: projectResult.status === "fulfilled" ? projectResult.value : null,
        developmentProfile: profileResult.status === "fulfilled" ? profileResult.value : null,
        agents: agentsResult.status === "fulfilled" ? agentsResult.value : [],
        focusTask: taskResult.status === "fulfilled" ? taskResult.value : null,
        taskRuns: runsResult.status === "fulfilled" ? runsResult.value : [],
        artifacts: artifactsResult.status === "fulfilled" ? artifactsResult.value : [],
        artifactError: artifactsResult.status === "rejected" ? "Could not load mission artifacts." : null,
      }));
    } catch (cause) {
      if (requestId === detailRequest.current) setError(cause instanceof Error ? cause.message : "Mission workspace could not refresh.");
    }
  }, [selectedId]);

  const refreshInventory = useCallback(async (nextView: View) => {
    if (!["companies", "projects", "agents", "tasks"].includes(nextView)) return;
    setData((current) => ({ ...current, inventoryError: null }));
    try {
      if (nextView === "companies") {
        const companies = await forge<Company[]>("companies?limit=100");
        setData((current) => ({ ...current, companies }));
      } else if (nextView === "tasks") {
        const allTasks = await forge<TaskDetail[]>("tasks?limit=100");
        setData((current) => ({ ...current, allTasks }));
      } else {
        const companies = await forge<Company[]>("companies?limit=100");
        if (nextView === "projects") {
          const results = await Promise.allSettled(companies.map((company) => forge<Project[]>(`companies/${company.id}/projects?limit=100`)));
          const projects = results.flatMap((result) => result.status === "fulfilled" ? result.value : []);
          setData((current) => ({ ...current, companies, projects, inventoryError: results.some((result) => result.status === "rejected") ? "Some company projects could not be loaded." : null }));
        } else {
          const results = await Promise.allSettled(companies.map((company) => forge<Agent[]>(`companies/${company.id}/agents?limit=100`)));
          const allAgents = results.flatMap((result) => result.status === "fulfilled" ? result.value : []);
          setData((current) => ({ ...current, companies, allAgents, inventoryError: results.some((result) => result.status === "rejected") ? "Some company agents could not be loaded." : null }));
        }
      }
    } catch (cause) {
      setData((current) => ({ ...current, inventoryError: cause instanceof Error ? cause.message : "Inventory could not be loaded." }));
    }
  }, []);

  useEffect(() => {
    applyRoute(readRoute());
    routeReady.current = true;
    const pop = () => applyRoute(readRoute());
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, [applyRoute]);
  useEffect(() => {
    const initial = window.setTimeout(() => void refreshCore(), 0);
    const interval = window.setInterval(() => { void refreshCore(); void refreshDetail(); }, 10_000);
    return () => { window.clearTimeout(initial); window.clearInterval(interval); };
  }, [refreshCore, refreshDetail]);
  useEffect(() => {
    const next = window.setTimeout(() => void refreshDetail(), 0);
    return () => window.clearTimeout(next);
  }, [refreshDetail]);
  useEffect(() => { void refreshInventory(view); }, [refreshInventory, view]);
  useEffect(() => {
    const source = new EventSource("/api/events/stream");
    setStreamState("connecting");
    source.onopen = () => setStreamState("connected");
    source.onerror = () => setStreamState("reconnecting");
    const receive = (message: Event) => {
      try {
        const incoming = JSON.parse((message as MessageEvent<string>).data) as ForgeEvent;
        setData((current) => {
          if (!current.status || archivedEvent(incoming, current.archivedMissions)) return current;
          const incomingId = incoming.event_id ?? incoming.id;
          return { ...current, status: { ...current.status, recentEvents: [incoming, ...current.status.recentEvents.filter((event) => (event.event_id ?? event.id) !== incomingId)].slice(0, 20) } };
        });
        if (refreshTimer.current) window.clearTimeout(refreshTimer.current);
        refreshTimer.current = window.setTimeout(() => { void refreshCore(); void refreshDetail(); }, 500);
      } catch { /* malformed transport event is ignored */ }
    };
    source.addEventListener("forge-event", receive);
    return () => {
      source.removeEventListener("forge-event", receive);
      source.close();
      if (refreshTimer.current) window.clearTimeout(refreshTimer.current);
    };
  }, [refreshCore, refreshDetail]);

  const navigate = (next: View, nextTab?: MissionTab) => {
    const nextMission = next === "overview" && data.mission?.archived_at ? data.missions[0]?.id ?? null : selectedId;
    syncRoute({ view: next, tab: nextTab ?? tab, missionId: nextMission, inspect: next === "missions" ? inspectionTaskId : null });
    setMobileNav(false);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  const chooseMission = (missionId: string | null, scope: MissionScope, nextTab: MissionTab = "overview") => syncRoute({ view: "missions", missionId, scope, tab: nextTab, inspect: null });
  const chooseTab = (nextTab: MissionTab) => syncRoute({ view: "missions", tab: nextTab, inspect: nextTab === "tasks" ? inspectionTaskId : null });
  const inspectTask = (taskId: string | null) => syncRoute({ view: "missions", tab: "tasks", inspect: taskId });
  const action = async (path: string, body?: unknown, method: "POST" | "DELETE" = "POST") => {
    setBusy(true);
    try {
      await forge(path, method, body ?? {});
      if (method === "DELETE") chooseMission(null, missionScope);
      await refreshCore();
      if (method !== "DELETE") await refreshDetail();
      setError(null);
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Action could not be completed.");
      return false;
    } finally { setBusy(false); }
  };
  const create = async (body: Record<string, unknown>) => {
    const startImmediately = body.start_immediately !== false;
    const payload = { ...body };
    delete payload.start_immediately;
    setBusy(true);
    let mission: Mission | null = pendingCreate && pendingCreate.title === body.title && pendingCreate.goal === body.goal ? pendingCreate : null;
    try {
      if (!mission) {
        setCreateStage("Creating mission…");
        mission = await forge<Mission>("missions", "POST", payload);
        setPendingCreate(mission);
      } else {
        setCreateStage("Checking saved mission…");
        mission = await forge<Mission>(`missions/${mission.id}`);
      }
      chooseMission(mission.id, "active");
      if (startImmediately) {
        if (mission.status === "PLANNING") throw new Error("The interrupted planning run is still being recovered. Retry when the mission returns to Draft.");
        if (mission.status === "DRAFT") {
          setCreateStage("Planning execution…");
          await forge(`missions/${mission.id}/plan`, "POST", {});
          mission = await forge<Mission>(`missions/${mission.id}`);
        }
        if (mission.status === "PLAN_READY") {
          setCreateStage("Activating mission…");
          await forge(`missions/${mission.id}/activate`, "POST", {});
        } else if (!["ACTIVE", "REVIEW", "COMPLETED"].includes(mission.status)) {
          throw new Error(`Saved mission is ${mission.status.replaceAll("_", " ").toLowerCase()} and cannot be activated yet.`);
        }
      }
      setCreateStage("Loading execution state…");
      await refreshCore();
      await refreshDetail();
      setDrawer(false);
      setPendingCreate(null);
      setError(null);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "Mission could not be created.";
      setError(mission ? `Mission was created, but the next step failed: ${message}` : message);
      throw cause;
    } finally {
      setCreateStage(null);
      setBusy(false);
    }
  };
  const setAutonomy = async (enabled: boolean) => {
    setControlBusy(true);
    try {
      const response = await fetch(`/api/orchestrator/${enabled ? "resume" : "pause"}`, { method: "POST" });
      const payload = await response.json().catch(() => null) as { error?: { message?: string } } | null;
      if (!response.ok) throw new Error(payload?.error?.message ?? "Orchestrator control failed.");
      await refreshCore();
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Autonomy control failed.");
    } finally { setControlBusy(false); }
  };
  const attention = data.mission?.archived_at ? 0 :
    (data.mission?.status === "PLAN_READY" ? 1 : 0) +
    (data.graph?.nodes.filter((node) => ["REVIEW", "FIX_REQUIRED", "FAILED"].includes(node.status) || node.blocked_reason?.includes("FAILED")).length ?? 0);
  const runtimeOnline = Boolean(data.status?.orchestrator.orchestrator_online);

  return <div className="control-center">
    <button className={`mobile-scrim ${mobileNav ? "mobile-scrim-open" : ""}`} onClick={() => setMobileNav(false)} aria-label="Close navigation" />
    <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
      <div className="brand"><div className="brand-mark"><span /></div><div><strong>FORGE</strong><small>CONTROL CENTER</small></div><button className="ml-auto md:hidden" onClick={() => setMobileNav(false)} aria-label="Close navigation"><Icon name="close" /></button></div>
      <nav aria-label="Primary navigation">{navigation.map((item) => <button key={item.id} onClick={() => navigate(item.id)} aria-current={view === item.id ? "page" : undefined} className={view === item.id ? "nav-active" : ""}><Icon name={item.icon} /><span>{item.label}</span>{item.id === "missions" && attention > 0 && <em>{attention}</em>}</button>)}</nav>
      <div className="sidebar-missions"><p>Recent missions</p>{loading && !data.missions.length && <span className="sidebar-loading">Loading missions…</span>}{data.missions.slice(0, 5).map((mission) => <button key={mission.id} className={mission.id === selectedId ? "selected" : ""} onClick={() => chooseMission(mission.id, "active")}><span className={`mission-state mission-state-${mission.status.toLowerCase()}`} /><span className="truncate">{mission.title}</span></button>)}{!loading && !data.missions.length && <span className="sidebar-loading">No active missions</span>}<button className="sidebar-view-all" onClick={() => chooseMission(data.missions[0]?.id ?? null, "active")}>View all missions <Icon name="arrow" /></button></div>
      <div className="sidebar-footer"><div className="runtime-indicator"><span className={runtimeOnline ? "online" : ""} /><div><strong>{runtimeOnline ? "Runtime online" : "Runtime offline"}</strong><small>{data.status?.orchestrator.active_workers ?? 0} active workers</small></div></div><p>Forge v{data.status?.system.version ?? "0.8.0"}</p></div>
    </aside>
    <div className="workspace">
      <header className="topbar"><button className="icon-button md:hidden" onClick={() => setMobileNav(true)} aria-label="Open navigation"><Icon name="menu" /></button><div className="topbar-mission"><span>MISSION</span><button onClick={() => navigate("missions")}>{data.mission?.title ?? "No active mission"}<Icon name="arrow" /></button></div><div className="topbar-actions"><div className="stream-status hidden items-center gap-2 lg:flex" title={`Live updates: ${streamState}`}><span className={`health-dot ${streamState === "connected" ? "health-online" : ""}`} /><span>{streamState === "connected" ? "Live updates" : streamState === "connecting" ? "Connecting…" : "Reconnecting…"}</span></div><button disabled={controlBusy || !runtimeOnline} title={!runtimeOnline ? "Runtime must be online before autonomy can be controlled." : undefined} onClick={() => void setAutonomy(!data.status?.orchestrator.autonomy_enabled)} className={`autonomy-control ${data.status?.orchestrator.autonomy_enabled ? "autonomy-on" : ""}`}><Icon name={data.status?.orchestrator.autonomy_enabled ? "pause" : "play"} /><span className="hidden sm:inline">{controlBusy ? "Applying…" : data.status?.orchestrator.autonomy_enabled ? "Pause autonomy" : "Resume autonomy"}</span><span className="sm:hidden">{data.status?.orchestrator.autonomy_enabled ? "Pause" : "Resume"}</span></button><button onClick={() => setDrawer(true)} className="create-button"><Icon name="plus" /><span className="hidden sm:inline">New mission</span></button></div></header>
      <main className="content-area">
        {error && <div className="global-error" role="alert"><Icon name="alert" /><span>{error}</span><button className="error-retry" onClick={() => { setError(null); void refreshCore(); void refreshDetail(); }}>Refresh</button><button onClick={() => setError(null)} aria-label="Dismiss error"><Icon name="close" /></button></div>}
        {view === "overview" && <Overview data={data} loading={loading} onNavigate={navigate} onInspectTask={inspectTask} />}
        {view === "missions" && <div className="space-y-8"><MissionView key={data.mission?.id ?? "no-mission"} data={data} tab={tab} busy={busy} inspectionTaskId={inspectionTaskId} onTab={chooseTab} onAction={action} onInspectTask={inspectTask} /><MissionDirectory current={data.missions} archived={data.archivedMissions} scope={missionScope} selectedId={selectedId} busy={busy} onScope={(scope) => chooseMission((scope === "archived" ? data.archivedMissions : data.missions)[0]?.id ?? null, scope)} onOpen={(mission) => chooseMission(mission.id, mission.archived_at ? "archived" : "active")} onAction={action} /></div>}
        {["companies", "projects", "agents", "tasks"].includes(view) && <OperationsView view={view as "companies" | "projects" | "agents" | "tasks"} data={data} onInspectTask={inspectTask} />}
        {view === "activity" && <ActivityPage data={data} />}
        {view === "system" && <SystemPage data={data} />}
      </main>
    </div>
    <CreateMission open={drawer} busy={busy} stage={createStage} retrying={Boolean(pendingCreate)} runtimePaused={!data.status?.orchestrator.autonomy_enabled} onClose={() => { if (!busy) setDrawer(false); }} onCreate={create} />
  </div>;
}
