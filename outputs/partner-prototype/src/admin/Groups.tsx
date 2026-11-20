import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { ApiError, AuthClient, isRecord } from '../api/client';
import { Icon } from '../ui';
import '../knowledge/knowledge.css';
import './groups.css';

type Entry = { id: string; name: string };
type Member = { id: string; login: string; disabled: boolean };
type Page<T> = { items: T[]; next_cursor: string | null };
type Removal = { scope: 'workspace' | 'group'; member: Member };
interface Props { auth: AuthClient; close: () => void; onExpired: () => void }
function page<T>(raw: unknown, valid: (item: unknown) => boolean): Page<T> {
  if (!isRecord(raw) || !Array.isArray(raw.items) || !raw.items.every(valid)
    || !(typeof raw.next_cursor === 'string' || raw.next_cursor === null)) throw new ApiError(502, 'invalid_response');
  return raw as unknown as Page<T>;
}
const named = (item: unknown) => isRecord(item) && typeof item.id === 'string' && typeof item.name === 'string';
const member = (item: unknown) => isRecord(item) && typeof item.id === 'string' && typeof item.login === 'string' && typeof item.disabled === 'boolean';
const empty = <T,>(): Page<T> => ({ items: [], next_cursor: null });
export function Groups({ auth, close, onExpired }: Props) {
  const panel = useRef<HTMLElement>(null);
  const confirmation = useRef<HTMLElement>(null);
  const [workspaces, setWorkspaces] = useState<Page<Entry>>(empty);
  const [groups, setGroups] = useState<Page<Entry>>(empty);
  const [users, setUsers] = useState<Page<Member>>(empty);
  const [workspaceMembers, setWorkspaceMembers] = useState<Page<Member>>(empty);
  const [groupMembers, setGroupMembers] = useState<Page<Member>>(empty);
  const [workspace, setWorkspace] = useState<Entry | null>(null);
  const [group, setGroup] = useState<Entry | null>(null);
  const [workspaceName, setWorkspaceName] = useState('');
  const [groupName, setGroupName] = useState('');
  const [userId, setUserId] = useState('');
  const [groupUserId, setGroupUserId] = useState('');
  const [removal, setRemoval] = useState<Removal | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  function fail(problem: unknown) {
    if (problem instanceof ApiError && problem.status === 401) { onExpired(); return; }
    setError(problem instanceof ApiError && problem.code === 'invalid_member'
      ? 'Нужна активная учётная запись — участник этого пространства.'
      : 'Не удалось подтвердить состояние. Обновите список и проверьте результат.');
  }
  async function load<T>(path: string, valid: (item: unknown) => boolean, cursor?: string): Promise<Page<T>> {
    return page<T>(await auth.request(`${path}?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`), valid);
  }
  const workspacePath = workspace ? `/admin/workspaces/${workspace.id}` : '';
  const groupPath = workspace && group ? `${workspacePath}/groups/${group.id}` : '';
  useEffect(() => { if (removal) confirmation.current?.focus(); }, [removal]);
  useEffect(() => {
    let live = true; const previous = document.activeElement;
    panel.current?.querySelector<HTMLElement>('button')?.focus();
    void Promise.all([load<Entry>('/admin/workspaces', named), load<Member>('/admin/users', member)])
      .then(([spaces, accounts]) => { if (live) { setWorkspaces(spaces); setUsers(accounts); } })
      .catch((problem: unknown) => { if (live) fail(problem); });
    return () => { live = false; if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
    // This dialog is recreated for each session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  function trap(event: KeyboardEvent<HTMLElement>) {
    if (event.key === 'Escape' && !busy) { event.preventDefault(); close(); return; }
    if (event.key !== 'Tab') return;
    const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled)')];
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
  async function chooseWorkspace(item: Entry) {
    if (busy) return; setBusy(true); setError(''); setNotice(''); setRemoval(null); setGroup(null);
    setWorkspace(item); setGroupMembers(empty()); setGroupUserId(''); setUserId('');
    try {
      const [nextGroups, members] = await Promise.all([
        load<Entry>(`/admin/workspaces/${item.id}/groups`, named),
        load<Member>(`/admin/workspaces/${item.id}/members`, member),
      ]);
      setGroups(nextGroups); setWorkspaceMembers(members);
    } catch (problem) { setGroups(empty()); setWorkspaceMembers(empty()); fail(problem); } finally { setBusy(false); }
  }
  async function chooseGroup(item: Entry) {
    if (!workspace || busy) return; setBusy(true); setError(''); setNotice(''); setRemoval(null);
    setGroup(item); setGroupUserId('');
    try { setGroupMembers(await load<Member>(`${workspacePath}/groups/${item.id}/members`, member)); }
    catch (problem) { setGroupMembers(empty()); fail(problem); } finally { setBusy(false); }
  }
  async function createSpace(event: FormEvent) {
    event.preventDefault(); if (!workspaceName.trim() || busy) return; setBusy(true); setError(''); setNotice('');
    try {
      await auth.mutate('/admin/workspaces', 'POST', { name: workspaceName.trim() });
      setWorkspaceName(''); setWorkspaces(await load<Entry>('/admin/workspaces', named)); setNotice('Пространство создано.');
    } catch (problem) {
      if (problem instanceof ApiError && (problem.status === 0 || problem.status >= 500)) {
        setWorkspaceName('');
        try { setWorkspaces(await load<Entry>('/admin/workspaces', named)); } catch { /* Check manually. */ }
      }
      fail(problem);
    } finally { setBusy(false); }
  }
  async function createGroup(event: FormEvent) {
    event.preventDefault(); if (!workspace || !groupName.trim() || busy) return; setBusy(true); setError(''); setNotice('');
    try {
      await auth.mutate(`${workspacePath}/groups`, 'POST', { name: groupName.trim() });
      setGroupName(''); setGroups(await load<Entry>(`${workspacePath}/groups`, named)); setNotice('Группа создана.');
    } catch (problem) {
      if (problem instanceof ApiError && (problem.status === 0 || problem.status >= 500)) {
        setGroupName('');
        try { setGroups(await load<Entry>(`${workspacePath}/groups`, named)); } catch { /* Check manually. */ }
      }
      fail(problem);
    } finally { setBusy(false); }
  }
  async function add(scope: 'workspace' | 'group') {
    const id = scope === 'workspace' ? userId : groupUserId;
    if (!id || !workspace || (scope === 'group' && !group) || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await auth.mutate(`${scope === 'workspace' ? workspacePath : groupPath}/members/${id}`, 'PUT');
      if (scope === 'workspace') { setWorkspaceMembers(await load<Member>(`${workspacePath}/members`, member)); setUserId(''); }
      else { setGroupMembers(await load<Member>(`${groupPath}/members`, member)); setGroupUserId(''); }
      setNotice('Участник добавлен.');
    } catch (problem) { fail(problem); } finally { setBusy(false); }
  }
  async function remove() {
    if (!removal || !workspace || busy) return; const scope = removal.scope;
    setBusy(true); setError(''); setNotice('');
    try {
      await auth.mutate(`${scope === 'workspace' ? workspacePath : groupPath}/members/${removal.member.id}`, 'DELETE');
      setRemoval(null);
      if (scope === 'workspace') {
        setWorkspaceMembers(await load<Member>(`${workspacePath}/members`, member));
        if (group) setGroupMembers(await load<Member>(`${groupPath}/members`, member));
      } else setGroupMembers(await load<Member>(`${groupPath}/members`, member));
      setNotice('Участник удалён.');
    } catch (problem) { setRemoval(null); fail(problem); } finally { setBusy(false); }
  }
  async function more<T>(current: Page<T>, path: string, valid: (item: unknown) => boolean, setter: (next: Page<T>) => void) {
    if (!current.next_cursor || busy) return; setBusy(true); setError('');
    try { const next = await load<T>(path, valid, current.next_cursor); setter({ ...next, items: [...current.items, ...next.items] }); }
    catch (problem) { fail(problem); } finally { setBusy(false); }
  }
  const accountOptions = (entries: Member[]) => entries.map((item) => <option key={item.id} value={item.id} disabled={item.disabled}>{item.login}{item.disabled ? ' · отключён' : ''} · {item.id}</option>);
  const memberList = (entries: Page<Member>, scope: 'workspace' | 'group', path: string, setter: (next: Page<Member>) => void) => <><div className="admin-member-list">{entries.items.map((item) => <div className="admin-member" key={item.id}><span>{item.login}{item.disabled && ' · отключён'}<small>{item.id}</small></span><button className="knowledge-secondary" disabled={busy} onClick={() => setRemoval({ scope, member: item })}>Удалить</button></div>)}{!entries.items.length && <p>Участников пока нет.</p>}</div>{entries.next_cursor && <button className="knowledge-link" disabled={busy} onClick={() => { void more(entries, path, member, setter); }}>Показать ещё участников</button>}</>;
  return <div className="knowledge-overlay"><section ref={panel} className="knowledge-panel" role="dialog" aria-modal="true" aria-labelledby="admin-groups-title" onKeyDown={trap}>
    <header className="knowledge-header"><div><span className="knowledge-kicker">АДМИНИСТРИРОВАНИЕ</span><h2 id="admin-groups-title">Пространства и группы</h2></div><button className="knowledge-close icon-button" aria-label="Закрыть управление группами" disabled={busy} onClick={close}><Icon name="close" /></button></header>
    <div className="knowledge-layout"><aside className="knowledge-rail" aria-label="Рабочие пространства"><div className="knowledge-section-title">Пространства <span>{workspaces.items.length}</span></div>
      <form className="knowledge-create" onSubmit={(event) => { void createSpace(event); }}><label className="visually-hidden" htmlFor="space-name">Название пространства</label><input id="space-name" value={workspaceName} maxLength={128} disabled={busy} placeholder="Новое пространство" onChange={(event) => setWorkspaceName(event.target.value)} /><button aria-label="Создать пространство" disabled={busy || !workspaceName.trim()}><Icon name="plus" /></button></form>
      <div className="knowledge-doc-list">{workspaces.items.map((item) => <button key={item.id} className={`knowledge-doc ${workspace?.id === item.id ? 'selected' : ''}`} disabled={busy} onClick={() => { void chooseWorkspace(item); }}><span>{item.name}<small className="admin-id">{item.id}</small></span></button>)}{workspaces.next_cursor && <button className="knowledge-link" disabled={busy} onClick={() => { void more(workspaces, '/admin/workspaces', named, setWorkspaces); }}>Показать ещё</button>}</div></aside>
    <main className="knowledge-content">{error && <div className="knowledge-message error" role="alert">{error}</div>}{notice && <div className="knowledge-message" role="status">{notice}</div>}{!workspace ? <div className="knowledge-empty"><h3>Рабочие пространства</h3><p>Выберите пространство или создайте новое. Группы и участники настраиваются внутри него.</p></div> : <>
      <div className="knowledge-document-head"><div><span className="knowledge-kicker">ПРОСТРАНСТВО</span><h3>{workspace.name}</h3><small className="admin-id">{workspace.id}</small></div><button className="knowledge-secondary" disabled={busy} onClick={() => { void chooseWorkspace(workspace); }}>Обновить</button></div>
      {removal && <section ref={confirmation} tabIndex={-1} className="knowledge-reindex-confirm" role="alert"><p>Удалить {removal.member.login} из {removal.scope === 'workspace' ? 'пространства и всех его групп' : 'группы'}? Это может сразу закрыть доступ к документам.</p><div><button className="knowledge-secondary" disabled={busy} onClick={() => setRemoval(null)}>Отмена</button><button className="knowledge-primary" disabled={busy} onClick={() => { void remove(); }}>Удалить</button></div></section>}
      <section className="admin-section"><h4>Участники пространства</h4><p>Только участников пространства можно добавлять в его группы.</p><div className="admin-add"><label>Учётная запись<select value={userId} disabled={busy} onChange={(event) => setUserId(event.target.value)}><option value="">Выберите пользователя</option>{accountOptions(users.items)}</select></label><button className="knowledge-primary" disabled={busy || !userId} onClick={() => { void add('workspace'); }}>Добавить</button></div>{users.next_cursor && <button className="knowledge-link" disabled={busy} onClick={() => { void more(users, '/admin/users', member, setUsers); }}>Показать ещё учётные записи</button>}{memberList(workspaceMembers, 'workspace', `${workspacePath}/members`, setWorkspaceMembers)}</section>
      <section className="admin-section"><h4>Группы</h4><p>Названия задаются организацией; одинаковые названия различаются по ID.</p><form className="admin-add" onSubmit={(event) => { void createGroup(event); }}><label>Новая группа<input value={groupName} maxLength={128} disabled={busy} onChange={(event) => setGroupName(event.target.value)} /></label><button className="knowledge-primary" disabled={busy || !groupName.trim()}>Создать</button></form><div className="admin-group-list">{groups.items.map((item) => <button key={item.id} className={`knowledge-doc ${group?.id === item.id ? 'selected' : ''}`} disabled={busy} onClick={() => { void chooseGroup(item); }}><span>{item.name}<small className="admin-id">{item.id}</small></span></button>)}{groups.next_cursor && <button className="knowledge-link" disabled={busy} onClick={() => { void more(groups, `${workspacePath}/groups`, named, setGroups); }}>Показать ещё группы</button>}</div></section>
      {group && <section className="admin-section"><h4>Участники группы «{group.name}»</h4><p>Добавленный участник получит действующие разрешения этой группы.</p><div className="admin-add"><label>Участник пространства<select value={groupUserId} disabled={busy} onChange={(event) => setGroupUserId(event.target.value)}><option value="">Выберите участника</option>{accountOptions(workspaceMembers.items)}</select></label><button className="knowledge-primary" disabled={busy || !groupUserId} onClick={() => { void add('group'); }}>Добавить</button></div>{workspaceMembers.next_cursor && <p>Чтобы выбрать других участников, загрузите их в списке пространства выше.</p>}{memberList(groupMembers, 'group', `${groupPath}/members`, setGroupMembers)}</section>}
    </>}</main></div></section></div>;
}
