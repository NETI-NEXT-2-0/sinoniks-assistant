import { useEffect, useState } from 'react';
import { ApiError, type AuthClient } from '../api/client';
import { corpusSummary, type CorpusSummary as Summary } from '../api/knowledge';

interface Props { auth: AuthClient; onExpired: () => void }
const labels: Record<keyof Summary, string> = {
  documents: 'Всего документов',
  published_documents: 'Опубликовано',
  searchable_documents: 'Документов в поиске',
  unique_source_urls: 'Уникальных веб-адресов в поиске',
  searchable_excerpts: 'Доступных выдержек',
};

export function CorpusSummary({ auth, onExpired }: Props) {
  const [refresh, setRefresh] = useState(0);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    void corpusSummary(auth).then((result) => {
      if (live) setSummary(result);
    }).catch((error: unknown) => {
      if (!live) return;
      if (error instanceof ApiError && error.status === 401) onExpired();
      else setFailed(true);
    });
    return () => { live = false; };
  }, [auth, onExpired, refresh]);

  return <section className="knowledge-summary" aria-labelledby="knowledge-summary-title">
    <div className="knowledge-summary-head">
      <div>
        <h3 id="knowledge-summary-title">Сводка базы знаний</h3>
        <p>Ваши документы и выдержки, доступные вам на момент обновления.</p>
      </div>
      <button className="knowledge-secondary" disabled={!summary && !failed} onClick={() => {
        setSummary(null);
        setFailed(false);
        setRefresh((value) => value + 1);
      }}>Обновить сводку</button>
    </div>
    {failed ? <p role="alert">Не удалось загрузить сводку.</p> : summary ? <dl>
      {(Object.entries(labels) as [keyof Summary, string][]).map(([key, label]) => <div key={key}>
        <dt>{label}</dt><dd>{summary[key].toLocaleString('ru-RU')}</dd>
      </div>)}
    </dl> : <p role="status">Загружаем сводку…</p>}
  </section>;
}
