import { useEffect, useRef, useState } from 'react';
import { ApiError, AuthClient, isRecord } from '../api/client';

type Member = { user_id: string; login: string; disabled: boolean };
type Page = { items: Member[]; next_after: string | null; acl_revision: number; membership_revision: number };
interface Props {
  auth: AuthClient; documentId: string; group: { id: string; name: string };
  close: () => void; select: (login: string) => void; onExpired: () => void;
}
function pageOf(value: unknown): Page {
  if (!isRecord(value) || !Array.isArray(value.items)
    || typeof value.acl_revision !== 'number' || typeof value.membership_revision !== 'number'
    || !(typeof value.next_after === 'string' || value.next_after === null)
    || !value.items.every((item: unknown) => isRecord(item) && typeof item.user_id === 'string'
      && typeof item.login === 'string' && typeof item.disabled === 'boolean')) throw new ApiError(502, 'invalid_response');
  return value as unknown as Page;
}
export function GroupMembers({ auth, documentId, group, close, select, onExpired }: Props) {
  const panel = useRef<HTMLElement>(null);
  const [page, setPage] = useState<Page | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const url = `/knowledge/documents/${documentId}/group-grants/${group.id}/members`;
  async function load(after?: string, acl?: number, membership?: number) {
    const query = after ? `?after=${encodeURIComponent(after)}&expected_acl_revision=${acl}&expected_membership_revision=${membership}` : '';
    return pageOf(await auth.request(url + query));
  }
  function fail(problem: unknown) {
    setPage(null);
    if (problem instanceof ApiError && problem.status === 401) { onExpired(); return; }
    setError(problem instanceof ApiError && problem.code === 'revision_conflict'
      ? 'Состав группы или права изменились. Обновите список.'
      : 'Не удалось загрузить участников. Проверьте доступ к пространству.');
  }
  useEffect(() => {
    let live = true;
    panel.current?.focus();
    void load().then((result) => { if (live) setPage(result); })
      .catch((problem: unknown) => { if (live) fail(problem); });
    return () => { live = false; };
    // The panel is remounted for each group.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function reload() {
    if (busy) return; setBusy(true); setError('');
    try { setPage(await load()); } catch (problem) { fail(problem); } finally { setBusy(false); }
  }
  async function more() {
    if (!page?.next_after || busy) return; setBusy(true); setError('');
    try {
      const next = await load(page.next_after, page.acl_revision, page.membership_revision);
      setPage({ ...next, items: [...page.items, ...next.items] });
    } catch (problem) { fail(problem); } finally { setBusy(false); }
  }
  return <section ref={panel} tabIndex={-1} className="knowledge-group-denies" aria-label={`Участники группы ${group.name}`}>
    <div className="knowledge-access-head"><div><strong>Участники группы «{group.name}»</strong><small>{group.id}</small></div><button className="knowledge-secondary" disabled={busy} onClick={close}>Закрыть</button></div>
    <p>Участие в группе не гарантирует доступ: действуют личные ограничения и состояние документа. Выберите пользователя, чтобы проверить его права.</p>
    {error && <div className="knowledge-message error" role="alert">{error} <button className="knowledge-link" disabled={busy} onClick={() => { void reload(); }}>Обновить</button></div>}
    {page ? page.items.length ? page.items.map((member) => <button className="knowledge-grant-row" key={member.user_id} disabled={busy} onClick={() => select(member.login)}><strong>{member.login}</strong><span>{member.disabled ? 'Отключён' : 'Выбрать'}</span></button>) : <p>В группе нет участников.</p> : !error && <p role="status">Загружаем участников…</p>}
    {page?.next_after && <button className="knowledge-link" disabled={busy} onClick={() => { void more(); }}>Показать ещё участников</button>}
  </section>;
}
