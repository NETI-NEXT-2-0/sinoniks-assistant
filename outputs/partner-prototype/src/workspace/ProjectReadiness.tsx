import { useId } from 'react';
import type { ReadinessItem, ReadinessReport } from '../api/readiness';
import './readiness.css';

function Items({ items }: { items: ReadinessItem[] }) {
  return <ul>{items.map((item, index) => <li key={`${item.path ?? item.code ?? ''}-${index}`}>
    <strong>{item.subject}</strong><span>{item.message}</span>
    {item.answered_unknown && <small>Ответ «не знаю» сохранён. Значение не подставлено автоматически.</small>}
    {item.source && <small>Основание: {item.source}</small>}
  </li>)}</ul>;
}

export function ProjectReadiness({ report, compact = false }: {
  report: ReadinessReport | null | undefined; compact?: boolean;
}) {
  const id = useId();
  if (!report) return compact ? null : <p className="readiness-legacy">
    Для этой сохранённой версии отчёт проверки не создавался.
    Сохраните новую версию, чтобы получить проверку готовности.
  </p>;
  const groups: { key: 'conflicts' | 'missing' | 'limitations' | 'recommendations' | 'assumptions'; title: string }[] = [
    { key: 'conflicts', title: 'Противоречия — нужно исправить' },
    { key: 'missing', title: 'Недостающие данные' },
    { key: 'limitations', title: 'Ограничения кейса и продукта' },
    { key: 'assumptions', title: 'Принятые допущения' },
    { key: 'recommendations', title: 'Необязательные рекомендации' },
  ];
  return <section className={`project-readiness readiness-${report.status}`} aria-labelledby={id}>
    <h4 id={id}>{report.title}</h4>
    <p>{report.summary}</p>
    {!compact && <ul className="readiness-checks">{report.checks.map(check => <li key={check.code}>
      <span>{check.status === 'passed' ? 'Проверено' : check.status === 'failed' ? 'Не совпадает' : 'Пока не подтверждено'}</span>
      {check.title}
    </li>)}</ul>}
    {groups.map(({ key, title }) => report[key].length > 0 && <details key={key}
      open={!compact && (key === 'conflicts' || key === 'missing')}>
      <summary>{title} <span>({report[key].length})</span></summary>
      {key === 'recommendations' && <p>Можно оставить текущие условия. Советы не меняют проект автоматически.</p>}
      <Items items={report[key]} />
    </details>)}
  </section>;
}
