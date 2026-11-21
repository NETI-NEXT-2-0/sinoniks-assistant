import { useEffect, useRef, useState } from 'react';
import { ApiError, isRecord, type AuthClient } from '../api/client';
import { VendorWishesExport } from './VendorWishesExport';
import './feedback.css';

type Entry = { id: string; rating: number; comment: string; author: string; created_at: string };
function entry(raw: unknown): Entry {
  if (!isRecord(raw) || typeof raw.id !== 'string' || !Number.isInteger(raw.rating)
    || Number(raw.rating) < 1 || Number(raw.rating) > 5 || typeof raw.comment !== 'string'
    || typeof raw.author !== 'string' || typeof raw.created_at !== 'string') throw new ApiError(502, 'invalid_response');
  return raw as unknown as Entry;
}

export function FeedbackPrompt({ auth, chatId, runId, onExpired }: {
  auth: AuthClient; chatId: string; runId: string; onExpired: () => void;
}) {
  const [loaded, setLoaded] = useState(false), [sent, setSent] = useState<Entry | null>(null);
  const [rating, setRating] = useState(0), [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [dismissed, setDismissed] = useState(false);
  const id = useRef(crypto.randomUUID()), dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    let live = true;
    void auth.request(`/chats/${chatId}/feedback`).then(raw => {
      if (!isRecord(raw) || !('feedback' in raw)) throw new ApiError(502, 'invalid_response');
      const value = raw.feedback === null ? null : entry(raw.feedback);
      if (live) { setSent(value); setLoaded(true); }
    }).catch(failure => { if (!live) return; if (failure instanceof ApiError && failure.status === 401) onExpired(); else setLoaded(true); });
    return () => { live = false; };
  }, [auth, chatId, onExpired]);
  useEffect(() => { if (rating > 0 && !dialog.current?.open) dialog.current?.showModal(); }, [rating]);
  function close() { if (!busy) { dialog.current?.close(); setRating(0); opener.current?.focus(); } }
  async function send() {
    setBusy(true); setError('');
    try {
      let raw: unknown;
      try { raw = await auth.mutate(`/chats/${chatId}/feedback`, 'POST', { id: id.current, run_id: runId, rating, comment }); }
      catch (failure) {
        if (!(failure instanceof ApiError) || failure.code !== 'feedback_exists') throw failure;
        raw = await auth.request(`/chats/${chatId}/feedback`);
      }
      if (!isRecord(raw)) throw new ApiError(502, 'invalid_response');
      setSent(entry(raw.feedback)); dialog.current?.close(); opener.current?.focus();
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError('Отправка не подтверждена. Текст сохранён в этом окне — попробуйте ещё раз.');
    } finally { setBusy(false); }
  }
  if (!loaded || dismissed) return null;
  if (sent) return <p className="feedback-thanks" role="status">Спасибо за отзыв · {sent.rating} из 5</p>;
  return <div className="feedback-prompt">
    <span>Было полезно?</span><div className="feedback-stars" role="group" aria-label="Оценить помощь от 1 до 5">
      {[1, 2, 3, 4, 5].map(n => <button key={n} type="button" aria-label={`Оценить на ${n} из 5`} onClick={event => { opener.current = event.currentTarget; setRating(n); }}>☆</button>)}
    </div><button type="button" className="feedback-later" onClick={() => setDismissed(true)}>Не сейчас</button>
    <dialog ref={dialog} className="feedback-dialog" onCancel={event => { event.preventDefault(); close(); }}>
      <form onSubmit={event => { event.preventDefault(); void send(); }}>
        <header><h2>Что можно улучшить?</h2><button type="button" aria-label="Закрыть отзыв" disabled={busy} onClick={close}>×</button></header>
        <div className="feedback-stars" role="group" aria-label="Ваша оценка">{[1, 2, 3, 4, 5].map(n => <button key={n} type="button" disabled={busy} aria-label={`${n} из 5`} aria-pressed={rating === n} onClick={() => setRating(n)}>{n <= rating ? '★' : '☆'}</button>)}</div>
        <label htmlFor={`feedback-${chatId}`}>Пожелание или комментарий <span>(необязательно)</span></label>
        <textarea autoFocus id={`feedback-${chatId}`} rows={4} maxLength={2000} value={comment} disabled={busy} onChange={event => setComment(event.target.value)} placeholder="Что было непонятно? Какой возможности не хватает?" />
        <p>Администратор увидит оценку, ваш логин и этот комментарий. Переписка не отправляется. Не указывайте пароли и конфиденциальные данные.</p>
        {error && <p role="alert">{error}</p>}<footer><button type="button" disabled={busy} onClick={close}>Отмена</button><button className="feedback-submit" disabled={busy}>{busy ? 'Отправляем…' : 'Отправить отзыв'}</button></footer>
      </form>
    </dialog>
  </div>;
}

export function FeedbackPanel({ auth, manager, chatId, close, onExpired }: {
  auth: AuthClient; manager: boolean; chatId?: string; close: () => void; onExpired: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null), [items, setItems] = useState<Entry[]>([]);
  const [next, setNext] = useState<number | null>(0), [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function load(offset = 0) {
    setBusy(true); setError('');
    try {
      const raw = await auth.request(`/admin/feedback?offset=${offset}&limit=50`);
      if (!isRecord(raw) || !Array.isArray(raw.items) || !(raw.next_offset === null || Number.isInteger(raw.next_offset))) throw new ApiError(502, 'invalid_response');
      const entries = raw.items.map(entry); setItems(old => offset === 0 ? entries : [...old, ...entries.filter(item => !old.some(x => x.id === item.id))]); setNext(raw.next_offset as number | null);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError('Не удалось загрузить отзывы. Попробуйте ещё раз.');
    } finally { setBusy(false); }
  }
  useEffect(() => { dialog.current?.showModal(); if (manager) void load(); }, []);
  return <dialog ref={dialog} className="feedback-dialog feedback-inbox" onCancel={event => { event.preventDefault(); close(); }}>
    <header><div><h2>Пожелания и отзывы</h2><p>{manager ? 'Оценки и комментарии, которые пользователи отправили добровольно.' : 'Оценить помощь можно после первого ответа в чате.'}</p></div><button type="button" aria-label="Закрыть пожелания" onClick={close}>×</button></header>
    {manager && <><button type="button" disabled={busy} onClick={() => void load()}>Обновить отзывы</button>{error && <p role="alert">{error}</p>}
      {!busy && !error && items.length === 0 && <p className="feedback-empty">Отзывов пока нет. Они появятся здесь после отправки пользователем.</p>}
      <ol className="feedback-list">{items.map(item => <li key={item.id}><div><strong aria-label={`${item.rating} из 5`}>{'★'.repeat(item.rating)}{'☆'.repeat(5 - item.rating)}</strong><span>{item.author}</span><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString('ru-RU')}</time></div><p>{item.comment || 'Оценка без комментария'}</p></li>)}</ol>
      {busy && <p role="status">Загружаем отзывы…</p>}{next !== null && items.length > 0 && <button type="button" disabled={busy} onClick={() => void load(next)}>Показать ещё</button>}</>}
    {chatId && <section className="feedback-session"><h3>Пожелания из текущего чата</h3><p>Ваши ответы «Другое» и пожелания к возможностям продукта доступны отдельной выгрузкой.</p><VendorWishesExport auth={auth} chatId={chatId} onExpired={onExpired} /></section>}
  </dialog>;
}
