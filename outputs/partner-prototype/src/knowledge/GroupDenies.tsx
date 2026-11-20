import { useEffect, useRef, useState } from 'react';
import { ApiError, AuthClient, isRecord } from '../api/client';
import type { KnowledgeVersion } from '../api/knowledge';

type Unit = { id: string; excerpt: string; truncated: boolean; denied: boolean };
type UnitPage = { revision: number; items: Unit[]; next_after: string | null };
interface Props {
  auth: AuthClient; group: { id: string; name: string }; versions: KnowledgeVersion[];
  active: string | null; changed: (revision: number) => Promise<void>; close: () => void;
  onExpired: () => void; onChangeStart: () => void;
}
function parse(value: unknown): UnitPage {
  if (!isRecord(value) || typeof value.revision !== 'number' || !Array.isArray(value.items)
    || !(typeof value.next_after === 'string' || value.next_after === null)
    || !value.items.every((unit: unknown) => isRecord(unit) && typeof unit.id === 'string'
      && typeof unit.excerpt === 'string' && typeof unit.truncated === 'boolean' && typeof unit.denied === 'boolean')) throw new ApiError(502, 'invalid_response');
  return value as unknown as UnitPage;
}
export function GroupDenies({ auth, group, versions, active, changed, close, onExpired, onChangeStart }: Props) {
  const panel = useRef<HTMLElement>(null);
  const [version, setVersion] = useState(active ?? versions[0]?.id ?? '');
  const [page, setPage] = useState<UnitPage | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  function fail(problem: unknown) {
    if (problem instanceof ApiError && problem.status === 401) { onExpired(); return; }
    setPage(null);
    setError(problem instanceof ApiError && problem.code === 'revision_conflict'
      ? 'Документ изменился. Закройте и откройте ограничения заново.'
      : 'Не удалось подтвердить ограничения. Закройте и откройте их заново.');
  }
  async function load(target: string, after?: string, revision?: number) {
    const query = after ? `?after=${encodeURIComponent(after)}&expected_revision=${revision}` : '';
    return parse(await auth.request(`/knowledge/versions/${target}/group-denies/${group.id}/units${query}`));
  }
  useEffect(() => {
    let live = true;
    panel.current?.focus();
    if (version) void load(version).then((result) => { if (live) setPage(result); })
      .catch((problem: unknown) => { if (live) fail(problem); });
    return () => { live = false; };
    // This editor is remounted when a group is selected.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function selectVersion(target: string) {
    setVersion(target); setCursor(null); setPage(null); setNotice(''); setError('');
    if (!target) return; setBusy(true);
    try { setPage(await load(target)); } catch (problem) { fail(problem); } finally { setBusy(false); }
  }
  async function change(unit: Unit) {
    if (!page || !version || busy) return; setBusy(true); setError(''); setNotice('');
    onChangeStart();
    try {
      const result = await auth.mutate(`/knowledge/versions/${version}/units/${unit.id}/group-denies/${group.id}`, 'PUT', {
        expected_revision: page.revision, denied: !unit.denied,
      });
      if (!isRecord(result) || typeof result.revision !== 'number') throw new ApiError(502, 'invalid_response');
      setPage(await load(version, cursor ?? undefined, result.revision));
      await changed(result.revision); setNotice('Ограничение обновлено.');
    } catch (problem) { fail(problem); } finally { setBusy(false); }
  }
  async function next() {
    if (!page?.next_after || busy) return; const nextCursor = page.next_after;
    setBusy(true); setError(''); setNotice('');
    try { setPage(await load(version, nextCursor, page.revision)); setCursor(nextCursor); }
    catch (problem) { fail(problem); } finally { setBusy(false); }
  }
  return <section ref={panel} tabIndex={-1} className="knowledge-group-denies" aria-label={`Ограничения группы ${group.name}`}>
    <div className="knowledge-access-head"><div><strong>Ограничения группы «{group.name}»</strong><small>{group.id}</small></div><button className="knowledge-secondary" disabled={busy} onClick={close}>Закрыть</button></div>
    <p>Запрет исключает выдержку из ответов для участников группы, даже при личном разрешении. Оригинал регулируется отдельно.</p>
    <label className="knowledge-access-version">Версия<select value={version} disabled={busy} onChange={(event) => { void selectVersion(event.target.value); }}><option value="">Выберите версию</option>{active && !versions.some((item) => item.id === active) && <option value={active}>Опубликованная версия</option>}{versions.map((item) => <option key={item.id} value={item.id}>Версия {item.generation}</option>)}</select></label>
    {error && <div className="knowledge-message error" role="alert">{error}</div>}{notice && <div className="knowledge-message" role="status">{notice}</div>}
    {page ? <><div className="knowledge-access-units">{page.items.length ? page.items.map((unit) => <article key={unit.id}><p>{unit.excerpt}</p>{unit.truncated && <small>Выдержка показана не полностью.</small>}<label><input type="checkbox" checked={unit.denied} disabled={busy} onChange={() => { void change(unit); }} /> Запретить этой группе</label></article>) : <p>В этой версии нет согласованных выдержек.</p>}</div><div className="knowledge-access-pages">{cursor && <button className="knowledge-secondary" disabled={busy} onClick={() => { void selectVersion(version); }}>К началу</button>}{page.next_after && <button className="knowledge-secondary" disabled={busy} onClick={() => { void next(); }}>Следующие выдержки</button>}</div></> : version && !error && <p role="status">Загружаем выдержки…</p>}
  </section>;
}
