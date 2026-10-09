#!/usr/bin/env node
import { mkdir } from 'node:fs/promises';
import { loadConfig } from './config.ts';

const command = process.argv[2] ?? 'mcp';
let config = loadConfig();
if (command === 'workspace') {
  const { workspacePaths, workspaceEnvironment, initWorkspace } = await import('./workspace.ts');
  const action = process.argv[3];
  if (action !== 'init' && action !== 'daemon') {
    throw new Error('Usage: herdr-task-mcp workspace [init|daemon] [workspace-path]');
  }
  const paths = await workspacePaths(process.argv[4]);
  if (action === 'init') {
    await initWorkspace(paths);
    console.error(`Workspace MCP configured: ${paths.root}`);
    process.exit(0);
  }
  config = loadConfig({ ...process.env, ...workspaceEnvironment(paths) });
}


if (command === 'daemon' || command === 'workspace') {
  const [{ TaskStore }, { HerdrRuntime }, { Orchestrator }, { serveSocket }] = await Promise.all([
    import('./store.ts'), import('./herdr.ts'), import('./orchestrator.ts'), import('./http.ts')
  ]);
  await mkdir(config.dataDir, { recursive: true, mode: 0o700 });
  const store = new TaskStore(config.dbPath);
  const orchestrator = new Orchestrator(store, new HerdrRuntime(config.herdrBin), config);
  const server = await serveSocket(config.socketPath, orchestrator);
  orchestrator.start();
  console.error(`herdr-task-mcp daemon listening: ${config.socketPath}`);
  const shutdown = () => {
    void (async () => {
      await orchestrator.stop();
      await new Promise<void>(resolve => server.close(() => resolve()));
      store.close();
      process.exit(0);
    })();
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
} else if (command === 'mcp') {
  const { serveMcp } = await import('./mcp.ts');
  await serveMcp(config.socketPath);
} else {
  console.error('Usage: herdr-task-mcp [daemon|mcp|workspace init|workspace daemon]');
  process.exitCode = 2;
}
