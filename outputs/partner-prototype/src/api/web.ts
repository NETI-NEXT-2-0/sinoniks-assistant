import { ApiError, isRecord, type AuthClient } from './client';

export interface OfficialResult { url: string; title: string; excerpt: string }
export interface OfficialSearch { items: OfficialResult[]; partial: boolean }

export async function officialDomains(auth: AuthClient): Promise<string[]> {
  const result = await auth.request('/web/official-domains');
  if (!isRecord(result) || !Array.isArray(result.domains)
    || !result.domains.every((item: unknown) => typeof item === 'string')) {
    throw new ApiError(502, 'invalid_response');
  }
  return result.domains;
}

export async function searchOfficial(auth: AuthClient, domain: string, query: string): Promise<OfficialSearch> {
  const result = await auth.mutate('/web/search', 'POST', { domain, query, confirmed_public: true });
  if (!isRecord(result) || typeof result.partial !== 'boolean' || !Array.isArray(result.items)) {
    throw new ApiError(502, 'invalid_response');
  }
  const items = result.items.filter((item: unknown): item is OfficialResult => {
    if (!isRecord(item) || typeof item.url !== 'string' || typeof item.title !== 'string'
      || typeof item.excerpt !== 'string') return false;
    try {
      const url = new URL(item.url);
      return url.protocol === 'https:' && !url.username && !url.password && !url.port
        && (url.hostname === domain || url.hostname.endsWith(`.${domain}`));
    } catch { return false; }
  });
  return { items, partial: result.partial };
}
