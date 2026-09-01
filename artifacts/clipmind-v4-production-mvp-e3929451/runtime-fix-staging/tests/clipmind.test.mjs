import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeSelection, createSelectionPayload } from '../src/selection.mjs';
import { validateDraft, sanitizeProviderDraft } from '../src/schema.mjs';
import { createNote, createStorageApi, normalizeStoredNote } from '../src/storage.mjs';
import { generateDraft } from '../src/generation.mjs';
import {
  createContextMenuClickHandler,
  createToolbarClickHandler,
  registerBackground,
  MENU_ID
} from '../src/background.mjs';
import { readFile } from 'node:fs/promises';

function memoryChrome() {
  const store = new Map();
  return {
    storage: {
      local: {
        async get(keys) {
          if (!keys) {
            return Object.fromEntries(store.entries());
          }
          if (Array.isArray(keys)) {
            return Object.fromEntries(keys.map((key) => [key, store.get(key)]));
          }
          return { [keys]: store.get(keys) };
        },
        async set(entries) {
          for (const [key, value] of Object.entries(entries)) {
            store.set(key, value);
          }
        }
      }
    }
  };
}

test('selection normalization trims whitespace and keeps excerpt', () => {
  const selection = normalizeSelection({ text: '  Hello   world  ', title: ' Tab ', url: 'https://example.com' });
  assert.equal(selection.text, 'Hello world');
  assert.equal(selection.source.pageTitle, 'Tab');
});

test('selection payload uses canonical source shape', () => {
  const payload = createSelectionPayload({ text: 'Hello', title: 'Page', url: 'https://example.com' });
  assert.equal(payload.source.pageTitle, 'Page');
  assert.equal(payload.source.url, 'https://example.com');
});

test('empty selection is rejected', () => {
  assert.equal(normalizeSelection({ text: '   ' }), null);
});

test('schema validation accepts valid structured draft', () => {
  const draft = validateDraft({
    summary: 'Summary',
    keyIdeas: ['Idea'],
    actionItems: ['Act'],
    importantFacts: ['Fact'],
    suggestedTags: ['tag']
  });
  assert.equal(draft.summary, 'Summary');
});

test('malformed output is rejected', () => {
  assert.throws(() => validateDraft({ summary: '', keyIdeas: 'bad' }), /summary|keyIdeas/i);
});

test('provider output cannot populate userNotes', () => {
  const draft = sanitizeProviderDraft({
    summary: 'Summary',
    keyIdeas: ['Idea'],
    actionItems: [],
    importantFacts: [],
    suggestedTags: [],
    userNotes: 'hack'
  });
  assert.equal('userNotes' in draft, false);
});

test('note creation includes stable fields and canonical source metadata', () => {
  const note = createNote({
    source: { text: 'Example text', title: 'Page', url: 'https://example.com' },
    draft: { summary: 'Summary', keyIdeas: [], actionItems: [], importantFacts: [], suggestedTags: [] },
    userNotes: 'Mine',
    tags: ['tag']
  });
  assert.ok(note.id);
  assert.equal(note.source.source.pageTitle, 'Page');
  assert.equal(note.source.excerpt, 'Example text');
});

test('local persistence round-trips canonical source metadata and selection text', async () => {
  const api = createStorageApi(memoryChrome());
  const saved = await api.saveNote(createNote({
    source: { text: 'Example text', title: 'Page', url: 'https://example.com' },
    draft: { summary: 'Summary', keyIdeas: [], actionItems: [], importantFacts: [], suggestedTags: [] },
    userNotes: 'Mine',
    tags: ['alpha']
  }));
  const history = await api.listNotes();
  const reopened = await api.getNote(saved.id);
  assert.equal(history.length, 1);
  assert.equal(reopened.id, saved.id);
  assert.equal(reopened.source.source.pageTitle, 'Page');
  assert.equal(reopened.source.source.url, 'https://example.com');
  assert.equal(reopened.source.text, 'Example text');
  assert.deepEqual(reopened.tags, ['alpha']);
});

test('backward-safe note normalization upgrades legacy source shape for reopen state', () => {
  const normalized = normalizeStoredNote({
    id: 'note-1',
    createdAt: '2024-01-01T00:00:00.000Z',
    updatedAt: '2024-01-01T00:00:00.000Z',
    source: {
      text: 'Legacy text',
      excerpt: 'Legacy text',
      title: 'Legacy Page',
      url: 'https://example.com/legacy',
      capturedAt: '2024-01-01T00:00:00.000Z'
    },
    summary: 'Summary',
    keyIdeas: [],
    actionItems: [],
    importantFacts: [],
    suggestedTags: ['legacy'],
    userNotes: 'Keep',
    tags: ['legacy']
  });

  assert.equal(normalized.source.source.pageTitle, 'Legacy Page');
  assert.equal(normalized.source.source.url, 'https://example.com/legacy');
  assert.equal(normalized.source.text, 'Legacy text');
});

test('selection normalization upgrades legacy source metadata shape', () => {
  const normalized = normalizeSelection({ text: 'Hello', title: 'Legacy title', url: 'https://example.com' });
  assert.equal(normalized.source.pageTitle, 'Legacy title');
  assert.equal(normalized.source.url, 'https://example.com');
});

test('saving an open note updates rather than duplicates', async () => {
  const api = createStorageApi(memoryChrome());
  const note = createNote({
    source: { text: 'Text', title: 'Page', url: 'https://example.com' },
    draft: { summary: 'Summary', keyIdeas: [], actionItems: [], importantFacts: [], suggestedTags: [] },
    userNotes: '',
    tags: []
  });
  const first = await api.saveNote(note);
  const second = await api.saveNote({ ...first, summary: 'Updated' });
  const history = await api.listNotes();
  assert.equal(history.length, 1);
  assert.equal(second.summary, 'Updated');
});

test('failure preservation keeps selection, draft, notes, and tags intact', async () => {
  const state = {
    selection: normalizeSelection({ text: 'Keep me', title: 'Page', url: 'https://example.com' }),
    draft: { summary: 'Old', keyIdeas: [], actionItems: [], importantFacts: [], suggestedTags: [] },
    userNotes: 'Persist',
    tags: ['keep']
  };
  await assert.rejects(() => generateDraft(state.selection, { mode: 'provider', endpoint: 'https://api.example.com' }, async () => ({ ok: false, status: 500, json: async () => ({}) })), /Provider error/);
  assert.equal(state.selection.text, 'Keep me');
  assert.equal(state.draft.summary, 'Old');
  assert.equal(state.userNotes, 'Persist');
  assert.deepEqual(state.tags, ['keep']);
});

test('deterministic local adapter returns validated draft', async () => {
  const selection = normalizeSelection({ text: 'Alpha. Beta. Gamma.', title: 'Page', url: 'https://example.com' });
  const draft = await generateDraft(selection, { mode: 'local-demo' });
  assert.ok(draft.summary.length > 0);
  assert.ok(Array.isArray(draft.keyIdeas));
});

test('built artifacts do not contain credentials', async () => {
  const files = ['manifest.json', 'src/generation.mjs'];
  for (const file of files) {
    const content = await readFile(new URL(`../${file}`, import.meta.url), 'utf8');
    assert.doesNotMatch(content, /(api[_-]?key|secret|token\s*[:=])/i);
  }
});

test('background selection imports are exported by selection module', async () => {
  const background = await readFile(new URL('../src/background.mjs', import.meta.url), 'utf8');
  const selection = await readFile(new URL('../src/selection.mjs', import.meta.url), 'utf8');

  const importMatch = background.match(/import\s*\{([^}]+)\}\s*from\s*['"]\.\/selection\.mjs['"]/);
  assert.ok(importMatch, 'background should import from ./selection.mjs');

  const importedNames = importMatch[1].split(',').map((name) => name.trim()).filter(Boolean);
  for (const name of importedNames) {
    assert.match(selection, new RegExp(`export\\s+function\\s+${name}\\b|export\\s+const\\s+${name}\\b`), `selection.mjs should export ${name}`);
  }
});

test('context-menu handler opens the Side Panel before persistence settles', async () => {
  const calls = [];
  let settlePersistence;
  const persistence = new Promise((resolve) => {
    settlePersistence = resolve;
  });
  const errors = [];
  const handler = createContextMenuClickHandler({
    storageApi: {
      saveCurrentSelection(selection) {
        calls.push(['storage-start', selection.text]);
        return persistence;
      }
    },
    sidePanelApi: {
      open(options) {
        calls.push(['side-panel-open', options.windowId]);
        return Promise.resolve();
      }
    },
    logger: {
      error(message, details) {
        errors.push({ message, details });
      }
    }
  });

  const returnValue = handler(
    {
      menuItemId: MENU_ID,
      selectionText: 'Selected text',
      pageUrl: 'https://example.com/article'
    },
    {
      title: 'Article',
      windowId: 42
    }
  );

  calls.push(['handler-returned']);
  assert.equal(returnValue, undefined, 'the user-action callback must not yield a promise');
  assert.deepEqual(calls, [
    ['storage-start', 'Selected text'],
    ['side-panel-open', 42],
    ['handler-returned']
  ]);
  assert.deepEqual(errors, []);

  settlePersistence();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(errors, []);
});

test('context-menu handler settles storage and Side Panel failures without unhandled rejection', async () => {
  const errors = [];
  const handler = createContextMenuClickHandler({
    storageApi: {
      saveCurrentSelection() {
        return Promise.reject(new Error('sensitive storage detail'));
      }
    },
    sidePanelApi: {
      open() {
        return Promise.reject(new Error('sensitive browser detail'));
      }
    },
    logger: {
      error(message, details) {
        errors.push({ message, details });
      }
    }
  });

  assert.doesNotThrow(() => handler(
    { menuItemId: MENU_ID, selectionText: 'Selected text' },
    { title: 'Page', windowId: 7 }
  ));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(errors.map((entry) => entry.details.code), [
    'SELECTION_STORAGE_FAILED',
    'SIDE_PANEL_OPEN_FAILED'
  ]);
  assert.doesNotMatch(JSON.stringify(errors), /sensitive (storage|browser) detail/);
});

test('background registers the toolbar action listener without clearing storage', () => {
  const listeners = {};
  const storageCalls = [];
  const chromeApi = {
    storage: {
      local: {
        get() {
          storageCalls.push('get');
          return Promise.resolve({});
        },
        set() {
          storageCalls.push('set');
          return Promise.resolve();
        },
        clear() {
          storageCalls.push('clear');
          return Promise.resolve();
        }
      }
    },
    runtime: {
      onInstalled: { addListener(listener) { listeners.installed = listener; } },
      onMessage: { addListener(listener) { listeners.message = listener; } }
    },
    contextMenus: {
      create() {},
      onClicked: { addListener(listener) { listeners.contextMenu = listener; } }
    },
    action: {
      onClicked: { addListener(listener) { listeners.toolbar = listener; } }
    },
    sidePanel: {
      open() {
        return Promise.resolve();
      }
    }
  };

  registerBackground(chromeApi, { error() {} });

  assert.equal(typeof listeners.toolbar, 'function');
  assert.equal(typeof listeners.contextMenu, 'function');
  assert.equal(typeof listeners.message, 'function');
  assert.deepEqual(storageCalls, [], 'registration must not read, write, or clear saved history');
});

test('toolbar click opens the Side Panel synchronously without requiring a selection', () => {
  const calls = [];
  const handler = createToolbarClickHandler({
    sidePanelApi: {
      open(options) {
        calls.push(['side-panel-open', options.windowId]);
        return Promise.resolve();
      }
    },
    logger: { error() { calls.push(['error']); } }
  });

  const returnValue = handler({ windowId: 73 });
  calls.push(['handler-returned']);

  assert.equal(returnValue, undefined, 'toolbar user-action callback must not yield a promise');
  assert.deepEqual(calls, [
    ['side-panel-open', 73],
    ['handler-returned']
  ]);
});

test('toolbar click handles Side Panel rejection with safe structured logging', async () => {
  const errors = [];
  const handler = createToolbarClickHandler({
    sidePanelApi: {
      open() {
        return Promise.reject(new Error('sensitive browser detail'));
      }
    },
    logger: {
      error(message, details) {
        errors.push({ message, details });
      }
    }
  });

  assert.doesNotThrow(() => handler({ windowId: 91 }));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(errors.map((entry) => entry.details.code), ['SIDE_PANEL_OPEN_FAILED']);
  assert.doesNotMatch(JSON.stringify(errors), /sensitive browser detail/);
});

test('sidepanel selectors resolve to ids in sidepanel.html', async () => {
  const sidepanel = await readFile(new URL('../src/sidepanel.mjs', import.meta.url), 'utf8');
  const html = await readFile(new URL('../sidepanel.html', import.meta.url), 'utf8');

  const selectorMatches = [...sidepanel.matchAll(/querySelector\('#([^']+)'\)/g)];
  const ids = new Set(selectorMatches.map((match) => match[1]));
  const expectedIds = ['app', 'status', 'sourceExcerpt', 'sourceMeta', 'historyList', 'emptySelection', 'editor', 'formView', 'historyView', 'emptyHistoryMessage', 'generateBtn', 'retryBtn', 'saveBtn', 'historyBtn', 'backBtn', 'summary', 'keyIdeas', 'actionItems', 'importantFacts', 'userNotes', 'suggestedTags'];

  assert.ok(ids.size > 0, 'sidepanel should query id selectors');
  assert.deepEqual([...ids].sort(), [...expectedIds].sort());

  for (const id of ids) {
    assert.match(html, new RegExp(`id=["']${id}["']`), `sidepanel.html should contain #${id}`);
  }
});
