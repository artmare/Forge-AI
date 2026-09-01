import { sanitizeUserTags, validateDraft } from './schema.mjs';

export const STORAGE_KEYS = {
  currentSelection: 'clipmind.currentSelection',
  currentDraft: 'clipmind.currentDraft',
  notes: 'clipmind.notes'
};

export function normalizeSourceMetadata(source = {}) {
  const pageTitle = String(
    source.pageTitle
    ?? source.title
    ?? source.source?.pageTitle
    ?? source.source?.title
    ?? ''
  ).trim();
  const url = String(source.url ?? source.source?.url ?? '').trim();

  return {
    pageTitle,
    url
  };
}

export function normalizeSelectionSource(source = {}) {
  const normalizedMeta = normalizeSourceMetadata(source);
  const text = String(source.text ?? '').trim();
  const excerptSource = String(source.excerpt ?? text).trim();
  const excerpt = excerptSource.length > 280 ? `${excerptSource.slice(0, 277)}...` : excerptSource;

  return {
    text,
    excerpt,
    wordCount: Number.isFinite(source.wordCount)
      ? source.wordCount
      : text.split(/\s+/).filter(Boolean).length,
    capturedAt: source.capturedAt || new Date().toISOString(),
    source: normalizedMeta
  };
}

export function normalizeStoredNote(note) {
  if (!note || typeof note !== 'object') {
    return null;
  }

  const normalizedSource = normalizeSelectionSource(note.source || {});

  return {
    ...note,
    source: normalizedSource,
    summary: typeof note.summary === 'string' ? note.summary : '',
    keyIdeas: Array.isArray(note.keyIdeas) ? note.keyIdeas : [],
    actionItems: Array.isArray(note.actionItems) ? note.actionItems : [],
    importantFacts: Array.isArray(note.importantFacts) ? note.importantFacts : [],
    suggestedTags: Array.isArray(note.suggestedTags) ? note.suggestedTags : [],
    userNotes: typeof note.userNotes === 'string' ? note.userNotes : '',
    tags: sanitizeUserTags(note.tags || note.suggestedTags || [])
  };
}

export function createNote({ source, draft, userNotes = '', tags = [], id, createdAt, updatedAt }) {
  const validated = validateDraft(draft);
  const now = updatedAt || new Date().toISOString();
  const normalizedSource = normalizeSelectionSource({ ...source, capturedAt: source?.capturedAt || now });

  return {
    id: id || `note-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    createdAt: createdAt || now,
    updatedAt: now,
    source: normalizedSource,
    summary: validated.summary,
    keyIdeas: validated.keyIdeas,
    actionItems: validated.actionItems,
    importantFacts: validated.importantFacts,
    suggestedTags: validated.suggestedTags,
    userNotes: typeof userNotes === 'string' ? userNotes : '',
    tags: sanitizeUserTags(tags)
  };
}

export function createStorageApi(chromeLike = chrome) {
  const storage = chromeLike.storage.local;

  async function listNotes() {
    const result = await storage.get(STORAGE_KEYS.notes);
    const notes = Array.isArray(result[STORAGE_KEYS.notes]) ? result[STORAGE_KEYS.notes] : [];
    return notes.map((note) => normalizeStoredNote(note)).filter(Boolean);
  }

  async function getNote(id) {
    const notes = await listNotes();
    return notes.find((note) => note.id === id) || null;
  }

  async function saveCurrentSelection(selection) {
    const normalized = selection ? normalizeSelectionSource(selection) : null;
    await storage.set({ [STORAGE_KEYS.currentSelection]: normalized });
    return normalized;
  }

  async function loadCurrentSelection() {
    const result = await storage.get(STORAGE_KEYS.currentSelection);
    const selection = result[STORAGE_KEYS.currentSelection];
    return selection ? normalizeSelectionSource(selection) : null;
  }

  async function saveDraftState(draftState) {
    const normalized = draftState ? normalizeStoredNote(draftState) : null;
    await storage.set({ [STORAGE_KEYS.currentDraft]: normalized });
    return normalized;
  }

  async function loadDraftState() {
    const result = await storage.get(STORAGE_KEYS.currentDraft);
    const draftState = result[STORAGE_KEYS.currentDraft];
    return draftState ? normalizeStoredNote(draftState) : null;
  }

  async function saveNote(note) {
    const notes = await listNotes();
    const next = normalizeStoredNote({ ...note, tags: sanitizeUserTags(note.tags) });
    const index = notes.findIndex((entry) => entry.id === next.id);

    if (index >= 0) {
      next.createdAt = notes[index].createdAt;
      notes[index] = next;
    } else {
      notes.unshift(next);
    }

    await storage.set({
      [STORAGE_KEYS.notes]: notes,
      [STORAGE_KEYS.currentDraft]: next
    });

    return next;
  }

  return {
    listNotes,
    getNote,
    saveNote,
    saveCurrentSelection,
    loadCurrentSelection,
    saveDraftState,
    loadDraftState
  };
}
