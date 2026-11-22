import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'vite';

// Test the production TypeScript adapter through Vite's compiler, without a
// second test runner or browser tokens. Build stays in memory and never touches dist.
const bundle = await build({
  configFile: false, logLevel: 'silent',
  build: { write: false, minify: false, lib: { entry: 'src/api/events.ts', formats: ['es'] } },
});
const output = Array.isArray(bundle) ? bundle[0].output : bundle.output;
const entry = output.find((item) => item.type === 'chunk' && item.isEntry);
assert.ok(entry);
const { SseFrames, subscribeChat } = await import(`data:text/javascript;base64,${Buffer.from(entry.code).toString('base64')}`);
const encoder = new TextEncoder();

function event(seq, type = 'message.delta', payload = { text: 'Проверка 🙂' }) {
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ v: 1, chat_id: 'chat', branch_id: 'branch', run_id: 'run', seq, at: '2026-11-20T07:01:00Z', payload })}\n\n`;
}
function response(text) {
  return new Response(new ReadableStream({ start(controller) { controller.enqueue(encoder.encode(text)); } }), {
    headers: { 'Content-Type': 'text/event-stream' },
  });
}
async function subscription({ initial, replies, resetCursor, expected }) {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  globalThis.window = globalThis;
  const requests = [], received = [], states = [];
  let resets = 0, stop;
  let resolve, reject;
  const finished = new Promise((yes, no) => { resolve = yes; reject = no; });
  const timeout = setTimeout(() => reject(new Error('Stream test timed out')), 1000);
  globalThis.fetch = async (url, options) => {
    requests.push({ url, cursor: options.headers['Last-Event-ID'] });
    const next = replies.shift();
    if (next === undefined) throw new Error('Unexpected stream reconnect');
    return next;
  };
  try {
    stop = subscribeChat({ chatId: 'chat', cursor: initial,
      onState: (state) => states.push(state),
      onReset: async () => { resets++; return resetCursor; },
      onError: reject,
      onEvent: (value) => {
        received.push(value.seq);
        if (received.length === expected.length) { stop(); resolve(); }
      },
    });
    await finished;
    assert.deepEqual(received, expected);
    return { requests, resets, states };
  } finally {
    clearTimeout(timeout); stop?.();
    // Let the aborted reader finish its finally block before restoring browser globals.
    await new Promise(resolve => setTimeout(resolve, 0));
    globalThis.fetch = originalFetch;
    if (originalWindow === undefined) delete globalThis.window; else globalThis.window = originalWindow;
  }
}

test('SSE framing handles split CRLF, comments and UTF-8 payloads', () => {
  const parser = new SseFrames();
  const wire = ': heartbeat\r\n\r\n' + event(1).replaceAll('\n', '\r\n');
  const bytes = encoder.encode(wire), decoder = new TextDecoder();
  const frames = [];
  for (let index = 0; index < bytes.length; index++) frames.push(...parser.push(decoder.decode(bytes.slice(index, index + 1), { stream: true })));
  assert.equal(frames.length, 1);
  assert.equal(frames[0].id, '1');
  assert.equal(JSON.parse(frames[0].data).payload.text, 'Проверка 🙂');
});

test('unfinished frames are bounded, but a backlog of complete frames is accepted', () => {
  assert.throws(() => new SseFrames().push('x'.repeat(262_145)), /stream_frame_too_large/);
  assert.equal(new SseFrames().push(event(1).repeat(2000)).length, 2000);
});

test('durable cursor replay deduplicates events without repeating text', async () => {
  const result = await subscription({ initial: 5, replies: [response(event(5) + event(6) + event(6) + event(7))], expected: [6, 7] });
  assert.equal(result.requests[0].cursor, '5');
  assert.match(result.requests[0].url, /after=5$/);
  assert.equal(result.resets, 0);
});

test('sequence gap reloads the snapshot before the next event connection', async () => {
  const result = await subscription({ initial: 3, replies: [response(event(7)), response(event(10))], resetCursor: 9, expected: [10] });
  assert.equal(result.resets, 1);
  assert.equal(result.requests[1].cursor, '9');
});

test('stream.reset is honored even at the current cursor', async () => {
  const result = await subscription({ initial: 20, replies: [response(event(20, 'stream.reset', { cursor: 41 })), response(event(42))], resetCursor: 41, expected: [42] });
  assert.equal(result.resets, 1);
  assert.equal(result.requests[1].cursor, '41');
});
