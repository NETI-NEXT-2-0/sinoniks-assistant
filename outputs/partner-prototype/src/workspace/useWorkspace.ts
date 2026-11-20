import type { TurnStarts } from './useTurnTiming';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, AuthClient } from '../api/client';
import { ChatsClient, parseMessage, type Admission, type BranchBody, type Chat, type ResumeBody, type RequestMode, type RunBody, type Snapshot } from '../api/chats';
import { subscribeChat, type ChatEvent, type ConnectionState } from '../api/events';

export type Command = { key: string; chatId: string; branchId: string } & (
  { kind: 'send'; body: RunBody } | { kind: 'resume'; runId: string; body: ResumeBody }
  | { kind: 'fork'; body: BranchBody }
);
export const terminal = (status: string): boolean => ['succeeded', 'failed', 'cancelled'].includes(status);

export function useWorkspace(auth: AuthClient, onExpired: () => void) {
  const api = useMemo(() => new ChatsClient(auth), [auth]);
  const [chats, setChats] = useState<Chat[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [listLoading, setListLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [connection, setConnection] = useState<ConnectionState>('closed');
  const [command, setCommand] = useState<Command | null>(null);
  const [sending, setSending] = useState(false);
  const [acting, setActing] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [requestModes, setRequestModes] = useState<Record<string, RequestMode>>({});
  const turnStarts = useRef<TurnStarts>(new Map());
  const commandStarts = useRef(new Map<string, number>());
  const live = useRef(true);
  const generation = useRef(0);
  const current = useRef<Snapshot | null>(null);
  const selectedRef = useRef<string | null>(null);
  const branchRef = useRef<string | undefined>(undefined);
  const stop = useRef<(() => void) | null>(null);
  const refreshTimer = useRef<number | null>(null);
  const selectRef = useRef<(id: string, branch?: string) => Promise<void>>(async () => undefined);
  const expiredRef = useRef(onExpired);
  expiredRef.current = onExpired;

  const commit = useCallback((value: Snapshot | null): void => { current.current = value; setSnapshot(value); }, []);
  const handleError = useCallback((failure: unknown): void => {
    if (!live.current) return;
    if (failure instanceof ApiError && failure.status === 401) { stop.current?.(); expiredRef.current(); return; }
    setError(failure);
    if (failure instanceof ApiError && [403, 404].includes(failure.status)) commit(null);
  }, [commit]);

  const refreshList = useCallback(async (append = false): Promise<void> => {
    setListLoading(true);
    try {
      const page = await api.list(append ? nextCursor ?? undefined : undefined);
      if (!live.current) return;
      setChats((old) => append ? [...old, ...page.items.filter((row) => !old.some((item) => item.id === row.id))] : page.items);
      setNextCursor(page.next_cursor);
      if (!selectedRef.current && page.items[0] && !append) void selectRef.current(page.items[0].id);
      else if (!selectedRef.current) setLoading(false);
    } catch (failure) { handleError(failure); setLoading(false); }
    finally { if (live.current) setListLoading(false); }
  }, [api, nextCursor, handleError]);
  const refreshListRef = useRef(refreshList); refreshListRef.current = refreshList;

  const selectChat = useCallback(async (id: string, branchId?: string): Promise<void> => {
    const requestGeneration = ++generation.current;
    stop.current?.(); stop.current = null;
    if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
    const switching = selectedRef.current !== id || (!!branchId && branchRef.current !== branchId);
    selectedRef.current = id; branchRef.current = branchId;
    setSelected(id); setLoading(switching || !current.current); setError(null); setConnection('connecting');
    if (switching) commit(null);
    const valid = (): boolean => live.current && generation.current === requestGeneration;
    async function fetchSnapshot(): Promise<number | null> {
      const value = await api.snapshot(id, branchRef.current);
      if (!valid()) return null;
      branchRef.current = value.selected_branch_id;
      const previous = current.current;
      if (previous?.chat.id === id && previous.selected_branch_id === value.selected_branch_id && value.messages[0]) {
        const overlap = previous.messages.findIndex((message) => message.id === value.messages[0].id);
        if (overlap >= 0) {
          value.messages = [...previous.messages.slice(0, overlap), ...value.messages];
          value.messages_next_cursor = previous.messages_next_cursor;
        }
      }
      commit(value);
      setChats((old) => old.map((row) => row.id === id ? value.chat : row));
      return value.cursor;
    }
    const schedule = (): void => {
      if (refreshTimer.current !== null) return;
      refreshTimer.current = window.setTimeout(() => {
        refreshTimer.current = null;
        if (valid()) void selectRef.current(id, branchRef.current);
      }, 100);
    };
    function event(event: ChatEvent): void {
      if (!valid()) return;
      const value = current.current;
      if (!value) { schedule(); return; }
      const next = { ...value, cursor: event.seq };
      if (event.type === 'message.delta') {
        const text = event.payload.text;
        const offset = event.payload.offset;
        if (typeof text !== 'string' || !event.run_id) { schedule(); return; }
        const run = value.runs.find((item) => item.id === event.run_id);
        if (!run) { schedule(); return; }
        if (event.payload.offset_unit === 'utf16' && typeof offset === 'number' && offset !== run.partial_text.length) { schedule(); return; }
        next.runs = value.runs.map((item) => item.id === event.run_id ? { ...item, partial_text: item.partial_text + text } : item);
      } else if (event.type === 'message.completed') {
        try {
          const message = parseMessage(event.payload.message);
          if (message.branch_id === value.selected_branch_id) next.messages = [...value.messages.filter((row) => row.id !== message.id), message];
        } catch { schedule(); return; }
      } else if (event.type === 'run.started') {
        next.runs = value.runs.map((item) => item.id === event.run_id ? { ...item, status: 'running' as const } : item);
      } else if (['run.succeeded', 'run.failed', 'run.cancelled', 'run.waiting_input', 'run.queued'].includes(event.type)) {
        schedule();
        if (event.type !== 'run.queued') void refreshListRef.current();
      } else if (event.type !== 'run.progress') schedule();
      commit(next);
    }
    try {
      const cursor = await fetchSnapshot();
      if (cursor === null || !valid()) return;
      setLoading(false);
      stop.current = subscribeChat({ chatId: id, cursor, onEvent: event,
        onReset: async () => {
          if (!valid()) return null;
          try { return await fetchSnapshot(); } catch (failure) { handleError(failure); return null; }
        },
        onState: (state) => { if (valid()) setConnection(state); },
        onError: (failure) => {
          if (!valid()) return;
          if ([401, 403, 404].includes(failure.status)) handleError(failure);
          // Event streams cannot assume that a transport failure is just a
          // network error: recheck the actual session before reconnecting.
          else void auth.me().catch((error) => { if (valid()) handleError(error); });
        },
      });
    } catch (failure) { if (valid()) { handleError(failure); setLoading(false); setConnection('closed'); } }
  }, [api, auth, commit, handleError]);
  selectRef.current = selectChat;

  useEffect(() => {
    live.current = true; void refreshListRef.current();
    return () => {
      live.current = false; ++generation.current; stop.current?.();
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      // Component unmount on logout/401 discards chats, snapshots and drafts.
    };
  }, []);

  async function create(): Promise<void> {
    if (acting) return;
    setActing(true); setError(null);
    try {
      const value = await api.create();
      if (!live.current) return;
      setChats((old) => [value, ...old.filter((row) => row.id !== value.id)]);
      await selectChat(value.id);
    } catch (failure) { handleError(failure); if (failure instanceof ApiError && failure.status === 0) void refreshListRef.current(); }
    finally { if (live.current) setActing(false); }
  }

  async function execute(value: Command): Promise<boolean> {
    if (sending) return false;
    setCommand(value); setSending(true); setError(null);
    if (!commandStarts.current.has(value.key)) commandStarts.current.set(value.key, performance.now());
    try {
      let result: Admission;
      if (value.kind === 'send') result = await api.submit(value.chatId, value.body, value.key);
      else if (value.kind === 'resume') result = await api.resume(value.runId, value.body, value.key);
      else result = await api.fork(value.chatId, value.body, value.key);
      if (!live.current) return false;
      if (value.kind !== 'resume' && !turnStarts.current.has(result.run_id)) {
        turnStarts.current.set(result.run_id, { chatId: value.chatId, started: commandStarts.current.get(value.key)! });
      }
      commandStarts.current.delete(value.key);
      setCommand(null);
      if (value.kind === 'send') setDrafts((old) => ({ ...old, [`${value.chatId}:${value.branchId}`]: old[`${value.chatId}:${value.branchId}`]?.trim() === value.body.message ? '' : old[`${value.chatId}:${value.branchId}`] ?? '' }));
      if (selectedRef.current === value.chatId) await selectChat(value.chatId, result.branch_id ?? value.branchId);
      void refreshListRef.current();
      return true;
    } catch (failure) {
      handleError(failure);
      const ambiguous = failure instanceof ApiError && (failure.status === 0 || failure.status >= 500);
      if (!ambiguous) setCommand(null);
      if (failure instanceof ApiError && failure.status === 409 && selectedRef.current) { await selectChat(selectedRef.current, branchRef.current); handleError(failure); }
      return false;
    } finally { if (live.current) setSending(false); }
  }

  const draftKey = snapshot ? `${snapshot.chat.id}:${snapshot.selected_branch_id}` : '';
  const draft = drafts[draftKey] ?? '';
  const requestMode: RequestMode = requestModes[draftKey] ?? snapshot?.runs.filter(
    run => run.branch_id === snapshot.selected_branch_id,
  ).at(-1)?.request_mode ?? 'auto';
  const setRequestMode = (mode: RequestMode): void => {
    if (draftKey && !command) setRequestModes(old => ({ ...old, [draftKey]: mode }));
  };
  const setDraft = (value: string): void => { if (draftKey) setDrafts((old) => ({ ...old, [draftKey]: value })); };
  const send = async (attachmentIds: string[] = []): Promise<boolean> => {
    if (!snapshot || command || !draft.trim()) return false;
    return execute({ kind: 'send', key: crypto.randomUUID(), chatId: snapshot.chat.id, branchId: snapshot.selected_branch_id,
      body: { branch_id: snapshot.selected_branch_id, message: draft.trim(), attachment_ids: attachmentIds, web_mode: 'off', request_mode: requestMode } });
  };
  async function cancel(runId: string): Promise<void> {
    setError(null);
    try { await api.cancel(runId); if (live.current && selectedRef.current) await selectChat(selectedRef.current, branchRef.current); }
    catch (failure) { handleError(failure); }
  }
  async function rename(title: string): Promise<boolean> {
    if (!snapshot) return false;
    try {
      const changed = await api.rename(snapshot.chat.id, title, snapshot.chat.revision);
      if (live.current) { setChats((old) => old.map((row) => row.id === changed.id ? changed : row)); if (current.current?.chat.id === changed.id) commit({ ...current.current, chat: changed }); }
      return true;
    } catch (failure) { handleError(failure); if (failure instanceof ApiError && failure.status === 409) { await selectChat(snapshot.chat.id, snapshot.selected_branch_id); handleError(failure); } return false; }
  }
  async function remove(): Promise<boolean> {
    if (!snapshot) return false;
    try {
      await api.remove(snapshot.chat.id);
      if (!live.current) return false;
      stop.current?.(); ++generation.current; selectedRef.current = null; branchRef.current = undefined;
      setSelected(null); commit(null); setChats((old) => old.filter((row) => row.id !== snapshot.chat.id));
      await refreshListRef.current();
      return true;
    } catch (failure) { handleError(failure); return false; }
  }
  async function loadOlder(): Promise<void> {
    const before = current.current;
    if (!before?.messages_next_cursor || historyLoading) return;
    const requestGeneration = generation.current;
    setHistoryLoading(true);
    try {
      const page = await api.messages(before.chat.id, before.selected_branch_id, before.messages_next_cursor);
      if (!live.current || generation.current !== requestGeneration) return;
      const latest = current.current;
      if (!latest) return;
      commit({ ...latest, messages: [...page.items.filter((row) => !latest.messages.some((message) => message.id === row.id)), ...latest.messages], messages_next_cursor: page.next_cursor });
    } catch (failure) { handleError(failure); }
    finally { if (live.current) setHistoryLoading(false); }
  }
  return { turnStarts: turnStarts.current, chats, nextCursor, selected, snapshot, loading, listLoading, error, setError, connection,
    command, sending, acting, historyLoading, loadOlder, draft, setDraft, requestMode, setRequestMode, create, send, execute, cancel, rename, remove, selectChat,
    refreshList, retry: () => command ? execute(command) : Promise.resolve(false),
    refresh: () => selected ? selectChat(selected, snapshot?.selected_branch_id) : refreshList() };
}
