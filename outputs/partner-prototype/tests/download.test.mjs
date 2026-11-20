import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'vite';

const bundle = await build({
  configFile: false, logLevel: 'silent',
  build: { write: false, minify: false, lib: { entry: 'src/api/client.ts', formats: ['es'] } },
});
const output = Array.isArray(bundle) ? bundle[0].output : bundle.output;
const entry = output.find((item) => item.type === 'chunk' && item.isEntry);
assert.ok(entry);
const { AuthClient } = await import(`data:text/javascript;base64,${Buffer.from(entry.code).toString('base64')}`);

async function withFetch(handler, run) {
  const previousFetch = globalThis.fetch, previousWindow = globalThis.window;
  globalThis.window = globalThis;
  globalThis.fetch = handler;
  try { await run(); } finally { globalThis.fetch = previousFetch; globalThis.window = previousWindow; }
}
function csrf() {
  return new Response(JSON.stringify({ csrf_token: 'synthetic-token' }), {
    headers: { 'Content-Type': 'application/json' },
  });
}

test('draft original download uses session, CSRF and a safe generic filename', async () => {
  const requests = [];
  await withFetch(async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith('/auth/csrf')) return csrf();
    return new Response(new TextEncoder().encode('synthetic PDF bytes'), { headers: {
      'Content-Type': 'application/octet-stream',
      'Content-Disposition': 'attachment; filename="document.pdf"',
    } });
  }, async () => {
    const result = await new AuthClient().download('/knowledge/versions/id/original/download');
    assert.equal(result.filename, 'document.pdf');
    assert.equal(await result.blob.text(), 'synthetic PDF bytes');
    assert.equal(requests[1].options.credentials, 'same-origin');
    assert.equal(requests[1].options.method, 'POST');
    assert.equal(requests[1].options.headers['X-CSRF-Token'], 'synthetic-token');
    assert.equal(requests[1].options.cache, 'no-store');
  });
});

test('download sanitizes unexpected filenames and rejects oversized streaming bodies', async () => {
  let cancelled = false;
  await withFetch(async (url) => {
    if (url.endsWith('/auth/csrf')) return csrf();
    const stream = new ReadableStream({
      pull(controller) { controller.enqueue(new Uint8Array(1024 * 1024)); },
      cancel() { cancelled = true; },
    });
    return new Response(stream, { headers: { 'Content-Type': 'application/octet-stream' } });
  }, async () => {
    await assert.rejects(() => new AuthClient().download('/knowledge/versions/id/original/download'),
      (error) => error.code === 'invalid_response');
    assert.equal(cancelled, true);
  });
  await withFetch(async (url) => url.endsWith('/auth/csrf') ? csrf() : new Response('x', { headers: {
    'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment; filename="../../secret.pdf"',
  } }), async () => {
    assert.equal((await new AuthClient().download('/knowledge/versions/id/original/download')).filename, 'document.bin');
  });
});

test('CSRF rejection resets the token and does not expose raw server text', async () => {
  let csrfCalls = 0, downloads = 0;
  await withFetch(async (url) => {
    if (url.endsWith('/auth/csrf')) { csrfCalls += 1; return csrf(); }
    downloads += 1;
    if (downloads === 1) return new Response(JSON.stringify({
      code: 'csrf_rejected', message: 'synthetic private error', request_id: 'request', retryable: false,
    }), { status: 403, headers: { 'Content-Type': 'application/json' } });
    return new Response('x', { headers: { 'Content-Type': 'application/octet-stream' } });
  }, async () => {
    const client = new AuthClient();
    await assert.rejects(() => client.download('/knowledge/versions/id/original/download'),
      (error) => error.code === 'csrf_rejected' && !error.message.includes('synthetic private error'));
    await client.download('/knowledge/versions/id/original/download');
    assert.equal(csrfCalls, 2);
  });
});
