import { useState } from 'react';
import { ApiError, AuthClient, isRecord } from '../api/client';

export function VendorWishesExport({ auth, chatId, onExpired }: {
  auth: AuthClient; chatId: string; onExpired: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState('');

  async function download(): Promise<void> {
    setPending(true);
    setFeedback('');
    try {
      const payload = await auth.request(`/chats/${chatId}/vendor-wishes/export`);
      if (!isRecord(payload) || payload.session_id !== chatId || !Array.isArray(payload.wishes)) {
        throw new ApiError(502, 'invalid_response');
      }
      if (payload.wishes.length === 0) {
        setFeedback('В сессии пока нет пожеланий.');
        return;
      }
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `vendor-wishes-${chatId}.json`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setFeedback('Не удалось выгрузить пожелания. Попробуйте ещё раз.');
    } finally {
      setPending(false);
    }
  }

  return <details className="session-accounting">
    <summary>Пожелания вендору</summary>
    <button type="button" disabled={pending} onClick={() => { void download(); }}>
      {pending ? 'Готовим файл…' : 'Скачать пожелания JSON'}
    </button>
    {feedback && <p role="status">{feedback}</p>}
  </details>;
}
