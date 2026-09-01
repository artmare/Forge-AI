import type { PlannerErrorDetails } from "./types";

function receivedShape(received: { type?: string; length?: number } | undefined) {
  if (!received) return "unknown shape";
  return `${received.type ?? "unknown"}${typeof received.length === "number" ? ` (length ${received.length})` : ""}`;
}

export function PlannerResponseDiagnostics({ error }: { error: PlannerErrorDetails }) {
  const fields = error.validation_errors ?? [];
  if (!fields.length && !error.finish_reason && !error.response_shape) return null;
  return <div className="mt-3 rounded-lg border border-white/10 bg-black/15 p-3" aria-label="Planner response diagnostics">
    <strong className="text-xs text-slate-200">Malformed response evidence</strong>
    {(error.finish_reason || error.response_shape) && <small className="mt-1 block">
      {[error.finish_reason && `Gemini finish: ${error.finish_reason}`, error.response_shape?.type && `response: ${error.response_shape.type}`, typeof error.response_shape?.text_length === "number" && `${error.response_shape.text_length} characters`].filter(Boolean).join(" · ")}
    </small>}
    {fields.length > 0 && <ul className="mt-2 space-y-2 text-xs text-slate-300">
      {fields.map((item, index) => <li key={`${item.field}-${index}`}>
        <code>{item.field}</code> — expected {item.expected}; received {receivedShape(item.received)}
      </li>)}
    </ul>}
  </div>;
}
