import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, AuthClient, isRecord } from '../api/client';
import type { KnowledgeDocument, KnowledgeVersion } from '../api/knowledge';
import { GroupAccess } from './GroupAccess';
import { Audience } from './Audience';

interface Unit {
  id: string; excerpt: string; truncated: boolean; denied: boolean; group_denied: boolean;
  denying_groups: { id: string; name: string }[]; denying_groups_incomplete: boolean;
  policy_allows_answer: boolean;
}
interface Grant { user_id: string; login: string; disabled: boolean; allow_answer: boolean; allow_original: boolean; has_denies: boolean }
interface GrantPage { items: Grant[]; next_after: string | null; acl_revision: number }
interface Snapshot {
  user_id: string; login: string; owner: boolean; disabled: boolean; revision: number;
  allow_answer: boolean; allow_original: boolean; units: Unit[]; next_after: string | null;
  policy_allows_answer: boolean; policy_allows_original: boolean;
  groups: { id: string; name: string; allow_answer: boolean; allow_original: boolean }[];
  groups_incomplete: boolean; groups_hidden: boolean;
  snapshot_token: string | null; membership_snapshot_checked: boolean;
}
interface Props {
  auth: AuthClient; document: KnowledgeDocument; versions: KnowledgeVersion[];
  refresh: () => Promise<void>; close: () => void; onExpired: () => void;
}
function parse(value: unknown): Snapshot {
  if (!isRecord(value) || typeof value.user_id !== 'string' || typeof value.login !== 'string'
    || typeof value.owner !== 'boolean' || typeof value.disabled !== 'boolean' || typeof value.revision !== 'number'
    || typeof value.allow_answer !== 'boolean' || typeof value.allow_original !== 'boolean'
    || typeof value.policy_allows_answer !== 'boolean' || typeof value.policy_allows_original !== 'boolean'
    || typeof value.groups_incomplete !== 'boolean' || typeof value.groups_hidden !== 'boolean'
    || !(typeof value.snapshot_token === 'string' || value.snapshot_token === null)
    || typeof value.membership_snapshot_checked !== 'boolean'
    || !Array.isArray(value.groups)
    || !value.groups.every((group: unknown) => isRecord(group) && typeof group.id === 'string'
      && typeof group.name === 'string' && typeof group.allow_answer === 'boolean'
      && typeof group.allow_original === 'boolean')
    || !Array.isArray(value.units) || !(typeof value.next_after === 'string' || value.next_after === null)
    || !value.units.every((unit: unknown) => isRecord(unit) && typeof unit.id === 'string'
      && typeof unit.excerpt === 'string' && typeof unit.truncated === 'boolean' && typeof unit.denied === 'boolean'
      && typeof unit.group_denied === 'boolean' && typeof unit.policy_allows_answer === 'boolean'
      && typeof unit.denying_groups_incomplete === 'boolean' && Array.isArray(unit.denying_groups)
      && unit.denying_groups.every((group: unknown) => isRecord(group)
        && typeof group.id === 'string' && typeof group.name === 'string'))) {
    throw new ApiError(502, 'invalid_response');
  }
  return value as unknown as Snapshot;
}
function parseGrants(value: unknown): GrantPage {
  if (!isRecord(value) || typeof value.acl_revision !== 'number' || !Array.isArray(value.items)
    || !(typeof value.next_after === 'string' || value.next_after === null)
    || !value.items.every((item: unknown) => isRecord(item) && typeof item.user_id === 'string'
      && typeof item.login === 'string' && typeof item.disabled === 'boolean'
      && typeof item.allow_answer === 'boolean' && typeof item.allow_original === 'boolean'
      && typeof item.has_denies === 'boolean')) {
    throw new ApiError(502, 'invalid_response');
  }
  return value as unknown as GrantPage;
}
export function Access({ auth, document, versions, refresh, close, onExpired }: Props) {
  const [login, setLogin] = useState('');
  const [version, setVersion] = useState(document.active_version_id ?? versions[0]?.id ?? '');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [audienceEpoch, setAudienceEpoch] = useState(0);
  const [grants, setGrants] = useState<GrantPage | null>(null);
  const [listError, setListError] = useState('');
  const grantSequence = useRef(0);
  const snapshotSequence = useRef(0);
  const loginInput = useRef<HTMLInputElement>(null);

  async function grantPage(after?: string, revision?: number): Promise<GrantPage> {
    const query = after ? `?after=${encodeURIComponent(after)}&expected_acl_revision=${revision}` : '';
    return parseGrants(await auth.request(`/knowledge/documents/${document.id}/direct-subjects${query}`));
  }
  useEffect(() => {
    let live = true;
    const sequence = ++grantSequence.current;
    void grantPage().then((result) => { if (live && sequence === grantSequence.current) setGrants(result); }).catch((failure: unknown) => {
      if (!live || sequence !== grantSequence.current) return;
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setListError('Не удалось загрузить список разрешений.');
    });
    return () => { live = false; };
    // The component is keyed by document; auth and onExpired are stable for its lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function fail(failure: unknown) {
    if (failure instanceof ApiError && failure.status === 401) { onExpired(); return; }
    setSnapshot(null);
    setError(failure instanceof ApiError && failure.code === 'subject_not_found'
      ? 'Учётная запись не найдена.'
      : failure instanceof ApiError && failure.code === 'revision_conflict'
        ? 'Права изменились. Откройте проверку пользователя заново.'
        : 'Не удалось подтвердить состояние прав. Откройте проверку заново.');
  }
  async function load(after: string | null = null, selectedVersion = version, token: string | null = null) {
    return parse(await auth.mutate(`/knowledge/documents/${document.id}/access-review`, 'POST', {
      login, version_id: selectedVersion || null, after, snapshot_token: token,
    }));
  }
  async function open(event: FormEvent) {
    event.preventDefault(); if (!login.trim() || busy) return;
    const sequence = ++snapshotSequence.current;
    setBusy(true); setError(''); setNotice('');
    try { const result = await load(); if (sequence === snapshotSequence.current) { setSnapshot(result); setCursor(null); } }
    catch (failure) { if (sequence === snapshotSequence.current) fail(failure); } finally { setBusy(false); }
  }
  async function changeGrant(allowAnswer: boolean, allowOriginal: boolean) {
    if (!snapshot || busy) return;
    const sequence = ++snapshotSequence.current;
    ++grantSequence.current; setAudienceEpoch((value) => value + 1);
    setBusy(true); setError(''); setNotice('');
    try {
      await auth.mutate(`/knowledge/documents/${document.id}/grants/${snapshot.user_id}`, 'PUT', {
        expected_revision: snapshot.revision, allow_answer: allowAnswer, allow_original: allowOriginal,
      });
      const result = await load(cursor); if (sequence === snapshotSequence.current) setSnapshot(result); await refresh();
      setGrants(await grantPage()); setListError(''); setNotice('Права обновлены.');
    } catch (failure) { setGrants(null); setListError('Обновите список разрешений.'); fail(failure); } finally { setBusy(false); }
  }
  async function changeDeny(unit: Unit) {
    if (!snapshot || !version || busy) return;
    const sequence = ++snapshotSequence.current;
    ++grantSequence.current; setAudienceEpoch((value) => value + 1);
    setBusy(true); setError(''); setNotice('');
    try {
      await auth.mutate(`/knowledge/versions/${version}/units/${unit.id}/denies/${snapshot.user_id}`, 'PUT', {
        expected_revision: snapshot.revision, denied: !unit.denied,
      });
      const result = await load(cursor); if (sequence === snapshotSequence.current) setSnapshot(result); await refresh();
      setGrants(await grantPage()); setListError(''); setNotice('Ограничение обновлено.');
    } catch (failure) { setGrants(null); setListError('Обновите список разрешений.'); fail(failure); } finally { setBusy(false); }
  }
  async function next() {
    if (!snapshot?.next_after || !snapshot.snapshot_token || busy) return;
    const sequence = ++snapshotSequence.current;
    const nextCursor = snapshot.next_after; setBusy(true); setError(''); setNotice('');
    try { const result = await load(nextCursor, version, snapshot.snapshot_token); if (sequence === snapshotSequence.current) { setSnapshot(result); setCursor(nextCursor); } }
    catch (failure) { if (sequence === snapshotSequence.current) fail(failure); } finally { setBusy(false); }
  }
  async function changeVersion(selected: string) {
    const sequence = ++snapshotSequence.current;
    setVersion(selected); setBusy(true); setError(''); setNotice('');
    try { const result = await load(null, selected); if (sequence === snapshotSequence.current) { setSnapshot(result); setCursor(null); } }
    catch (failure) { if (sequence === snapshotSequence.current) fail(failure); } finally { setBusy(false); }
  }
  async function moreGrants() {
    if (!grants?.next_after || busy) return;
    ++grantSequence.current;
    setBusy(true); setListError('');
    try {
      const next = await grantPage(grants.next_after, grants.acl_revision);
      setGrants({ ...next, items: [...grants.items, ...next.items] });
    } catch (failure) {
      setGrants(null);
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setListError('Список изменился или недоступен. Загрузите его заново.');
    } finally { setBusy(false); }
  }
  async function reloadGrants() {
    ++grantSequence.current;
    setBusy(true); setListError('');
    try { setGrants(await grantPage()); }
    catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setListError('Не удалось загрузить список разрешений.');
    } finally { setBusy(false); }
  }
  return <section className="knowledge-access" aria-labelledby="access-title">
    <div className="knowledge-access-head"><div><span className="knowledge-kicker">ДОСТУП К ДОКУМЕНТУ</span><h4 id="access-title">Права пользователя</h4></div><button className="knowledge-secondary" disabled={busy} onClick={close}>Закрыть</button></div>
    <Audience key={`${document.id}:${document.revision}:${audienceEpoch}`} auth={auth} documentId={document.id} onExpired={onExpired} select={(value) => { ++snapshotSequence.current; setLogin(value); setSnapshot(null); setNotice(''); loginInput.current?.focus(); }} />
    <GroupAccess auth={auth} document={document} versions={versions} refresh={refresh} onExpired={onExpired} onAudienceChange={() => { ++snapshotSequence.current; setAudienceEpoch((value) => value + 1); setSnapshot(null); setNotice(''); }} onSelectUser={(value) => { ++snapshotSequence.current; setLogin(value); setSnapshot(null); setNotice(''); loginInput.current?.focus(); }} />
    <div className="knowledge-grant-list"><div className="knowledge-grant-head"><strong>Личные права и ограничения</strong><button className="knowledge-link" disabled={busy} onClick={() => { void reloadGrants(); }}>Обновить список</button></div>
      <p>Личные разрешения и ограничения во всех версиях документа. Групповые разрешения настраиваются отдельно.</p>
      {listError && <div className="knowledge-message error" role="alert">{listError}</div>}
      {grants ? grants.items.length ? grants.items.map((grant) => <button className="knowledge-grant-row" key={grant.user_id} disabled={busy} onClick={() => { setLogin(grant.login); setSnapshot(null); setNotice(''); }}><strong>{grant.login}</strong><span>{grant.disabled ? 'Отключён · ' : ''}{[grant.allow_answer && 'ответы', grant.allow_original && 'оригинал', grant.has_denies && 'есть ограничения'].filter(Boolean).join(' · ')}</span></button>) : <p>Личных разрешений и ограничений пока нет.</p> : !listError && <p role="status">Загружаем разрешения…</p>}
      {grants?.next_after && <button className="knowledge-link" disabled={busy} onClick={() => { void moreGrants(); }}>Показать ещё</button>}
    </div>
    <p>Выберите участника из списка или введите точный логин, затем нажмите «Проверить». Изменения применяются сразу.</p>
    <form className="knowledge-access-form" onSubmit={(event) => { void open(event); }}>
      <label>Логин<input ref={loginInput} value={login} maxLength={128} disabled={busy} onChange={(event) => { setLogin(event.target.value); setSnapshot(null); setNotice(''); }} /></label>
      <button className="knowledge-primary" disabled={busy || !login.trim()}>Проверить</button>
    </form>
    {error && <div className="knowledge-message error" role="alert">{error}</div>}{notice && <div className="knowledge-message" role="status">{notice}</div>}
    {snapshot && <><div className="knowledge-access-subject"><strong>{snapshot.login}</strong>{snapshot.owner && <span>Владелец документа</span>}{snapshot.disabled && <span>Учётная запись отключена</span>}</div>
      {snapshot.owner ? <p>Владелец получает доступ к документу как куратор. Запреты на выдержки действуют и для владельца.</p> : <div className="knowledge-access-grants">
        <label><input type="checkbox" disabled={busy || (snapshot.disabled && !snapshot.allow_answer)} checked={snapshot.allow_answer} onChange={(event) => { void changeGrant(event.target.checked, snapshot.allow_original); }} /><span>Использовать разрешённые выдержки в ответах</span></label>
        <label><input type="checkbox" disabled={busy || (snapshot.disabled && !snapshot.allow_original)} checked={snapshot.allow_original} onChange={(event) => { void changeGrant(snapshot.allow_answer, event.target.checked); }} /><span>Скачивать оригинал целиком</span></label>
      </div>}
      <div className="knowledge-effective"><strong>Права по текущим правилам</strong><p>Ответы: {snapshot.policy_allows_answer ? 'разрешены с учётом ограничений выдержек' : 'не разрешены'} · Оригинал: {snapshot.policy_allows_original ? 'разрешён' : 'не разрешён'}</p><p>Это снимок прав; выдача также зависит от публикации и состояния версии.</p>{snapshot.groups.length > 0 && <p>Группы: {snapshot.groups.map((group) => `${group.name} (${group.id}): ${[group.allow_answer && 'ответы', group.allow_original && 'оригинал'].filter(Boolean).join(' и ') || 'без разрешений'}`).join('; ')}{snapshot.groups_incomplete ? ' и другие' : ''}.</p>}{snapshot.groups_incomplete && <p>Список групп неполный; итоговые права рассчитаны по всем группам.</p>}{snapshot.groups_hidden && <p>Состав групп скрыт: вы больше не участник пространства. Изменения членства между страницами не отслеживаются.</p>}{!snapshot.snapshot_token && <p>Слишком много групп для согласованного просмотра следующих страниц. Обратитесь к администратору.</p>}</div>
      <p>Доступ к оригиналу открывает весь файл, даже если отдельные выдержки запрещены.</p>
      <label className="knowledge-access-version">Версия для ограничений<select value={version} disabled={busy} onChange={(event) => { void changeVersion(event.target.value); }}><option value="">Без версии</option>{versions.map((item) => <option key={item.id} value={item.id}>Версия {item.generation}</option>)}</select></label>
      <p>Запрет действует только для выбранной версии и выдержки. Если текст повторяется в других выдержках, ограничьте каждую.</p>
      <div className="knowledge-access-units">{snapshot.units.length ? snapshot.units.map((unit) => <article key={unit.id}><p>{unit.excerpt}</p>{unit.truncated && <small>Выдержка показана не полностью.</small>}<small className="knowledge-unit-policy">По правилам ответа: {unit.policy_allows_answer ? 'разрешена' : 'запрещена'}.{unit.group_denied && (snapshot.groups_hidden ? ' Действует групповое ограничение; состав групп скрыт.' : ` Ограничивают группы: ${unit.denying_groups.map((item) => `${item.name} (${item.id})`).join(', ')}${unit.denying_groups_incomplete ? ' и другие' : ''}.`)}</small><label><input type="checkbox" disabled={busy || (snapshot.disabled && !unit.denied)} checked={unit.denied} onChange={() => { void changeDeny(unit); }} /> Запретить эту выдержку в ответах</label></article>) : <p>В этой версии нет согласованных выдержек.</p>}</div>
      <div className="knowledge-access-pages">{cursor && <button className="knowledge-secondary" disabled={busy} onClick={() => { void changeVersion(version); }}>К началу</button>}{snapshot.next_after && <button className="knowledge-secondary" disabled={busy || !snapshot.snapshot_token} onClick={() => { void next(); }}>Следующие выдержки</button>}</div>
    </>}
  </section>;
}
