import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, mkdir, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { TaskStore } from '../src/store.ts';
import { Orchestrator } from '../src/orchestrator.ts';
import { loadConfig } from '../src/config.ts';
import { parseHerdrResult, validateReport, type AgentRuntime } from '../src/herdr.ts';
import { rpc, serveSocket } from '../src/http.ts';
import type { Task } from '../src/types.ts';

class FakeRuntime implements AgentRuntime {
  readonly invoked: string[] = [];
  outcomes = new Map<string, 'success' | 'failure' | 'blocked' | 'missing'>();
  async start(task: Task): Promise<{ agentName: string; paneId: string }> {
    this.invoked.push(task.id);
    return { agentName: `ht${task.id.replaceAll('-', '').slice(0, 10)}`, paneId: 'w1:p2' };
  }
  async prompt(task: Task, _name: string, path: string, signal: AbortSignal): Promise<'settled'> {
    if (signal.aborted) throw new Error('aborted');
    const outcome = this.outcomes.get(task.instruction) ?? 'success';
    if (outcome !== 'missing') await writeFile(path, JSON.stringify({ task_id: task.id, outcome, summary: `Finished ${task.instruction}` }));
    return 'settled';
  }
  async read(): Promise<string> { return 'worker screen log'; }
  async interrupt(): Promise<void> {}
}

async function setup() {
  const dir = await mkdtemp(join(tmpdir(), 'htmcp-'));
  const config = loadConfig({ HERDR_TASK_DATA_DIR: dir, HERDR_TASK_POLL_MS: '20' });
  await mkdir(config.reportDir);
  const store = new TaskStore(config.dbPath);
  const runtime = new FakeRuntime();
  const orchestrator = new Orchestrator(store, runtime, config);
  orchestrator.start();
  return { dir, config, store, runtime, orchestrator };
}

test('task_submit reaches SUCCEEDED only with matching JSON worker report', async () => {
  const { dir, store, orchestrator } = await setup();
  try {
    const task = await orchestrator.submit({ target: 'claude', cwd: dir, instruction: 'review code' });
    const result = await orchestrator.wait(task.id, 3000);
    assert.equal(result.status, 'SUCCEEDED');
    assert.equal(result.target, 'claude');
    assert.match(result.summary!, /Finished review code/);
    assert.deepEqual(JSON.parse(await readFile(result.report_path!, 'utf8')).task_id, task.id);
  } finally { await orchestrator.stop(); store.close(); }
});

test('missing report is BLOCKED and never silently SUCCEEDED', async () => {
  const { dir, store, runtime, orchestrator } = await setup();
  try {
    runtime.outcomes.set('no report', 'missing');
    const task = await orchestrator.submit({ target: 'codex', cwd: dir, instruction: 'no report' });
    const result = await orchestrator.wait(task.id, 3000);
    assert.equal(result.status, 'BLOCKED');
    assert.match(result.error!, /Missing\/invalid worker report/);
    assert.match(result.summary!, /screen log/);
  } finally { await orchestrator.stop(); store.close(); }
});

test('dependency failure blocks queued task without starting worker', async () => {
  const { dir, store, runtime, orchestrator } = await setup();
  try {
    runtime.outcomes.set('failing', 'failure');
    const first = await orchestrator.submit({ target: 'codex', cwd: dir, instruction: 'failing' });
    const second = await orchestrator.submit({ target: 'claude', cwd: dir, instruction: 'blocked dependent', dependencies: [first.id] });
    assert.equal((await orchestrator.wait(first.id, 3000)).status, 'FAILED');
    assert.equal((await orchestrator.wait(second.id, 3000)).status, 'BLOCKED');
    assert.ok(!runtime.invoked.includes(second.id));
  } finally { await orchestrator.stop(); store.close(); }
});

test('delegation depth and parent rules prevent recursive cycles', async () => {
  const { dir, store, orchestrator } = await setup();
  try {
    const parent = await orchestrator.submit({ target: 'codex', cwd: dir, instruction: 'parent' });
    // Parent may finish immediately: test depth via a store task explicitly queued.
    const holding = store.create({ target: 'codex', instruction: 'holding', cwd: dir }, 0);
    const child = await orchestrator.submit({ target: 'claude', cwd: dir, instruction: 'child', parent_task_id: holding.id });
    assert.equal(child.depth, 1);
    await assert.rejects(() => orchestrator.submit({ target: 'codex', cwd: dir, instruction: 'nested', parent_task_id: child.id }), /depth/);
    await assert.rejects(() => orchestrator.submit({ target: 'codex', cwd: dir, instruction: 'invalid', parent_task_id: holding.id, dependencies: [holding.id] }), /parent/);
    assert.ok(parent.id);
  } finally { await orchestrator.stop(); store.close(); }
});

test('cancelling queued task persists CANCELLED', async () => {
  const { dir, store, orchestrator } = await setup();
  try {
    const task = store.create({ target: 'codex', instruction: 'later', cwd: dir, dependencies: ['waiting'] }, 0);
    assert.equal((await orchestrator.cancel(task.id)).status, 'CANCELLED');
  } finally { await orchestrator.stop(); store.close(); }
});

test('daemon restarts move unfinished RUNNING task to BLOCKED', async () => {
  const { dir, store, orchestrator } = await setup();
  try {
    const task = store.create({ target: 'codex', instruction: 'interrupted', cwd: dir }, 0);
    store.claim(task.id);
    assert.equal(store.recoverInterrupted(), 1);
    assert.equal(store.get(task.id)?.status, 'BLOCKED');
  } finally { await orchestrator.stop(); store.close(); }
});

test('Herdr CLI envelope and report task ID are validated', () => {
  assert.equal(parseHerdrResult<{ ok: true }>(' {"result":{"ok":true}} ').ok, true);
  assert.throws(() => parseHerdrResult('not-json'), /invalid JSON/);
  assert.throws(() => validateReport('{"task_id":"wrong","outcome":"success","summary":"fake"}', 'right'), /Invalid worker report/);
});

test('shared daemon RPC can be accessed through its Unix socket', async () => {
  const { dir, config, store, orchestrator } = await setup();
  const socket = await serveSocket(config.socketPath, orchestrator);
  try {
    const task = await rpc(config.socketPath, 'submit', { target: 'claude', cwd: dir, instruction: 'rpc check' }) as Task;
    const done = await rpc(config.socketPath, 'wait', { id: task.id, timeout_ms: 3000 }) as Task;
    assert.equal(done.status, 'SUCCEEDED');
    assert.equal((await rpc(config.socketPath, 'result', { id: task.id }) as { summary: string }).summary, 'Finished rpc check');
  } finally {
    await orchestrator.stop();
    await new Promise<void>(resolve => socket.close(() => resolve()));
    store.close();
  }
});
