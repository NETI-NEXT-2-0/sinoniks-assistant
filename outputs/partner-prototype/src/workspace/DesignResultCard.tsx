import { useState } from 'react';
import { ApiError, type AuthClient } from '../api/client';

interface Props { auth: AuthClient; chatId: string; id: string; revision: number; ready: boolean; open: () => void; onExpired: () => void }

export function DesignResultCard({ auth, chatId, id, revision, ready, open, onExpired }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function download(extension: string): Promise<void> {
    setBusy(true); setError('');
    try {
      const file = await auth.download(`/chats/${chatId}/designs/${id}/download/${extension}`);
      const url = URL.createObjectURL(file.blob);
      const link = document.createElement('a'); link.href = url; link.download = `sinoniks-v${revision}.${extension}`;
      document.body.append(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError('Не удалось скачать файл. Повторите попытку.');
    } finally { setBusy(false); }
  }
  return <section className="design-chat-card" aria-label={`Проект Синоникса · версия ${revision}`}>
    <strong>Проект Синоникса · версия {revision}</strong>
    <p>{ready ? 'Схема и расчёт сохранены. Можно скачать файлы или изменить условия.' : 'Условия сохранены. Ответьте на вопросы в чате или дополните проект вручную.'}</p>
    <div><button onClick={open}>Открыть проект</button>{ready && ['drawio', 'json', 'txt'].map(extension => <button key={extension} disabled={busy} onClick={() => { void download(extension); }}>{extension === 'drawio' ? 'Схема draw.io' : extension.toUpperCase()}</button>)}</div>
    {error && <p role="alert">{error}</p>}
  </section>;
}
