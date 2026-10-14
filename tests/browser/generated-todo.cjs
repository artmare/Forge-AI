/* Open exactly the built artifacts of the UI-created Todo Mission. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const { api, output } = require('./reliability.cjs');
const { chromium } = require('playwright');

(async () => {
  const id = (await fs.readFile(path.join(output, 'todo-mission-id.txt'), 'utf8')).trim();
  const mission = await api(`missions/${id}`);
  const directory = path.join(output, 'generated-todo');
  await fs.mkdir(directory, { recursive: true });
  const files = new Map();
  for (const [name, type] of [['index.html', 'text/html'], ['styles.css', 'text/css'], ['app.mjs', 'text/javascript'], ['model.mjs', 'text/javascript']]) {
    const artifact = await api(`projects/${mission.project_id}/artifacts/content?path=dist/${name}`);
    files.set(`/${name}`, { content: artifact.content, type });
    await fs.writeFile(path.join(directory, name), artifact.content);
  }
  const server = http.createServer((request, response) => {
    const file = files.get(request.url === '/' ? '/index.html' : request.url);
    if (!file) { response.writeHead(request.url === '/favicon.ico' ? 204 : 404); response.end(); return; }
    response.writeHead(200, { 'content-type': file.type }); response.end(file.content);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.getByText('No tasks yet.', { exact: true }).waitFor();
    await page.getByLabel('New task', { exact: true }).fill('Ship Forge');
    await page.getByRole('button', { name: 'Add task', exact: true }).click();
    await page.getByText('1 remaining', { exact: true }).waitFor();
    await page.getByRole('checkbox', { name: 'Ship Forge' }).check();
    await page.getByText('0 remaining', { exact: true }).waitFor();
    assert.equal(await page.getByRole('checkbox', { name: 'Ship Forge' }).isChecked(), true);
    await page.screenshot({ path: path.join(output, 'generated-todo-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: path.join(output, 'generated-todo-mobile.png'), fullPage: true });
    await page.getByRole('button', { name: 'Delete Ship Forge', exact: true }).click();
    await page.getByText('No tasks yet.', { exact: true }).waitFor();
    assert.equal(await page.getByRole('checkbox').count(), 0);
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(output, 'generated-todo-evidence.json'), JSON.stringify({ mission_id: id, project_id: mission.project_id, source: 'unmodified dist artifacts fetched from Forge', checks: ['empty state', 'add', 'complete', 'delete', '390px no horizontal overflow'], errors }, null, 2));
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
