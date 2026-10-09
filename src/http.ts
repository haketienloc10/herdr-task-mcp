import http from 'node:http';
import { promises as fs } from 'node:fs';
import { dirname } from 'node:path';
import type { AddressInfo } from 'node:net';
import type { Orchestrator } from './orchestrator.ts';
import type { SubmitTask, TaskStatus } from './types.ts';

interface ApiRequest { action: string; payload?: Record<string, unknown> }

export function createApiHandler(orchestrator: Orchestrator) {
  return async (request: http.IncomingMessage, response: http.ServerResponse): Promise<void> => {
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    if (request.method !== 'POST' || request.url !== '/rpc') { response.writeHead(404).end(JSON.stringify({ error: 'Not found' })); return; }
    try {
      let raw = '';
      for await (const chunk of request) {
        raw += String(chunk);
        if (raw.length > 64 * 1024) throw new Error('Request too large');
      }
      const { action, payload = {} } = JSON.parse(raw) as ApiRequest;
      let result: unknown;
      switch (action) {
        case 'submit': result = await orchestrator.submit(payload as unknown as SubmitTask); break;
        case 'status': result = orchestrator.status(String(payload.id ?? '')); break;
        case 'list': result = orchestrator.list(payload.status as TaskStatus | undefined); break;
        case 'wait': result = await orchestrator.wait(String(payload.id ?? ''), Number(payload.timeout_ms ?? 15000)); break;
        case 'cancel': result = await orchestrator.cancel(String(payload.id ?? '')); break;
        case 'result': {
          const task = orchestrator.status(String(payload.id ?? ''));
          result = { task_id: task.id, status: task.status, summary: task.summary, error: task.error,
            report_path: task.report_path, pane_id: task.pane_id };
          break;
        }
        default: throw new Error(`Unknown action: ${action}`);
      }
      response.writeHead(200).end(JSON.stringify({ result }));
    } catch (error) {
      response.writeHead(400).end(JSON.stringify({ error: String(error) }));
    }
  };
}

export async function serveSocket(socketPath: string, orchestrator: Orchestrator): Promise<http.Server> {
  await fs.mkdir(dirname(socketPath), { recursive: true, mode: 0o700 });
  try {
    await fs.lstat(socketPath);
    // Never unlink a socket without checking whether it has a live listener.
    try { await rpc(socketPath, 'list', {}, 1000); throw new Error(`Daemon already listening on ${socketPath}`); }
    catch (error) {
      if (!(error instanceof Error) || !/ECONNREFUSED|ENOENT/.test(error.message)) throw error;
      await fs.unlink(socketPath).catch(err => { if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err; });
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const server = http.createServer((req, res) => { void createApiHandler(orchestrator)(req, res); });
  try {
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(socketPath, () => { server.off('error', reject); resolve(); });
    });
    await fs.chmod(socketPath, 0o600);
  } catch (error) { server.close(); throw error; }
  return server;
}

export async function rpc(socketPath: string, action: string, payload: Record<string, unknown> = {}, timeoutMs = 24000): Promise<unknown> {
  return await new Promise((resolve, reject) => {
    const request = http.request({ socketPath, path: '/rpc', method: 'POST', headers: { 'content-type': 'application/json' }, timeout: timeoutMs }, response => {
      let data = '';
      response.setEncoding('utf8');
      response.on('data', chunk => { data += chunk; if (data.length > 1024 * 1024) request.destroy(new Error('Response too large')); });
      response.on('end', () => {
        try {
          const body = JSON.parse(data) as { error?: string; result?: unknown };
          if (response.statusCode !== 200 || body.error) reject(new Error(body.error ?? `HTTP ${response.statusCode}`));
          else resolve(body.result);
        } catch (error) { reject(error); }
      });
    });
    request.on('timeout', () => request.destroy(new Error('RPC timeout')));
    request.on('error', reject);
    request.end(JSON.stringify({ action, payload }));
  });
}
