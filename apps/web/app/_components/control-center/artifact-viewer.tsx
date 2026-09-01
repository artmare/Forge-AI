"use client";

import { useEffect, useMemo, useState } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { forge, ForgeRequestError } from "./helpers";
import type { ArtifactContent, ArtifactMetadata } from "./types";
import { Icon, relativeTime } from "./ui";

interface Props {
  projectId: string | null;
  artifact: ArtifactMetadata | null;
  onClose: () => void;
}

export function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function previewError(error: unknown) {
  if (error instanceof ForgeRequestError) {
    if (error.code === "ARTIFACT_TOO_LARGE") return "This file is too large to preview.";
    if (["ARTIFACT_UNSUPPORTED_TYPE", "ARTIFACT_NOT_UTF8"].includes(error.code)) {
      return "Preview is not available for this file type.";
    }
  }
  return "Could not load this artifact.";
}

function displaySource(content: ArtifactContent) {
  if (content.preview_kind !== "json") return content.content;
  try {
    return JSON.stringify(JSON.parse(content.content), null, 2);
  } catch {
    return content.content;
  }
}

function safeUrlTransform(url: string) {
  if (url.startsWith("#")) return url;
  try {
    const parsed = new URL(url);
    if (["http:", "https:", "mailto:"].includes(parsed.protocol)) {
      return defaultUrlTransform(url);
    }
  } catch {
    return "";
  }
  return "";
}

export function ArtifactViewer({ projectId, artifact, onClose }: Props) {
  const [content, setContent] = useState<ArtifactContent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"rendered" | "source">(
    artifact?.preview_kind === "markdown" ? "rendered" : "source",
  );

  useEffect(() => {
    let active = true;
    if (!artifact || !projectId || !artifact.previewable) return () => { active = false; };
    const query = new URLSearchParams({ path: artifact.path });
    void forge<ArtifactContent>(`projects/${projectId}/artifacts/content?${query}`)
      .then((result) => { if (active) setContent(result); })
      .catch((cause: unknown) => { if (active) setError(previewError(cause)); });
    return () => { active = false; };
  }, [artifact, projectId]);

  useEffect(() => {
    if (!artifact) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [artifact, onClose]);

  const source = useMemo(() => content ? displaySource(content) : "", [content]);
  if (!artifact) return null;

  return <div className="artifact-viewer-layer" role="dialog" aria-modal="true" aria-labelledby="artifact-viewer-title">
    <button className="artifact-viewer-backdrop" onClick={onClose} aria-label="Close artifact viewer"/>
    <section className="artifact-viewer">
      <header className="artifact-viewer-header">
        <div className="min-w-0">
          <p className="eyebrow">Read-only artifact</p>
          <h2 id="artifact-viewer-title">{artifact.name}</h2>
          <p>{artifact.file_type} · {formatBytes(artifact.size_bytes)} · Modified {relativeTime(artifact.modified_at)}</p>
        </div>
        <button className="icon-button" onClick={onClose} aria-label="Close artifact viewer"><Icon name="close"/></button>
      </header>
      <div className="artifact-viewer-toolbar">
        <span className="truncate">{artifact.path}</span>
        {artifact.preview_kind === "markdown" && <div className="artifact-view-toggle" role="tablist" aria-label="Artifact view">
          <button role="tab" aria-selected={mode === "rendered"} onClick={() => setMode("rendered")}>Rendered</button>
          <button role="tab" aria-selected={mode === "source"} onClick={() => setMode("source")}>Source</button>
        </div>}
      </div>
      <div className="artifact-viewer-body">
        {!artifact.previewable && <div className="artifact-preview-message"><Icon name="file"/><strong>Preview is not available for this file type.</strong><p>Forge will not execute or interpret this artifact.</p></div>}
        {artifact.previewable && !content && !error && <div aria-label="Loading artifact" className="artifact-preview-loading"><div/><div/><div/></div>}
        {error && <div role="alert" className="artifact-preview-message artifact-preview-error"><Icon name="alert"/><strong>{error}</strong><p>The artifact was not executed and no internal path was exposed.</p></div>}
        {content && mode === "rendered" && content.preview_kind === "markdown" && <article className="markdown-document" data-testid="rendered-markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeSanitize]}
            urlTransform={safeUrlTransform}
            components={{
              a: ({ children, href, title }) => <a href={href} title={title} target={href?.startsWith("#") ? undefined : "_blank"} rel="noopener noreferrer">{children}</a>,
              img: ({ alt }) => <span className="markdown-image-placeholder">[Image: {alt || "not loaded"}]</span>,
            }}
          >{content.content}</ReactMarkdown>
        </article>}
        {content && (mode === "source" || content.preview_kind !== "markdown") && <pre className="artifact-source" data-testid="artifact-source"><code>{source}</code></pre>}
      </div>
    </section>
  </div>;
}
