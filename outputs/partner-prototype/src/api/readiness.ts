import { isRecord } from './client';

export interface ReadinessItem {
  code?: string;
  path?: string | null;
  subject: string;
  message: string;
  answered_unknown?: boolean;
  source?: string;
}
export interface ReadinessReport {
  schema_version: 1;
  checker_version: string;
  status: 'checked' | 'needs_clarification' | 'preliminary' | 'conflict' | 'unsupported';
  title: string;
  summary: string;
  missing: ReadinessItem[];
  conflicts: ReadinessItem[];
  limitations: ReadinessItem[];
  recommendations: ReadinessItem[];
  assumptions: ReadinessItem[];
  checks: { code: string; title: string; status: 'passed' | 'pending' | 'failed' }[];
}
export function readReadiness(value: unknown): ReadinessReport | null {
  if (!isRecord(value) || value.schema_version !== 1 || typeof value.checker_version !== 'string'
    || !['checked', 'needs_clarification', 'preliminary', 'conflict', 'unsupported'].includes(String(value.status))
    || typeof value.title !== 'string' || typeof value.summary !== 'string') return null;
  for (const key of ['missing', 'conflicts', 'limitations', 'recommendations', 'assumptions']) {
    const items = value[key];
    if (!Array.isArray(items) || items.length > 4096 || !items.every(item => isRecord(item)
      && typeof item.subject === 'string' && typeof item.message === 'string'
      && (item.source === undefined || typeof item.source === 'string'))) return null;
  }
  if (!Array.isArray(value.checks) || !value.checks.every(item => isRecord(item)
    && typeof item.code === 'string' && typeof item.title === 'string'
    && ['passed', 'pending', 'failed'].includes(String(item.status)))) return null;
  return value as unknown as ReadinessReport;
}
