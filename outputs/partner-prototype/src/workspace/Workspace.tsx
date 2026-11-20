import { useTurnTiming } from './useTurnTiming';
import { AccountingPanel } from './AccountingPanel';
import { VendorWishesExport } from './VendorWishesExport';
import { product } from '../product';
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { ApiError, AuthClient, errorMessage, isRecord, type User } from '../api/client';
import { type Message, type Run, type RunStatus, type RequestMode } from '../api/chats';
import { Brand, Icon } from '../ui';
import { Knowledge } from '../knowledge/Knowledge';
import { Groups } from '../admin/Groups';
import { terminal, useWorkspace } from './useWorkspace';
import { ChatAttachments, type ChatFile } from './ChatAttachments';
import { useAnswerPlayback, type AnswerTarget } from './useAnswerPlayback';
import { AnswerSources, type AnswerCitation } from './AnswerSources';
import { ProjectPanel } from './ProjectPanel';
import { DesignResultCard } from './DesignResultCard';
import './workspace.css';

interface Props { user: User; auth: AuthClient; appearance: boolean; openAppearance: () => void; logout: () => Promise<void>; logoutPending: boolean; onExpired: () => void; sessionError: { error: unknown; action: 'logout' | 'session' } | null }
const labels: Record<RunStatus, string> = { queued: 'В очереди', running: 'Выполняется', waiting_input: 'Нужно уточнение', succeeded: 'Готово', failed: 'Техническая ошибка', cancelled: 'Остановлено' };
const disabledHint = 'Файлы доступны только владельцу чата.';
const requestModeLabels: Record<RequestMode, string> = { auto: 'Авто', design: 'Проектирование', reference: 'Справка' };
const designExample = 'Есть открытая офисная сеть и закрытая сеть цеха. '
  + 'Два компьютера цеха отправляют журналы одному серверу офиса по UDP 514. '
  + 'Обратный обмен не нужен.';

function DesignExample({ disabled, useExample }: { disabled: boolean; useExample: () => void }) {
  return <div className="design-example">
    <p>{designExample}</p>
    <button type="button" onClick={useExample} disabled={disabled}>Подставить пример в сообщение</button>
  </div>;
}

function WorkspaceError({ error }: { error: unknown }) {
  let message = errorMessage(error, 'session');
  if (error instanceof ApiError && error.status === 404) message = 'Чат удалён или недоступен. Выберите другой чат.';
  if (error instanceof ApiError && error.status === 409) message = 'Состояние изменилось. Данные обновлены; повторите действие.';
  if (error instanceof ApiError && error.code === 'clipboard_unavailable') message = 'Копирование недоступно в этом браузере. Выделите текст ответа.';
  if (error instanceof ApiError && error.status === 422) message = 'Проверьте сообщение. Максимум — 32 КиБ текста.';
  if (error instanceof ApiError && ['question_context_changed', 'design_revision_conflict'].includes(error.code)) message = 'Условия проекта уже изменились. Откройте актуальную версию и выберите ответ снова.';
  if (error instanceof ApiError && error.code === 'invalid_project_answer') message = 'Ответ не согласуется с другими условиями. Проверьте поле в актуальном проекте.';
  return <div className="workspace-error" role="alert"><p>{message}</p>{error instanceof ApiError && error.requestId && <span className="request-reference">Номер запроса: {error.requestId}</span>}</div>;
}

function runLabel(run?: Run): string {
  const outcome = isRecord(run?.outcome) ? run.outcome : null;
  const profile = outcome?.profile ?? run?.runtime_profile;
  if (outcome?.execution === 'local_project_answer') return 'Уточнение · без вызова модели';
  if (profile === 'offline_fixture') return 'Офлайн-стенд · без LLM';
  if (outcome?.answer_mode === 'project_explanation') return 'Синоникс · пояснение схемы';
  if (profile === 'openai_compatible') return outcome?.answer_mode === 'design' ? 'Синоникс · проектирование' : outcome?.answer_mode === 'conversation' ? 'Ассистент' : 'Ответ по источникам';
  return 'Чат по базе знаний';
}

function runFailure(run: Run): string {
  const outcome = isRecord(run.outcome) ? run.outcome : null;
  if (outcome?.code === 'question_context_changed') return 'Уточнение относится к прежним условиям. Откройте актуальный проект.';
  if (outcome?.code === 'invalid_project_answer') return 'Ответ не согласуется с другими условиями. Проверьте актуальный проект.';
  if (outcome?.code === 'design_revision_conflict') return 'Условия проекта изменились во время подготовки ответа. Откройте актуальный проект и повторите уточнение.';
  if (outcome?.code === 'request_mode_violation' || outcome?.code === 'provider_request_mode_violation') return 'Ответ модели не соответствует выбранному режиму. Повторите запрос или выберите другой режим.';
  if (outcome?.code === 'source_context_changed') return 'Источники изменились или больше недоступны. Проверьте исходные условия проекта перед новым расчётом.';
  if (outcome?.code === 'rag_context_limit') return 'Сообщение вместе с условиями проекта слишком большое. Сократите сообщение или отправьте уточнения по частям. Сохранённый проект доступен для продолжения.';
  if (outcome?.code === 'provider_incomplete_response') return 'Сервис модели прервал ответ. Изменения проекта не сохранены. Попробуйте уточнить меньше условий за один запрос или дополните проект вручную.';
  if (typeof outcome?.code === 'string' && outcome.code.startsWith('provider_invalid_design')) return 'Не удалось проверить условия, предложенные моделью. Повторите запрос или дополните проект вручную.';
  if (outcome?.code === 'provider_request_failed') return 'Не удалось получить ответ от сервиса модели. Проверьте подключение к провайдеру и повторите запрос.';
  return 'Выполнение завершилось технической ошибкой. Ответ не подтверждён.';
}

function RunState({ run }: { run: Run }) {
  return <span className={`run-status run-${run.status}`}><i aria-hidden="true" />{labels[run.status]}{run.status === 'queued' && run.queue_position > 0 ? ` · ${run.queue_position}` : ''}</span>;
}

export function Workspace({ user, auth, appearance, openAppearance, logout, logoutPending, onExpired, sessionError }: Props) {
  const space = useWorkspace(auth, onExpired);
  const [menu, setMenu] = useState(false);
  const [knowledgeOpen, setKnowledgeOpen] = useState(() => window.location.pathname === '/knowledge');
  const [groupsOpen, setGroupsOpen] = useState(false);
  const [designOpen, setDesignOpen] = useState(false);
  const [questionsRefresh, setQuestionsRefresh] = useState(0);
  const [readyDesigns, setReadyDesigns] = useState<Set<string>>(new Set());
  const [designRevision, setDesignRevision] = useState<string | null>(null);
  const [mobile, setMobile] = useState(() => matchMedia('(max-width:760px)').matches);
  const [search, setSearch] = useState('');
  const [dialog, setDialog] = useState<{ kind: 'rename' | 'delete' | 'edit'; message?: Message } | null>(null);
  const [dialogText, setDialogText] = useState('');
  const [dialogBusy, setDialogBusy] = useState(false);
  const [clarification, setClarification] = useState<Record<string, string>>({});
  const [copied, setCopied] = useState<string | null>(null);
  const [showJump, setShowJump] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const closeMenu = useRef<HTMLButtonElement>(null);
  const conversation = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const dialogPanel = useRef<HTMLElement>(null);
  const following = useRef(true);
  const scrollOwner = useRef<string | null>(null);
  const scrollPositions = useRef(new Map<string, { top: number; following: boolean }>());
  const value = space.snapshot;
  const [draftFiles, setDraftFiles] = useState<Record<string, ChatFile[]>>({});
  const [filesUploading, setFilesUploading] = useState(false);
  const fileKey = value ? `${value.chat.id}:${value.selected_branch_id}` : '';
  const files = draftFiles[fileKey] ?? [];
  const filesReady = !filesUploading && files.every(file => file.status === 'ready');
  async function sendMessage(): Promise<boolean> {
    if (!filesReady) return false;
    const key = fileKey, sent = files.map(file => file.id);
    const accepted = await space.send(sent);
    if (accepted) setDraftFiles(old => ({ ...old, [key]: (old[key] ?? []).filter(file => !sent.includes(file.id)) }));
    return accepted;
  }
  async function retryMessage(): Promise<void> {
    const command = space.command;
    const accepted = await space.retry();
    if (accepted && command?.kind === 'send') {
      const key = `${command.chatId}:${command.branchId}`;
      const sent = command.body.attachment_ids;
      setDraftFiles(old => ({ ...old, [key]: (old[key] ?? []).filter(file => !sent.includes(file.id)) }));
    }
  }
  const answerTargets = useMemo(() => {
    if (!value) return [];
    const targets = new Map<string, AnswerTarget>();
    for (const run of value.runs.filter(item => item.branch_id === value.selected_branch_id)) {
      targets.set(run.id, { id: run.id, text: run.partial_text, finished: run.status === 'succeeded', stopped: ['failed', 'cancelled', 'waiting_input'].includes(run.status) });
    }
    for (const message of value.messages.filter(item => item.role === 'assistant')) {
      const id = message.run_id ?? message.id;
      targets.set(id, { id, text: message.content, finished: true, stopped: false });
    }
    return [...targets.values()];
  }, [value]);
  const shownAnswer = useAnswerPlayback(fileKey, answerTargets, () => {
    if (conversation.current && following.current) conversation.current.scrollTop = conversation.current.scrollHeight;
  });
  const timingRevision = useTurnTiming(auth, value?.chat.id, value?.runs ?? [], answerTargets, shownAnswer, space.turnStarts, readyDesigns);
  const currentBranch = value?.selected_branch_id;
  const active = value?.runs.filter((run) => !terminal(run.status)).sort((left, right) => left.queue_seq - right.queue_seq) ?? [];
  const selectedRuns = value?.runs.filter((run) => run.branch_id === currentBranch) ?? [];
  const selectedMessages = value?.messages ?? [];
  const latestProjectId = selectedRuns.reduce<string | null>((last, run) => isRecord(run.outcome) && typeof run.outcome.design_id === 'string' ? run.outcome.design_id : last, null);
  const selectedRunMap = new Map(selectedRuns.map((run) => [run.id, run]));
  const groups = new Map<string, Message[]>();
  for (const message of selectedMessages) {
    const key = message.run_id ?? message.id;
    groups.set(key, [...(groups.get(key) ?? []), message]);
  }
  const filtered = space.chats.filter((chat) => chat.title.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  const role = { admin: 'Администратор', curator: 'Администратор', user: 'Участник команды' }[user.role];
  const connectionText = { connected: 'Соединение установлено', connecting: 'Подключаемся…', reconnecting: 'Восстанавливаем соединение…', closed: 'Соединение закрыто' }[space.connection];

  const manager = user.role === 'admin' || user.role === 'curator';
  function navigateKnowledge(open: boolean): void {
    window.history.pushState(null, '', open ? '/knowledge' : '/');
    setKnowledgeOpen(open); setMenu(false);
  }
  useEffect(() => {
    const navigate = (): void => { setKnowledgeOpen(window.location.pathname === '/knowledge'); setGroupsOpen(false); };
    window.addEventListener('popstate', navigate);
    return () => window.removeEventListener('popstate', navigate);
  }, []);
  useEffect(() => {
    document.title = `${product.name} — ${knowledgeOpen ? 'документы' : 'рабочее пространство'}`;
  }, [knowledgeOpen]);
  useEffect(() => {
    const query = matchMedia('(max-width:760px)');
    const update = (): void => { setMobile(query.matches); if (!query.matches) setMenu(false); };
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    if (!menu || !mobile) return;
    closeMenu.current?.focus();
    return () => trigger.current?.focus();
  }, [menu, mobile]);
  useEffect(() => {
    const escape = (event: KeyboardEvent): void => { if (event.key === 'Escape') { setMenu(false); setDialog(null); } };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, []);
  useEffect(() => {
    if (!dialog) return;
    const previous = document.activeElement;
    dialogPanel.current?.querySelector<HTMLElement>('textarea,input,button')?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, [dialog?.kind]);
  useLayoutEffect(() => {
    const view = conversation.current;
    const key = value ? `${value.chat.id}:${value.selected_branch_id}` : null;
    if (!key) scrollOwner.current = null;
    if (view && key && scrollOwner.current !== key) {
      const position = scrollPositions.current.get(key);
      following.current = position?.following ?? true;
      view.scrollTop = following.current ? view.scrollHeight : position?.top ?? 0;
      scrollOwner.current = key;
      setShowJump(!following.current);
    } else if (view && following.current) view.scrollTop = view.scrollHeight;
    if (input.current) { input.current.style.height = '28px'; input.current.style.height = `${Math.min(input.current.scrollHeight, 160)}px`; }
  }, [value, space.draft]);

  async function submit(event: FormEvent): Promise<void> { event.preventDefault(); if (await sendMessage()) input.current?.focus(); }
  function selectChat(id: string, branch?: string): void { setMenu(false); void space.selectChat(id, branch); }
  async function fork(message: Message, mode: 'edit' | 'regenerate', revisedText?: string): Promise<boolean> {
    if (!value || space.command) return false;
    return space.execute({ kind: 'fork', key: crypto.randomUUID(), chatId: value.chat.id, branchId: value.selected_branch_id,
      body: { parent_branch_id: value.selected_branch_id, message_id: message.id, mode, ...(mode === 'edit' ? { revised_text: revisedText } : {}) } });
  }
  async function resume(run: Run, answer: string): Promise<void> {
    if (!value || !run.clarification_id || !answer.trim() || space.command) return;
    if (new TextEncoder().encode(answer.trim()).length > 32768) { space.setError(new ApiError(422, 'message_too_large')); return; }
    await space.execute({ kind: 'resume', key: crypto.randomUUID(), chatId: value.chat.id, branchId: run.branch_id, runId: run.id,
      body: { clarification_id: run.clarification_id, answer: answer.trim() } });
  }
  async function dialogSubmit(event: FormEvent): Promise<void> {
    event.preventDefault(); if (!dialog || dialogBusy) return;
    setDialogBusy(true);
    try {
      const done = dialog.kind === 'rename' ? await space.rename(dialogText.trim()) : dialog.kind === 'delete' ? await space.remove()
        : dialog.message ? await fork(dialog.message, 'edit', dialogText.trim()) : false;
      if (done) setDialog(null);
    } finally { setDialogBusy(false); }
  }
  async function copy(message: Message): Promise<void> {
    try { await navigator.clipboard.writeText(message.content); setCopied(message.id); }
    catch { space.setError(new ApiError(0, 'clipboard_unavailable')); }
  }
  function runDetails(run: Run) {
    return <div className="run-detail" key={run.id}>
      <RunState run={run} />
      {!terminal(run.status) && <button className="run-stop" aria-label={`Остановить выполнение ${run.queue_seq}`} onClick={() => { void space.cancel(run.id); }}><Icon name="stop" />Стоп</button>}
      {run.status === 'failed' && <p className="run-explanation">{runFailure(run)}</p>}
      {run.status === 'cancelled' && <p className="run-explanation">Выполнение остановлено. Полученный фрагмент сохранён.</p>}
      {run.status === 'waiting_input' && <div className="clarification">
        <p>{run.clarification_question ?? 'Уточните запрос, чтобы продолжить.'}</p>
        {!!run.clarification_options.length && <div className="clarification-options">{run.clarification_options.map((option) => <button key={option} disabled={!!space.command || space.sending} onClick={() => { void resume(run, option); }}>{option}</button>)}</div>}
        <form onSubmit={(event) => { event.preventDefault(); void resume(run, clarification[run.id] ?? ''); }}><label className="visually-hidden" htmlFor={`clarification-${run.id}`}>Ответ на уточнение</label><input id={`clarification-${run.id}`} value={clarification[run.id] ?? ''} onChange={(event) => setClarification((old) => ({ ...old, [run.id]: event.target.value }))} placeholder="Или напишите ответ…" disabled={!!space.command || space.sending} maxLength={32768} /><button disabled={!!space.command || space.sending || !clarification[run.id]?.trim()}>Продолжить</button></form>
      </div>}
    </div>;
  }
  function messageRow(message: Message) {
    const run = message.run_id ? selectedRunMap.get(message.run_id) : undefined;
    if (message.role === 'user') return <div className="user-row" key={message.id} data-message-id={message.id}><div className="user-message"><p>{message.content}</p>{run && run.request_mode !== 'auto' && <span className="message-request-mode">{requestModeLabels[run.request_mode]}</span>}{!!run?.attachments?.length && <div className="message-attachments">{run.attachments.map(file => <span key={file.id}><Icon name="doc" />{file.name}</span>)}</div>}<button className="edit-message" aria-label="Редактировать сообщение в новой ветке" disabled={!!space.command || message.ordinal !== 0} onClick={() => { setDialog({ kind: 'edit', message }); setDialogText(message.content); }}><Icon name="edit" />Изменить</button></div></div>;
    const outcome = run?.outcome as { profile?: string; citations?: AnswerCitation[]; usage?: { total_tokens?: number }; estimated_cost_usd?: number; design_id?: string; design_revision?: number; design_status?: string; design_schema_version?: number; design_has_diagram?: boolean; design_has_result?: boolean; design_has_questions?: boolean } | null | undefined;
    const citations = outcome?.citations ?? [];
    const presentationPending = run?.status === 'succeeded' && (
      shownAnswer(message.run_id ?? message.id, message.content) !== message.content
      || ((outcome?.design_status === 'conceptual' || outcome?.design_has_diagram === true || outcome?.design_has_questions === true) && typeof outcome.design_id === 'string'
        && !readyDesigns.has(outcome.design_id))
    );
    return <article className="assistant-message" key={message.id} data-message-id={message.id}><div className="agent-mark"><Icon name="message" /></div><div className="message-content"><div className="author">{product.name}<span>{runLabel(run)}</span></div><div className="prose stream-prose">{shownAnswer(message.run_id ?? message.id, message.content)}</div><AnswerSources citations={citations} />{value && typeof outcome?.design_id === 'string' && typeof outcome.design_revision === 'number' && <DesignResultCard auth={auth} chatId={value.chat.id} id={outcome.design_id} revision={outcome.design_revision} ready={outcome.design_status === 'conceptual' || outcome.design_has_diagram === true} requirements={outcome.design_schema_version === 3} calculated={outcome.design_has_result === true} questions={outcome.design_has_questions === true} branchId={value.selected_branch_id} newer={latestProjectId !== null && latestProjectId !== outcome.design_id} questionsRefresh={questionsRefresh} disabled={!!space.command || space.sending || selectedRuns.some(item => !terminal(item.status))} onAnswer={(message, answer) => space.execute({ kind: 'send', key: crypto.randomUUID(), chatId: value.chat.id, branchId: value.selected_branch_id, body: { branch_id: value.selected_branch_id, message, attachment_ids: [], web_mode: 'off', request_mode: 'design', ...(answer ? { project_answer: answer } : {}) } })} onDiagramReady={() => { const id = outcome.design_id; if (typeof id === 'string') setReadyDesigns(old => old.has(id) ? old : new Set([...old, id])); }} open={() => { setDesignRevision(outcome.design_id ?? null); setDesignOpen(true); }} onExpired={onExpired} />}<div className="message-actions"><button className="copy icon-button" aria-label="Скопировать ответ" onClick={() => { void copy(message); }}><Icon name={copied === message.id ? 'check' : 'copy'} /></button><button className="regenerate-button" disabled={!!space.command} aria-label="Повторить в новой ветке" onClick={() => { void fork(message, 'regenerate'); }}><Icon name="refresh" />Новая ветка</button></div>{run && terminal(run.status) && <div className="message-run-state">{presentationPending ? <span>Показываем результат…</span> : <RunState run={run} />}</div>}</div></article>;
  }
  if (knowledgeOpen && manager) return <>
    <div inert={groupsOpen || appearance || undefined}><Knowledge auth={auth} close={() => navigateKnowledge(false)} onExpired={onExpired} openGroups={() => setGroupsOpen(true)} /></div>
    {groupsOpen && <Groups auth={auth} close={() => setGroupsOpen(false)} onExpired={onExpired} />}
  </>;
  const privateInert = appearance || !!dialog || knowledgeOpen || groupsOpen || designOpen || (mobile && menu);
  return <>
    <div className={`layout auth-layout chat-layout ${menu ? 'menu-open' : ''}`}>
      <aside className="sidebar" aria-label="Панель чатов" inert={appearance || !!dialog || knowledgeOpen || groupsOpen || designOpen || (mobile && !menu) || undefined} aria-hidden={mobile && !menu || undefined} onKeyDown={(event) => {
        if (!mobile || !menu || event.key !== 'Tab') return;
        const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')];
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
        <Brand /><button ref={closeMenu} className="sidebar-close icon-button" aria-label="Закрыть список чатов" onClick={() => setMenu(false)}><Icon name="close" /></button>
        <button className="new-chat" disabled={space.acting} onClick={() => { setMenu(false); void space.create(); }}><Icon name="plus" /><span>{space.acting ? 'Создаём…' : 'Новый чат'}</span></button>
        <div className="search-box"><Icon name="search" /><input aria-label="Поиск чатов" placeholder="Поиск по чатам" value={search} onChange={(event) => setSearch(event.target.value)} /></div>
        <div className="chat-list"><div className="group-label">ВАШИ ЧАТЫ</div>{filtered.map((chat) => <button key={chat.id} className={`chat-item ${space.selected === chat.id ? 'active' : ''}`} title={chat.title} onClick={() => selectChat(chat.id)}><span>{chat.title}</span>{space.selected === chat.id && <span className="active-dot" />}</button>)}{!filtered.length && <p className="sidebar-empty">{space.listLoading ? 'Загружаем чаты…' : search ? 'Чаты не найдены' : 'Создайте первый разговор.'}</p>}{space.nextCursor && <button className="load-chats" disabled={space.listLoading} onClick={() => { void space.refreshList(true); }}>Показать ещё</button>}</div>
        <div className="sidebar-bottom">{manager && <button className="knowledge-nav" onClick={() => { setMenu(false); setGroupsOpen(true); }}><Icon name="doc" /><span>Пространства и группы</span></button>}{manager && <button className="knowledge-nav" onClick={() => { navigateKnowledge(true); }}><Icon name="doc" /><span>Документы и база знаний</span></button>}<p className="prototype-label">Прототип для хакатона<br /><span>{product.disclaimer}</span></p><div className="profile"><div className="avatar" aria-hidden="true">{user.login.slice(0, 1).toUpperCase()}</div><div className="profile-name">{user.login}<span>{role}</span></div><button className="settings logout-button" aria-label="Выйти" title="Выйти" disabled={logoutPending} onClick={() => { void logout(); }}><Icon name="logout" /></button></div></div>
      </aside>
      <button className="scrim" aria-label="Закрыть меню" onClick={() => setMenu(false)} tabIndex={menu ? 0 : -1} />
      <main className="main" inert={privateInert || undefined}>
        <header className="header"><div className="header-left"><button ref={trigger} className="menu-button icon-button" aria-label="Показать список чатов" aria-expanded={menu} onClick={() => setMenu(true)}><Icon name="panel" /></button><span className="model-button">{product.name}</span><span className="header-divider" /><span className="chat-title">{value?.chat.title ?? 'Рабочее пространство'}</span></div><div className="header-right">{value && <button className="appearance-button" onClick={() => { setDesignRevision(null); setDesignOpen(true); }}><Icon name="doc" /><span>Проект Синоникса</span></button>}<button className="appearance-button" aria-label="Настройки оформления" onClick={openAppearance}><Icon name="sun" /><span>Оформление</span></button>{value && <><button className="icon-button" aria-label="Переименовать чат" title="Переименовать чат" onClick={() => { setDialog({ kind: 'rename' }); setDialogText(value.chat.title); }}><Icon name="edit" /></button><button className="icon-button" aria-label="Удалить чат" title="Удалить чат" onClick={() => setDialog({ kind: 'delete' })}><Icon name="trash" /></button></>}</div></header>
        <div className="chat-context-bar"><span className="fixture-label">{runLabel(selectedRuns.at(-1))}</span>{value && <label className="branch-picker">Ветка<select aria-label="Ветка разговора" value={value.selected_branch_id} onChange={(event) => selectChat(value.chat.id, event.target.value)}>{value.branches.map((branch, index) => <option key={branch.id} value={branch.id}>{branch.id === value.chat.default_branch_id ? 'Основная' : `Ветка ${index + 1}`}</option>)}</select></label>}<span className={`stream-state stream-${space.connection}`} role="status">{value ? connectionText : ''}</span></div>
        {(space.error || sessionError) && <div className="chat-banner"><WorkspaceError error={space.error ?? sessionError?.error} /><button className="inline-retry" onClick={() => { void space.refresh(); }}>Обновить</button></div>}
        {space.command && <div className="command-banner" role="status"><p>{space.sending ? 'Подтверждаем приём…' : 'Приём не подтверждён. Повтор использует тот же запрос и не создаст дубликат.'}</p>{!space.sending && <button onClick={() => { void retryMessage(); }}>Повторить отправку</button>}{space.command.chatId !== space.selected && <button onClick={() => selectChat(space.command!.chatId, space.command!.branchId)}>Открыть этот чат</button>}</div>}
        {active.length > 0 && <div className="run-queue" aria-label="Очередь выполнения">{active.map((run) => <div key={run.id} className="queue-item"><RunState run={run} />{run.branch_id !== currentBranch && <button onClick={() => { if (value) selectChat(value.chat.id, run.branch_id); }}>Другая ветка</button>}<button aria-label={`Остановить в очереди ${run.queue_seq}`} title="Остановить" onClick={() => { void space.cancel(run.id); }}><Icon name="stop" /></button></div>)}</div>}
        <div ref={conversation} className="conversation chat-conversation" aria-label="История сообщений" role="log" aria-live="off" tabIndex={0} onPointerDown={() => { following.current = false; }} onWheel={(event) => { if (event.deltaY < 0) following.current = false; }} onKeyDown={(event) => { if (['PageUp', 'Home', 'ArrowUp'].includes(event.key)) following.current = false; }} onScroll={(event) => { const view = event.currentTarget; if (view.scrollHeight - view.clientHeight - view.scrollTop < 60) following.current = true; setShowJump(!following.current); if (value) scrollPositions.current.set(`${value.chat.id}:${value.selected_branch_id}`, { top: view.scrollTop, following: following.current }); }}>
          {space.loading && !value ? <div className="thread chat-loading" role="status">Загружаем переписку…</div> : !value ? <section className="empty connected-empty">
            <span className="workspace-symbol" aria-hidden="true"><Icon name="message" /></span>
            <h1>С чего начнём?</h1>
            <p>Опишите инфраструктуру — помогу уточнить условия обмена и подготовить проект Синоникса.</p>
            <button className="empty-new-chat" aria-label="Создать первый чат" onClick={() => { void space.create(); }} disabled={space.acting}>Новый чат<Icon name="plus" /></button>
            <DesignExample disabled={space.acting} useExample={() => { void space.create(designExample); }} />
          </section> : <div className="thread">
            {!selectedMessages.length && !selectedRuns.length && <section className="empty chat-welcome">
              <h1>Новый разговор</h1>
              <p>Опишите сети, отправителей и получателей данных.<br />Я уточню недостающие условия и подготовлю схему. Можно прикрепить документы или задать вопрос о Синониксе.</p>
              <DesignExample disabled={!!space.command || !!space.draft || space.loading} useExample={() => {
                space.setDraft(designExample);
                input.current?.focus();
              }} />
            </section>}
            {value.messages_next_cursor && <button className="load-older" disabled={space.historyLoading} onClick={() => {
              const view = conversation.current;
              const height = view?.scrollHeight ?? 0, top = view?.scrollTop ?? 0;
              following.current = false;
              void space.loadOlder().then(() => requestAnimationFrame(() => { if (view) view.scrollTop = top + view.scrollHeight - height; }));
            }}>{space.historyLoading ? 'Загружаем…' : 'Загрузить предыдущие сообщения'}</button>}
            {[...groups.entries()].map(([id, messages]) => {
              const run = selectedRunMap.get(id);
              const showPartial = run && run.status !== 'succeeded' && !messages.some((message) => message.role === 'assistant');
              return <Fragment key={id}>{messages.map(messageRow)}{showPartial && <article className={`assistant-message run-message ${run.status === 'running' ? 'is-streaming' : ''}`} data-run-id={run.id}><div className="agent-mark"><Icon name="message" /></div><div className="message-content"><div className="author">{product.name}<span>{runLabel(run)}</span></div>{run.partial_text && <div className="prose stream-prose">{shownAnswer(run.id, run.partial_text)}</div>}{runDetails(run)}</div></article>}</Fragment>;
            })}
            <div className="thread-tail" />
          </div>}
        </div>
        {showJump && <button className="jump-latest" aria-label="К последнему сообщению" onClick={() => { if (conversation.current) conversation.current.scrollTop = conversation.current.scrollHeight; following.current = true; setShowJump(false); }}><Icon name="down" />Вниз</button>}
        <div className="composer-area"><div className="composer-inner">{value && <VendorWishesExport key={`wishes-${value.chat.id}`} auth={auth} chatId={value.chat.id} onExpired={onExpired} />}{value && <AccountingPanel key={value.chat.id} auth={auth} chatId={value.chat.id} revision={value.cursor + timingRevision} onExpired={onExpired} />}<form className="composer" data-lg-role="dock" onSubmit={(event) => { void submit(event); }}><ChatAttachments key={fileKey} auth={auth} chatId={value?.chat.id ?? ''} files={files} change={next => setDraftFiles(old => ({ ...old, [fileKey]: next }))} disabled={!value || !!space.command} busy={setFilesUploading} onExpired={onExpired} active={!privateInert}><textarea ref={input} rows={1} aria-label="Сообщение" value={space.draft} disabled={!value || !!space.command} placeholder={value ? 'Напишите сообщение…' : 'Сначала создайте чат'} onChange={(event) => space.setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (space.draft.trim()) void sendMessage(); } }} /><button className="send" type="submit" disabled={!value || !!space.command || !space.draft.trim() || !filesReady} aria-label="Отправить сообщение"><Icon name="arrow" /></button></ChatAttachments></form><div className="composer-caption"><label className="composer-mode">Режим<select aria-label="Режим ответа" value={space.requestMode} disabled={!value || !!space.command || space.loading} onChange={event => space.setRequestMode(event.target.value as RequestMode)}>{Object.entries(requestModeLabels).map(([mode, label]) => <option key={mode} value={mode}>{label}</option>)}</select></label><span id="unsupported-tools">{disabledHint}</span></div>{user.runtime_profile === 'offline_fixture' && <details className="fixture-tools"><summary>Проверка офлайн-стенда</summary><div>{[['Медленный ответ', '/fixture slow Проверка остановки'], ['Уточнение', '/fixture clarify Выбор'], ['Техническая ошибка', '/fixture fail']].map(([title, text]) => <button key={text} disabled={!value || !!space.command} onClick={() => { space.setDraft(text); input.current?.focus(); }}>{title}</button>)}</div></details>}</div></div>
      </main>
    </div>
    {designOpen && value && <ProjectPanel key={`${value.chat.id}:${value.selected_branch_id}`} auth={auth} chatId={value.chat.id} branchId={value.selected_branch_id} revisionId={designRevision} close={() => { setDesignOpen(false); setQuestionsRefresh(old => old + 1); void space.refresh(); }} onExpired={onExpired} />}
    {groupsOpen && <Groups auth={auth} close={() => { setGroupsOpen(false); if (mobile) requestAnimationFrame(() => trigger.current?.focus()); }} onExpired={onExpired} />}
    {dialog && <div className="appearance-overlay" onClick={(event) => { if (event.target === event.currentTarget && !dialogBusy) setDialog(null); }}><section ref={dialogPanel} className="appearance-panel chat-dialog" role="dialog" aria-modal="true" aria-labelledby="chat-dialog-title" onKeyDown={(event) => {
      if (event.key !== 'Tab') return;
      const items = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled)')];
      const first = items[0], last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}><header className="appearance-heading"><h2 id="chat-dialog-title">{dialog.kind === 'rename' ? 'Переименовать чат' : dialog.kind === 'delete' ? 'Удалить чат?' : 'Изменить в новой ветке'}</h2><button className="icon-button" aria-label="Закрыть диалог" disabled={dialogBusy} onClick={() => setDialog(null)}><Icon name="close" /></button></header><form onSubmit={(event) => { void dialogSubmit(event); }}>{dialog.kind === 'delete' ? <p>Этот чат и его переписка больше не будут доступны.</p> : <><label htmlFor="chat-dialog-text">{dialog.kind === 'rename' ? 'Название чата' : 'Текст сообщения'}</label><textarea id="chat-dialog-text" rows={dialog.kind === 'edit' ? 4 : 2} maxLength={dialog.kind === 'rename' ? 200 : 32768} value={dialogText} onChange={(event) => setDialogText(event.target.value)} disabled={dialogBusy} required /></>}{dialog.kind === 'edit' && <p>Исходный разговор сохранится. Ответ появится в новой ветке.</p>}{space.error !== null && <WorkspaceError error={space.error} />}{dialog.kind === 'edit' && space.command && <div className="dialog-pending"><p>Приём не подтверждён. Повтор использует исходный запрос.</p><button type="button" disabled={space.sending} onClick={() => { void space.retry().then((done) => { if (done) setDialog(null); }); }}>Повторить отправку</button></div>}<button className={`login-submit ${dialog.kind === 'delete' ? 'delete-confirm' : ''}`} disabled={dialogBusy || (dialog.kind === 'edit' && !!space.command) || (dialog.kind !== 'delete' && !dialogText.trim())}>{dialogBusy ? 'Сохраняем…' : dialog.kind === 'rename' ? 'Сохранить' : dialog.kind === 'delete' ? 'Удалить' : 'Создать ветку'}</button></form></section></div>}
  </>;
}
