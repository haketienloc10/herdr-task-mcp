import { lstat, mkdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_WORKER_SETTINGS, loadWorkerSettings } from './worker-settings.ts';

const START = '# >>> herdr-task-mcp workspace (managed)';
const END = '# <<< herdr-task-mcp workspace (managed)';
const SERVER_NAME = 'herdr-task';
const CODEX_DIRECT_NAMESPACE = `mcp__${SERVER_NAME.replace(/[^A-Za-z0-9_]/g, '_')}`;

export interface WorkspacePaths {
  root: string;
  dataDir: string;
  socketPath: string;
  entryPoint: string;
}

/** Resolve the same workspace-local paths for MCP clients and the daemon. */
export async function workspacePaths(
  directory = process.cwd(),
  entryPoint = fileURLToPath(new URL('./cli.js', import.meta.url))
): Promise<WorkspacePaths> {
  const root = await realpath(resolve(directory));
  const dataDir = join(root, '.herdr-task-mcp');
  return { root, dataDir, socketPath: join(dataDir, 'orchestrator.sock'), entryPoint: resolve(entryPoint) };
}

export function workspaceEnvironment(paths: WorkspacePaths): Record<string, string> {
  return {
    HERDR_TASK_DATA_DIR: paths.dataDir,
    HERDR_TASK_SOCKET: paths.socketPath,
    HERDR_TASK_WORKSPACE_ROOT: paths.root
  };
}

async function readOptional(path: string): Promise<string> {
  try { return await readFile(path, 'utf8'); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return '';
    throw error;
  }
}

/** Preserve user code-mode settings when adding our MCP to direct-only tools. */
function addCodexDirectNamespace(config: string): { config: string; needsSection: boolean } {
  const headers = Array.from(config.matchAll(/^[ \t]*\[features\.code_mode\][ \t]*(?:#.*)?$/gm));
  if (headers.length > 1) throw new Error('.codex/config.toml defines features.code_mode more than once');
  if (headers.length === 0) return { config, needsSection: true };
  const header = headers[0];
  const bodyStart = header.index! + header[0].length;
  const nextTable = /^[ \t]*\[\[?[^\n]+/gm;
  nextTable.lastIndex = bodyStart;
  const bodyEnd = nextTable.exec(config)?.index ?? config.length;
  const body = config.slice(bodyStart, bodyEnd);
  const keys = Array.from(body.matchAll(/^[ \t]*direct_only_tool_namespaces[ \t]*=/gm));
  if (keys.length > 1) throw new Error('.codex/config.toml defines direct_only_tool_namespaces more than once');
  if (keys.length === 0) {
    const inserted = '\ndirect_only_tool_namespaces = [' + JSON.stringify(CODEX_DIRECT_NAMESPACE) + ']';
    return { config: config.slice(0, bodyStart) + inserted + config.slice(bodyStart), needsSection: false };
  }
  const key = keys[0];
  const arrayStart = key.index! + key[0].length + (body.slice(key.index! + key[0].length).match(/^[ \t]*/)?.[0].length ?? 0);
  if (body[arrayStart] !== '[') {
    throw new Error('.codex/config.toml direct_only_tool_namespaces must be a TOML array');
  }
  const values: string[] = [];
  let i = arrayStart + 1;
  const skip = () => {
    while (i < body.length) {
      if (/\s/.test(body[i])) { i++; continue; }
      if (body[i] === '#') { while (i < body.length && body[i] !== '\n') i++; continue; }
      break;
    }
  };
  let closing = -1;
  while (i < body.length) {
    skip();
    if (body[i] === ']') { closing = i; break; }
    const quote = body[i];
    if (quote !== '"' && quote !== "'") {
      throw new Error('.codex/config.toml direct_only_tool_namespaces must contain quoted strings');
    }
    i++;
    let value = '';
    let closed = false;
    while (i < body.length) {
      if (body[i] === '\\' && quote === '"') {
        if (i + 1 >= body.length) break;
        value += body[i + 1]; i += 2;
      } else if (body[i] === quote) { i++; closed = true; break; }
      else { value += body[i++]; }
    }
    if (!closed) throw new Error('.codex/config.toml has an unterminated direct_only_tool_namespaces string');
    values.push(value);
    skip();
    if (body[i] === ',') { i++; continue; }
    if (body[i] === ']') { closing = i; break; }
    throw new Error('.codex/config.toml has an invalid direct_only_tool_namespaces array');
  }
  if (closing < 0) throw new Error('.codex/config.toml has an unterminated direct_only_tool_namespaces array');
  if (values.includes(CODEX_DIRECT_NAMESPACE)) return { config, needsSection: false };
  const addition = JSON.stringify(CODEX_DIRECT_NAMESPACE) + (values.length ? ', ' : '');
  const absoluteStart = bodyStart + arrayStart + 1;
  return { config: config.slice(0, absoluteStart) + addition + config.slice(absoluteStart), needsSection: false };
}

function codexConfig(existing: string, paths: WorkspacePaths): string {
  const begin = existing.indexOf(START);
  const end = existing.indexOf(END);
  if ((begin < 0) !== (end < 0) || (begin >= 0 && end < begin)) {
    throw new Error('Malformed herdr-task-mcp managed section in .codex/config.toml');
  }
  const original = begin < 0 ? existing : existing.slice(0, begin) + existing.slice(end + END.length);
  if (/^\s*\[\s*mcp_servers\.(?:herdr-task|"herdr-task"|'herdr-task')(?:\s*\]|\s*\.)/m.test(original)) {
    throw new Error('.codex/config.toml already defines mcp_servers.herdr-task outside its managed section');
  }
  const { config, needsSection } = addCodexDirectNamespace(original);
  const env = workspaceEnvironment(paths);
  const section = [
    START,
    ...(needsSection ? ['[features.code_mode]',
      'direct_only_tool_namespaces = [' + JSON.stringify(CODEX_DIRECT_NAMESPACE) + ']', ''] : []),
    '[mcp_servers.herdr-task]',
    'command = "node"',
    `args = [${JSON.stringify(paths.entryPoint)}, "mcp"]`,
    '[mcp_servers.herdr-task.env]',
    ...Object.entries(env).map(([key, value]) => `${key} = ${JSON.stringify(value)}`),
    END
  ].join('\n');
  return `${config.trimEnd()}${config.trim() ? '\n\n' : ''}${section}\n`;
}

function claudeConfig(existing: string, paths: WorkspacePaths): string {
  let config: Record<string, unknown>;
  try { config = existing.trim() ? JSON.parse(existing) as Record<string, unknown> : {}; }
  catch { throw new Error('.mcp.json is not valid JSON; refusing to overwrite it'); }
  if (!config || Array.isArray(config) || typeof config !== 'object') {
    throw new Error('.mcp.json must be a JSON object');
  }
  const servers = config.mcpServers ?? {};
  if (!servers || Array.isArray(servers) || typeof servers !== 'object') {
    throw new Error('.mcp.json mcpServers must be an object');
  }
  const other = (servers as Record<string, unknown>)[SERVER_NAME];
  if (other !== undefined) {
    const args = (other && typeof other === 'object' && !Array.isArray(other))
      ? (other as { args?: unknown }).args : undefined;
    if (!Array.isArray(args) || args.length !== 2 ||
        typeof args[0] !== 'string' || !args[0].endsWith('/dist/src/cli.js') || args[1] !== 'mcp') {
      throw new Error('.mcp.json already defines herdr-task; refusing to overwrite unrelated configuration');
    }
  }
  config.mcpServers = {
    ...servers,
    [SERVER_NAME]: {
      command: 'node',
      args: [paths.entryPoint, 'mcp'],
      env: workspaceEnvironment(paths)
    }
  };
  return `${JSON.stringify(config, null, 2)}\n`;
}

async function rejectSymlink(path: string): Promise<void> {
  try { if ((await lstat(path)).isSymbolicLink()) throw new Error(`Refusing symbolic link at ${path}`); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
}

/** Configure project-scoped MCP, without editing ~/.codex or ~/.claude. */
export async function initWorkspace(paths: WorkspacePaths): Promise<void> {
  const codexPath = join(paths.root, '.codex', 'config.toml');
  const claudePath = join(paths.root, '.mcp.json');
  const workerSettingsPath = join(paths.dataDir, 'settings.json');
  await Promise.all([
    rejectSymlink(join(paths.root, '.codex')), rejectSymlink(codexPath),
    rejectSymlink(claudePath), rejectSymlink(paths.dataDir), rejectSymlink(workerSettingsPath)
  ]);
  // Validate both before performing any writes to avoid partially applied configuration on normal errors.
  const [codexOld, claudeOld] = await Promise.all([readOptional(codexPath), readOptional(claudePath)]);
  // Validate existing settings before writing any workspace configuration.
  await loadWorkerSettings(workerSettingsPath);
  const codexNew = codexConfig(codexOld, paths);
  const claudeNew = claudeConfig(claudeOld, paths);
  await mkdir(join(paths.root, '.codex'), { recursive: true });
  await mkdir(paths.dataDir, { recursive: true, mode: 0o700 });
  // Git ignores every state file even if the containing project has no .gitignore entry.
  await writeFile(join(paths.dataDir, '.gitignore'), '*\n!.gitignore\n');
  await writeFile(workerSettingsPath, JSON.stringify(DEFAULT_WORKER_SETTINGS, null, 2) + '\n', {
    flag: 'wx', mode: 0o600
  }).catch(error => {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
  });
  await writeFile(codexPath, codexNew);
  await writeFile(claudePath, claudeNew);
}
