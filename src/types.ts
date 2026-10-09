export type AgentKind = 'codex' | 'claude';
export type TaskStatus = 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'BLOCKED' | 'CANCELLED';
export type TaskMode = 'implement' | 'review' | 'research';

export interface SubmitTask {
  target: AgentKind;
  instruction: string;
  cwd: string;
  mode?: TaskMode;
  source_pane_id?: string;
  parent_task_id?: string;
  dependencies?: string[];
  timeout_ms?: number;
}

export interface Task {
  id: string;
  target: AgentKind;
  instruction: string;
  cwd: string;
  mode: TaskMode;
  source_pane_id: string | null;
  parent_task_id: string | null;
  depth: number;
  dependencies: string[];
  timeout_ms: number;
  status: TaskStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  agent_name: string | null;
  pane_id: string | null;
  summary: string | null;
  error: string | null;
  report_path: string | null;
}

export interface WorkerReport {
  task_id: string;
  outcome: 'success' | 'failure' | 'blocked';
  summary: string;
  artifacts?: string[];
}

export const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set(['SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED']);
