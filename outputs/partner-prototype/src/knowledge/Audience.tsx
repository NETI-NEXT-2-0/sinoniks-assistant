import { useEffect, useRef, useState } from 'react';
import { ApiError, AuthClient, isRecord } from '../api/client';

type Person = {
  user_id: string; login: string; disabled: boolean; owner: boolean;
  direct_answer: boolean; direct_original: boolean; group_answer: boolean; group_original: boolean;
  policy_allows_answer: boolean; policy_allows_original: boolean; has_denies: boolean;
};
type Page = { items: Person[]; next_after: string | null; snapshot_token: string };
interface Props { auth: AuthClient; documentId: string; select: (login: string) => void; onExpired: () => void }
function parse(value: unknown): Page {
  if (!isRecord(value) || !Array.isArray(value.items)
    || typeof value.snapshot_token !== 'string' || !/^[0-9a-f]{64}$/.test(value.snapshot_token)
    || !(typeof value.next_after === 'string' || value.next_after === null)
    || !value.items.every((item: unknown) => isRecord(item)
      && typeof item.user_id === 'string' && typeof item.login === 'string'
      && ['disabled', 'owner', 'direct_answer', 'direct_original', 'group_answer', 'group_original',
        'policy_allows_answer', 'policy_allows_original', 'has_denies']
        .every((key) => typeof item[key] === 'boolean'))) throw new ApiError(502, 'invalid_response');
  return value as unknown as Page;
}
export function Audience({ auth, documentId, select, onExpired }: Props) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<Page | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  useEffect(() => () => { ++sequence.current; }, []);
  async function load(after?: string, token?: string): Promise<Page> {
    const query = after ? `?after=${encodeURIComponent(after)}&snapshot_token=${encodeURIComponent(token ?? '')}` : '';
    return parse(await auth.request(`/knowledge/documents/${documentId}/audience${query}`));
  }
  function fail(problem: unknown) {
    setPage(null);
    if (problem instanceof ApiError && problem.status === 401) { onExpired(); return; }
    setError(problem instanceof ApiError && problem.code === 'revision_conflict'
      ? 'Права или состав групп изменились. Обновите список.'
      : problem instanceof ApiError && problem.code === 'audience_scope_too_large'
        ? 'Слишком много связанных групп для обзора. Обратитесь к администратору.'
        : 'Не удалось загрузить аудиторию. Проверьте доступ к пространству.');
  }
  async function refresh() {
    if (busy) return;
    const current = ++sequence.current;
    setOpen(true); setBusy(true); setError(''); setPage(null);
    try { const result = await load(); if (current === sequence.current) setPage(result); }
    catch (problem) { if (current === sequence.current) fail(problem); }
    finally { if (current === sequence.current) setBusy(false); }
  }
  async function more() {
    if (!page?.next_after || busy) return;
    const current = ++sequence.current;
    setBusy(true); setError('');
    try {
      const result = await load(page.next_after, page.snapshot_token);
      if (current === sequence.current) setPage({ ...result, items: [...page.items, ...result.items] });
    } catch (problem) { if (current === sequence.current) fail(problem); }
    finally { if (current === sequence.current) setBusy(false); }
  }
  function hide() { ++sequence.current; setOpen(false); setPage(null); setError(''); setBusy(false); }
  return <section className="knowledge-grant-list" aria-label="Обзор аудитории">
    <div className="knowledge-grant-head"><strong>Обзор аудитории</strong>{open
      ? <span><button className="knowledge-link" disabled={busy} onClick={() => { void refresh(); }}>Обновить</button> · <button className="knowledge-link" onClick={hide}>Закрыть</button></span>
      : <button className="knowledge-link" onClick={() => { void refresh(); }}>Показать</button>}</div>
    {open && <><p>Владелец, личные права и участники связанных групп. Ответы указаны до проверки отдельных выдержек; ограничения могут относиться к другим версиям. Публикация и состояние учётной записи проверяются отдельно.</p>
      {error && <div className="knowledge-message error" role="alert">{error} <button className="knowledge-link" disabled={busy} onClick={() => { void refresh(); }}>Обновить</button></div>}
      {busy && !page && <p role="status">Загружаем аудиторию…</p>}
      {page && <>{page.items.length ? page.items.map((person) => <button className="knowledge-grant-row" key={person.user_id} disabled={busy} onClick={() => select(person.login)}><strong>{person.login}</strong><span>{person.disabled ? 'Отключён · ' : ''}{person.owner ? 'владелец · ' : ''}{[person.policy_allows_answer && 'ответы по правам', person.policy_allows_original && 'оригинал по правам', person.has_denies && 'есть ограничения'].filter(Boolean).join(' · ') || 'нет разрешений'}</span></button>) : <p>Нет участников с личными или групповыми правилами.</p>}{page.next_after && <button className="knowledge-link" disabled={busy} onClick={() => { void more(); }}>Показать ещё участников</button>}</>}
    </>}
  </section>;
}
