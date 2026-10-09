import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { HerdrRuntime } from '../src/herdr.ts';
import { workspacePaths, initWorkspace } from '../src/workspace.ts';
import {
  buildAgentStartArgs, loadWorkerSettings, parseWorkerSettings
} from '../src/worker-settings.ts';
import type { Task } from '../src/types.ts';

function settings(codex: string[], claude: string[]): string {
  return JSON.stringify({ agents: { codex: { args: codex }, claude: { args: claude } } }, null, 2);
}

test('worker settings parse both agent argument lists and use safe defaults', async () => {
  assert.deepEqual(parseWorkerSettings(settings(['--yolo'], ['--permission-mode=auto'])), {
    agents: { codex: { args: ['--yolo'] }, claude: { args: ['--permission-mode=auto'] } }
  });
  assert.deepEqual(parseWorkerSettings('{"agents":{"codex":{"args":["--model","gpt-6"]}}}').agents.claude.args, []);
  assert.deepEqual((await loadWorkerSettings()).agents.codex.args, []);
  assert.deepEqual(buildAgentStartArgs('worker1', 'codex', 'w1:p2', ['--yolo']), [
    'agent', 'start', 'worker1', '--kind', 'codex', '--pane', 'w1:p2',
    '--timeout', '30000', '--', '--yolo'
  ]);
  assert.deepEqual(buildAgentStartArgs('worker1', 'claude', 'w1:p2', []), [
    'agent', 'start', 'worker1', '--kind', 'claude', '--pane', 'w1:p2', '--timeout', '30000'
  ]);
});

test('worker settings reject malformed JSON, unknown keys, bad flags and excessive args', () => {
  assert.throws(() => parseWorkerSettings('bad-json'), /Invalid JSON/);
  assert.throws(() => parseWorkerSettings('{"agents":{"codex":{"args":"--yolo"}}}'), /array/);
  assert.throws(() => parseWorkerSettings('{"agents":{"codex":{"argv":["--yolo"]}}}'), /Unknown/);
  assert.throws(() => parseWorkerSettings('{"agents":{"codex":{"args":[""]}}}'), /non-empty/);
  assert.throws(() => parseWorkerSettings('{"agents":{"codex":{"args":["bad\\nflag"]}}}'), /control characters/);
  assert.throws(() => parseWorkerSettings('{"agents":{"codex":{"args":[]},"other":{"args":[]}}}'), /Unknown/);
  assert.deepEqual(parseWorkerSettings('{"agents":{}}').agents.codex.args, []);
});

test('workspace init creates settings once and preserves user edits', async () => {
  const root = await mkdtemp(join(tmpdir(), 'htmcp-flags-'));
  const paths = await workspacePaths(root, join(root, 'dist', 'src', 'cli.js'));
  const path = join(paths.dataDir, 'settings.json');
  await initWorkspace(paths);
  assert.equal((await loadWorkerSettings(path)).agents.codex.args.length, 0);
  assert.equal((await loadWorkerSettings(path)).agents.claude.args.length, 0);
  const edited = settings(['--yolo'], ['--permission-mode=auto']);
  await writeFile(path, edited);
  await initWorkspace(paths);
  assert.equal(await readFile(path, 'utf8'), edited);
});

test('workspace init refuses invalid worker settings before changing other config files', async () => {
  const root = await mkdtemp(join(tmpdir(), 'htmcp-flags-invalid-'));
  const paths = await workspacePaths(root, join(root, 'dist', 'src', 'cli.js'));
  await mkdir(paths.dataDir);
  await writeFile(join(paths.dataDir, 'settings.json'), '{"agents":{"codex":{"args":42}}}');
  await assert.rejects(() => initWorkspace(paths), /must be an array/);
  await assert.rejects(() => readFile(join(paths.root, '.mcp.json')), /ENOENT/);
});

test('HerdrRuntime forwards flags after -- and reloads settings for subsequent tasks', async () => {
  const root = await mkdtemp(join(tmpdir(), 'htmcp-fake-herdr-'));
  const fakePath = join(root, 'herdr-shim.mjs');
  const logPath = join(root, 'calls.jsonl');
  const configPath = join(root, 'settings.json');
  // Stub exercises the complete execFile argv boundary without relying on Herdr installation.
  const fake = [
    '#!/usr/bin/env node',
    "import { appendFileSync } from 'node:fs';",
    'const argv = process.argv.slice(2);',
    'appendFileSync(' + JSON.stringify(logPath) + ', JSON.stringify(argv) + "\\n");',
    'if (argv[0] === "workspace") console.log(JSON.stringify({result:{root_pane:{pane_id:"w1:p1"}}}));',
    'else if (argv[0] === "agent" && argv[1] === "start") console.log(JSON.stringify({result:{ok:true}}));',
    'else process.exitCode = 2;',
    ''
  ].join('\n');
  await writeFile(fakePath, fake);
  await chmod(fakePath, 0o700);
  await writeFile(configPath, settings(['--yolo'], []));
  const runtime = new HerdrRuntime(fakePath, 30000, configPath);
  const makeTask = (target: 'codex' | 'claude'): Task => ({
    id: '00000000-0000-4000-8000-000000000001',
    target, cwd: root, source_pane_id: null
  }) as Task;

  await runtime.start(makeTask('codex'));
  await writeFile(configPath, settings([], ['--permission-mode=auto']));
  await runtime.start(makeTask('claude'));

  const calls = (await readFile(logPath, 'utf8')).trim().split('\n').map(x => JSON.parse(x) as string[]);
  const starts = calls.filter(args => args[0] === 'agent' && args[1] === 'start');
  assert.equal(starts.length, 2);
  assert.deepEqual(starts[0].slice(-2), ['--', '--yolo']);
  assert.equal(starts[0][4], 'codex');
  assert.deepEqual(starts[1].slice(-2), ['--', '--permission-mode=auto']);
  assert.equal(starts[1][4], 'claude');

  await writeFile(configPath, '{"agents":{"codex":{"args":"--bad"}}}');
  await assert.rejects(() => runtime.start(makeTask('codex')), /must be an array/);
  const updated = (await readFile(logPath, 'utf8')).trim().split('\n');
  assert.equal(updated.length, calls.length, 'malformed settings must fail before spawning any pane');
});
