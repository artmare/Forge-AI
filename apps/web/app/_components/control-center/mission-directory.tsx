import type { Mission, MissionScope } from "./types";
import { Icon, Panel, StatusBadge, relativeTime } from "./ui";

export type MissionAction = (
  path: string,
  body?: unknown,
  method?: "POST" | "DELETE",
) => Promise<boolean>;

const organizable = new Set(["DRAFT", "PLAN_READY", "COMPLETED", "FAILED", "CANCELLED"]);

export function MissionActions({
  mission,
  busy,
  onAction,
}: {
  mission: Mission;
  busy: boolean;
  onAction: MissionAction;
}) {
  const archive = () => {
    const confirmed = window.confirm(
      "Archive this Mission?\n\nThe Mission and its full history will be preserved, but it will be hidden from normal operational views.",
    );
    if (confirmed) void onAction(`missions/${mission.id}/archive`);
  };
  const remove = () => {
    const confirmation = window.prompt(
      `Delete Mission permanently?\n\nThis permanently removes the Mission and its project data from Forge. This cannot be undone.\n\nType the Mission name to continue:\n${mission.title}`,
    );
    if (confirmation !== null) {
      void onAction(`missions/${mission.id}`, { confirmation }, "DELETE");
    }
  };
  const cancellable = !["COMPLETED", "CANCELLED"].includes(mission.status);

  return (
    <details className="mission-actions">
      <summary aria-label={`Actions for ${mission.title}`} title="Mission actions">
        <span aria-hidden="true">•••</span>
      </summary>
      <div role="menu">
        {mission.archived_at ? (
          <>
            <button disabled={busy} onClick={() => void onAction(`missions/${mission.id}/restore`)} role="menuitem">
              Restore Mission
            </button>
            {organizable.has(mission.status) && (
              <button className="danger" disabled={busy} onClick={remove} role="menuitem">
                Delete permanently
              </button>
            )}
          </>
        ) : (
          <>
            {organizable.has(mission.status) && (
              <button disabled={busy} onClick={archive} role="menuitem">
                Archive Mission
              </button>
            )}
            {cancellable && (
              <button disabled={busy} onClick={() => void onAction(`missions/${mission.id}/cancel`)} role="menuitem">
                Cancel Mission
              </button>
            )}
          </>
        )}
      </div>
    </details>
  );
}

export function MissionDirectory({
  current,
  archived,
  scope,
  selectedId,
  busy,
  onScope,
  onOpen,
  onAction,
}: {
  current: Mission[];
  archived: Mission[];
  scope: MissionScope;
  selectedId: string | null;
  busy: boolean;
  onScope: (scope: MissionScope) => void;
  onOpen: (mission: Mission) => void;
  onAction: MissionAction;
}) {
  const missions = scope === "active" ? current : archived;
  return (
    <Panel className="mission-directory">
      <div className="mission-directory-head">
        <div>
          <p className="eyebrow">Mission library</p>
          <h1>Current and archived work</h1>
        </div>
        <div className="mission-scope" role="tablist" aria-label="Mission lifecycle filter">
          <button role="tab" aria-selected={scope === "active"} onClick={() => onScope("active")}>Current <span>{current.length}</span></button>
          <button role="tab" aria-selected={scope === "archived"} onClick={() => onScope("archived")}>Archived <span>{archived.length}</span></button>
        </div>
      </div>
      {missions.length ? (
        <div className="mission-directory-list">
          {missions.map((mission) => {
            const complete = mission.progress.total
              ? Math.round((mission.progress.done / mission.progress.total) * 100)
              : mission.status === "COMPLETED" ? 100 : 0;
            return (
              <article className={mission.id === selectedId ? "selected" : ""} key={mission.id}>
                <button className="mission-directory-open" onClick={() => onOpen(mission)}>
                  <span className={`mission-state mission-state-${mission.status.toLowerCase()}`} />
                  <span className="mission-directory-copy">
                    <strong>{mission.title}</strong>
                    <small>{mission.project_name ?? "No materialized project"}</small>
                  </span>
                  <StatusBadge status={mission.status} />
                  <span className="mission-directory-date">
                    {scope === "archived" ? "Archived" : "Created"} {relativeTime(scope === "archived" ? mission.archived_at : mission.created_at)}
                  </span>
                  <span className="mission-directory-progress">{complete}%</span>
                  <Icon name="arrow" />
                </button>
                <MissionActions mission={mission} busy={busy} onAction={onAction} />
              </article>
            );
          })}
        </div>
      ) : (
        <div className="mission-directory-empty">
          <Icon name={scope === "archived" ? "missions" : "check"} />
          <strong>{scope === "archived" ? "No archived Missions" : "No current Missions"}</strong>
          <p>{scope === "archived" ? "Archived work will appear here with its complete history." : "Create a Mission to start a new project."}</p>
        </div>
      )}
    </Panel>
  );
}
