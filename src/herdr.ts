import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomBytes } from 'node:crypto';
import { readFile, stat } from 'node:fs/promises';
import { setTimeout as sleep } from 'node:timers/promises';
import type { AgentKind, Task, WorkerReport } from './types.ts';
import { buildAgentStartArgs, loadWorkerSettings } from './worker-settings.ts';

const execFileAsync = promisify(execFile);

export interface AgentRuntime {
  start(task: Task): Promise<{ agentName: string; paneId: string }>;
  prompt(task: Task, agentName: string, reportPath: string, signal: AbortSignal): Promise<'settled' | 'blocked'>;
  read(agentName: string): Promise<string>;
  interrupt(agentName: string): Promise<void>;
}

export function parseHerdrResult<T>(stdout: string): T {
  let envelope: { result?: T; error?: unknown };
  try { envelope = JSON.parse(stdout) as typeof envelope; }
  catch { throw new Error(`Herdr returned invalid JSON: ${stdout.slice(0, 300)}`); }
  if (envelope.error || !envelope.result) throw new Error(`Herdr error: ${JSON.stringify(envelope.error ?? envelope).slice(0, 500)}`);
  return envelope.result;
}

export function buildWorkerPrompt(task: Task, reportPath: string): string {
  const detailsPath = reportPath.endsWith('.json')
    ? reportPath.slice(0, -5) + '.md' : reportPath + '.md';
  return `You are a task worker managed by herdr-task-mcp.\n` +
    `Task ID: ${task.id}\nMode: ${task.mode}\nWorking directory: ${task.cwd}\n` +
    `Complete the following work without delegating unless necessary:\n\n${task.instruction}\n\n` +
    `When all work, tests, and commits are finished, write a UTF-8 JSON report to the EXACT absolute path ${JSON.stringify(reportPath)} ` +
    `using this schema: {"task_id":${JSON.stringify(task.id)},"outcome":"success|failure|blocked","summary":"your concise result","artifacts":["optional paths"]}. ` +
    `Keep summary below 600 characters. If there are longer findings, logs or explanations, ` +
    `write them as Markdown to ${JSON.stringify(detailsPath)} and include that path in artifacts. ` +
    `Write the JSON file atomically (temporary file followed by rename) and LAST, after completing all work. ` +
    `Do not print long outputs to the terminal. Reply with at most one short acknowledgement line ` +
    `(task ID, outcome, report path), never the full report. Never report success if the work is unfinished. ` +
    `If you call task_submit to delegate, pass parent_task_id=${task.id}. ` +
    `The orchestrator uses the report (not terminal idle) to determine the task outcome.`;
}

/** Raised after Herdr creates a pane but cannot start the requested agent. */
export class WorkerStartupError extends Error {
  readonly name = 'WorkerStartupError';
  readonly kind: AgentKind;
  readonly agentName: string;
  readonly paneId: string;
  readonly launchError: string;
  readonly paneOutput: string;

  constructor(kind: AgentKind, agentName: string, paneId: string, launchError: string, paneOutput: string) {
    const transcript = paneOutput.trim() ? `\nPane output (last lines):\n${paneOutput.slice(-3000)}` : '';
    super(`Failed to start ${kind} worker in pane ${paneId}: ${launchError}${transcript}`);
    this.kind = kind;
    this.agentName = agentName;
    this.paneId = paneId;
    this.launchError = launchError;
    this.paneOutput = paneOutput;
  }
}

export class HerdrRuntime implements AgentRuntime {
  private readonly binary: string;
  private readonly cliTimeoutMs: number;
  private readonly workerSettingsPath?: string;
  constructor(binary: string, cliTimeoutMs = 30000, workerSettingsPath?: string) {
    this.binary = binary;
    this.cliTimeoutMs = cliTimeoutMs;
    this.workerSettingsPath = workerSettingsPath;
  }
  private async call(args: string[], timeout = this.cliTimeoutMs, signal?: AbortSignal): Promise<string> {
    try {
      const result = await execFileAsync(this.binary, args, { timeout, signal, maxBuffer: 4 * 1024 * 1024, encoding: 'utf8', windowsHide: true });
      return result.stdout;
    } catch (error) {
      const e = error as Error & { stderr?: string };
      throw new Error(`herdr ${args.slice(0, 2).join(' ')}: ${(e.stderr || e.message).slice(0, 700)}`);
    }
  }
  async start(task: Task): Promise<{ agentName: string; paneId: string }> {
    // Read before allocating a pane; each new task uses the latest workspace settings.
    const settings = await loadWorkerSettings(this.workerSettingsPath);
    const agentName = `ht${randomBytes(8).toString('hex')}`;
    const env = [`--env`, `HERDR_TASK_PARENT_ID=${task.id}`];
    const paneArgs = task.source_pane_id
      ? ['pane', 'split', task.source_pane_id, '--direction', 'right', '--cwd', task.cwd, ...env, '--no-focus']
      : ['workspace', 'create', '--cwd', task.cwd, '--label', `task-${task.id.slice(0, 8)}`, '--no-focus'];
    const created = parseHerdrResult<{ pane?: { pane_id: string }; root_pane?: { pane_id: string } }>(await this.call(paneArgs));
    const paneId = created.pane?.pane_id ?? created.root_pane?.pane_id;
    if (!paneId) throw new Error('Herdr did not return a pane_id');
    try {
      parseHerdrResult(await this.call(buildAgentStartArgs(
        agentName, task.target, paneId, settings.agents[task.target].args
      ), 35000));
    } catch (error) {
      // The agent might not exist yet, so use pane read rather than agent read.
      const paneOutput = await this.call(
        ['pane', 'read', paneId, '--source', 'recent-unwrapped', '--lines', '80'], 10000
      ).catch(() => '');
      throw new WorkerStartupError(task.target, agentName, paneId, String(error), paneOutput);
    }
    return { agentName, paneId };
  }
  /**
   * The worker report is the completion signal, not terminal output or a Herdr
   * idle/done observation (which is not associated with an individual turn).
   */
  async prompt(task: Task, agentName: string, reportPath: string, signal: AbortSignal): Promise<'settled' | 'blocked'> {
    const timeout = Math.max(3000, task.timeout_ms);
    // --wait tracks terminal state and can misclassify or miss a long TUI response.
    // Submit exactly once; a successful response acknowledges the prompt input only.
    parseHerdrResult(await this.call(
      ['agent', 'prompt', agentName, buildWorkerPrompt(task, reportPath)],
      Math.min(timeout, 30000), signal
    ));
    const deadline = Date.now() + timeout;
    let nextStatusCheck = 0;
    while (Date.now() < deadline) {
      if (signal.aborted) throw new Error('Worker wait aborted');
      if (await this.hasValidReport(reportPath, task.id)) return 'settled';
      if (Date.now() >= nextStatusCheck) {
        nextStatusCheck = Date.now() + 2000;
        // Terminal state is advisory: only blocked requires operator attention.
        // Never conclude success from idle/done, which can describe another turn.
        const state = await this.agentState(agentName, signal);
        if (state === 'blocked') return 'blocked';
      }
      await sleep(Math.min(250, Math.max(1, deadline - Date.now())), undefined, { signal });
    }
    if (await this.hasValidReport(reportPath, task.id)) return 'settled';
    throw new Error(`Worker report timeout after ${timeout}ms; inspect agent pane and report ${reportPath}`);
  }

  private async hasValidReport(reportPath: string, taskId: string): Promise<boolean> {
    try {
      const metadata = await stat(reportPath);
      if (!metadata.isFile()) throw new Error('Worker report must be a regular file');
      if (metadata.size > 256 * 1024) {
        throw new Error('Worker report exceeds 256 KiB; write long results to artifacts');
      }
      validateReport(await readFile(reportPath, 'utf8'), taskId);
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT' || error instanceof SyntaxError) return false;
      if (error instanceof Error && /Invalid worker report schema\/task_id/.test(error.message)) return false;
      throw error;
    }
  }

  private async agentState(agentName: string, signal: AbortSignal): Promise<string | undefined> {
    try {
      const result = parseHerdrResult<{
        agent?: { status?: string; state?: string; agent_status?: string };
        status?: string;
      }>(await this.call(['agent', 'get', agentName], 10000, signal));
      return result.agent?.status ?? result.agent?.state ?? result.agent?.agent_status ?? result.status;
    } catch {
      // A missed status read should not discard a valid report written by the worker.
      return undefined;
    }
  }
  async read(agentName: string): Promise<string> {
    return (await this.call(['agent', 'read', agentName, '--source', 'recent-unwrapped', '--lines', '100'], 10000)).slice(-12000);
  }
  async interrupt(agentName: string): Promise<void> {
    await this.call(['agent', 'send-keys', agentName, 'ctrl+c'], 10000);
  }
}

export function validateReport(raw: string, taskId: string): WorkerReport {
  const value: unknown = JSON.parse(raw);
  if (typeof value !== 'object' || value === null) throw new Error('Invalid report object');
  const report = value as Record<string, unknown>;
  if (report.task_id !== taskId || !['success', 'failure', 'blocked'].includes(String(report.outcome)) ||
      typeof report.summary !== 'string' || report.summary.length > 20000 ||
      (report.artifacts !== undefined && (!Array.isArray(report.artifacts) ||
        report.artifacts.some(path => typeof path !== 'string')))) throw new Error('Invalid worker report schema/task_id');
  return report as unknown as WorkerReport;
}
