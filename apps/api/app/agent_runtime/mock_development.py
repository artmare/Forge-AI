"""Explicit, dependency-free development sample for local end-to-end validation.

This is a scripted mock, never a substitute for a live model or proof that an
arbitrary Mission goal was implemented. The normal runtime owns every side effect.
"""

import json

SAMPLE = "todo_demo"

FILES = {
    "index.html": """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forge Todo demo</title><link rel="stylesheet" href="styles.css"></head>
<body><main><h1>Todo</h1><p>Local Forge mock development sample</p>
<form id="add-form"><label for="task-title">New task</label>
<input id="task-title" name="title" maxlength="200" required>
<button type="submit">Add task</button></form>
<p id="empty">No tasks yet.</p><ul id="tasks" aria-label="Tasks"></ul>
<p id="status" role="status" aria-live="polite"></p></main>
<script type="module" src="app.mjs"></script></body></html>
""",
    "styles.css": """* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font: 16px system-ui; color: #e2e8f0; background: #101827; }
main { max-width: 640px; margin: 40px auto; }
form { display: grid; gap: 12px; }
input, button { font: inherit; padding: 12px; border-radius: 8px; }
input { min-width: 0; width: 100%; }
button { color: white; background: #4338ca; border: 0; cursor: pointer; }
:focus-visible { outline: 3px solid #a5b4fc; outline-offset: 3px; }
ul { padding: 0; list-style: none; }
li { display: flex; gap: 12px; align-items: center; margin: 16px 0; }
li label { flex: 1; min-width: 0; overflow-wrap: anywhere; }
li input { width: auto; }
li.done label { text-decoration: line-through; }
@media (max-width: 480px) { body { padding: 16px; } main { margin: 16px auto; } }
""",
    "model.mjs": """export function addTask(tasks, title, id) {
  const clean = title.trim();
  return clean ? [...tasks, { id, title: clean, completed: false }] : tasks;
}
export function toggleTask(tasks, id) {
  return tasks.map(task => task.id === id ? { ...task, completed: !task.completed } : task);
}
export function deleteTask(tasks, id) { return tasks.filter(task => task.id !== id); }
""",
    "app.mjs": """import { addTask, toggleTask, deleteTask } from './model.mjs';
let tasks = [];
const list = document.querySelector('#tasks');
function render() {
  list.replaceChildren();
  document.querySelector('#empty').hidden = tasks.length > 0;
  for (const task of tasks) {
    const row = document.createElement('li');
    row.className = task.completed ? 'done' : '';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.id = `task-${task.id}`;
    checkbox.checked = task.completed;
    checkbox.addEventListener('change', () => { tasks = toggleTask(tasks, task.id); render(); });
    const label = document.createElement('label');
    label.htmlFor = checkbox.id; label.textContent = task.title;
    const remove = document.createElement('button');
    remove.textContent = 'Delete'; remove.setAttribute('aria-label', `Delete ${task.title}`);
    remove.addEventListener('click', () => { tasks = deleteTask(tasks, task.id); render(); });
    row.append(checkbox, label, remove); list.append(row);
  }
  const remaining = tasks.filter(t => !t.completed).length;
  document.querySelector('#status').textContent = `${remaining} remaining`;
}
document.querySelector('#add-form').addEventListener('submit', event => {
  event.preventDefault();
  const input = document.querySelector('#task-title');
  tasks = addTask(tasks, input.value, crypto.randomUUID());
  input.value = ''; render(); input.focus();
});
render();
""",
    "tests/todo.test.mjs": """import test from 'node:test';
import assert from 'node:assert/strict';
import { addTask, toggleTask, deleteTask } from '../model.mjs';
test('add, complete and delete a task', () => {
  const added = addTask([], '  Ship Forge  ', 'one');
  assert.deepEqual(added, [{ id: 'one', title: 'Ship Forge', completed: false }]);
  assert.equal(toggleTask(added, 'one')[0].completed, true);
  assert.equal(added[0].completed, false);
  assert.deepEqual(deleteTask(added, 'one'), []);
});
test('empty input is rejected and other tasks stay unchanged', () => {
  assert.deepEqual(addTask([], '   ', 'one'), []);
  const tasks = addTask([], 'Keep me', 'one');
  assert.deepEqual(deleteTask(tasks, 'missing'), tasks);
  assert.deepEqual(toggleTask(tasks, 'missing'), tasks);
});
""",
    "build.mjs": """import { mkdir, copyFile, readFile } from 'node:fs/promises';
const files = ['index.html', 'styles.css', 'app.mjs', 'model.mjs'];
await mkdir('dist', { recursive: true });
for (const file of files) {
  if (!(await readFile(file, 'utf8')).trim()) throw new Error(`${file} is empty`);
  await copyFile(file, `dist/${file}`);
}
console.log('Built Todo demo into dist');
""",
    "package.json": json.dumps({
        "name": "forge-mock-todo", "version": "1.0.0", "private": True,
        "type": "module", "scripts": {"test": "node --test tests/*.test.mjs",
                                       "build": "node build.mjs"},
    }, indent=2),
}


def proposal(title: str) -> dict:
    permissions = {"filesystem.list": True, "filesystem.read": True,
                   "development.execute": True, "git.read": True}
    return {
        "project": {"name": title, "description": "Explicit mock Todo development sample."},
        "agents": [
            {"key": "developer", "name": "Sample Developer", "role": "DEVELOPER",
             "model_alias": "default",
             "requested_permissions": {**permissions, "filesystem.write": True}},
            {"key": "qa", "name": "Sample QA", "role": "QA", "model_alias": "default",
             "requested_permissions": permissions},
        ],
        "tasks": [{
            "key": "implement", "title": "Build and verify the mock Todo sample",
            "description": "Write the fixed Todo sample, run its tests and production build.",
            "kind": "DEVELOPMENT", "assigned_agent_key": "developer",
            "input": {"mock_scenario": SAMPLE, "deliverables": list(FILES)},
            "acceptance_criteria": ["index.html exists", "npm test exits successfully",
                                    "npm build exits successfully"],
            "max_iterations": 4,
        }],
        "dependencies": [],
    }


def turn(number: int) -> dict:
    actions = [
        {"type": "tool_call", "tool_name": "filesystem.write",
         "arguments": {"path": path, "content": content}}
        for path, content in FILES.items()
    ] + [
        {"type": "tool_call", "tool_name": "development.execute",
         "arguments": {"action": action}}
        for action in ("NODE_TEST", "NODE_BUILD")
    ] + [
        {"type": "tool_call", "tool_name": name, "arguments": {}}
        for name in ("git.status", "git.diff")
    ]
    if 1 <= number <= len(actions):
        return actions[number - 1]
    return {"type": "final", "result": {
        "status": "completed", "summary": "Mock Todo sample submitted for independent QA.",
        "output": {"artifacts": list(FILES), "details": [], "execution_claims": []},
        "notes": ["Scripted mock sample; not goal-directed model generation."],
    }}
