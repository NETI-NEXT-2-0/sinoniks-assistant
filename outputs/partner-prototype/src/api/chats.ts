import type { ChatFile } from '../workspace/ChatAttachments';
import { ApiError, AuthClient, isRecord } from './client';

export type RunStatus = 'queued' | 'running' | 'waiting_input' | 'succeeded' | 'failed' | 'cancelled';
export type RequestMode = 'auto' | 'design' | 'reference';
export interface Chat { id: string; title: string; revision: number; default_branch_id: string; active_run_id: string | null; created_at: string; updated_at: string }
export interface Branch { id: string; chat_id: string; parent_branch_id: string | null; fork_message_id: string | null; head_message_id: string | null; revision: number; created_at: string }
export interface Message { id: string; branch_id: string; run_id: string | null; role: 'user' | 'assistant'; origin: string; ordinal: number; status: string; content: string; created_at: string }
export interface Run { id: string; input_message_id: string; branch_id: string; status: RunStatus; partial_text: string; clarification_id: string | null; clarification_question: string | null; clarification_options: string[]; outcome: unknown; runtime_profile?: string; request_mode: RequestMode; attachments?: ChatFile[]; queue_seq: number; queue_position: number }
export interface Snapshot { chat: Chat; branches: Branch[]; messages: Message[]; messages_next_cursor: string | null; runs: Run[]; selected_branch_id: string; cursor: number }
export interface Page<T> { items: T[]; next_cursor: string | null }
export interface Admission { run_id: string; message_id: string; status: RunStatus; queue_position: number; events_url: string; branch_id?: string; request_mode: RequestMode }
export interface ProjectAnswerInput { schema_version?: 1; base_revision_id: string; path: string; target_id: string | null; side_network_id: string | null; value?: unknown; unknown?: boolean }
export interface RunBody { branch_id: string; message: string; attachment_ids: string[]; web_mode: 'off'; request_mode: RequestMode; project_answer?: ProjectAnswerInput }
export interface ResumeBody { clarification_id: string; answer: string }
export interface BranchBody { parent_branch_id: string; message_id: string; mode: 'edit' | 'regenerate'; revised_text?: string }

function invalid(): never { throw new ApiError(502, 'invalid_response'); }
function record(value: unknown): Record<string, unknown> { return isRecord(value) ? value : invalid(); }
function str(value: unknown): string { return typeof value === 'string' ? value : invalid(); }
function nullable(value: unknown): string | null { return value === null || value === undefined ? null : str(value); }
function num(value: unknown): number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : invalid(); }
function list<T>(value: unknown, parse: (value: unknown) => T): T[] { return Array.isArray(value) ? value.map(parse) : invalid(); }
function status(value: unknown): RunStatus {
  return ['queued', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled'].includes(String(value)) ? value as RunStatus : invalid();
}
function requestMode(value: unknown): RequestMode {
  if (value === undefined) return 'auto';
  if (value === 'auto' || value === 'design' || value === 'reference') return value;
  return invalid();
}
function chat(value: unknown): Chat {
  const row = record(value);
  return { id: str(row.id), title: str(row.title), revision: num(row.revision), default_branch_id: str(row.default_branch_id), active_run_id: nullable(row.active_run_id), created_at: str(row.created_at), updated_at: str(row.updated_at) };
}
function branch(value: unknown): Branch {
  const row = record(value);
  return { id: str(row.id), chat_id: str(row.chat_id), parent_branch_id: nullable(row.parent_branch_id), fork_message_id: nullable(row.fork_message_id), head_message_id: nullable(row.head_message_id), revision: num(row.revision), created_at: str(row.created_at) };
}
export function parseMessage(value: unknown): Message {
  const row = record(value);
  if (row.role !== 'user' && row.role !== 'assistant') invalid();
  return { id: str(row.id), branch_id: str(row.branch_id), run_id: nullable(row.run_id), role: row.role, origin: str(row.origin ?? 'user'), ordinal: num(row.ordinal ?? 0), status: str(row.status), content: str(row.content), created_at: str(row.created_at) };
}
export function parseRun(value: unknown): Run {
  const row = record(value);
  return { id: str(row.id), input_message_id: str(row.input_message_id), branch_id: str(row.branch_id), status: status(row.status), partial_text: str(row.partial_text ?? ''), clarification_id: nullable(row.clarification_id), clarification_question: nullable(row.clarification_question), clarification_options: list(row.clarification_options ?? [], str), outcome: row.outcome, attachments: Array.isArray(row.attachments) ? row.attachments as ChatFile[] : [], request_mode: requestMode(row.request_mode), runtime_profile: typeof row.runtime_profile === 'string' ? row.runtime_profile : undefined, queue_seq: num(row.queue_seq ?? 0), queue_position: num(row.queue_position ?? 0) };
}
function admission(value: unknown): Admission {
  const row = record(value);
  return { run_id: str(row.run_id), message_id: str(row.message_id), status: status(row.status), queue_position: num(row.queue_position), events_url: str(row.events_url), request_mode: requestMode(row.request_mode), ...(typeof row.branch_id === 'string' ? { branch_id: row.branch_id } : {}) };
}
export class ChatsClient {
  constructor(private readonly auth: AuthClient) {}
  async list(cursor?: string): Promise<Page<Chat>> {
    const result = record(await this.auth.request(`/chats${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`));
    return { items: list(result.items, chat), next_cursor: nullable(result.next_cursor) };
  }
  async create(): Promise<Chat> { return chat(await this.auth.mutate('/chats', 'POST', {})); }
  async rename(id: string, title: string, revision: number): Promise<Chat> {
    return chat(await this.auth.mutate(`/chats/${id}`, 'PATCH', { title, expected_revision: revision }));
  }
  async remove(id: string): Promise<void> { await this.auth.mutate(`/chats/${id}`, 'DELETE'); }
  async snapshot(id: string, branchId?: string): Promise<Snapshot> {
    const result = record(await this.auth.request(`/chats/${id}/snapshot${branchId ? `?branch_id=${encodeURIComponent(branchId)}` : ''}`));
    return { chat: chat(result.chat), branches: list(result.branches, branch), messages: list(result.messages, parseMessage), messages_next_cursor: nullable(result.messages_next_cursor), runs: list(result.runs, parseRun), selected_branch_id: str(result.selected_branch_id), cursor: num(result.cursor) };
  }
  async messages(id: string, branchId: string, before: string): Promise<Page<Message>> {
    const result = record(await this.auth.request(`/chats/${id}/messages?branch_id=${encodeURIComponent(branchId)}&before=${encodeURIComponent(before)}`));
    return { items: list(result.items, parseMessage), next_cursor: nullable(result.next_cursor) };
  }
  async submit(id: string, body: RunBody, key: string): Promise<Admission> { return admission(await this.auth.mutate(`/chats/${id}/runs`, 'POST', body, key)); }
  async cancel(id: string): Promise<Run> { return parseRun(await this.auth.mutate(`/runs/${id}/cancel`, 'POST')); }
  async resume(id: string, body: ResumeBody, key: string): Promise<Admission> { return admission(await this.auth.mutate(`/runs/${id}/resume`, 'POST', body, key)); }
  async fork(id: string, body: BranchBody, key: string): Promise<Admission> { return admission(await this.auth.mutate(`/chats/${id}/branches`, 'POST', body, key)); }
}
