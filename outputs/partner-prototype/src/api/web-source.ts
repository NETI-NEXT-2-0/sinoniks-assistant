export interface WebSource { url: string; captured_on: string }

export function readWebSource(value: unknown): WebSource | null {
  if (!value || typeof value !== 'object' || !('url' in value) || !('captured_on' in value)
    || typeof value.url !== 'string' || typeof value.captured_on !== 'string'
    || !/^\d{4}-\d{2}-\d{2}$/.test(value.captured_on)) return null;
  try {
    const url = new URL(value.url);
    if (!value.url.startsWith('https://') || url.username || url.password || url.port) return null;
    return { url: url.href, captured_on: value.captured_on };
  } catch { return null; }
}
