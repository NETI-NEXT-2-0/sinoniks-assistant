import test from 'node:test';
import assert from 'node:assert/strict';
import { registerFileDrop } from '../src/workspace/file-drop.mjs';

function drag(target, name, files = [], types = ['Files']) {
  const event = new Event(name, { cancelable: true });
  event.dataTransfer = { files, types };
  // Browsers may omit relatedTarget even when moving between children.
  event.relatedTarget = null;
  target.dispatchEvent(event);
  return event;
}
test('nested file drags keep the target active until drop and deliver every file once', () => {
  const target = new EventTarget(), active = [], batches = [];
  const dispose = registerFileDrop(target, { onActive: value => active.push(value), onFiles: files => batches.push(files), isAvailable: () => true });
  drag(target, 'dragenter'); drag(target, 'dragenter'); drag(target, 'dragleave');
  assert.equal(active.at(-1), true);
  const files = [new File(['one'], 'one.txt'), new File(['two'], 'two.pdf')];
  assert.equal(drag(target, 'drop', files).defaultPrevented, true);
  assert.deepEqual(batches, [files]); assert.equal(active.at(-1), false);
  dispose(); drag(target, 'drop', files); assert.equal(batches.length, 1);
});
test('disabled chat blocks file navigation, while text drags keep native behavior', () => {
  const target = new EventTarget(), batches = [];
  const dispose = registerFileDrop(target, { onActive: () => {}, onFiles: files => batches.push(files), isAvailable: () => false });
  const over = drag(target, 'dragover');
  assert.equal(over.dataTransfer.dropEffect, 'none');
  assert.equal(drag(target, 'drop', [new File(['one'], 'one.txt')]).defaultPrevented, true);
  assert.equal(batches.length, 0);
  assert.equal(drag(target, 'drop', [], ['text/plain']).defaultPrevented, false);
  dispose();
});
test('escape and leaving the window dismiss the drop target', () => {
  const target = new EventTarget(), active = [];
  const dispose = registerFileDrop(target, { onActive: value => active.push(value), onFiles: () => {}, isAvailable: () => true });
  drag(target, 'dragenter');
  const escape = new Event('keydown'); escape.key = 'Escape'; target.dispatchEvent(escape);
  assert.equal(active.at(-1), false);
  drag(target, 'dragenter'); target.dispatchEvent(new Event('blur'));
  assert.equal(active.at(-1), false);
  dispose();
});
