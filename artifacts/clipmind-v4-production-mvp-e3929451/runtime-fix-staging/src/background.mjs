import { createSelectionPayload } from './selection.mjs';
import { createStorageApi } from './storage.mjs';

export const MENU_ID = 'clipmind-capture-selection';

function reportFailure(logger, code, message, error) {
  logger?.error?.('ClipMind background operation failed', {
    code,
    message,
    errorName: typeof error?.name === 'string' ? error.name : 'Error'
  });
}

export function createContextMenuClickHandler({ storageApi, sidePanelApi, logger = console }) {
  return (info, tab) => {
    if (info?.menuItemId !== MENU_ID) {
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

    let persistencePromise;
    try {
      // Start persistence in the same event turn, but do not await it before
      // sidePanel.open or the browser will discard the user gesture.
      persistencePromise = Promise.resolve(storageApi.saveCurrentSelection(selection));
    } catch (error) {
      persistencePromise = Promise.reject(error);
    }

    let openPromise = Promise.resolve();
    if (tab?.windowId !== undefined) {
      try {
        // This call must remain synchronous with contextMenus.onClicked.
        openPromise = Promise.resolve(sidePanelApi.open({ windowId: tab.windowId }));
      } catch (error) {
        openPromise = Promise.reject(error);
      }
    }

    void Promise.allSettled([persistencePromise, openPromise]).then(([persistenceResult, openResult]) => {
      if (persistenceResult.status === 'rejected') {
        reportFailure(
          logger,
          'SELECTION_STORAGE_FAILED',
          'The captured selection could not be stored.',
          persistenceResult.reason
        );
      }
      if (openResult.status === 'rejected') {
        reportFailure(
          logger,
          'SIDE_PANEL_OPEN_FAILED',
          'The Side Panel could not be opened.',
          openResult.reason
        );
      }
    });
  };
}

export function createToolbarClickHandler({ sidePanelApi, logger = console }) {
  return (tab) => {
    if (tab?.windowId === undefined) {
      reportFailure(
        logger,
        'SIDE_PANEL_OPEN_UNAVAILABLE',
        'The active browser window is unavailable.',
        new Error('Missing windowId')
      );
      return;
    }

    let openPromise;
    try {
      // Keep this call in the original chrome.action user-gesture turn.
      openPromise = Promise.resolve(sidePanelApi.open({ windowId: tab.windowId }));
    } catch (error) {
      openPromise = Promise.reject(error);
    }

    void openPromise.catch((error) => {
      reportFailure(
        logger,
        'SIDE_PANEL_OPEN_FAILED',
        'The Side Panel could not be opened.',
        error
      );
    });
  };
}

export function registerBackground(chromeApi = globalThis.chrome, logger = console) {
  if (!chromeApi) {
    return;
  }

  const storageApi = createStorageApi(chromeApi);

  chromeApi.runtime.onInstalled.addListener(() => {
    chromeApi.contextMenus.create({
      id: MENU_ID,
      title: 'Send selection to ClipMind',
      contexts: ['selection']
    });
  });

  chromeApi.contextMenus.onClicked.addListener(createContextMenuClickHandler({
    storageApi,
    sidePanelApi: chromeApi.sidePanel,
    logger
  }));

  chromeApi.action.onClicked.addListener(createToolbarClickHandler({
    sidePanelApi: chromeApi.sidePanel,
    logger
  }));

  chromeApi.runtime.onMessage.addListener((message, _sender, sendResponse) => {
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
}

registerBackground();
