import { lstat, mkdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const START = '# >>> herdr-task-mcp workspace (managed)';
const END = '# <<< herdr-task-mcp workspace (managed)';
const SERVER_NAME = 'herdr-task';

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
  const env = workspaceEnvironment(paths);
  const section = [
    START,
    '[mcp_servers.herdr-task]',
    'command = "node"',
    `args = [${JSON.stringify(paths.entryPoint)}, "mcp"]`,
    '[mcp_servers.herdr-task.env]',
    ...Object.entries(env).map(([key, value]) => `${key} = ${JSON.stringify(value)}`),
    END
  ].join('\n');
  return `${original.trimEnd()}${original.trim() ? '\n\n' : ''}${section}\n`;
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
  await Promise.all([
    rejectSymlink(join(paths.root, '.codex')), rejectSymlink(codexPath),
    rejectSymlink(claudePath), rejectSymlink(paths.dataDir)
  ]);
  // Validate both before performing any writes to avoid partially applied configuration on normal errors.
  const [codexOld, claudeOld] = await Promise.all([readOptional(codexPath), readOptional(claudePath)]);
  const codexNew = codexConfig(codexOld, paths);
  const claudeNew = claudeConfig(claudeOld, paths);
  await mkdir(join(paths.root, '.codex'), { recursive: true });
  await mkdir(paths.dataDir, { recursive: true, mode: 0o700 });
  // Git ignores every state file even if the containing project has no .gitignore entry.
  await writeFile(join(paths.dataDir, '.gitignore'), '*\n!.gitignore\n');
  await writeFile(codexPath, codexNew);
  await writeFile(claudePath, claudeNew);
}
