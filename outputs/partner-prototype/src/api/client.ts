export interface User {
  id: string;
  login: string;
  role: 'user' | 'curator' | 'admin';
  runtime_profile?: 'offline_fixture' | 'openai_compatible';
}

interface ErrorEnvelope {
  code: string;
  message: string;
  request_id: string;
  retryable: boolean;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly requestId: string | null = null,
    public readonly retryable = false,
  ) {
    // Never include raw server responses or credential-bearing request bodies.
    super(code);
    this.name = 'ApiError';
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isUser(value: unknown): value is User {
  return isRecord(value) && typeof value.id === 'string' && typeof value.login === 'string'
    && ['user', 'curator', 'admin'].includes(String(value.role));
}

function isError(value: unknown): value is ErrorEnvelope {
  return isRecord(value) && typeof value.code === 'string' && typeof value.message === 'string'
    && typeof value.request_id === 'string' && typeof value.retryable === 'boolean';
}

/** Session and CSRF state live only in memory. The session cookie is HttpOnly. */
export class AuthClient {
  private csrfToken: string | null = null;
  private csrfPending: Promise<void> | null = null;

  reset(): void {
    this.csrfToken = null;
  }

  async request(path: string, init: RequestInit = {}, timeoutMs = 15_000): Promise<unknown> {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`/api/v1${path}`, {
        ...init,
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
        headers: { Accept: 'application/json', ...init.headers },
      });
      const body: unknown = response.status === 204 ? null : await response.json().catch(() => null);
      if (!response.ok) {
        if (isError(body)) {
          throw new ApiError(response.status, body.code, body.request_id, body.retryable);
        }
        throw new ApiError(response.status, 'invalid_response', response.headers.get('X-Request-ID'));
      }
      return body;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(0, controller.signal.aborted ? 'request_timeout' : 'network_error', null, true);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async mutate(path: string, method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', body?: unknown, idempotencyKey?: string): Promise<unknown> {
    if (!this.csrfToken) await this.csrf();
    const headers: Record<string, string> = { 'X-CSRF-Token': this.csrfToken ?? '' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
    try {
      return await this.request(path, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    } catch (error) {
      // A rejected synchronizer token did not authorize a write. The UI may
      // retry the same command with its original idempotency key after refresh.
      if (error instanceof ApiError && error.status === 403 && error.code === 'csrf_rejected') this.reset();
      throw error;
    }
  }

  async download(path: string): Promise<{ blob: Blob; filename: string }> {
    if (!this.csrfToken) await this.csrf();
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 120_000);
    try {
      const response = await fetch(`/api/v1${path}`, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: { Accept: 'application/octet-stream', 'X-CSRF-Token': this.csrfToken ?? '' },
      });
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        if (isError(body)) throw new ApiError(response.status, body.code, body.request_id, body.retryable);
        throw new ApiError(response.status, 'invalid_response', response.headers.get('X-Request-ID'));
      }
      if (response.headers.get('Content-Type')?.split(';')[0] !== 'application/octet-stream') {
        throw new ApiError(502, 'invalid_response');
      }
      const length = response.headers.get('Content-Length');
      if (length && (!/^\d+$/.test(length) || Number(length) > 25 * 1024 * 1024)) {
        throw new ApiError(502, 'invalid_response');
      }
      const reader = response.body?.getReader();
      if (!reader) throw new ApiError(502, 'invalid_response');
      const chunks: BlobPart[] = [];
      let size = 0;
      try {
        while (true) {
          const part = await reader.read();
          if (part.done) break;
          size += part.value.byteLength;
          if (size > 25 * 1024 * 1024) {
            await reader.cancel().catch(() => {});
            throw new ApiError(502, 'invalid_response');
          }
          chunks.push(part.value.slice().buffer as ArrayBuffer);
        }
      } finally { reader.releaseLock(); }
      if (!size) throw new ApiError(502, 'invalid_response');
      const blob = new Blob(chunks, { type: 'application/octet-stream' });
      const disposition = response.headers.get('Content-Disposition') ?? '';
      const match = /^attachment; filename="(document\.(?:pdf|docx|png|jpg|txt|md|bin))"$/.exec(disposition);
      return { blob, filename: match?.[1] ?? 'document.bin' };
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.status === 403 && error.code === 'csrf_rejected') this.reset();
        throw error;
      }
      throw new ApiError(0, controller.signal.aborted ? 'request_timeout' : 'network_error', null, true);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async upload(path: string, file: File, contentType: string, method: 'POST' | 'PUT' = 'POST', idempotencyKey?: string): Promise<unknown> {
    if (!this.csrfToken) await this.csrf();
    try {
      return await this.request(path, {
        method, body: file,
        headers: { 'X-CSRF-Token': this.csrfToken ?? '', 'Content-Type': contentType,
          ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}) },
      }, 120_000);
    } catch (error) {
      if (error instanceof ApiError && error.status === 403 && error.code === 'csrf_rejected') this.reset();
      throw error;
    }
  }

  async csrf(): Promise<void> {
    if (this.csrfPending) return this.csrfPending;
    const pending = (async () => {
      const result = await this.request('/auth/csrf');
      if (!isRecord(result) || typeof result.csrf_token !== 'string') throw new ApiError(502, 'invalid_response');
      this.csrfToken = result.csrf_token;
    })();
    this.csrfPending = pending;
    try { await pending; }
    finally { if (this.csrfPending === pending) this.csrfPending = null; }
  }

  async me(): Promise<User> {
    const result = await this.request('/auth/me');
    if (!isUser(result)) throw new ApiError(502, 'invalid_response');
    return result;
  }

  async login(login: string, password: string): Promise<User> {
    // Refresh the synchronizer token before each login attempt. A parallel tab
    // may have changed the shared pre-login cookie since this form was opened.
    await this.csrf();
    const result = await this.request('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this.csrfToken ?? '' },
      body: JSON.stringify({ login, password }),
    });
    if (!isRecord(result) || !isUser(result.user) || typeof result.csrf_token !== 'string') {
      this.reset();
      throw new ApiError(502, 'invalid_response');
    }
    this.csrfToken = result.csrf_token;
    return result.user;
  }

  async logout(): Promise<void> {
    // Required after a page reload and safe if another tab rotated CSRF.
    await this.csrf();
    try {
      await this.request('/auth/logout', {
        method: 'POST', headers: { 'X-CSRF-Token': this.csrfToken ?? '' },
      });
    } finally {
      this.reset();
    }
  }
}

export function errorMessage(error: unknown, action: 'login' | 'logout' | 'session'): string {
  if (!(error instanceof ApiError)) return 'Не удалось выполнить действие. Повторите попытку.';
  if (error.status === 401) {
    return action === 'login' ? 'Неверный логин или пароль.' : 'Сессия истекла. Войдите снова.';
  }
  if (error.status === 403) return 'Проверка безопасности не пройдена. Повторите попытку.';
  if (error.status === 429) return error.code === 'login_rate_limited'
    ? 'Слишком много попыток входа. Подождите минуту и попробуйте снова.'
    : 'Слишком много запросов. Подождите минуту и повторите попытку.';
  if (error.status === 503) return 'Сервис временно недоступен. Повторите попытку позже.';
  if (error.code === 'request_timeout') return 'Сервис не ответил вовремя. Повторите попытку.';
  if (error.status === 0) return 'Нет связи с сервисом. Проверьте подключение и повторите попытку.';
  if (error.status === 422) return 'Проверьте логин и пароль и попробуйте снова.';
  return action === 'logout' ? 'Не удалось выйти. Повторите попытку.'
    : 'Не удалось загрузить данные. Повторите попытку.';
}
