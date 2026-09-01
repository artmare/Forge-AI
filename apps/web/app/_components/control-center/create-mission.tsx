import { FormEvent, useEffect, useState } from "react";
import { ForgeRequestError, type ForgeErrorDetails } from "./helpers";
import { PlannerResponseDiagnostics } from "./planner-diagnostics";
import { Icon } from "./ui";

interface Props {
  open: boolean;
  busy: boolean;
  stage: string | null;
  retrying: boolean;
  runtimePaused: boolean;
  onClose: () => void;
  onCreate: (body: Record<string, unknown>) => Promise<void>;
}
function pretty(value: string) { return value.trim() ? JSON.stringify(JSON.parse(value), null, 2) : "{}"; }
type PlannerFailure = ForgeErrorDetails & { message: string; code?: string };

function plannerFailure(cause: unknown): PlannerFailure {
  if (cause instanceof ForgeRequestError) {
    return { ...cause.details, code: cause.code, message: cause.message };
  }
  return {
    message: cause instanceof Error ? cause.message : "Mission could not be created.",
  };
}

export function CreateMission({ open, busy, stage, retrying, runtimePaused, onClose, onCreate }: Props) {
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [maxAgents, setMaxAgents] = useState(3);
  const [maxTasks, setMaxTasks] = useState(8);
  const [paidAiBudget, setPaidAiBudget] = useState(0);
  const [constraints, setConstraints] = useState("{}");
  const [context, setContext] = useState("{}");
  const [advanced, setAdvanced] = useState(false);
  const [startImmediately, setStartImmediately] = useState(true);
  const [error, setError] = useState<PlannerFailure | null>(null);
  const retryUnavailable = retrying && error?.retryable === false;

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose, open]);

  if (!open) return null;
  async function submit(event: FormEvent) {
    event.preventDefault();
    try {
      const parsedConstraints = JSON.parse(constraints || "{}");
      const parsedContext = JSON.parse(context || "{}");
      await onCreate({
        title, goal, start_immediately: startImmediately, paid_ai_budget: paidAiBudget,
        constraints: { ...parsedConstraints, max_agents: maxAgents, max_tasks: maxTasks, internet_access: false, browser_access: false },
        context: parsedContext,
      });
      setTitle(""); setGoal(""); setConstraints("{}"); setContext("{}");
      setMaxAgents(3); setMaxTasks(8); setPaidAiBudget(0); setAdvanced(false); setStartImmediately(true); setError(null);
    } catch (cause) {
      setError(cause instanceof SyntaxError
        ? { code: "INVALID_JSON", message: "Advanced fields must contain valid JSON.", retryable: false }
        : plannerFailure(cause));
    }
  }

  return <div className="drawer-layer" role="dialog" aria-modal="true" aria-labelledby="create-title">
    <button className="drawer-backdrop" onClick={onClose} disabled={busy} aria-label="Close create mission dialog" />
    <aside className="create-drawer">
      <div className="drawer-header"><div><p className="eyebrow">New operation</p><h2 id="create-title">Create a mission</h2></div><button onClick={onClose} disabled={busy} className="icon-button" aria-label="Close"><Icon name="close" /></button></div>
      <form onSubmit={submit} className="drawer-form">
        <div><label htmlFor="mission-title">Mission title</label><input id="mission-title" autoFocus required value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Launch the customer insight hub" /></div>
        <div><label htmlFor="mission-goal">Outcome</label><textarea id="mission-goal" required rows={5} value={goal} onChange={(event) => setGoal(event.target.value)} onInput={(event) => { event.currentTarget.style.height = "auto"; event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 320)}px`; }} placeholder="Describe the concrete outcome Forge should organize and execute…" /></div>
        <label className="start-mission-control"><input type="checkbox" checked={startImmediately} onChange={(event) => setStartImmediately(event.target.checked)} /><span><strong>Plan and activate after creation</strong><small>{runtimePaused ? "The mission will be activated and queued. Resume autonomy when you want workers to execute it." : "Forge will create the plan and begin execution immediately."}</small></span></label>
        <fieldset><legend>Planning boundaries</legend><div className="grid grid-cols-2 gap-3"><div><label htmlFor="max-agents">Maximum agents</label><input id="max-agents" type="number" min={1} max={12} value={maxAgents} onChange={(event) => setMaxAgents(Number(event.target.value))} /></div><div><label htmlFor="max-tasks">Maximum tasks</label><input id="max-tasks" type="number" min={1} max={50} value={maxTasks} onChange={(event) => setMaxTasks(Number(event.target.value))} /></div></div><div className="mt-3 grid grid-cols-2 gap-3"><div className="disabled-control"><span>Internet access</span><strong>OFF</strong></div><div className="disabled-control"><span>Browser access</span><strong>OFF</strong></div></div></fieldset>
        <button type="button" className="advanced-toggle" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>Advanced structured inputs <Icon name="arrow" className={`size-4 transition ${advanced ? "rotate-90" : ""}`} /></button>
        {advanced && <div className="space-y-4 rounded-xl border border-white/[.07] bg-black/10 p-4"><div><label htmlFor="paid-ai-budget">Maximum paid AI spend (USD)</label><input id="paid-ai-budget" type="number" min={0} step="0.01" value={paidAiBudget} onChange={(event) => setPaidAiBudget(Math.max(Number(event.target.value), 0))}/><p className="mt-1 text-[10px] leading-4 text-slate-600">Defaults to $0. Free models and deterministic tools remain available; paid routing still requires the global operator opt-in.</p></div><div><div className="flex items-center justify-between"><label htmlFor="constraints">Constraints JSON</label><button type="button" onClick={() => { try { setConstraints(pretty(constraints)); setError(null); } catch { setError({ code: "INVALID_JSON", message: "Constraints JSON is invalid.", retryable: false }); } }} className="format-button">Format</button></div><textarea id="constraints" rows={5} className="font-mono! text-xs!" value={constraints} onChange={(event) => setConstraints(event.target.value)} /></div><div><div className="flex items-center justify-between"><label htmlFor="context">Context JSON</label><button type="button" onClick={() => { try { setContext(pretty(context)); setError(null); } catch { setError({ code: "INVALID_JSON", message: "Context JSON is invalid.", retryable: false }); } }} className="format-button">Format</button></div><textarea id="context" rows={5} className="font-mono! text-xs!" value={context} onChange={(event) => setContext(event.target.value)} /></div><p className="text-[10px] leading-4 text-slate-600">Never place secrets, credentials, or API keys in mission context.</p></div>}
        {error && <div className="form-error planner-failure" role="alert"><strong>{error.code ?? "MISSION_PLANNING_FAILED"}</strong><p>{error.message}</p>{(error.category || error.phase) && <small>{[error.category, error.phase].filter(Boolean).join(" · ")}</small>}{typeof error.provider_attempts === "number" && <small>Provider attempts: {error.provider_attempts} / {error.max_provider_attempts ?? "—"}{error.retry_exhausted ? " · automatic retry budget exhausted" : ""}</small>}{error.provider_error_code && <small>Provider cause: {error.provider_error_code}</small>}<PlannerResponseDiagnostics error={error}/><b>{error.retryable ? "The saved Mission can be retried safely." : "Correct the provider or configuration issue before trying again."}</b></div>}
        {busy && <div className="create-progress" role="status"><span /><p><strong>{stage ?? "Working…"}</strong><small>Keep this panel open while Forge persists the current step.</small></p></div>}
        <div className="mt-auto flex gap-2 border-t border-white/[.07] pt-5"><button type="button" disabled={busy} onClick={onClose} className="btn-ghost flex-1">Cancel</button><button disabled={busy || retryUnavailable} title={retryUnavailable ? "Resolve the non-transient planner failure before retrying." : undefined} className="btn-primary flex-1 justify-center"><Icon name={busy ? "activity" : retrying ? "play" : "plus"} />{busy ? stage ?? "Working…" : retryUnavailable ? "Retry unavailable" : retrying ? "Retry saved mission" : startImmediately ? "Create & start" : "Create draft"}</button></div>
      </form>
    </aside>
  </div>;
}
