import { normalizeSelection } from './selection.mjs';
import { sanitizeProviderDraft, validateDraft } from './schema.mjs';

export const GENERATION_ERRORS = {
  EMPTY_SELECTION: 'empty-selection',
  INVALID_RESPONSE: 'invalid-response',
  PROVIDER_ERROR: 'provider-error',
  TIMEOUT: 'timeout'
};

export function createDemoDraft(selection) {
  const normalized = normalizeSelection(selection);
  if (!normalized) {
    const error = new Error('No selection available');
    error.code = GENERATION_ERRORS.EMPTY_SELECTION;
    throw error;
  }

  const sentences = normalized.text
    .split(/(?<=[.!?])\s+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const words = normalized.text.split(/\s+/).filter(Boolean);

  return validateDraft({
    summary: sentences[0] || normalized.excerpt,
    keyIdeas: sentences.slice(0, 3),
    actionItems: words.length > 24 ? ['Review the selected text and confirm the generated actions.'] : [],
    importantFacts: [`Characters: ${normalized.text.length}`, `Words: ${words.length}`],
    suggestedTags: Array.from(new Set((normalized.text.toLowerCase().match(/[a-z]{4,}/g) || []).slice(0, 5)))
  });
}

export async function requestStructuredDraft({ selection, endpoint, fetchImpl = globalThis.fetch, timeoutMs = 8000 }) {
  const normalized = normalizeSelection(selection);
  if (!normalized) {
    const error = new Error('No selection available');
    error.code = GENERATION_ERRORS.EMPTY_SELECTION;
    throw error;
  }

  if (!endpoint) {
    return createDemoDraft(normalized);
  }

  if (typeof fetchImpl !== 'function') {
    const error = new Error('Fetch is unavailable');
    error.code = GENERATION_ERRORS.PROVIDER_ERROR;
    throw error;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetchImpl(endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        selection: normalized.text,
        metadata: normalized.source
      }),
      signal: controller.signal
    });

    if (!response.ok) {
      const error = new Error(`Provider error: ${response.status}`);
      error.code = GENERATION_ERRORS.PROVIDER_ERROR;
      throw error;
    }

    const json = await response.json();
    return validateDraft(sanitizeProviderDraft(json));
  } catch (error) {
    if (error?.name === 'AbortError') {
      const timeoutError = new Error('Provider request timed out');
      timeoutError.code = GENERATION_ERRORS.TIMEOUT;
      throw timeoutError;
    }

    if (error?.code === GENERATION_ERRORS.EMPTY_SELECTION || error?.code === GENERATION_ERRORS.INVALID_RESPONSE) {
      throw error;
    }

    const providerError = new Error(error?.message || 'Provider error');
    providerError.code = GENERATION_ERRORS.PROVIDER_ERROR;
    throw providerError;
  } finally {
    clearTimeout(timeout);
  }
}

export async function generateDraft(selection, config = {}, fetchImpl) {
  const mode = config.mode || 'local-demo';
  if (mode === 'local-demo') {
    return createDemoDraft(selection);
  }

  return requestStructuredDraft({
    selection,
    endpoint: config.endpoint,
    timeoutMs: config.timeoutMs,
    fetchImpl
  });
}
