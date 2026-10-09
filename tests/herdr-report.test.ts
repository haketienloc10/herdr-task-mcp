import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { HerdrRuntime, buildWorkerPrompt } from '../src/herdr.ts';
import type { Task } from '../src/types.ts';

async function stubHerdr(state: 'idle' | 'blocked' = 'idle') {
  const root = await mkdtemp(join(tmpdir(), 'htmcp-screen-'));
  const bin = join(root, 'herdr-stub.mjs');
  const log = join(root, 'commands.jsonl');
  await writeFile(bin, [
    '#!/usr/bin/env node',
    "import { appendFileSync } from 'node:fs';",
    'const args = process.argv.slice(2);',
    'appendFileSync(' + JSON.stringify(log) + ', JSON.stringify(args) + "\\n");',
    'if (args[0] === "agent" && args[1] === "prompt") {',
    '  console.log(JSON.stringify({result:{agent:{status:"idle"}}}));',
    '} else if (args[0] === "agent" && args[1] === "get") {',
    '  console.log(JSON.stringify({result:{agent:{status:' + JSON.stringify(state) + '}}}));',
    '} else { process.exitCode = 3; }',
    ''
  ].join('\n'));
  await chmod(bin, 0o700);
  const id = '22222222-2222-4222-8222-222222222222';
  const task = {
    id, target: 'codex', mode: 'implement', cwd: root, timeout_ms: 5000,
    instruction: 'Implement and report'
  } as Task;
  const report = join(root, 'reports', id + '.json');
  const runtime = new HerdrRuntime(bin);
  return { root, id, task, report, runtime, log };
}

test('worker reports complete long answers without requiring any terminal history or idle state', async () => {
  const { task, id, report, runtime, log } = await stubHerdr('idle');
  // The agent returns idle immediately. Only a valid later file can complete the task.
  const pending = runtime.prompt(task, 'myworker', report, new AbortController().signal);
  await new Promise(resolve => setTimeout(resolve, 450));
  await assert.rejects(() => readFile(report, 'utf8'), /ENOENT/);
  // A full Markdown artifact can be arbitrarily larger than the terminal viewport.
  await writeFile(report.slice(0, -5) + '.md', 'Long response\n'.repeat(15000));
  await writeFile(report, JSON.stringify({
    task_id: id, outcome: 'success', summary: 'Finished', artifacts: [report.slice(0, -5) + '.md']
  }));
  assert.equal(await pending, 'settled');
  const calls = (await readFile(log, 'utf8')).trim().split('\n').map(x => JSON.parse(x) as string[]);
  assert.equal(calls.filter(c => c[0] === 'agent' && c[1] === 'prompt').length, 1);
  assert.equal(calls.filter(c => c[0] === 'agent' && c[1] === 'read').length, 0);
  assert.ok(!calls.some(c => c.includes('--wait')));
  const prompt = calls.find(c => c[1] === 'prompt')![3];
  assert.match(prompt, /report .*\.json/);
  assert.match(prompt, /Do not print long outputs to the terminal/);
  assert.match(prompt, /under 600 characters/);
  assert.match(buildWorkerPrompt(task, report), /artifacts/);
});

test('blocked worker is still detected without reading the screen', async () => {
  const { task, report, runtime, log } = await stubHerdr('blocked');
  assert.equal(await runtime.prompt(task, 'myworker', report, new AbortController().signal), 'blocked');
  const calls = (await readFile(log, 'utf8')).trim().split('\n').map(x => JSON.parse(x) as string[]);
  assert.equal(calls.filter(c => c[1] === 'prompt').length, 1);
  assert.equal(calls.filter(c => c[1] === 'read').length, 0);
});

test('cancel aborts waiting on report without resubmitting the prompt', async () => {
  const { task, report, runtime, log } = await stubHerdr('idle');
  const controller = new AbortController();
  const pending = runtime.prompt(task, 'myworker', report, controller.signal);
  setTimeout(() => controller.abort(), 350);
  await assert.rejects(pending, /aborted/i);
  const calls = (await readFile(log, 'utf8')).trim().split('\n').map(x => JSON.parse(x) as string[]);
  assert.equal(calls.filter(c => c[1] === 'prompt').length, 1);
});
