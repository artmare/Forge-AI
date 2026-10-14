/* Read-only snapshot of audit Missions and their persisted execution evidence. */
const fs = require('node:fs/promises');
const path = require('node:path');
const { api, output } = require('./reliability.cjs');

(async () => {
  const missions = await api('missions?limit=100');
  const records = [];
  for (const mission of missions.filter(m => m.title.startsWith('Reliability'))) {
    const plan = mission.planning_attempts ? await api(`missions/${mission.id}/plan`) : null;
    const graph = mission.project_id ? await api(`projects/${mission.project_id}/graph`) : null;
    const inspections = [];
    for (const task of graph?.nodes || []) inspections.push(await api(`tasks/${task.id}/inspection`));
    records.push({ mission, plan, graph, inspections });
  }
  await fs.writeFile(path.join(output, 'persisted-missions.json'), JSON.stringify(records, null, 2));
  const summary = records.map(({ mission: m, plan, graph, inspections }) => ({
    title: m.title, id: m.id, status: m.status, project: m.project_id,
    planning_run: plan?.planning_run?.id, provider: plan?.planning_run?.provider,
    model: plan?.planning_run?.resolved_model, plan_status: plan?.planning_run?.status,
    tasks: graph?.nodes.map(n => ({ id: n.id, title: n.title, status: n.status, iteration: n.iteration,
      qa: n.qa_results.map(q => ({ id: q.id, decision: q.decision })),
      acceptance: n.acceptance_verifications.map(a => a.status) })),
    runs: inspections.flatMap(i => i.iterations.map(r => ({ task: i.id,
      task_run: r.task_run.id, agent_runs: r.agent_runs.map(a => a.id),
      command_count: r.commands.length, tool_count: r.tool_calls.length }))),
  }));
  await fs.writeFile(path.join(output, 'mission-summary.json'), JSON.stringify(summary, null, 2));
  console.log(JSON.stringify(summary, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
