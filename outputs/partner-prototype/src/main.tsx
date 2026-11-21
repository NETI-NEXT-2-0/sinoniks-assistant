import { product } from './product';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, AuthClient, errorMessage, type User } from './api/client';
import { Icon, Brand } from './ui';
import { Workspace } from './workspace/Workspace';
import './style.css';
import './themes.css';
import './brand.css';
import './auth.css';
import { EntryPage } from './entry/EntryPage';
import './entry/entry.css';

type SessionState = { kind: 'loading' } | { kind: 'unavailable'; error: unknown }
  | { kind: 'signed-out'; notice?: string } | { kind: 'signed-in'; user: User };
document.title = `${product.name} — рабочее пространство`;
const auth = new AuthClient();
function ErrorNotice({ error, action }: { error: unknown; action: Parameters<typeof errorMessage>[1] }) {
  return <div className="auth-error" role="alert">
    <p>{errorMessage(error, action)}</p>
    {error instanceof ApiError && error.requestId && <span className="request-reference">Номер запроса: {error.requestId}</span>}
  </div>;
}

function EntryAuth({ notice, onLogin }: { notice?: string; onLogin: (user: User) => void }) {
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const heading = useRef<HTMLHeadingElement>(null);
  const confirmationInput = useRef<HTMLInputElement>(null);
  const registering = mode === 'register';

  function choose(next: typeof mode): void {
    if (pending) return;
    if (next !== mode) {
      setMode(next);
      setPassword('');
      setConfirmation('');
      setError(null);
    }
    heading.current?.focus({ preventScroll: true });
    heading.current?.closest('section')?.scrollIntoView({ block: 'center' });
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (pending) return;
    if (registering && password !== confirmation) {
      confirmationInput.current?.setCustomValidity('Пароли должны совпадать.');
      confirmationInput.current?.reportValidity();
      return;
    }
    setPending(true); setError(null);
    try {
      const user = await (registering ? auth.register(login, password) : auth.login(login, password));
      onLogin(user);
    } catch (failure) {
      setPassword(''); setConfirmation(''); setError(failure);
    } finally { setPending(false); }
  }

  return <EntryPage onRegister={auth.selfRegistrationEnabled ? () => choose('register') : undefined}
    onLogin={() => choose('login')} busy={pending}>
    <section className="auth-card" aria-labelledby="login-title" aria-busy={pending}>

    <h2 id="login-title" tabIndex={-1} ref={heading}>{registering ? 'Создайте аккаунт' : 'Войдите в команду'}</h2>
    <p className="auth-description">{registering ? 'Выберите логин без пробелов и пароль от 12 символов.' : 'Используйте учётную запись вашей команды.'}</p>
    {!registering && notice && !error && <p className="auth-notice" role="status">{notice}</p>}
    <form key={mode} className="login-form" aria-label={registering ? 'Регистрация' : 'Вход'} onSubmit={(event) => { void submit(event); }}>
      <label htmlFor="login">Логин</label>
      <input id="login" name="username" autoComplete="username" required maxLength={128} autoCapitalize="none" spellCheck={false} value={login} onChange={(event) => setLogin(event.target.value)} disabled={pending} />
      <label htmlFor="password">Пароль</label>
      <input id="password" name="password" type="password" autoComplete={registering ? 'new-password' : 'current-password'} required minLength={registering ? 12 : undefined} maxLength={1024} value={password} onChange={(event) => {
        setPassword(event.target.value);
        confirmationInput.current?.setCustomValidity('');
      }} disabled={pending} />
      {registering && <>
        <label htmlFor="confirmation">Повторите пароль</label>
        <input id="confirmation" name="confirmation" ref={confirmationInput} type="password" autoComplete="new-password" required maxLength={1024} value={confirmation} onChange={(event) => {
          event.target.setCustomValidity('');
          setConfirmation(event.target.value);
        }} disabled={pending} />
      </>}
      {error !== null && <ErrorNotice error={error} action={mode} />}
      <button className="login-submit" type="submit" disabled={pending}>
        {registering ? (pending ? 'Создаём аккаунт…' : 'Зарегистрироваться') : (pending ? 'Входим…' : 'Войти')}
      </button>
    </form>
    {registering ? <p className="account-help">Уже есть аккаунт? <button type="button" className="auth-switch" disabled={pending} onClick={() => choose('login')}>Войти</button></p>
      : auth.selfRegistrationEnabled
        ? <p className="account-help">Первый раз здесь? <button type="button" className="auth-switch" disabled={pending} onClick={() => choose('register')}>Создать аккаунт</button></p>
        : <p className="account-help">Если у вас нет доступа, обратитесь к администратору команды.</p>}
    </section>
  </EntryPage>;
}

function App() {
  const [session, setSession] = useState<SessionState>({ kind: 'loading' });
  const appearance = false;
  const [menu, setMenu] = useState(false);
  const [mobile, setMobile] = useState(() => matchMedia('(max-width:760px)').matches);
  const menuTrigger = useRef<HTMLButtonElement>(null);
  const sidebarClose = useRef<HTMLButtonElement>(null);
  const [logoutPending, setLogoutPending] = useState(false);
  const [sessionError, setSessionError] = useState<{ error: unknown; action: 'logout' | 'session' } | null>(null);
  const epoch = useRef(0);
  const refreshBusy = useRef(false);
  const logoutActive = useRef(false);
  const user = session.kind === 'signed-in' ? session.user : null;

  useEffect(() => { document.documentElement.dataset.theme = 'dark'; }, []);

  const initialize = useCallback(async (notice?: string): Promise<void> => {
    const generation = ++epoch.current;
    setSession({ kind: 'loading' }); setSessionError(null);
    try {
      let current: User | null = null;
      try { current = await auth.me(); }
      catch (failure) { if (!(failure instanceof ApiError) || failure.status !== 401) throw failure; }
      if (!current) await auth.csrf();
      if (epoch.current === generation) setSession(current ? { kind: 'signed-in', user: current } : { kind: 'signed-out', notice });
    } catch (failure) {
      if (epoch.current === generation) setSession({ kind: 'unavailable', error: failure });
    }
  }, []);
  useEffect(() => { void initialize(); }, [initialize]);

  const expireSession = useCallback((): void => {
    auth.reset();
    setMenu(false);
    void initialize('Сессия истекла. Войдите снова.');
  }, [initialize]);

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
          expireSession();
        } else setSessionError({ error: failure, action: 'session' });
      } finally { refreshBusy.current = false; }
    }
    const check = (): void => { void refresh(); };
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', check);
    return () => { window.removeEventListener('focus', check); document.removeEventListener('visibilitychange', check); };
  }, [user?.id, expireSession]);

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
        expireSession();
      } else setSessionError({ error: failure, action: 'logout' });
    } finally { logoutActive.current = false; setLogoutPending(false); }
  }

  return <>
    <a className="skip-link" href="#main-content">К основному содержимому</a>
    {user ? <Workspace key={user.id} user={user} auth={auth} appearance={appearance}
      openAppearance={() => { /* Dark theme is fixed for this version. */ }} logout={logout} logoutPending={logoutPending}
      sessionError={sessionError} onExpired={expireSession} /> : <div className={`layout auth-layout public-entry ${menu ? 'menu-open' : ''}`}>
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
      <main id="main-content" tabIndex={-1} className="main" inert={appearance || (mobile && menu) || undefined}>
        <header className="header"><div className="header-left"><Brand /><span className="entry-header-caption">Ассистент инженера</span></div></header>
        <div className="conversation auth-conversation">
          {session.kind === 'loading' && <EntryPage><section className="auth-card loading-card" role="status"><span className="auth-spinner" aria-hidden="true" /><h2>Проверяем сессию…</h2><p className="auth-description">Подключаемся к рабочему пространству.</p></section></EntryPage>}
          {session.kind === 'unavailable' && <EntryPage><section className="auth-card unavailable-card"><h2>Не удалось подключиться</h2><ErrorNotice error={session.error} action="session" /><button className="login-submit" onClick={() => { void initialize(); }}>Повторить подключение</button></section></EntryPage>}
          {session.kind === 'signed-out' && <EntryAuth notice={session.notice} onLogin={(current) => { ++epoch.current; setSessionError(null); setSession({ kind: 'signed-in', user: current }); }} />}
        </div><p id="chat-signin-hint" className="visually-hidden">Войдите, чтобы открыть чаты.</p>
      </main>
    </div>}
  </>;
}

const container = document.getElementById('app');
if (!container) throw new Error('Application container missing');
createRoot(container).render(<App />);
