import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import type { GraphNode } from "./types";
import { Icon } from "./ui";

const MAX_FEEDBACK_LENGTH = 10_000;

interface Props {
  task: GraphNode;
  busy: boolean;
  onClose: () => void;
  onSubmit: (feedback: string) => Promise<boolean>;
}

export function ReviewFeedbackDialog({ task, busy, onClose, onSubmit }: Props) {
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy && !submitting) onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onClose, submitting]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized = feedback.trim();
    if (!normalized) {
      setError("Describe what the agent should change.");
      return;
    }
    setSubmitting(true);
    try {
      const succeeded = await onSubmit(normalized);
      if (succeeded) onClose();
    } finally {
      setSubmitting(false);
    }
  };

  return <div className="review-dialog-layer" role="dialog" aria-modal="true" aria-labelledby="review-dialog-title">
    <button className="review-dialog-backdrop" onClick={onClose} aria-label="Cancel request changes"/>
    <form className="review-dialog" onSubmit={(event) => void submit(event)}>
      <header>
        <div><p className="eyebrow">Human review</p><h2 id="review-dialog-title">Request changes</h2></div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close request changes"><Icon name="close"/></button>
      </header>
      <div className="review-dialog-body">
        <p>Explain what needs to be corrected in <strong>{task.title}</strong>.</p>
        <label htmlFor="review-feedback">Reviewer instructions</label>
        <textarea
          id="review-feedback"
          autoFocus
          maxLength={MAX_FEEDBACK_LENGTH}
          rows={9}
          value={feedback}
          onChange={(event) => { setFeedback(event.target.value); setError(null); }}
          placeholder="Describe the problem, the required correction, and anything that must remain unchanged."
          aria-describedby="review-feedback-help review-feedback-count"
          aria-invalid={Boolean(error)}
        />
        <div className="review-feedback-meta">
          <span id="review-feedback-help">The agent will receive this feedback on the next iteration.</span>
          <span id="review-feedback-count">{feedback.length.toLocaleString()} / {MAX_FEEDBACK_LENGTH.toLocaleString()}</span>
        </div>
        {error && <p className="review-feedback-error" role="alert">{error}</p>}
      </div>
      <footer>
        <button className="btn-ghost" type="button" disabled={busy || submitting} onClick={onClose}>Cancel</button>
        <button className="btn-secondary" type="submit" disabled={busy || submitting}><Icon name="alert"/>{submitting ? "Requesting…" : "Request changes"}</button>
      </footer>
    </form>
  </div>;
}
