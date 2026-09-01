export function normalizeSelection(input) {
  const rawText = typeof input === 'string' ? input : input?.text ?? '';
  const text = String(rawText).replace(/\r\n/g, '\n').trim();
  if (!text) {
    return null;
  }

  const collapsed = text.replace(/\s+/g, ' ').trim();
  const excerpt = collapsed.length > 240 ? `${collapsed.slice(0, 237)}...` : collapsed;
  const pageTitle = typeof input === 'object' && input
    ? String(input.pageTitle ?? input.title ?? '').trim()
    : '';
  const url = typeof input === 'object' && input ? String(input.url ?? '').trim() : '';

  return {
    text: collapsed,
    excerpt,
    wordCount: collapsed.split(/\s+/).filter(Boolean).length,
    source: {
      pageTitle,
      url
    }
  };
}

export function createSelectionPayload(selection, metadata = {}) {
  const normalized = normalizeSelection({
    text: selection?.text ?? selection ?? '',
    pageTitle: selection?.source?.pageTitle ?? selection?.pageTitle ?? selection?.title ?? metadata.pageTitle ?? '',
    url: selection?.source?.url ?? selection?.url ?? metadata.pageUrl ?? ''
  });

  return {
    hasSelection: Boolean(normalized),
    text: normalized?.text ?? '',
    excerpt: normalized?.excerpt ?? '',
    wordCount: normalized?.wordCount ?? 0,
    capturedAt: new Date().toISOString(),
    source: {
      pageTitle: normalized?.source?.pageTitle ?? String(metadata.pageTitle ?? '').trim(),
      url: normalized?.source?.url ?? String(metadata.pageUrl ?? '').trim()
    }
  };
}
