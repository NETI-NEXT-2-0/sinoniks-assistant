import { ApiError, isRecord, type AuthClient } from "./client";
import type { DesignRevision } from "./designs";

export interface Network19 {
  id: string;
  name: string | null;
  trust: "open" | "closed" | null;
}
export interface System19 {
  id: string;
  name: string | null;
  network_id: string | null;
  kind: "system" | "storage" | "usb";
  attachment?: "node-A" | null;
}
export interface Check19 {
  kind:
    | "name_mask"
    | "type_extension"
    | "size"
    | "signature"
    | "icap"
    | "unsupported";
  details?: string | null;
  icap_service?: "antivirus" | "sandbox" | "dlp" | null;
}
export interface FileSide19 {
  network_id: string;
  manual?: boolean | null;
  usb?: boolean | null;
  manual_protocol?: "SFTP" | null;
  actor_ids?: string[];
  automations?: {
    operation: "PULL" | "PUSH";
    protocol: "SFTP" | "FTP" | "SMB" | null;
    storage_id: string | null;
  }[];
  checks?: Check19[] | null;
  received_files_shared?: boolean | null;
  access_group?: string | null;
}
export interface Flow19 {
  id: string;
  kind: "data" | "file";
  sender_network_id: string | null;
  receiver_network_id: string | null;
  sender_ids: string[];
  receiver_ids: string[];
  purpose: string | null;
  policy?: string | null;
  mode?: "one-way" | "two-way" | null;
  protocol?: string | null;
  destination_port?: number | null;
  application?: "generic" | "ntp";
  direction?: "forward" | "both" | null;
  sides?: FileSide19[];
}
export interface Note19 {
  path: string;
  target_id: string | null;
  reason?: "missing" | "user_unknown";
  explanation?: string;
  basis?: "product_fact" | "user_minimum";
}
export interface RequirementsInput {
  schema_version: 3;
  product_version?: "1.9";
  id: string;
  networks: Network19[];
  systems: System19[];
  flows: Flow19[];
  actors?: { id: string; name: string | null }[];
  manual_users?: { lower: number; upper: number | null } | null;
  protected_network_id?: string | null;
  existing_firewall_name?: string | null;
  unknowns?: Note19[];
  assumptions?: Note19[];
  requests_for_vendor?: unknown[];
  legacy?: unknown;
}
export interface CalculatedResult19 {
  schema_version: '1.1'; status: 'needs_clarification' | 'preliminary' | 'completed' | 'unsupported';
  explanation: string; rules_count: number; rules_lower_bound: number; confirmed_rules_count: number | null;
  file_users_count: number | null; file_users_range: { min: number; max: number | null } | null;
  networks: { id: string; name: string | null; trust: string | null }[];
  devices: { id: string; name: string | null; network_id: string | null }[];
  synonyx: { node_a_network_id: string; node_b_network_id: string } | null;
  rules: { id: number; rule_id: string; type: 'data' | 'file'; from: string; to: string;
    description: string; mode: string; protocol: string | Record<string, string | null> | null;
    port: number | null; checks?: string[];
    directions?: { access_groups?: Record<string, string | null> }[] }[];
}
export interface RequirementsRevision {
  id: string;
  branch_id: string;
  revision: number;
  parent_id: string | null;
  input: RequirementsInput;
  questions: {
    field: string;
    text: string;
    reason: string;
    answered_unknown: boolean;
    target_id: string | null;
  }[];
  result: CalculatedResult19 | null;
  artifact_sha256?: Record<string, string>;
  status: "needs_clarification" | "preliminary" | "ready_for_solver" | "completed" | "unsupported";
}
export type AnyRevision = DesignRevision | RequirementsRevision;
export function isRequirements(row: AnyRevision): row is RequirementsRevision {
  return row.input.schema_version === 3;
}
function parse(value: unknown): AnyRevision {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    typeof value.branch_id !== "string" ||
    typeof value.revision !== "number" ||
    !isRecord(value.input) ||
    !Array.isArray(value.questions)
  )
    throw new ApiError(502, "invalid_response");
  if (value.input.schema_version === 3) {
    if (
      !Array.isArray(value.input.networks) ||
      !Array.isArray(value.input.systems) ||
      !Array.isArray(value.input.flows) ||
      !["needs_clarification", "preliminary", "ready_for_solver", "completed", "unsupported"].includes(
        String(value.status),
      ) ||
      (value.result !== null && (!isRecord(value.result) || value.result.schema_version !== "1.1" || !Array.isArray(value.result.rules) || typeof value.result.explanation !== "string"))
    )
      throw new ApiError(502, "invalid_response");
  } else if (!Array.isArray(value.input.exchanges))
    throw new ApiError(502, "invalid_response");
  return value as unknown as AnyRevision;
}
export async function loadAnyDesign(
  auth: AuthClient,
  chat: string,
  branch: string,
  id?: string | null,
): Promise<{ selected: AnyRevision | null; latest: AnyRevision | null }> {
  const response = await auth.request(
    `/chats/${chat}/designs/latest?branch_id=${encodeURIComponent(branch)}`,
  );
  if (!isRecord(response)) throw new ApiError(502, "invalid_response");
  const latest = response.design === null ? null : parse(response.design);
  const selected =
    id && id !== latest?.id
      ? parse(await auth.request(`/chats/${chat}/designs/${id}`))
      : latest;
  return { selected, latest };
}
export async function saveRequirements(
  auth: AuthClient,
  chat: string,
  body: {
    branch_id: string;
    parent_id: string | null;
    input: RequirementsInput;
  },
  key: string,
): Promise<RequirementsRevision> {
  const row = parse(
    await auth.mutate(`/chats/${chat}/designs`, "POST", body, key),
  );
  if (!isRequirements(row)) throw new ApiError(502, "invalid_response");
  return row;
}
export async function upgradeDesign(
  auth: AuthClient,
  chat: string,
  id: string,
  branch: string,
  key: string,
): Promise<RequirementsRevision> {
  const row = parse(
    await auth.mutate(
      `/chats/${chat}/designs/${id}/upgrade`,
      "POST",
      { branch_id: branch },
      key,
    ),
  );
  if (!isRequirements(row)) throw new ApiError(502, "invalid_response");
  return row;
}
export function emptyRequirements(): RequirementsInput {
  return {
    schema_version: 3,
    id: `project-${crypto.randomUUID()}`,
    networks: [
      { id: "network-A", name: null, trust: null },
      { id: "network-B", name: null, trust: null },
    ],
    systems: [],
    flows: [],
    unknowns: [],
    assumptions: [],
  };
}
