// Local review hints only. These heuristics neither classify nor sanitize a document.
export function sensitiveSpans(text) {
  const patterns = [
    ['Возможный API-ключ', /(?:gw_sk_|sk-)[A-Za-z0-9_-]{12,}/gu],
    ['Электронная почта', /(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/giu],
    ['Возможный телефон', /(?<!\d)(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)/gu],
    ['Возможный СНИЛС', /(?<!\d)\d{3}-\d{3}-\d{3}[ -]\d{2}(?!\d)/gu],
    ['Возможные паспортные данные', /(?<!\d)\d{4}\s+\d{6}(?!\d)/gu],
    ['Возможный номер карты', /(?<!\d)(?:\d[ -]?){15}\d(?!\d)/gu],
  ];
  const hits = patterns.flatMap(([label, pattern]) => [...text.matchAll(pattern)]
    .map(match => ({ start: match.index, end: match.index + match[0].length, label })));
  hits.sort((a, b) => a.start - b.start || b.end - a.end);
  const result = [];
  let occupiedUntil = 0;
  for (const hit of hits) {
    if (hit.start >= occupiedUntil) result.push(hit);
    occupiedUntil = Math.max(occupiedUntil, hit.end);
  }
  return result;
}

export function eligibleBlock(block) {
  return Array.from(block.text).length <= 1600;
}

export function defaultSelection(blocks) {
  return new Set(blocks.filter(eligibleBlock).slice(0, 1000).map(block => block.id));
}
