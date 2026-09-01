import type { ReactNode, SVGProps } from "react";

export type IconName = "overview" | "missions" | "companies" | "projects" | "agents" | "tasks" | "activity" | "system" | "menu" | "close" | "plus" | "arrow" | "check" | "pause" | "play" | "alert" | "file" | "pulse" | "spark";

const paths: Record<IconName, ReactNode> = {
  overview: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
  missions: <><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></>,
  companies: <><path d="M4 21V5a2 2 0 0 1 2-2h8v18M14 9h4a2 2 0 0 1 2 2v10M2 21h20"/><path d="M8 7h2M8 11h2M8 15h2M17 13h1M17 17h1"/></>,
  projects: <><path d="M3 7h7l2 2h9v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M3 7V5a2 2 0 0 1 2-2h5l2 2h5"/></>,
  agents: <><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0M18 4l1-1M6 4 5 3"/></>,
  tasks: <><rect x="4" y="3" width="16" height="18" rx="2"/><path d="m8 8 1.5 1.5L12 7M14 9h3m-9 5 1.5 1.5L12 13M14 15h3"/></>,
  activity: <path d="M3 12h4l2-7 4 14 2-7h6"/>,
  system: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.08A1.7 1.7 0 0 0 9 19.37a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.63 15 1.7 1.7 0 0 0 3.08 14H3v-4h.08A1.7 1.7 0 0 0 4.63 9a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.63 1.7 1.7 0 0 0 10 3.08V3h4v.08A1.7 1.7 0 0 0 15 4.63a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.37 9c.17.6.72 1 1.55 1H21v4h-.08c-.83 0-1.38.4-1.52 1Z"/></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16"/>, close: <path d="m6 6 12 12M18 6 6 18"/>, plus: <path d="M12 5v14M5 12h14"/>,
  arrow: <path d="m9 18 6-6-6-6"/>, check: <path d="m5 12 4 4L19 6"/>, pause: <path d="M9 5v14M15 5v14"/>, play: <path d="m8 5 11 7-11 7z"/>,
  alert: <><path d="M10.3 3.6 2.5 18a2 2 0 0 0 1.8 3h15.4a2 2 0 0 0 1.8-3L13.7 3.6a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/></>,
  file: <><path d="M6 2h8l4 4v16H6z"/><path d="M14 2v5h5M9 13h6M9 17h4"/></>, pulse: <path d="M3 12h4l2-5 4 10 2-5h6"/>, spark: <path d="m12 2 1.6 5.4L19 9l-5.4 1.6L12 16l-1.6-5.4L5 9l5.4-1.6z"/>,
};

export function Icon({ name, className = "size-4", ...props }: { name: IconName } & SVGProps<SVGSVGElement>) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true" {...props}>{paths[name]}</svg>;
}

export function tone(status: string) {
  if (["COMPLETED", "DONE", "SUCCEEDED", "APPROVED", "PLAN_READY", "ONLINE", "HEALTHY", "IDLE", "PASS", "PASSED"].includes(status)) return "success";
  if (["FAILED", "FAIL", "INVALID", "CANCELLED", "OFFLINE", "UNHEALTHY", "DENIED"].includes(status)) return "danger";
  if (["REVIEW", "FIX_REQUIRED", "FIX_REQUESTED", "DRAFT", "PAUSED", "DRAINING", "UNVERIFIED", "NOT_VERIFIED"].includes(status)) return "warning";
  return "active";
}

export function StatusBadge({ status, dot = true }: { status: string; dot?: boolean }) {
  return <span className={`status-badge status-${tone(status)}`}>{dot && <span className="status-dot" />}{status.replaceAll("_", " ")}</span>;
}

export function Panel({ children, className = "", elevated = false }: { children: ReactNode; className?: string; elevated?: boolean }) {
  return <section className={`cc-panel ${elevated ? "cc-panel-elevated" : ""} ${className}`}>{children}</section>;
}

export function SectionHeading({ eyebrow, title, action }: { eyebrow?: string; title: string; action?: ReactNode }) {
  return <div className="mb-4 flex items-end justify-between gap-4"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2 className="mt-1 text-base font-semibold tracking-[-0.01em] text-slate-100">{title}</h2></div>{action}</div>;
}

export function EmptyState({ icon = "spark", title, body }: { icon?: IconName; title: string; body: string }) {
  return <div className="grid min-h-40 place-items-center px-5 py-8 text-center"><div><div className="mx-auto grid size-9 place-items-center rounded-lg border border-white/8 bg-white/[0.025] text-slate-500"><Icon name={icon} /></div><p className="mt-3 text-sm text-slate-300">{title}</p><p className="mx-auto mt-1 max-w-xs text-xs leading-5 text-slate-600">{body}</p></div></div>;
}

export function Skeleton({ className = "h-4 w-full" }: { className?: string }) { return <div className={`skeleton ${className}`} />; }

export function formatTime(value?: string | null, includeSeconds = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit", ...(includeSeconds ? { second: "2-digit" as const } : {}) }).format(date);
}

export function relativeTime(value?: string | null) {
  if (!value) return "just now";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function shortId(value?: string | null) { return value ? value.slice(0, 8) : "—"; }
