import { generateDraft, GENERATION_ERRORS } from './generation.mjs';
import { createEmptyDraft, validateDraft, sanitizeUserTags } from './schema.mjs';
import { createStorageApi, createNote, STORAGE_KEYS, normalizeSelectionSource, normalizeStoredNote } from './storage.mjs';

const storageApi = createStorageApi(chrome);
const endpoint = globalThis.CLIPMIND_ENDPOINT || '';

const fields = {
  summary: document.querySelector('#summary'),
  keyIdeas: document.querySelector('#keyIdeas'),
  actionItems: document.querySelector('#actionItems'),
  importantFacts: document.querySelector('#importantFacts'),
  userNotes: document.querySelector('#userNotes'),
  suggestedTags: document.querySelector('#suggestedTags')
};

const app = document.querySelector('#app');
const statusEl = document.querySelector('#status');
const sourceEl = document.querySelector('#sourceExcerpt');
const sourceMetaEl = document.querySelector('#sourceMeta');
const historyEl = document.querySelector('#historyList');
const emptySelectionEl = document.querySelector('#emptySelection');
const editorEl = document.querySelector('#editor');
const formViewEl = document.querySelector('#formView');
const historyViewEl = document.querySelector('#historyView');
const emptyHistoryMessageEl = document.querySelector('#emptyHistoryMessage');
const generateBtn = document.querySelector('#generateBtn');
const retryBtn = document.querySelector('#retryBtn');
const saveBtn = document.querySelector('#saveBtn');
const historyBtn = document.querySelector('#historyBtn');
const backBtn = document.querySelector('#backBtn');

const state = {
  selection: null,
  currentNoteId: null,
  createdAt: null,
  draft: createEmptyDraft(),
  userNotes: '',
  tags: [],
  historyOpen: false
};

function setStatus(mode, message) {
  app.dataset.state = mode;
  statusEl.textContent = message;
  statusEl.classList.toggle('error', /failed|invalid/i.test(mode));
}

function arrayToLines(items) {
  return (items || []).join('\n');
}

function linesToArray(value) {
  return value.split('\n').map((item) => item.trim()).filter(Boolean);
}

function tagsToArray(value) {
  return sanitizeUserTags(value.split(',').map((item) => item.trim()));
}

function updateView() {
  const hasSelection = Boolean(state.selection?.text);
  emptySelectionEl.hidden = hasSelection;
  editorEl.hidden = !hasSelection;
  formViewEl.hidden = state.historyOpen;
  historyViewEl.hidden = !state.historyOpen;
  backBtn.hidden = !state.historyOpen;
  historyBtn.hidden = state.historyOpen;
}

function renderForm() {
  fields.summary.value = state.draft.summary || '';
  fields.keyIdeas.value = arrayToLines(state.draft.keyIdeas);
  fields.actionItems.value = arrayToLines(state.draft.actionItems);
  fields.importantFacts.value = arrayToLines(state.draft.importantFacts);
  fields.userNotes.value = state.userNotes || '';
  fields.suggestedTags.value = state.tags.join(', ');
  sourceEl.textContent = state.selection?.excerpt || 'No selection captured yet.';
  sourceMetaEl.textContent = state.selection?.source?.pageTitle || state.selection?.source?.url
    ? [state.selection?.source?.pageTitle, state.selection?.source?.url].filter(Boolean).join(' — ')
    : '';
  updateView();
}

function syncStateFromForm() {
  state.userNotes = fields.userNotes.value;
  state.tags = tagsToArray(fields.suggestedTags.value);
  state.draft = {
    summary: fields.summary.value.trim(),
    keyIdeas: linesToArray(fields.keyIdeas.value),
    actionItems: linesToArray(fields.actionItems.value),
    importantFacts: linesToArray(fields.importantFacts.value),
    suggestedTags: tagsToArray(fields.suggestedTags.value)
  };
}

async function loadSelection() {
  state.historyOpen = false;
  const result = await chrome.storage.local.get([STORAGE_KEYS.currentSelection, STORAGE_KEYS.currentDraft]);
  state.selection = result[STORAGE_KEYS.currentSelection]
    ? normalizeSelectionSource(result[STORAGE_KEYS.currentSelection])
    : null;
  const savedDraft = result[STORAGE_KEYS.currentDraft] ? normalizeStoredNote(result[STORAGE_KEYS.currentDraft]) : null;
  if (savedDraft) {
    state.currentNoteId = savedDraft.id || null;
    state.createdAt = savedDraft.createdAt || null;
    state.draft = validateDraft({
      summary: savedDraft.summary || '',
      keyIdeas: savedDraft.keyIdeas || [],
      actionItems: savedDraft.actionItems || [],
      importantFacts: savedDraft.importantFacts || [],
      suggestedTags: savedDraft.suggestedTags || []
    });
    state.userNotes = savedDraft.userNotes || '';
    state.tags = sanitizeUserTags(savedDraft.tags || savedDraft.suggestedTags || []);
    if (!state.selection?.text && savedDraft.source?.text) {
      state.selection = savedDraft.source;
    }
  }

  renderForm();
  if (!state.selection?.text) {
    setStatus('no-selection', 'No selection captured yet.');
  } else {
    setStatus('ready', 'Selection ready for generation.');
  }
}

async function renderHistory() {
  state.historyOpen = true;
  updateView();
  const notes = await storageApi.listNotes();
  historyEl.innerHTML = '';
  emptyHistoryMessageEl.hidden = notes.length > 0;
  if (!notes.length) {
    setStatus('empty-history', 'No saved notes yet.');
    return;
  }

  setStatus('populated-history', 'Saved notes. Select one to reopen.');
  for (const note of notes) {
    const item = document.createElement('li');
    item.className = 'history-item';
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = `${note.summary || 'Untitled note'} — ${new Date(note.updatedAt).toLocaleString()}`;
    button.addEventListener('click', () => reopenNote(note.id));
    item.appendChild(button);
    historyEl.appendChild(item);
  }
}

async function reopenNote(id) {
  const note = await storageApi.getNote(id);
  if (!note) return;
  state.historyOpen = false;
  state.currentNoteId = note.id;
  state.createdAt = note.createdAt;
  state.selection = normalizeSelectionSource(note.source);
  state.draft = validateDraft({
    summary: note.summary,
    keyIdeas: note.keyIdeas,
    actionItems: note.actionItems,
    importantFacts: note.importantFacts,
    suggestedTags: note.suggestedTags
  });
  state.userNotes = note.userNotes || '';
  state.tags = sanitizeUserTags(note.tags || note.suggestedTags || []);
  await storageApi.saveCurrentSelection(state.selection);
  await storageApi.saveDraftState(note);
  renderForm();
  setStatus('editable', 'Saved note reopened.');
}

async function handleGenerate() {
  syncStateFromForm();
  if (!state.selection?.text) {
    setStatus('no-selection', 'Select text first, then use the ClipMind context menu.');
    return;
  }

  setStatus('generating', 'Generating draft…');
  try {
    const draft = await generateDraft(state.selection, endpoint ? { mode: 'provider', endpoint } : { mode: 'local-demo' });
    state.draft = draft;
    state.tags = sanitizeUserTags([...state.tags, ...draft.suggestedTags]);
    renderForm();
    setStatus('editable', 'Draft generated. Review and edit before saving.');
  } catch (error) {
    renderForm();
    if (error?.code === GENERATION_ERRORS.INVALID_RESPONSE) {
      setStatus('invalid-response', 'Provider response was invalid. You can retry or keep editing.');
    } else if (error?.code === GENERATION_ERRORS.TIMEOUT) {
      setStatus('generation-failed', 'Generation timed out. Retry when ready.');
    } else {
      setStatus('generation-failed', 'Generation failed. Your edits were preserved.');
    }
  }
}

async function handleSave() {
  syncStateFromForm();
  if (!state.selection?.text) {
    setStatus('save-failed', 'Nothing to save yet.');
    return;
  }

  setStatus('saving', 'Saving note…');
  try {
    const note = createNote({
      id: state.currentNoteId,
      createdAt: state.createdAt,
      source: state.selection,
      draft: state.draft,
      userNotes: state.userNotes,
      tags: state.tags
    });
    const saved = await storageApi.saveNote(note);
    state.currentNoteId = saved.id;
    state.createdAt = saved.createdAt;
    state.selection = saved.source;
    await storageApi.saveDraftState(saved);
    await storageApi.saveCurrentSelection(saved.source);
    setStatus('saved', 'Note saved locally.');
    await renderHistory();
  } catch {
    setStatus('save-failed', 'Save failed. Your edits were preserved.');
  }
}

generateBtn.addEventListener('click', handleGenerate);
retryBtn.addEventListener('click', handleGenerate);
saveBtn.addEventListener('click', handleSave);
historyBtn.addEventListener('click', renderHistory);
backBtn.addEventListener('click', loadSelection);

loadSelection();
