import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { workspacePaths, workspaceEnvironment, initWorkspace } from '../src/workspace.ts';
import { loadConfig } from '../src/config.ts';
import { TaskStore } from '../src/store.ts';
import { Orchestrator } from '../src/orchestrator.ts';
import type { AgentRuntime } from '../src/herdr.ts';

async function tempWorkspace() {
  const root = await mkdtemp(join(tmpdir(), 'htmcp-project-'));
  const entryPoint = join(root, '.tools', 'herdr-task-mcp', 'dist', 'src', 'cli.js');
  return await workspacePaths(root, entryPoint);
}

test('workspace init writes project-scoped Codex and Claude configs with isolated state', async () => {
  const paths = await tempWorkspace();
  await mkdir(join(paths.root, '.codex'));
  await writeFile(join(paths.root, '.codex', 'config.toml'), 'model = "gpt-6"\n');
  await writeFile(join(paths.root, '.mcp.json'), JSON.stringify({ mcpServers: { existing: { command: 'true' } }, otherSetting: true }));
  await initWorkspace(paths);
  const codex = await readFile(join(paths.root, '.codex', 'config.toml'), 'utf8');
  const claude = JSON.parse(await readFile(join(paths.root, '.mcp.json'), 'utf8'));
  assert.match(codex, /model = "gpt-6"/);
  assert.match(codex, /\[mcp_servers\.herdr-task\]/);
  assert.match(codex, /HERDR_TASK_WORKSPACE_ROOT/);
  assert.equal(claude.otherSetting, true);
  assert.equal(claude.mcpServers.existing.command, 'true');
  assert.deepEqual(claude.mcpServers['herdr-task'].env, workspaceEnvironment(paths));
  assert.deepEqual(claude.mcpServers['herdr-task'].args, [paths.entryPoint, 'mcp']);
  assert.equal((await readFile(join(paths.dataDir, '.gitignore'), 'utf8')).startsWith('*\n'), true);
  await initWorkspace(paths);
  const again = await readFile(join(paths.root, '.codex', 'config.toml'), 'utf8');
  assert.equal(again.split('[mcp_servers.herdr-task]').length - 1, 1);
});

test('workspace init refuses conflicting configuration without modifying Codex config', async () => {
  const paths = await tempWorkspace();
  await mkdir(join(paths.root, '.codex'));
  await writeFile(join(paths.root, '.codex', 'config.toml'), 'model = "gpt-6"\n');
  await writeFile(join(paths.root, '.mcp.json'), '{ broken');
  await assert.rejects(() => initWorkspace(paths), /valid JSON/);
  assert.equal(await readFile(join(paths.root, '.codex', 'config.toml'), 'utf8'), 'model = "gpt-6"\n');
  await writeFile(join(paths.root, '.mcp.json'), '{}');
  await writeFile(join(paths.root, '.codex', 'config.toml'), '[mcp_servers.herdr-task]\ncommand = "other"\n');
  await assert.rejects(() => initWorkspace(paths), /already defines/);
});

test('workspace scope blocks cwd outside root including symlink escapes', async () => {
  const paths = await tempWorkspace();
  const other = await mkdtemp(join(tmpdir(), 'htmcp-other-'));
  const config = loadConfig({ ...workspaceEnvironment(paths) });
  await mkdir(config.dataDir, { recursive: true });
  const store = new TaskStore(config.dbPath);
  const fakeRuntime = {} as AgentRuntime;
  const orchestrator = new Orchestrator(store, fakeRuntime, config);
  try {
    await assert.rejects(() => orchestrator.submit({ target: 'codex', cwd: other, instruction: 'outside' }), /inside workspace/);
    await symlink(other, join(paths.root, 'escape'));
    await assert.rejects(() => orchestrator.submit({ target: 'claude', cwd: join(paths.root, 'escape'), instruction: 'escape' }), /inside workspace/);
    const task = await orchestrator.submit({ target: 'claude', cwd: paths.root, instruction: 'allowed' });
    assert.equal(task.cwd, paths.root);
  } finally { await orchestrator.stop(); store.close(); }
});
