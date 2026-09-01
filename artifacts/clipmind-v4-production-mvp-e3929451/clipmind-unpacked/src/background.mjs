import { createSelectionPayload } from './selection.mjs';
import { createStorageApi } from './storage.mjs';

const MENU_ID = 'clipmind-capture-selection';
const storageApi = createStorageApi(chrome);

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: MENU_ID,
    title: 'Send selection to ClipMind',
    contexts: ['selection']
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== MENU_ID) {
    return;
  }

  const selection = createSelectionPayload(
    {
      text: info.selectionText ?? '',
      title: tab?.title ?? '',
      url: info.pageUrl ?? tab?.url ?? ''
    },
    {
      pageTitle: tab?.title ?? '',
      pageUrl: info.pageUrl ?? tab?.url ?? ''
    }
  );

  await storageApi.saveCurrentSelection(selection);

  if (tab?.windowId !== undefined) {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'clipmind:get-selection') {
    storageApi.loadCurrentSelection()
      .then((selection) => sendResponse({ ok: true, selection }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message?.type === 'clipmind:has-selection') {
    storageApi.loadCurrentSelection()
      .then((selection) => sendResponse({ ok: true, hasSelection: Boolean(selection?.hasSelection && selection?.text) }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  return false;
});
