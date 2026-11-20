import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { sensitiveSpans, defaultSelection } from '../src/knowledge/review-tools.mjs';

test('whole reviewed copies and many hints finish within a bounded process', () => {
  const module = new URL('../src/knowledge/review-tools.mjs', import.meta.url).href;
  const program = `import assert from 'node:assert/strict';
    import { sensitiveSpans } from ${JSON.stringify(module)};
    assert.equal(sensitiveSpans('a'.repeat(1_000_000)).length, 0);
    assert.equal(sensitiveSpans('person@example.test '.repeat(20_000)).length, 20_000);`;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', program], { timeout: 5000, encoding: 'utf8' });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stderr);
});

test('local hints locate potentially sensitive substrings without changing source text', () => {
  const text = 'Почта test@example.org, телефон +7 (999) 123-45-67. СНИЛС 123-456-789 00.';
  const spans = sensitiveSpans(text);
  assert.deepEqual(spans.map(span => text.slice(span.start, span.end)), [
    'test@example.org', '+7 (999) 123-45-67', '123-456-789 00',
  ]);
  assert.equal(sensitiveSpans('Срок 20–22 ноября, курс AB-123.').length, 0);
  assert.equal(sensitiveSpans('<script>test@example.org</script>')[0].start, 8);
});

test('review starts with eligible blocks included, including flagged blocks until explicitly excluded', () => {
  const blocks = [
    { id: 'normal', text: 'Обычный текст' }, { id: 'sensitive', text: 'test@example.org' },
    { id: 'oversized', text: 'x'.repeat(1601) }, { id: 'unicode', text: '😀'.repeat(1600) },
  ];
  assert.deepEqual([...defaultSelection(blocks)], ['normal', 'sensitive', 'unicode']);
  assert.equal(defaultSelection(Array.from({ length: 1001 }, (_, i) => ({ id: String(i), text: 'x' }))).size, 1000);
});
