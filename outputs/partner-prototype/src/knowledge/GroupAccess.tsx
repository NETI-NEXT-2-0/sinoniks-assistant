import { useEffect, useState } from 'react';
import { ApiError, AuthClient, isRecord } from '../api/client';
import type { KnowledgeDocument, KnowledgeVersion } from '../api/knowledge';
import { GroupDenies } from './GroupDenies';
import { GroupMembers } from './GroupMembers';

type Workspace = { id: string; name: string };
type Group = { id: string; name: string; allow_answer: boolean; allow_original: boolean; has_denies: boolean };
type GroupPage = { items: Group[]; next_after: string | null; acl_revision: number };
interface Props { auth: AuthClient; document: KnowledgeDocument; versions: KnowledgeVersion[]; refresh: () => Promise<void>; onExpired: () => void; onAudienceChange: () => void; onSelectUser: (login: string) => void }
function workspacesOf(value: unknown): { items: Workspace[]; next_after: string | null } {
  if (!isRecord(value) || !Array.isArray(value.items)
    || !(typeof value.next_after === 'string' || value.next_after === null)
    || !value.items.every((item: unknown) => isRecord(item) && typeof item.id === 'string' && typeof item.name === 'string')) throw new ApiError(502, 'invalid_response');
  return value as { items: Workspace[]; next_after: string | null };
}
function groupsOf(value: unknown): GroupPage {
  if (!isRecord(value) || !Array.isArray(value.items) || typeof value.acl_revision !== 'number'
    || !(typeof value.next_after === 'string' || value.next_after === null)
    || !value.items.every((item: unknown) => isRecord(item) && typeof item.id === 'string'
      && typeof item.name === 'string' && typeof item.allow_answer === 'boolean'
      && typeof item.allow_original === 'boolean' && typeof item.has_denies === 'boolean')) throw new ApiError(502, 'invalid_response');
  return value as unknown as GroupPage;
}
export function GroupAccess({ auth, document, versions, refresh, onExpired, onAudienceChange, onSelectUser }: Props) {
  const [workspaceId, setWorkspaceId] = useState(document.workspace_id);
  const [revision, setRevision] = useState(document.revision);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceCursor, setWorkspaceCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState('');
  const [groups, setGroups] = useState<GroupPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [denyGroup, setDenyGroup] = useState<Group | null>(null);
  const [memberGroup, setMemberGroup] = useState<Group | null>(null);
  const [confirmBind, setConfirmBind] = useState(false);
  const groupUrl = `/knowledge/documents/${document.id}/group-grants`;
  function fail(failure: unknown) {
    if (failure instanceof ApiError && failure.status === 401) { onExpired(); return; }
    setGroups(null); setConfirmBind(false);
    setError(failure instanceof ApiError && failure.code === 'revision_conflict'
      ? 'Документ или права изменились. Обновите документ и откройте настройки заново.'
      : 'Не удалось подтвердить состояние групп. Обновите документ и повторите.');
  }
  async function getGroups(after?: string, acl?: number): Promise<GroupPage> {
    const query = after ? `?after=${encodeURIComponent(after)}&expected_acl_revision=${acl}` : '';
    return groupsOf(await auth.request(groupUrl + query));
  }
  useEffect(() => {
    let live = true;
    const request = document.workspace_id ? getGroups() : auth.request('/knowledge/audience-workspaces');
    void request.then((result) => {
      if (!live) return;
      if (document.workspace_id) setGroups(result as GroupPage);
      else { const page = workspacesOf(result); setWorkspaces(page.items); setWorkspaceCursor(page.next_after); }
    }).catch((failure: unknown) => { if (live) fail(failure); });
    return () => { live = false; };
    // This panel is remounted when the document changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function moreWorkspaces() {
    if (!workspaceCursor || busy) return; setBusy(true); setError('');
    try {
      const page = workspacesOf(await auth.request(`/knowledge/audience-workspaces?after=${encodeURIComponent(workspaceCursor)}`));
      setWorkspaces((old) => [...old, ...page.items]); setWorkspaceCursor(page.next_after);
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function bind() {
    if (!selected || busy) return; setBusy(true); setError(''); setNotice(''); onAudienceChange();
    try {
      const result = await auth.mutate(`/knowledge/documents/${document.id}/workspace`, 'PUT', {
        expected_revision: revision, workspace_id: selected,
      });
      if (!isRecord(result) || typeof result.revision !== 'number' || result.workspace_id !== selected) throw new ApiError(502, 'invalid_response');
      setRevision(result.revision); setWorkspaceId(selected); setConfirmBind(false);
      setGroups(await getGroups()); await refresh(); setNotice('Рабочее пространство привязано.');
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function change(group: Group, answer: boolean, original: boolean) {
    if (busy) return; setBusy(true); setError(''); setNotice(''); setMemberGroup(null); onAudienceChange();
    try {
      const result = await auth.mutate(`${groupUrl}/${group.id}`, 'PUT', {
        expected_revision: revision, allow_answer: answer, allow_original: original,
      });
      if (!isRecord(result) || typeof result.revision !== 'number') throw new ApiError(502, 'invalid_response');
      setRevision(result.revision); setGroups(await getGroups()); await refresh(); setNotice('Разрешения группы обновлены.');
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function moreGroups() {
    if (!groups?.next_after || busy) return; setBusy(true); setError('');
    try {
      const page = await getGroups(groups.next_after, groups.acl_revision);
      setGroups({ ...page, items: [...groups.items, ...page.items] });
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  return <section className="knowledge-group-access" aria-labelledby="group-access-title">
    <h4 id="group-access-title">Доступ групп</h4>
    <p>Участие в группе само по себе не открывает документ. Разрешение на оригинал открывает весь файл.</p>
    {error && <div className="knowledge-message error" role="alert">{error}</div>}{notice && <div className="knowledge-message" role="status">{notice}</div>}
    {!workspaceId ? <><label className="knowledge-access-version">Рабочее пространство<select value={selected} disabled={busy || confirmBind} onChange={(event) => setSelected(event.target.value)}><option value="">Выберите пространство</option>{workspaces.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></label>
      {workspaceCursor && <button className="knowledge-link" disabled={busy} onClick={() => { void moreWorkspaces(); }}>Показать ещё пространства</button>}
      <p>Привязка постоянная; уже выданные личные права сохранятся.</p>
      {!confirmBind ? <button className="knowledge-secondary" disabled={busy || !selected} onClick={() => setConfirmBind(true)}>Привязать пространство</button> : <div className="knowledge-reindex-confirm"><p>Привязать документ к выбранному пространству? Изменить привязку пока нельзя.</p><div><button className="knowledge-secondary" disabled={busy} onClick={() => setConfirmBind(false)}>Отмена</button><button className="knowledge-primary" disabled={busy} onClick={() => { void bind(); }}>Привязать</button></div></div>}</> : <>
      <p>Личный запрет на выдержку действует и при групповом разрешении.</p>
      {groups ? groups.items.length ? groups.items.map((group) => <div className="knowledge-group-row" key={group.id}><strong>{group.name}<small>{group.id}</small></strong><label><input type="checkbox" aria-label={`Ответы: ${group.name}, ${group.id}`} disabled={busy} checked={group.allow_answer} onChange={(event) => { void change(group, event.target.checked, group.allow_original); }} /> Ответы</label><label><input type="checkbox" aria-label={`Оригинал: ${group.name}, ${group.id}`} disabled={busy} checked={group.allow_original} onChange={(event) => { void change(group, group.allow_answer, event.target.checked); }} /> Оригинал</label>{(group.allow_answer || group.allow_original || group.has_denies) && <button className="knowledge-secondary" disabled={busy} onClick={() => { setDenyGroup(null); setMemberGroup(group); }}>Участники</button>}<button className="knowledge-secondary" disabled={busy} onClick={() => { setMemberGroup(null); setDenyGroup(group); }}>Ограничить выдержки</button></div>) : <p>В пространстве пока нет групп.</p> : !error && <p role="status">Загружаем группы…</p>}
      {memberGroup && <GroupMembers key={memberGroup.id} auth={auth} documentId={document.id} group={memberGroup} onExpired={onExpired} close={() => setMemberGroup(null)} select={(login) => { setMemberGroup(null); onSelectUser(login); }} />}
      {denyGroup && <GroupDenies key={denyGroup.id} auth={auth} group={denyGroup} versions={versions} active={document.active_version_id} onExpired={onExpired} onChangeStart={onAudienceChange} close={() => setDenyGroup(null)} changed={async (nextRevision) => { setRevision(nextRevision); setGroups(await getGroups()); await refresh(); }} />}
      {groups?.next_after && <button className="knowledge-link" disabled={busy} onClick={() => { void moreGroups(); }}>Показать ещё группы</button>}
    </>}
  </section>;
}
