import { McpServer } from '@modelcontextprotocol/server';
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import * as z from 'zod/v4';
import { rpc } from './http.ts';

const taskId = z.string().uuid();
const taskStatus = z.enum(['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED']);

export async function serveMcp(socketPath: string): Promise<void> {
  const server = new McpServer({ name: 'herdr-task-mcp', version: '0.1.0' }, {
    instructions: 'Use task_submit to delegate tasks to Codex or Claude. Call task_wait/status then task_result. SUCCEEDED means worker-reported success, not independently verified code. Include parent_task_id when delegating from a worker. A BLOCKED task needs manual inspection in its Herdr pane.'
  });
  const result = (value: unknown) => ({ content: [{ type: 'text' as const, text: JSON.stringify(value) }] });
  const invoke = async (action: string, payload: Record<string, unknown>) => {
    try { return result(await rpc(socketPath, action, payload)); }
    catch (error) { return { ...result({ error: String(error) }), isError: true as const }; }
  };
  server.registerTool('task_submit', {
    description: 'Queue a task for an isolated Codex/Claude Herdr worker. Returns task ID immediately; does not wait for completion.',
    inputSchema: z.object({
      target: z.enum(['codex', 'claude']), instruction: z.string().min(1).max(30000),
      cwd: z.string().min(1), mode: z.enum(['implement', 'review', 'research']).default('implement'),
      source_pane_id: z.string().optional(), parent_task_id: taskId.optional(),
      dependencies: z.array(taskId).max(32).default([]), timeout_ms: z.number().int().min(5000).max(3600000).default(300000)
    })
  }, args => invoke('submit', {
    ...args,
    source_pane_id: args.source_pane_id ?? process.env.HERDR_PANE_ID,
    parent_task_id: process.env.HERDR_TASK_PARENT_ID ?? args.parent_task_id
  }));
  server.registerTool('task_status', {
    description: 'Get lifecycle state and metadata for an existing task.',
    inputSchema: z.object({ task_id: taskId })
  }, args => invoke('status', { id: args.task_id }));
  server.registerTool('task_wait', {
    description: 'Long-poll up to 20 seconds for terminal state; call again if still QUEUED/RUNNING.',
    inputSchema: z.object({ task_id: taskId, timeout_ms: z.number().int().min(0).max(20000).default(15000) })
  }, args => invoke('wait', { id: args.task_id, timeout_ms: args.timeout_ms }));
  server.registerTool('task_result', {
    description: 'Read the worker report summary and Herdr pane for a task.',
    inputSchema: z.object({ task_id: taskId })
  }, args => invoke('result', { id: args.task_id }));
  server.registerTool('task_cancel', {
    description: 'Cancel a task and its descendants; attempt Ctrl+C in running Herdr worker.',
    inputSchema: z.object({ task_id: taskId })
  }, args => invoke('cancel', { id: args.task_id }));
  server.registerTool('task_list', {
    description: 'List tasks stored by the shared orchestration daemon.',
    inputSchema: z.object({ status: taskStatus.optional() })
  }, args => invoke('list', args));
  await server.connect(new StdioServerTransport());
}
