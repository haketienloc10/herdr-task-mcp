import { randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import type { SubmitTask, Task, TaskStatus } from './types.ts';

interface Row extends Omit<Task, 'dependencies'> { dependencies: string }

export class TaskStore {
  private readonly db: DatabaseSync;
  constructor(path: string) {
    this.db = new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=3000;
      CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, target TEXT NOT NULL, instruction TEXT NOT NULL,
        cwd TEXT NOT NULL, mode TEXT NOT NULL, source_pane_id TEXT,
        parent_task_id TEXT, depth INTEGER NOT NULL, dependencies TEXT NOT NULL,
        timeout_ms INTEGER NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
        agent_name TEXT, pane_id TEXT, summary TEXT, error TEXT, report_path TEXT
      );
      CREATE INDEX IF NOT EXISTS tasks_status_created ON tasks(status, created_at);
      CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id);
    `);
  }
  private decode(row: unknown): Task {
    const task = row as Row;
    return { ...task, dependencies: JSON.parse(task.dependencies) as string[] };
  }
  get(id: string): Task | null {
    const row = this.db.prepare('SELECT * FROM tasks WHERE id=?').get(id);
    return row ? this.decode(row) : null;
  }
  list(status?: TaskStatus): Task[] {
    const rows = status ? this.db.prepare('SELECT * FROM tasks WHERE status=? ORDER BY created_at, id').all(status)
      : this.db.prepare('SELECT * FROM tasks ORDER BY created_at, id').all();
    return rows.map(row => this.decode(row));
  }
  childCount(id: string): number {
    const row = this.db.prepare('SELECT COUNT(*) as n FROM tasks WHERE parent_task_id=?').get(id) as { n: number };
    return row.n;
  }
  create(input: SubmitTask, depth: number): Task {
    const id = randomUUID();
    this.db.prepare(`INSERT INTO tasks
      (id, target, instruction, cwd, mode, source_pane_id, parent_task_id, depth, dependencies,
       timeout_ms, status, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?)`).run(
      id, input.target, input.instruction, input.cwd, input.mode ?? 'implement',
      input.source_pane_id ?? null, input.parent_task_id ?? null, depth,
      JSON.stringify(input.dependencies ?? []), input.timeout_ms ?? 300000,
      new Date().toISOString());
    return this.get(id)!;
  }
  patch(id: string, patch: Partial<Omit<Task, 'id' | 'dependencies'>>): Task {
    const entries = Object.entries(patch).filter(([key]) => key !== 'id');
    if (!entries.length) return this.get(id)!;
    const allowed = new Set(['status', 'started_at', 'finished_at', 'agent_name', 'pane_id', 'summary', 'error', 'report_path']);
    if (entries.some(([key]) => !allowed.has(key))) throw new Error('Invalid task patch');
    const set = entries.map(([key]) => `${key}=?`).join(', ');
    this.db.prepare(`UPDATE tasks SET ${set} WHERE id=?`).run(...entries.map(([, value]) => value), id);
    return this.get(id)!;
  }
  claim(id: string): boolean {
    const result = this.db.prepare("UPDATE tasks SET status='RUNNING', started_at=? WHERE id=? AND status='QUEUED'")
      .run(new Date().toISOString(), id);
    return result.changes === 1;
  }
  recoverInterrupted(): number {
    const result = this.db.prepare(`UPDATE tasks SET status='BLOCKED', finished_at=?,
      error='Daemon restarted while worker was running; inspect its pane before retrying.' WHERE status='RUNNING'`)
      .run(new Date().toISOString());
    return Number(result.changes);
  }
  close(): void { this.db.close(); }
}
