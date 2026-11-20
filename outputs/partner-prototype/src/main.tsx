import { product } from './product';
import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, AuthClient, errorMessage, type User } from './api/client';
import { mountGlass } from './glass';
import { Icon, Brand } from './ui';
import { Workspace } from './workspace/Workspace';
import { mountVideoBackdrop } from './scene';
import './style.css';
import './themes.css';
import './auth.css';

const themes = [
  { id: 'standard', name: 'Стандартная', caption: 'Чистый фон. Чёткий фокус.' },
  { id: 'dark', name: 'Тёмная', caption: 'Графит и жёлтые акценты.' },
  { id: 'glass', name: 'Liquid Glass', caption: 'Свет, глубина и преломление.' },
  { id: 'mixed', name: 'Смешанная', caption: 'Стекло там, где оно нужно.' },
] as const;
type Theme = typeof themes[number]['id'];
type SessionState = { kind: 'loading' } | { kind: 'unavailable'; error: unknown }
  | { kind: 'signed-out'; notice?: string } | { kind: 'signed-in'; user: User };
document.title = `${product.name} — рабочее пространство`;
const auth = new AuthClient();
function ErrorNotice({ error, action }: { error: unknown; action: 'login' | 'logout' | 'session' }) {
  return <div className="auth-error" role="alert">
    <p>{errorMessage(error, action)}</p>
    {error instanceof ApiError && error.requestId && <span className="request-reference">Номер запроса: {error.requestId}</span>}
  </div>;
}

function Login({ notice, onLogin }: { notice?: string; onLogin: (user: User) => void }) {
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (pending) return;
    setPending(true); setError(null);
    try {
      const user = await auth.login(login, password);
      setPassword('');
      onLogin(user);
    } catch (failure) {
      setPassword(''); setError(failure);
    } finally { setPending(false); }
  }
  return <section className="auth-card" aria-labelledby="login-title" aria-busy={pending}>
    <span className="auth-mark" aria-hidden="true"><Icon name="message" /></span>
    <span className="auth-eyebrow">ЛИЧНОЕ ПРОСТРАНСТВО</span>
    <h1 id="login-title">Вход в {product.name}</h1>
    <p className="auth-description">Используйте учётную запись вашей команды.</p>
    {notice && !error && <p className="auth-notice" role="status">{notice}</p>}
    <form className="login-form" aria-label="Вход" onSubmit={(event) => { void submit(event); }}>
      <label htmlFor="login">Логин</label>
      <input id="login" name="username" autoComplete="username" required maxLength={128} autoCapitalize="none" spellCheck={false} value={login} onChange={(event) => setLogin(event.target.value)} disabled={pending} />
      <label htmlFor="password">Пароль</label>
      <input id="password" name="password" type="password" autoComplete="current-password" required maxLength={1024} value={password} onChange={(event) => setPassword(event.target.value)} disabled={pending} aria-describedby={error ? 'login-error' : undefined} />
      {error !== null && <div id="login-error"><ErrorNotice error={error} action="login" /></div>}
      <button className="login-submit" type="submit" disabled={pending}>{pending ? 'Входим…' : 'Войти'}<Icon name="arrow" /></button>
    </form>
    <p className="account-help">Если у вас нет доступа, обратитесь к администратору команды.</p>
  </section>;
}

function Appearance({ theme, selectTheme, close }: { theme: Theme; selectTheme: (theme: Theme) => void; close: () => void }) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    panel.current?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  function keydown(event: KeyboardEvent<HTMLElement>): void {
    if (event.key === 'Escape') { event.preventDefault(); close(); return; }
    if (event.key !== 'Tab') return;
    const elements = [...(panel.current?.querySelectorAll<HTMLButtonElement>('button') ?? [])].filter((element) => element.tabIndex >= 0);
    const first = elements[0], last = elements.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
  function radioKey(event: KeyboardEvent<HTMLButtonElement>): void {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = themes.findIndex((item) => item.id === theme);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? themes.length - 1
      : (index + (['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 1) + themes.length) % themes.length;
    const selected = themes[next].id;
    selectTheme(selected);
    panel.current?.querySelector<HTMLElement>(`[data-theme-choice="${selected}"]`)?.focus();
  }
  return <div className="appearance-overlay" onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
    <section ref={panel} className="appearance-panel" role="dialog" aria-modal="true" aria-labelledby="appearance-title" onKeyDown={keydown}>
      <header className="appearance-heading"><div><span className="appearance-kicker">ВАШЕ ПРОСТРАНСТВО</span><h2 id="appearance-title">Оформление</h2></div><button className="appearance-close icon-button" aria-label="Закрыть настройки оформления" onClick={close}><Icon name="close" /></button></header>
      <p className="appearance-description">Выберите тему рабочего пространства.</p>
      <div className="theme-options" role="radiogroup" aria-label="Тема интерфейса">
        {themes.map((item) => <button key={item.id} className={`theme-option ${theme === item.id ? 'chosen' : ''}`} data-theme-choice={item.id} role="radio" aria-label={item.name} aria-checked={theme === item.id} tabIndex={theme === item.id ? 0 : -1} onClick={() => selectTheme(item.id)} onKeyDown={radioKey}>
          <span className={`theme-preview preview-${item.id}`} aria-hidden="true"><span className="preview-sidebar"><i /><i /><i /></span><span className="preview-content"><i className="preview-bubble" /><i className="preview-line" /><i className="preview-line short" /><i className="preview-input" /></span></span>
          <span className="theme-option-label">{item.name}<span className="theme-check"><Icon name="check" /></span></span><span className="theme-caption">{item.caption}</span>
        </button>)}
      </div>
      <footer className="appearance-footer"><span>Выбор сохраняется на этом устройстве</span><button className="appearance-done" onClick={close}>Готово <Icon name="check" /></button></footer>
    </section>
  </div>;
}

function App() {
  const [session, setSession] = useState<SessionState>({ kind: 'loading' });
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const saved = localStorage.getItem('partner-theme');
      if (themes.some((item) => item.id === saved)) return saved as Theme;
    } catch { /* Storage is optional and never contains authentication state. */ }
    return 'mixed';
  });
  const [appearance, setAppearance] = useState(false);
  const [menu, setMenu] = useState(false);
  const [mobile, setMobile] = useState(() => matchMedia('(max-width:760px)').matches);
  const menuTrigger = useRef<HTMLButtonElement>(null);
  const sidebarClose = useRef<HTMLButtonElement>(null);
  const [logoutPending, setLogoutPending] = useState(false);
  const [sessionError, setSessionError] = useState<{ error: unknown; action: 'logout' | 'session' } | null>(null);
  const epoch = useRef(0);
  const backdrop = useRef<ReturnType<typeof mountVideoBackdrop> | null>(null);
  const refreshBusy = useRef(false);
  const logoutActive = useRef(false);
  const user = session.kind === 'signed-in' ? session.user : null;

  useEffect(() => {
    backdrop.current = mountVideoBackdrop();
    return () => { backdrop.current?.dispose(); backdrop.current = null; };
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    backdrop.current?.sync(theme === 'glass');
    try { localStorage.setItem('partner-theme', theme); } catch { /* Optional preference only. */ }
  }, [theme]);
  useEffect(() => mountGlass(theme), [theme, appearance, session.kind]);

  const initialize = useCallback(async (): Promise<void> => {
    const generation = ++epoch.current;
    setSession({ kind: 'loading' }); setSessionError(null);
    try {
      let current: User | null = null;
      try { current = await auth.me(); }
      catch (failure) { if (!(failure instanceof ApiError) || failure.status !== 401) throw failure; }
      if (!current) await auth.csrf();
      if (epoch.current === generation) setSession(current ? { kind: 'signed-in', user: current } : { kind: 'signed-out' });
    } catch (failure) {
      if (epoch.current === generation) setSession({ kind: 'unavailable', error: failure });
    }
  }, []);
  useEffect(() => { void initialize(); }, [initialize]);

  // Recheck the server when a user returns; background polling would incorrectly
  // keep an otherwise idle session alive. No local timer assumes authentication.
  useEffect(() => {
    if (!user) return;
    async function refresh(): Promise<void> {
      if (document.hidden || refreshBusy.current || logoutActive.current) return;
      refreshBusy.current = true;
      const generation = epoch.current;
      try {
        const current = await auth.me();
        if (epoch.current === generation) { setSession({ kind: 'signed-in', user: current }); setSessionError(null); }
      } catch (failure) {
        if (epoch.current !== generation) return;
        if (failure instanceof ApiError && failure.status === 401) {
          ++epoch.current; auth.reset(); setMenu(false); setSessionError(null);
          setSession({ kind: 'signed-out', notice: 'Сессия истекла. Войдите снова.' });
        } else setSessionError({ error: failure, action: 'session' });
      } finally { refreshBusy.current = false; }
    }
    const check = (): void => { void refresh(); };
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', check);
    return () => { window.removeEventListener('focus', check); document.removeEventListener('visibilitychange', check); };
  }, [user?.id]);

  useEffect(() => {
    const query = matchMedia('(max-width:760px)');
    const update = (): void => { setMobile(query.matches); if (!query.matches) setMenu(false); };
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    if (!menu || !mobile) return;
    sidebarClose.current?.focus();
    return () => menuTrigger.current?.focus();
  }, [menu, mobile]);

  useEffect(() => {
    const escape = (event: globalThis.KeyboardEvent): void => { if (event.key === 'Escape') setMenu(false); };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, []);

  async function logout(): Promise<void> {
    if (logoutPending) return;
    ++epoch.current; logoutActive.current = true; setLogoutPending(true); setSessionError(null);
    try {
      await auth.logout();
      setMenu(false); setSession({ kind: 'signed-out' });
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) {
        auth.reset(); setMenu(false); setSession({ kind: 'signed-out', notice: 'Сессия истекла. Войдите снова.' });
      } else setSessionError({ error: failure, action: 'logout' });
    } finally { logoutActive.current = false; setLogoutPending(false); }
  }

  return <>
    {user ? <Workspace key={user.id} user={user} auth={auth} appearance={appearance}
      openAppearance={() => setAppearance(true)} logout={logout} logoutPending={logoutPending}
      sessionError={sessionError} onExpired={() => {
        ++epoch.current; auth.reset(); setMenu(false); setSessionError(null);
        setSession({ kind: 'signed-out', notice: 'Сессия истекла. Войдите снова.' });
      }} /> : <div className={`layout auth-layout ${menu ? 'menu-open' : ''}`}>
      <aside className="sidebar" aria-label="Панель чатов" inert={appearance || (mobile && !menu) || undefined} aria-hidden={mobile && !menu || undefined} onKeyDown={(event) => {
        if (!mobile || !menu || event.key !== 'Tab') return;
        const controls = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
        <Brand />
        <button ref={sidebarClose} className="sidebar-close icon-button" aria-label="Закрыть список чатов" onClick={() => setMenu(false)}><Icon name="close" /></button>
        <button className="new-chat" disabled aria-describedby="chat-signin-hint"><Icon name="plus" /><span>Новый чат</span></button>
        <div className="search-box"><Icon name="search" /><input aria-label="Поиск чатов" placeholder="Поиск по чатам" disabled /></div>
        <div className="chat-list"><div className="group-label">ВАШИ ЧАТЫ</div><p className="sidebar-empty">Ваши разговоры будут здесь после входа.</p></div>
        <div className="sidebar-bottom"><p className="prototype-label">Прототип для хакатона<br /><span>{product.disclaimer}</span></p><div className="profile"><div className="avatar" aria-hidden="true">P</div><div className="profile-name">{product.name}<span>Личное пространство</span></div></div></div>
      </aside>
      <button className="scrim" aria-label="Закрыть меню" onClick={() => setMenu(false)} tabIndex={menu ? 0 : -1} />
      <main className="main" inert={appearance || (mobile && menu) || undefined}>
        <header className="header"><div className="header-left"><button ref={menuTrigger} className="menu-button icon-button" aria-label="Показать список чатов" aria-expanded={menu} onClick={() => setMenu(true)}><Icon name="panel" /></button><span className="model-button">{product.name}</span><span className="header-divider" /><span className="chat-title">Вход</span></div><div className="header-right"><button className="appearance-button" aria-label="Настройки оформления" onClick={() => setAppearance(true)}><Icon name="sun" /><span>Оформление</span></button></div></header>
        <div className="conversation auth-conversation">
          {session.kind === 'loading' && <section className="auth-card loading-card" role="status"><span className="auth-spinner" aria-hidden="true" /><h1>Проверяем сессию…</h1><p className="auth-description">Подключаемся к рабочему пространству.</p></section>}
          {session.kind === 'unavailable' && <section className="auth-card unavailable-card"><span className="auth-mark" aria-hidden="true"><Icon name="message" /></span><h1>Не удалось подключиться</h1><ErrorNotice error={session.error} action="session" /><button className="login-submit" onClick={() => { void initialize(); }}>Повторить подключение<Icon name="arrow" /></button></section>}
          {session.kind === 'signed-out' && <Login notice={session.notice} onLogin={(current) => { ++epoch.current; setSessionError(null); setSession({ kind: 'signed-in', user: current }); }} />}
        </div><p id="chat-signin-hint" className="visually-hidden">Войдите, чтобы открыть чаты.</p>
      </main>
    </div>}
    {appearance && <Appearance theme={theme} selectTheme={setTheme} close={() => setAppearance(false)} />}
  </>;
}

const container = document.getElementById('app');
if (!container) throw new Error('Application container missing');
createRoot(container).render(<App />);
