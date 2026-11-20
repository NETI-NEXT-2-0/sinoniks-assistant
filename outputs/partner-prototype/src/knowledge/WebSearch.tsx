import { useEffect, useState, type FormEvent } from 'react';
import { ApiError, type AuthClient } from '../api/client';
import { officialDomains, searchOfficial, type OfficialSearch } from '../api/web';

interface Props { auth: AuthClient; onExpired: () => void }

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'web_public_terms_required') return 'Введите ключевые слова без адресов, ссылок и поисковых операторов.';
    if (error.status === 429) return 'Слишком много запросов. Подождите минуту и повторите поиск.';
    if (error.code === 'web_search_disabled') return 'Веб-поиск пока не настроен.';
  }
  return 'Веб-поиск сейчас недоступен. Повторите попытку позже.';
}

export function WebSearch({ auth, onExpired }: Props) {
  const [domains, setDomains] = useState<string[] | null>(null);
  const [domain, setDomain] = useState('');
  const [query, setQuery] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [result, setResult] = useState<OfficialSearch | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    void officialDomains(auth).then((items) => {
      if (live) { setDomains(items); setDomain(items[0] ?? ''); }
    }).catch((failure: unknown) => {
      if (!live) return;
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError(errorText(failure));
    });
    return () => { live = false; };
  }, [auth, onExpired]);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (busy || !domain || !query.trim() || !confirmed) return;
    setBusy(true);
    setResult(null);
    setError('');
    try { setResult(await searchOfficial(auth, domain, query.trim())); }
    catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError(errorText(failure));
    } finally { setBusy(false); }
  }

  return <section className="knowledge-search" aria-labelledby="official-search-title">
    <h3 id="official-search-title">Веб-поиск по официальным сайтам</h3>
    {domains === null && !error && <p role="status">Загружаем список доменов…</p>}
    {domains?.length === 0 && <p role="status">Администратор ещё не настроил поиск и список доменов.</p>}
    {!!domains?.length && <>
      <p>Поисковый сервис получит введённые ключевые слова. Используйте только публичную информацию.</p>
      <form className="knowledge-web-search-form" onSubmit={(event) => { void submit(event); }}>
        <label>Официальный домен
          <select value={domain} disabled={busy} onChange={(event) => {
            setDomain(event.target.value); setResult(null); setError('');
          }}>
            {domains.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>Публичные ключевые слова
          <input type="search" value={query} maxLength={200} disabled={busy} onChange={(event) => {
            setQuery(event.target.value); setConfirmed(false); setResult(null); setError('');
          }} />
        </label>
        <label className="knowledge-web-confirm">
          <input type="checkbox" checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />
          Подтверждаю, что запрос содержит только публичную информацию
        </label>
        <button className="knowledge-primary" disabled={busy || !confirmed || !query.trim()}>Искать на сайте</button>
      </form>
    </>}
    {busy && <p role="status">Ищем страницы…</p>}
    {error && <div className="knowledge-message error" role="alert">{error}</div>}
    {result?.partial && <p role="status">Часть поисковых источников сейчас недоступна.</p>}
    {result?.items.length === 0 && <p role="status">Подходящих страниц на этом домене не найдено.</p>}
    {!!result?.items.length && <ul className="knowledge-search-results" aria-label="Найденные страницы">
      {result.items.map(({ url, title, excerpt }) => <li key={url}>
        <a href={url} target="_blank" rel="noopener noreferrer"><strong>{title}</strong></a>
        <p className="knowledge-muted">{new URL(url).hostname}</p>
        {excerpt && <p>{excerpt}</p>}
      </li>)}
    </ul>}
  </section>;
}
