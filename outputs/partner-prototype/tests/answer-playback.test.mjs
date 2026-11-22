import test from 'node:test';
import assert from 'node:assert/strict';
import { AnswerPlayback } from '../src/workspace/answer-playback.mjs';

test('bursty chunks reveal a monotonic prefix and completion drains without replay', () => {
  const player = new AnswerPlayback();
  const target = 'Последовательный плавный ответ. '.repeat(60);
  player.update(target, {}, 0);
  assert.equal(player.text, '');
  let previous = '';
  for (let now = 16; now < 200; now += 16) {
    player.advance(now);
    assert.ok(player.text.startsWith(previous) && target.startsWith(player.text));
    previous = player.text;
  }
  assert.ok(previous.length > 0 && previous.length < target.length);
  player.update(target, { finished: true }, 200);
  assert.equal(player.text, previous);
  for (let now = 208; now <= 1632; now += 16) player.advance(now);
  assert.equal(player.text, target); assert.equal(player.pending, false);
  player.update(target, { finished: true }, 2000);
  assert.equal(player.text, target);
});
test('emoji sequences and combining marks are never split, even across transport chunks', () => {
  const player = new AnswerPlayback('A');
  player.update('A👨', {}, 0); player.advance(16);
  player.update('A👨‍👩‍👧‍👦е́ конец', {}, 17);
  for (let now = 32; now < 300; now += 16) {
    player.advance(now);
    assert.ok(['A', 'A👨‍👩‍👧‍👦', 'A👨‍👩‍👧‍👦е́', 'A👨‍👩‍👧‍👦е́ ', 'A👨‍👩‍👧‍👦е́ к', 'A👨‍👩‍👧‍👦е́ ко', 'A👨‍👩‍👧‍👦е́ кон', 'A👨‍👩‍👧‍👦е́ коне', 'A👨‍👩‍👧‍👦е́ конец'].includes(player.text));
  }
  assert.equal(player.text, 'A👨‍👩‍👧‍👦е́ конец');
});
test('corrections, stop/reduced-motion and history settle to authoritative text', () => {
  const player = new AnswerPlayback('Old response');
  player.update('Replacement', {}, 0);
  assert.equal(player.text, 'Replacement');
  player.update('Replacement plus a long tail', {}, 10);
  assert.equal(player.text, 'Replacement');
  player.update('Replacement plus a long tail', { immediate: true }, 20);
  assert.equal(player.pending, false);
  assert.equal(new AnswerPlayback('Historical answer').text, 'Historical answer');
});
test('idle time is not spent instantly revealing the next chunk', () => {
  const player = new AnswerPlayback('First chunk.');
  player.advance(0);
  player.update('First chunk. Second chunk with several words.', {}, 10000);
  player.advance(10016);
  assert.ok(player.text.length > 'First chunk.'.length && player.pending);
});
