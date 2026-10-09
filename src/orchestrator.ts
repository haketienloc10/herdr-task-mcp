import { mkdir, readFile, realpath } from 'node:fs/promises';
import { isAbsolute, join, relative } from 'node:path';
import { stat } from 'node:fs/promises';
import type { Config } from './config.ts';
import type { AgentRuntime } from './herdr.ts';
import { validateReport } from './herdr.ts';
import { TaskStore } from './store.ts';
import { TERMINAL_STATUSES, type SubmitTask, type Task, type TaskStatus } from './types.ts';

const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

export class Orchestrator {
  private running = new Map<string, AbortController>();
  private timer: NodeJS.Timeout | undefined;
  private pumping = false;
  private stopping = false;
  private readonly jobs = new Set<Promise<void>>();
  readonly store: TaskStore;
  readonly runtime: AgentRuntime;
  readonly config: Config;
  constructor(store: TaskStore, runtime: AgentRuntime, config: Config) {
    this.store = store; this.runtime = runtime; this.config = config;
  }

  start(): void {
    this.store.recoverInterrupted();
    this.timer = setInterval(() => { void this.pump(); }, this.config.pollMs);
    void this.pump();
  }
  async stop(): Promise<void> {
    this.stopping = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
    for (const controller of this.running.values()) controller.abort();
    await Promise.allSettled([...this.jobs]);
  }
  private async validate(input: SubmitTask): Promise<{ normalized: SubmitTask; depth: number }> {
    if (!['codex', 'claude'].includes(input.target)) throw new Error('target must be codex or claude');
    if (!['implement', 'review', 'research'].includes(input.mode ?? 'implement')) throw new Error('Invalid mode');
    if (!input.instruction?.trim() || input.instruction.length > 30000) throw new Error('instruction must be 1..30000 characters');
    if (input.source_pane_id && !/^[a-zA-Z0-9:_-]{2,80}$/.test(input.source_pane_id)) throw new Error('Invalid source_pane_id');
    if (input.timeout_ms !== undefined && (!Number.isSafeInteger(input.timeout_ms) || input.timeout_ms < 5000 || input.timeout_ms > 3600000))
      throw new Error('timeout_ms must be between 5000 and 3600000');
    if (input.dependencies && (input.dependencies.length > 32 || new Set(input.dependencies).size !== input.dependencies.length))
      throw new Error('Too many or duplicate dependencies');
    const cwd = await realpath(input.cwd);
    if (!(await stat(cwd)).isDirectory()) throw new Error('cwd must be a directory');
    if (this.config.workspaceRoot) {
      const root = await realpath(this.config.workspaceRoot);
      const rel = relative(root, cwd);
      if (rel === '..' || rel.startsWith(`..${process.platform === 'win32' ? '\\' : '/'}`) || isAbsolute(rel)) {
        throw new Error(`cwd must remain inside workspace: ${root}`);
      }
    }
    const parent = input.parent_task_id ? this.store.get(input.parent_task_id) : null;
    if (input.parent_task_id && !parent) throw new Error('Unknown parent_task_id');
    if (parent && TERMINAL_STATUSES.has(parent.status)) throw new Error('Parent task already finished');
    const depth = parent ? parent.depth + 1 : 0;
    if (depth > this.config.maxDepth) throw new Error(`Task delegation depth exceeds ${this.config.maxDepth}`);
    if (parent && this.store.childCount(parent.id) >= this.config.maxChildren) throw new Error('Parent task child limit exceeded');
    for (const id of input.dependencies ?? []) {
      if (!this.store.get(id)) throw new Error(`Unknown dependency: ${id}`);
      if (id === input.parent_task_id) throw new Error('Task cannot depend on its parent (deadlock risk)');
    }
    return { normalized: { ...input, cwd, instruction: input.instruction.trim() }, depth };
  }
  async submit(input: SubmitTask): Promise<Task> {
    const { normalized, depth } = await this.validate(input);
    const task = this.store.create(normalized, depth);
    void this.pump();
    return task;
  }
  status(id: string): Task {
    const task = this.store.get(id);
    if (!task) throw new Error(`Task ${id} not found`);
    return task;
  }
  list(status?: TaskStatus): Task[] { return this.store.list(status); }
  async wait(id: string, timeoutMs = 15000): Promise<Task> {
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 0 || timeoutMs > 20000) throw new Error('wait timeout must be 0..20000ms');
    const until = Date.now() + timeoutMs;
    let task = this.status(id);
    while (!TERMINAL_STATUSES.has(task.status) && Date.now() < until) {
      await sleep(Math.min(150, until - Date.now()));
      task = this.status(id);
    }
    return task;
  }
  async cancel(id: string): Promise<Task> {
    const task = this.status(id);
    if (TERMINAL_STATUSES.has(task.status)) return task;
    this.store.patch(id, { status: 'CANCELLED', finished_at: new Date().toISOString(), error: 'Cancelled by client' });
    this.running.get(id)?.abort();
    if (task.agent_name) {
      try { await this.runtime.interrupt(task.agent_name); }
      catch (err) { this.store.patch(id, { error: `Cancellation requested; interrupt failed: ${String(err)}` }); }
    }
    // Cancel all descendants (no orphaned nested tasks).
    for (const child of this.store.list().filter(item => item.parent_task_id === id && !TERMINAL_STATUSES.has(item.status))) {
      await this.cancel(child.id);
    }
    return this.status(id);
  }
  private async pump(): Promise<void> {
    if (this.pumping || this.stopping) return;
    this.pumping = true;
    try {
      const queued = this.store.list('QUEUED');
      for (const task of queued) {
        if (this.running.size >= this.config.maxConcurrent) break;
        const deps = task.dependencies.map(id => this.store.get(id));
        if (deps.some(d => !d || ['FAILED', 'BLOCKED', 'CANCELLED'].includes(d.status))) {
          this.store.patch(task.id, { status: 'BLOCKED', finished_at: new Date().toISOString(), error: 'Dependency did not succeed' });
          continue;
        }
        if (deps.some(d => d!.status !== 'SUCCEEDED')) continue;
        // Keep one worker slot for child delegation to avoid a trivial parent/child deadlock.
        if (task.depth === 0 && this.running.size >= Math.max(0, this.config.maxConcurrent - 1)) continue;
        if (!this.store.claim(task.id)) continue;
        const signal = new AbortController();
        this.running.set(task.id, signal);
        const job = this.run(task.id, signal).finally(() => {
          this.running.delete(task.id);
          this.jobs.delete(job);
          void this.pump();
        });
        this.jobs.add(job);
      }
    } finally { this.pumping = false; }
  }
  private async run(id: string, controller: AbortController): Promise<void> {
    const reportPath = join(this.config.reportDir, `${id}.json`);
    let agentName: string | undefined;
    try {
      await mkdir(this.config.reportDir, { recursive: true, mode: 0o700 });
      const task = this.status(id);
      const started = await this.runtime.start(task);
      agentName = started.agentName;
      this.store.patch(id, { agent_name: started.agentName, pane_id: started.paneId, report_path: reportPath });
      if (controller.signal.aborted) {
        await this.runtime.interrupt(started.agentName).catch(() => {});
        if (this.status(id).status === 'RUNNING') this.store.patch(id, {
          status: 'BLOCKED', finished_at: new Date().toISOString(), error: 'Daemon stopped during worker startup'
        });
        return;
      }
      const state = await this.runtime.prompt(task, started.agentName, reportPath, controller.signal);
      if (this.status(id).status !== 'RUNNING') return;
      if (controller.signal.aborted) {
        this.store.patch(id, { status: 'BLOCKED', finished_at: new Date().toISOString(), error: 'Daemon stopped; inspect worker pane' });
        return;
      }
      if (state === 'blocked') {
        this.store.patch(id, { status: 'BLOCKED', finished_at: new Date().toISOString(), error: 'Worker needs user input in Herdr pane' });
        return;
      }
      let report;
      try { report = validateReport(await readFile(reportPath, 'utf8'), id); }
      catch (error) {
        const screen = await this.runtime.read(started.agentName).catch(() => '');
        this.store.patch(id, { status: 'BLOCKED', finished_at: new Date().toISOString(),
          error: `Missing/invalid worker report: ${String(error)}`, summary: screen.slice(-3000) });
        return;
      }
      const resultStatus = report.outcome === 'success' ? 'SUCCEEDED' : report.outcome === 'blocked' ? 'BLOCKED' : 'FAILED';
      this.store.patch(id, { status: resultStatus, finished_at: new Date().toISOString(), summary: report.summary,
        error: resultStatus === 'FAILED' ? 'Worker reported failure' : null });
    } catch (error) {
      if (this.status(id).status !== 'RUNNING') return;
      const errorText = String(error);
      if (controller.signal.aborted) {
        this.store.patch(id, { status: 'BLOCKED', finished_at: new Date().toISOString(), error: 'Daemon stopped; inspect worker pane' });
        return;
      }
      const timedOut = errorText.toLowerCase().includes('timeout') || errorText.includes('ETIMEDOUT');
      this.store.patch(id, { status: timedOut ? 'BLOCKED' : 'FAILED', finished_at: new Date().toISOString(), error: errorText.slice(0, 1500) });
      if (timedOut && agentName) await this.runtime.interrupt(agentName).catch(() => {});
    }
  }
}
