import { readFile } from 'node:fs/promises';
import type { AgentKind } from './types.ts';

export interface WorkerSettings {
  agents: Record<AgentKind, { args: string[] }>;
}

/** Do not change permission policy unless the workspace owner opts in. */
export const DEFAULT_WORKER_SETTINGS: WorkerSettings = {
  agents: {
    codex: { args: [] },
    claude: { args: [] }
  }
};

const kinds: AgentKind[] = ['codex', 'claude'];

function object(value: unknown, label: string): Record<string, unknown> {
  if (value === null || Array.isArray(value) || typeof value !== 'object') {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function knownKeys(record: Record<string, unknown>, allowed: readonly string[], label: string): void {
  for (const key of Object.keys(record)) {
    if (!allowed.includes(key)) throw new Error(`Unknown ${label} key: ${key}`);
  }
}

/** Validate before launching panes; never execute shell commands from settings. */
export function parseWorkerSettings(raw: string, filePath = 'settings.json'): WorkerSettings {
  let parsed: unknown;
  try { parsed = JSON.parse(raw); }
  catch { throw new Error(`Invalid JSON in ${filePath}`); }
  try {
    const root = object(parsed, 'settings');
    knownKeys(root, ['agents'], 'settings');
    const agents = object(root.agents, 'agents');
    knownKeys(agents, kinds, 'agents');

    const result: WorkerSettings = {
      agents: { codex: { args: [] }, claude: { args: [] } }
    };
    for (const kind of kinds) {
      if (agents[kind] === undefined) continue;
      const entry = object(agents[kind], `agents.${kind}`);
      knownKeys(entry, ['args'], `agents.${kind}`);
      if (!Array.isArray(entry.args) || entry.args.length > 32) {
        throw new Error(`agents.${kind}.args must be an array with at most 32 entries`);
      }
      for (const value of entry.args) {
        if (typeof value !== 'string' || !value.trim() || value.length > 1024 || /[\u0000-\u001f\u007f]/.test(value)) {
          throw new Error(`agents.${kind}.args must contain non-empty CLI argument strings without control characters`);
        }
      }
      result.agents[kind].args = [...entry.args] as string[];
    }
    return result;
  } catch (error) {
    throw new Error(`Invalid worker settings at ${filePath}: ${String(error)}`);
  }
}

/** Reload on each worker startup; changes apply without daemon restart. */
export async function loadWorkerSettings(path?: string): Promise<WorkerSettings> {
  if (!path) return structuredClone(DEFAULT_WORKER_SETTINGS);
  let raw: string;
  try { raw = await readFile(path, 'utf8'); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return structuredClone(DEFAULT_WORKER_SETTINGS);
    throw error;
  }
  return parseWorkerSettings(raw, path);
}

export function buildAgentStartArgs(name: string, kind: AgentKind, paneId: string, args: string[]): string[] {
  return ['agent', 'start', name, '--kind', kind, '--pane', paneId, '--timeout', '30000',
    ...(args.length ? ['--', ...args] : [])];
}
