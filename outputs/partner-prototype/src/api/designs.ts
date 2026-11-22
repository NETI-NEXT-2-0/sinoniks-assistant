import { ApiError, isRecord, type AuthClient } from './client';

export type ExchangeMode = 'transport' | 'diode' | 'filetransfer';
export const networkProtocols = ['TCP', 'UDP', 'MQTT', 'Modbus TCP', 'МЭК 104'] as const;
export interface ExchangeInput {
  requirement_id?: string | null;
  sender_side: 'A' | 'B' | null; sender: string | null; receiver: string | null;
  mode: ExchangeMode | null; service: string | null; policy: string | null;
  protocol?: typeof networkProtocols[number] | null; destination_port?: number | null;
  sources?: { kind: string; reference: string; revision: number }[];
}
export interface DesignInput {
  schema_version?: number;
  network_a: string | null; network_b: string | null;
  firewall: 'A' | 'B' | 'both' | null; exchanges: ExchangeInput[];
}
export interface DesignResult {
  explanation: string; warnings: string[];
  network_nodes: { id: string; name: string }[]; device_nodes: { id: string; name: string }[];
  connections: { id: string; source: string; target: string; kind: string }[];
  rules: (ExchangeInput & { id: string; requirement_ids: string[]; additional_senders?: string[] })[];
  counts: { logical_exchanges: number; network_slot_pairs: number; network_node_settings: number; file_transfer_policies: number };
}
export interface DesignRevision {
  id: string; branch_id: string; revision: number; parent_id: string | null; input: DesignInput;
  questions: { field: string; text: string; reason: string }[];
  result: DesignResult | null; status: 'needs_clarification' | 'conceptual';
}
export interface DesignCommand { branch_id: string; parent_id: string | null; input: DesignInput }

function parse(value: unknown): DesignRevision {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.revision !== 'number'
    || typeof value.branch_id !== 'string'
    || !isRecord(value.input) || !Array.isArray(value.input.exchanges) || !Array.isArray(value.questions)
    || !['needs_clarification', 'conceptual'].includes(String(value.status))) {
    throw new ApiError(502, 'invalid_response');
  }
  return value as unknown as DesignRevision;
}
export async function latestDesign(auth: AuthClient, chatId: string, branch: string): Promise<DesignRevision | null> {
  const response = await auth.request(`/chats/${chatId}/designs/latest?branch_id=${encodeURIComponent(branch)}`);
  if (!isRecord(response)) throw new ApiError(502, 'invalid_response');
  return response.design === null ? null : parse(response.design);
}
export async function saveDesign(auth: AuthClient, chatId: string, command: DesignCommand, key: string): Promise<DesignRevision> {
  return parse(await auth.mutate(`/chats/${chatId}/designs`, 'POST', command, key));
}
export async function getDesign(auth: AuthClient, chatId: string, id: string): Promise<DesignRevision> {
  return parse(await auth.request(`/chats/${chatId}/designs/${id}`));
}
