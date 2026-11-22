import { useEffect, useRef, useState } from 'react';
import { ApiError, isRecord, type AuthClient } from '../api/client';
import { readWebSource } from '../api/web-source';
import { Icon } from '../ui';

export interface AnswerCitation { evidence_id: string; quote: string; title?: string; location?: string; web_source?: unknown }

interface Fragment { title: string; excerpt: string; location: string | null; original_download_path: string | null }

function SourceFragment({ auth, runId, index, citation, onExpired }: {
  auth: AuthClient; runId: string; index: number; citation: AnswerCitation; onExpired: () => void;
}) {
  const [source, setSource] = useState<Fragment | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  function failed(failure: unknown): void {
    if (!live.current) return;
    if (failure instanceof ApiError && failure.status === 401) onExpired();
    else setError(failure instanceof ApiError && [403, 404, 409].includes(failure.status)
      ? 'Источник изменился или больше недоступен. Сохранённая цитата относится к прежнему ответу.'
      : 'Не удалось открыть источник. Повторите попытку.');
  }
  async function open(): Promise<void> {
    setBusy(true); setError(''); setSource(null);
    try {
      const raw = await auth.request(`/runs/${runId}/citations/${index}`);
      if (!isRecord(raw) || raw.evidence_id !== citation.evidence_id || typeof raw.title !== 'string'
        || typeof raw.excerpt !== 'string' || raw.excerpt.length > 1600
        || (raw.location !== null && typeof raw.location !== 'string')
        || (raw.original_download_path !== null && (typeof raw.original_download_path !== 'string'
          || !/^\/knowledge\/versions\/[0-9a-f-]{36}\/original\/download$/.test(raw.original_download_path)))) throw new ApiError(502, 'invalid_response');
      if (live.current) setSource(raw as unknown as Fragment);
    } catch (failure) { failed(failure); }
    finally { if (live.current) setBusy(false); }
  }
  useEffect(() => { void open(); }, []);
  async function download(): Promise<void> {
    if (!source?.original_download_path) return;
    setBusy(true); setError('');
    try {
      const file = await auth.download(source.original_download_path);
      if (!live.current) return;
      const url = URL.createObjectURL(file.blob), anchor = document.createElement('a');
      anchor.href = url; anchor.download = file.filename; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (failure) { setSource(null); failed(failure); }
    finally { if (live.current) setBusy(false); }
  }
  return <div className="source-fragment">
    <button type="button" disabled={busy} onClick={() => { void open(); }}>{busy ? 'Проверяем доступ…' : source ? 'Обновить фрагмент' : 'Открыть фрагмент'}</button>
    {error && <p role="alert">{error}</p>}
    {source && <section aria-label="Актуальный фрагмент источника"><strong>{source.title}</strong>
      {source.location && <p>{source.location}</p>}<pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', font: 'inherit' }}>{source.excerpt}</pre>
      {source.original_download_path ? <button type="button" disabled={busy} onClick={() => { void download(); }}>Скачать оригинал</button>
        : <p>Скачивание оригинала из этой карточки недоступно.</p>}
    </section>}
  </div>;
}

function Citation({ citation, index, auth, runId, onExpired }: {
  citation: AnswerCitation; index: number; auth: AuthClient; runId: string | null; onExpired: () => void;
}) {
  const [open, setOpen] = useState(false);
  const source = readWebSource(citation.web_source);
  return <details className="answer-source" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="source-chip"><Icon name="doc" /><span title={citation.title}>{(citation.title ?? `Источник ${index + 1}`).replace(/^Проверенная редакция\s*·\s*/, '').replace(/^Материалы партнёра\s*·\s*/, '')}</span></summary>
    <p className="source-context-label">Фрагмент источника</p><blockquote>{citation.quote}</blockquote>
    {(citation.location || source) && <div className="answer-source-origin">
      {citation.location && <span>{citation.location}</span>}
      {source && <><a href={source.url} target="_blank" rel="noopener noreferrer">Открыть страницу</a>
        <span>Снимок от <time dateTime={source.captured_on}>{source.captured_on.split('-').reverse().join('.')}</time></span></>}
    </div>}
    {open && runId && <SourceFragment auth={auth} runId={runId} index={index} citation={citation} onExpired={onExpired} />}
  </details>;
}

export function AnswerSources({ citations, auth, runId, onExpired }: {
  citations: AnswerCitation[]; auth: AuthClient; runId: string | null; onExpired: () => void;
}) {
  if (!citations.length) return null;
  return <div className="message-sources" aria-label="Источники ответа">
    {citations.map((citation, index) => <Citation key={`${runId}-${citation.evidence_id}-${index}`}
      citation={citation} index={index} auth={auth} runId={runId} onExpired={onExpired} />)}
  </div>;
}
