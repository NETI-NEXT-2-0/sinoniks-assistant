export interface SensitiveSpan { start: number; end: number; label: string }
export function sensitiveSpans(text: string): SensitiveSpan[];
export function eligibleBlock(block: { text: string }): boolean;
export function defaultSelection(blocks: { id: string; text: string }[]): Set<string>;
