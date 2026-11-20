import { defineConfig } from 'vite';

const backend = 'http://localhost:8080';
const developmentOrigins = new Set(['http://localhost:5173', 'http://127.0.0.1:5173']);

export default defineConfig({
  server: {
    port: 5173,
    strictPort: true,
    proxy: Object.fromEntries(['/api', '/health'].map((prefix) => [prefix, {
      target: backend,
      changeOrigin: true,
      configure(proxy) {
        proxy.on('proxyReq', (outgoing, incoming) => {
          // Dev-only same-origin bridge. Preserve unexpected origins so the
          // backend still rejects cross-site requests to the development server.
          if (developmentOrigins.has(incoming.headers.origin ?? '')) {
            outgoing.setHeader('Origin', backend);
          }
        });
      },
    }])),
  },
});
