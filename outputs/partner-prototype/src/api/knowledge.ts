import { ApiError, AuthClient, isRecord } from './client';
import { readWebSource, type WebSource } from './web-source';

export interface KnowledgeDocument {
  id: string; title: string; revision: number; active_version_id: string | null;
  last_published_version_id: string | null;
  workspace_id: string | null;
}
export interface KnowledgeVersion {
  id: string; generation: number; status: 'pending' | 'processing' | 'ready_for_review' | 'failed';
  error_code: string | null; review_available: boolean; parser_retry_at: string | null;
  parser_attempts: number; text_attempts: number; text_retry_at: string | null;
  needs_reingest: boolean; index_error: string | null; index_attempts: number; index_retry_at: string | null;
  oversized_units: boolean;
  web_source: WebSource | null;
}
export interface KnowledgeExcerpt {
  evidence_id: string; title: string; excerpt: string; web_source: WebSource | null;
}
const summaryFields = [
  'documents', 'published_documents', 'searchable_documents', 'unique_source_urls', 'searchable_excerpts',
] as const;
export type CorpusSummary = Record<(typeof summaryFields)[number], number>;
export interface ReviewBlock {
  id: string; ordinal: number; kind: string; text: string;
  positions?: { page: number | null }[]; locator?: { line: number }; required_context: number[]; parent_context: number[]; needs_parent: boolean; mixed_script_identifier: boolean;
}
export interface AclReviewEntry { unit_id: string; user_id: string; login: string; subject_type: 'user' | 'group'; excerpt: string; truncated: boolean }
export interface AclReview {
  revision: number; incomplete: boolean; previous: AclReviewEntry[]; target: AclReviewEntry[];
}
export interface ReviewPage {
  total_blocks: number;
  canonical_sha256: string; blocks: ReviewBlock[]; warnings: string[]; next_offset: number | null;
  required_notes: number[]; unresolved_notes: boolean; quality_block: string | null;
}
function invalid(): never { throw new ApiError(502, 'invalid_response'); }
export async function corpusSummary(auth: AuthClient): Promise<CorpusSummary> {
  const result = await auth.request('/knowledge/summary');
  if (!isRecord(result) || summaryFields.some((key) => {
    const value = result[key];
    return typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0;
  })) invalid();
  return result as CorpusSummary;
}
export async function searchKnowledge(auth: AuthClient, query: string): Promise<KnowledgeExcerpt[]> {
  const result = await auth.mutate('/knowledge/search', 'POST', { query, limit: 8 });
  if (!isRecord(result) || !Array.isArray(result.items)) invalid();
  return result.items.map((item: unknown) => {
    if (!isRecord(item) || typeof item.evidence_id !== 'string' || typeof item.title !== 'string'
      || typeof item.excerpt !== 'string') invalid();
    return {
      evidence_id: item.evidence_id, title: item.title, excerpt: item.excerpt,
      web_source: readWebSource(item.web_source),
    };
  });
}
function documentOf(value: unknown): KnowledgeDocument {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.title !== 'string'
    || typeof value.revision !== 'number' || !(typeof value.active_version_id === 'string' || value.active_version_id === null)
    || !(typeof value.last_published_version_id === 'string' || value.last_published_version_id === null)
    || !(typeof value.workspace_id === 'string' || value.workspace_id === null)) invalid();
  return value as unknown as KnowledgeDocument;
}
function versionOf(value: unknown): KnowledgeVersion {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.generation !== 'number'
    || !['pending', 'processing', 'ready_for_review', 'failed'].includes(String(value.status))
    || !(typeof value.error_code === 'string' || value.error_code === null)
    || typeof value.review_available !== 'boolean' || typeof value.needs_reingest !== 'boolean'
    || typeof value.oversized_units !== 'boolean'
    || !(typeof value.index_error === 'string' || value.index_error === null)
    || typeof value.index_attempts !== 'number'
    || !(typeof value.index_retry_at === 'string' || value.index_retry_at === null)
    || typeof value.parser_attempts !== 'number' || typeof value.text_attempts !== 'number'
    || !(typeof value.text_retry_at === 'string' || value.text_retry_at === null)
    || !(typeof value.parser_retry_at === 'string' || value.parser_retry_at === null)) invalid();
  return { ...value, web_source: readWebSource(value.web_source) } as unknown as KnowledgeVersion;
}
export async function documents(auth: AuthClient, before?: string): Promise<{ items: KnowledgeDocument[]; next: string | null }> {
  const query = before ? `?before=${encodeURIComponent(before)}` : '';
  const result = await auth.request(`/knowledge/documents${query}`);
  if (!isRecord(result) || !Array.isArray(result.items) || !(typeof result.next_cursor === 'string' || result.next_cursor === null)) invalid();
  return { items: result.items.map(documentOf), next: result.next_cursor as string | null };
}
export async function versions(auth: AuthClient, id: string, before?: number): Promise<{ items: KnowledgeVersion[]; next: number | null }> {
  const query = before === undefined ? '' : `?before=${before}`;
  const result = await auth.request(`/knowledge/documents/${id}/versions${query}`);
  if (!isRecord(result) || !Array.isArray(result.items) || !(typeof result.next_before === 'number' || result.next_before === null)) invalid();
  return { items: result.items.map(versionOf), next: result.next_before as number | null };
}
export async function review(auth: AuthClient, id: string, offset: number): Promise<ReviewPage> {
  const result = await auth.request(`/knowledge/versions/${id}/review?offset=${offset}&limit=50`);
  if (!isRecord(result) || typeof result.canonical_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(result.canonical_sha256)
    || typeof result.total_blocks !== 'number' || !Number.isSafeInteger(result.total_blocks) || result.total_blocks < 0
    || !Array.isArray(result.blocks) || !Array.isArray(result.warnings)
    || !Array.isArray(result.required_notes) || !result.required_notes.every(Number.isInteger)
    || typeof result.unresolved_notes !== 'boolean'
    || !(typeof result.quality_block === 'string' || result.quality_block === null)
    || !result.warnings.every((warning: unknown) => typeof warning === 'string')
    || !(typeof result.next_offset === 'number' || result.next_offset === null)) invalid();
  for (const block of result.blocks) {
    if (!isRecord(block) || typeof block.id !== 'string' || typeof block.ordinal !== 'number'
      || typeof block.kind !== 'string' || typeof block.text !== 'string'
      || !Array.isArray(block.required_context) || !block.required_context.every(Number.isInteger)
      || !Array.isArray(block.parent_context) || !block.parent_context.every(Number.isInteger)
      || typeof block.needs_parent !== 'boolean' || typeof block.mixed_script_identifier !== 'boolean') invalid();
    if (block.positions !== undefined && (!Array.isArray(block.positions) || !block.positions.every(
      (position: unknown) => isRecord(position) && (typeof position.page === 'number' || position.page === null)
    ))) invalid();
    if (block.locator !== undefined && (!isRecord(block.locator) || typeof block.locator.line !== 'number')) invalid();
  }
  return result as unknown as ReviewPage;
}

export async function reviewPreview(auth: AuthClient, id: string): Promise<ReviewPage> {
  const first = await review(auth, id, 0);
  const combined = { ...first, blocks: [...first.blocks] };
  while (combined.next_offset !== null && combined.blocks.length < 1000) {
    const next = await review(auth, id, combined.next_offset);
    if (next.canonical_sha256 !== first.canonical_sha256) throw new ApiError(409, 'canonical_review_changed');
    if (!next.blocks.length || (next.next_offset !== null && next.next_offset <= combined.next_offset)) invalid();
    combined.blocks.push(...next.blocks);
    combined.next_offset = next.next_offset;
  }
  return combined;
}
export async function createDocument(auth: AuthClient, title: string): Promise<KnowledgeDocument> {
  return documentOf(await auth.mutate('/knowledge/documents', 'POST', { title }));
}
export async function publish(auth: AuthClient, id: string, revision: number, confirmDenyReview = false): Promise<KnowledgeDocument> {
  return documentOf(await auth.mutate(`/knowledge/versions/${id}/publish`, 'POST', {
    expected_revision: revision, confirm_deny_review: confirmDenyReview,
  }));
}
export async function unpublish(auth: AuthClient, id: string, revision: number): Promise<KnowledgeDocument> {
  return documentOf(await auth.mutate(`/knowledge/documents/${id}/unpublish`, 'POST', {
    expected_revision: revision,
  }));
}
export async function publicationAclReview(auth: AuthClient, documentId: string, versionId: string): Promise<AclReview> {
  const result = await auth.request(`/knowledge/documents/${documentId}/publication-acl-review?target_version_id=${encodeURIComponent(versionId)}`);
  if (!isRecord(result) || typeof result.revision !== 'number' || typeof result.incomplete !== 'boolean'
    || !Array.isArray(result.previous) || !Array.isArray(result.target)) invalid();
  for (const entry of [...result.previous, ...result.target]) {
    if (!isRecord(entry) || typeof entry.unit_id !== 'string' || typeof entry.user_id !== 'string'
      || typeof entry.login !== 'string' || !['user', 'group'].includes(String(entry.subject_type))
      || typeof entry.excerpt !== 'string' || typeof entry.truncated !== 'boolean') invalid();
  }
  return result as unknown as AclReview;
}
export async function document(auth: AuthClient, id: string): Promise<KnowledgeDocument> {
  return documentOf(await auth.request(`/knowledge/documents/${id}`));
}
