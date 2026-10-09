import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

export interface Config {
  dataDir: string;
  socketPath: string;
  dbPath: string;
  reportDir: string;
  herdrBin: string;
  maxConcurrent: number;
  maxDepth: number;
  maxChildren: number;
  pollMs: number;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const dataDir = resolve(env.HERDR_TASK_DATA_DIR ?? join(homedir(), '.herdr-task-mcp'));
  const positiveInteger = (key: string, fallback: number): number => {
    const n = Number(env[key] ?? fallback);
    if (!Number.isSafeInteger(n) || n < 1) throw new Error(`${key} must be a positive integer`);
    return n;
  };
  return {
    dataDir,
    socketPath: env.HERDR_TASK_SOCKET ?? join(dataDir, 'orchestrator.sock'),
    dbPath: join(dataDir, 'tasks.sqlite'),
    reportDir: join(dataDir, 'reports'),
    herdrBin: env.HERDR_BIN ?? 'herdr',
    maxConcurrent: Math.max(2, positiveInteger('HERDR_TASK_MAX_CONCURRENT', 3)),
    maxDepth: positiveInteger('HERDR_TASK_MAX_DEPTH', 1),
    maxChildren: positiveInteger('HERDR_TASK_MAX_CHILDREN', 6),
    pollMs: positiveInteger('HERDR_TASK_POLL_MS', 500)
  };
}
