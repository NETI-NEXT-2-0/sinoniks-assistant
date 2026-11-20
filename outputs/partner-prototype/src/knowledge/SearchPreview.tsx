import { useState, type FormEvent } from 'react';
import { ApiError, type AuthClient } from '../api/client';
import { searchKnowledge, type KnowledgeExcerpt } from '../api/knowledge';

interface Props { auth: AuthClient; onExpired: () => void }

export function SearchPreview({ auth, onExpired }: Props) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<KnowledgeExcerpt[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function search(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (busy || !query.trim()) return;
    setBusy(true);
    setResults(null);
    setError('');
    try {
      setResults(await searchKnowledge(auth, query.trim()));
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else if (failure instanceof ApiError && failure.code === 'search_scope_too_large') {
        setError('В доступных документах слишком много фрагментов для текущего поиска.');
      } else setError('Не удалось выполнить поиск. Повторите запрос.');
    } finally { setBusy(false); }
  }

  return <section id="knowledge-search" className="knowledge-search" aria-labelledby="knowledge-search-title">
    <h3 id="knowledge-search-title">Проверка поиска по базе знаний</h3>
    <p>Ищет в опубликованных материалах, доступных вашей учётной записи. Модель не вызывается.</p>
    <form onSubmit={(event) => { void search(event); }}>
      <label htmlFor="knowledge-search-query">Поисковый запрос</label>
      <div className="knowledge-search-input">
        <input id="knowledge-search-query" type="search" value={query} maxLength={2000} disabled={busy}
          onChange={(event) => { setQuery(event.target.value); setResults(null); setError(''); }} />
        <button className="knowledge-primary" disabled={busy || !query.trim()}>Найти выдержки</button>
      </div>
    </form>
    {busy && <p role="status">Ищем опубликованные выдержки…</p>}
    {error && <div className="knowledge-message error" role="alert">{error}</div>}
    {results?.length === 0 && <p role="status">Подходящих выдержек нет.</p>}
    {!!results?.length && <ul className="knowledge-search-results" aria-label="Найденные выдержки">
      {results.map(({ evidence_id, title, excerpt, web_source }) => <li key={evidence_id}>
        <strong>{title}</strong>
        <blockquote>{excerpt}</blockquote>
        {web_source && <p className="knowledge-snapshot">
          <a href={web_source.url} target="_blank" rel="noopener noreferrer">Открыть страницу</a>
          <span>Снимок от <time dateTime={web_source.captured_on}>{web_source.captured_on.split('-').reverse().join('.')}</time></span>
        </p>}
      </li>)}
    </ul>}
  </section>;
}
