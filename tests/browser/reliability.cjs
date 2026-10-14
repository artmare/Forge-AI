/* Live Compose smoke test. Install Playwright on the host; never mocks API traffic.
 * Usage: node tests/browser/reliability.cjs [navigation|brief|todo|invalid]
 * Only creates audit Missions and uses public Control Center actions.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const output = path.resolve('artifacts/reliability-audit');
const base = process.env.FORGE_WEB_URL || 'http://localhost:3000';

async function navigation(page) {
  await page.keyboard.press('Escape');
  const visits = [];
  for (const name of ['Dashboard', 'Missions', 'Companies', 'Projects', 'Agents', 'Artifacts', 'Tasks', 'Activity', 'System', 'Settings']) {
    await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: new RegExp(`^${name}`) }).click();
    await page.waitForTimeout(400);
    visits.push({ name, url: page.url(), text: await page.locator('main').innerText() });
    await page.screenshot({ path: path.join(output, `page-${name.toLowerCase()}.png`), fullPage: true });
  }
  const pause = page.getByRole('button', { name: 'Pause autonomy', exact: true });
  if (await pause.count()) {
    await pause.click();
    await page.getByRole('button', { name: 'Resume autonomy', exact: true }).waitFor();
    assert.equal((await api('orchestrator/status')).autonomy_enabled, false);
  }
  await page.getByRole('button', { name: 'Resume autonomy', exact: true }).click();
  await page.getByRole('button', { name: 'Pause autonomy', exact: true }).waitFor();
  assert.equal((await api('orchestrator/status')).autonomy_enabled, true);
  return { visits, autonomy: 'pause and resume matched persisted backend state' };
}

async function api(resource) {
  const response = await fetch(`${process.env.FORGE_API_URL || 'http://localhost:8000'}/api/v1/${resource}`);
  assert.equal(response.ok, true, `${resource}: ${response.status}`);
  return response.json();
}

async function create(page, kind = 'brief') {
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'New mission', exact: true }).click();
  const title = `Reliability ${kind} ${new Date().toISOString()}`;
  await page.getByLabel('Mission title', { exact: true }).fill(title);
  await page.getByLabel('Outcome', { exact: true }).fill(kind === 'todo'
    ? 'Run the explicitly scripted mock Todo sample: add, complete and delete tasks, with build and tests.'
    : 'Create a project brief and verify its contents through the human review workflow.');
  if (kind === 'todo') {
    await page.getByRole('button', { name: /Advanced structured inputs/ }).click();
    await page.getByLabel('Context JSON', { exact: true }).fill('{"mock_scenario":"todo_demo"}');
  }
  if (kind === 'invalid') await page.getByLabel('Maximum tasks').fill('1');
  await page.getByRole('button', { name: 'Create & start', exact: true }).click();
  if (kind === 'invalid') {
    await page.getByRole('dialog').getByRole('alert').waitFor({ timeout: 30000 });
    const text = await page.getByRole('dialog').getByRole('alert').innerText();
    assert.ok(text.includes('PLAN_LIMIT_EXCEEDED'), text);
    assert.ok(!text.includes('expected undefined'), text);
    await page.screenshot({ path: path.join(output, 'canonical-planning-failure.png'), fullPage: true });
    const rejectedId = new URL(page.url()).searchParams.get('mission');
    const rejectedPlan = await api(`missions/${rejectedId}/plan`);
    await page.getByLabel('Maximum tasks').fill('8');
    await page.getByRole('button', { name: 'Retry saved mission', exact: true }).click();
    await page.getByRole('dialog', { name: 'Create a mission' }).waitFor({ state: 'hidden', timeout: 30000 });
    const recoveredId = new URL(page.url()).searchParams.get('mission');
    assert.notEqual(recoveredId, rejectedId, 'changed constraints must create a new Mission');
    await page.getByRole('button', { name: 'Approve result', exact: true }).waitFor({ timeout: 120000 });
    const recovered = await api(`missions/${recoveredId}`);
    assert.equal(recovered.constraints.max_tasks, 8);
    assert.equal(recovered.status, 'ACTIVE');
    await fs.writeFile(path.join(output, 'recovered-mission-id.txt'), recoveredId);
    return { title, rejectedId, error: text, rejectedPlan, recovered };
  }
  await page.getByRole('dialog', { name: 'Create a mission' }).waitFor({ state: 'hidden', timeout: 30000 });
  await page.getByRole('heading', { name: title, exact: true }).waitFor();
  const id = new URL(page.url()).searchParams.get('mission');
  assert.ok(id);
  await fs.writeFile(path.join(output, `${kind}-mission-id.txt`), id);
  if (kind === 'queued') {
    const mission = await api(`missions/${id}`);
    const graph = await api(`projects/${mission.project_id}/graph`);
    assert.ok(graph.nodes.every(n => !['IN_PROGRESS', 'REVIEW', 'DONE'].includes(n.status)));
    return { id, mission, graph, autonomy: 'paused before creation' };
  }
  await page.getByRole('button', { name: 'Approve result', exact: true }).waitFor({ timeout: 120000 });
  const mission = await api(`missions/${id}`);
  const plan = await api(`missions/${id}/plan`);
  const graph = await api(`projects/${mission.project_id}/graph`);
  assert.ok(graph.nodes.some(task => task.status === 'REVIEW'), 'UI review must match this Mission');
  await page.screenshot({ path: path.join(output, `${kind}-review.png`), fullPage: true });
  return { title, id, mission, plan, graph };
}

module.exports = { navigation, create, api, output };

async function review(page, kind) {
  const id = (await fs.readFile(path.join(output, `${kind}-mission-id.txt`), 'utf8')).trim();
  await page.goto(`${base}/?view=missions&mission=${id}`);
  const mission = await api(`missions/${id}`);
  await page.getByRole('heading', { name: mission.title, exact: true }).waitFor();
  for (const name of ['Plan', 'Tasks', 'Files', 'Activity', 'Technical', 'Overview']) {
    await page.getByRole('tab', { name, exact: true }).click();
    await page.screenshot({ path: path.join(output, `${kind}-tab-${name.toLowerCase()}.png`), fullPage: true });
  }
  await page.getByRole('button', { name: 'Inspect execution', exact: true }).click();
  const inspector = page.getByRole('dialog');
  for (const name of ['Summary', 'Evidence', 'Timeline', 'Logs']) {
    await inspector.getByRole('tab', { name, exact: true }).click();
    await page.screenshot({ path: path.join(output, `${kind}-inspection-${name.toLowerCase()}.png`), fullPage: true });
  }
  await inspector.getByRole('button', { name: 'Close task inspection', exact: true }).click();
  await page.getByRole('tab', { name: 'Overview', exact: true }).click();
  if (kind === 'brief' && mission.status !== 'COMPLETED') {
    await page.getByRole('button', { name: 'Request a fix', exact: true }).click();
    await page.getByLabel('Reviewer instructions').fill('Rerun the deterministic brief sample and verify the saved file before resubmission.');
    await page.getByRole('button', { name: 'Request changes', exact: true }).click();
    await page.getByRole('dialog', { name: 'Request changes' }).waitFor({ state: 'hidden' });
    await page.waitForFunction(async (project) => {
      const graph = await fetch(`/api/forge/projects/${project}/graph`).then(r => r.json());
      return graph.nodes.some(n => n.iteration === 2 && n.status === 'REVIEW');
    }, mission.project_id, { timeout: 120000 });
    await page.reload();
    await page.getByRole('button', { name: 'Approve result', exact: true }).waitFor();
  }
  const before = await api(`projects/${mission.project_id}/graph`);
  const alreadyDone = before.nodes.filter(n => n.status === 'DONE').length;
  const taskCount = before.nodes.length;
  for (let i = alreadyDone; i < taskCount; i++) {
    await page.getByRole('button', { name: 'Approve result', exact: true }).waitFor({ timeout: 120000 });
    await page.getByRole('button', { name: 'Approve result', exact: true }).click();
    await page.waitForFunction(async ({ project, done }) => {
      const graph = await fetch(`/api/forge/projects/${project}/graph`).then(r => r.json());
      return graph.nodes.filter(n => n.status === 'DONE').length >= done;
    }, { project: mission.project_id, done: i + 1 });
    await page.reload();
  }
  const finished = await api(`missions/${id}`);
  assert.equal(finished.status, 'COMPLETED');
  const graph = await api(`projects/${mission.project_id}/graph`);
  assert.ok(graph.nodes.every(n => n.status === 'DONE'));
  await page.getByRole('heading', { name: mission.title, exact: true }).waitFor();
  await page.getByRole('tab', { name: 'Files', exact: true }).click();
  const open = page.getByRole('button', { name: /^Open / }).first();
  await open.click();
  await page.getByRole('dialog').waitFor();
  await page.locator('[data-testid="artifact-source"], [data-testid="rendered-markdown"]').waitFor();
  await page.screenshot({ path: path.join(output, `${kind}-artifact.png`), fullPage: true });
  const preview = await page.getByRole('dialog').innerText();
  await page.keyboard.press('Escape');
  await page.screenshot({ path: path.join(output, `${kind}-completed.png`), fullPage: true });
  return { id, mission: finished, graph, preview };
}

if (require.main === module) (async () => {
  const { chromium } = require('playwright');
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.setDefaultTimeout(10000);
  const errors = [], network = [];
  page.on('pageerror', error => errors.push({ type: 'pageerror', message: error.message }));
  page.on('console', message => { if (message.type() === 'error') errors.push({ type: 'console', message: message.text(), location: message.location() }); });
  page.on('response', response => { if (response.status() >= 400) network.push({ url: response.url(), status: response.status() }); });
  const mode = process.argv[2] || 'navigation';
  let evidence;
  try {
    await page.goto(base);
    await page.getByText('Runtime online', { exact: true }).waitFor();
    if (mode === 'queued') {
      await page.getByRole('button', { name: 'Pause autonomy', exact: true }).click();
      await page.getByRole('button', { name: 'Resume autonomy', exact: true }).waitFor();
    }
    if (mode === 'review-queued') {
      await page.getByRole('button', { name: 'Resume autonomy', exact: true }).click();
      await page.getByRole('button', { name: 'Pause autonomy', exact: true }).waitFor();
    }
    evidence = mode === 'navigation' ? await navigation(page)
      : mode.startsWith('review-') ? await review(page, mode.slice(7)) : await create(page, mode);
    console.log(JSON.stringify(evidence, null, 2));
  } catch (error) {
    evidence = { failure: error.message, url: page.url(), text: await page.locator('body').innerText() };
    await page.screenshot({ path: path.join(output, `${mode}-failure.png`), fullPage: true });
    console.error(JSON.stringify(evidence, null, 2));
    process.exitCode = 1;
  } finally {
    await fs.writeFile(path.join(output, `${mode}-evidence.json`), JSON.stringify({ evidence, errors, network }, null, 2));
    await browser.close();
  }
})();
