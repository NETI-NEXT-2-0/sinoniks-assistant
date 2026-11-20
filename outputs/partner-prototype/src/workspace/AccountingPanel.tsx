import { useEffect, useState } from 'react';
import { ApiError, AuthClient, isRecord } from '../api/client';

type Summary = { calls_count: number; models: string[]; input_tokens: number | null; output_tokens: number | null;
  input_cost_usd: string | null; output_cost_usd: string | null; estimated_cost_usd: string | null;
  provider_cost_usd: string | null; known_recorded_cost_usd: string; unknown_cost_calls: number };
type Ledger = { session_id: string; summary: Summary; accounting_complete: boolean; tariff_declaration_complete: boolean;
  legacy_untracked_turns: number; mean_full_elapsed_ms: number | null; mean_elapsed_ms: number | null; timing_complete: boolean;
  by_model: (Summary & { model_id: string })[];
  calls: { model_id: string; status: string; estimated_cost_usd: string | null }[] };

function parse(value: unknown): Ledger {
  if (!isRecord(value) || !isRecord(value.summary) || !Array.isArray(value.calls)
    || typeof value.summary.calls_count !== 'number' || !Array.isArray(value.summary.models)
    || !Array.isArray(value.by_model) || typeof value.accounting_complete !== 'boolean') throw new ApiError(502, 'invalid_response');
  return value as unknown as Ledger;
}
function money(value: string | null): string {
  if (value === null) return 'неизвестно';
  const number = Number(value);
  if (!Number.isFinite(number)) return 'неизвестно';
  return `$${number === 0 ? '0' : number < 0.000001 ? number.toExponential(4) : number.toFixed(8).replace(/0+$/, '').replace(/\.$/, '')}`;
}

export function AccountingPanel({ auth, chatId, revision, onExpired }: {
  auth: AuthClient; chatId: string; revision: number; onExpired: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<Ledger | null>(null);
  const [error, setError] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (!open) return;
    let live = true;
    const timer = window.setTimeout(() => {
      void auth.request(`/chats/${chatId}/accounting`).then(raw => {
        if (live) { setData(parse(raw)); setError(false); }
      }).catch(failure => {
        if (!live) return;
        if (failure instanceof ApiError && failure.status === 401) onExpired();
        else setError(true);
      });
    }, 300);
    return () => { live = false; clearTimeout(timer); };
  }, [auth, chatId, revision, open, refresh, onExpired]);
  async function download(): Promise<void> {
    try {
      const result = await auth.request(`/chats/${chatId}/accounting/export`);
      const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = `usage-${chatId}.json`; link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError(true);
    }
  }
  const totals = data?.summary;
  return <details className="session-accounting" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Расходы и время сессии</summary>
    {error && <p role="alert">Не удалось загрузить журнал. <button onClick={() => setRefresh(value => value + 1)}>Повторить</button></p>}
    {!data && !error && <p>Загружаем журнал…</p>}
    {data && totals && <>
      <dl><div><dt>Вызовы</dt><dd>{totals.calls_count}</dd></div>
        <div><dt>Токены: вход / выход</dt><dd>{totals.input_tokens ?? 'неизвестно'} / {totals.output_tokens ?? 'неизвестно'}</dd></div>
        <div><dt>Оценка: вход / выход</dt><dd>{money(totals.input_cost_usd)} / {money(totals.output_cost_usd)}</dd></div>
        <div><dt>Всего по снимку тарифов</dt><dd>{money(totals.estimated_cost_usd)}</dd></div>
        {totals.provider_cost_usd !== null && <div><dt>Списание по данным API</dt><dd>{money(totals.provider_cost_usd)}</dd></div>}
        <div><dt>Среднее время до полного результата</dt><dd>{data.mean_full_elapsed_ms === null ? 'полный замер неизвестен' : `${(data.mean_full_elapsed_ms / 1000).toFixed(2)} с`}</dd></div></dl>
      {!data.accounting_complete && <p>Учёт неполный: неизвестные расходы не считаются нулём. Известная часть: {money(totals.known_recorded_cost_usd)}. Старых шагов без журнала: {data.legacy_untracked_turns}.</p>}
      {!data.tariff_declaration_complete && <p>Конкурсный маршрут и снимок тарифов ещё не объявлены полностью.</p>}
      {totals.models.length > 0 && <p>Модели: {totals.models.join(', ')}</p>}
      {data.by_model.map(model => <p key={model.model_id}>{model.model_id}: {model.calls_count} вызовов · {model.input_tokens ?? '?'} / {model.output_tokens ?? '?'} токенов · {money(model.estimated_cost_usd)}</p>)}
      <button type="button" onClick={() => { void download(); }}>Скачать журнал JSON</button>
      <button type="button" onClick={() => setRefresh(value => value + 1)}>Обновить</button>
    </>}
  </details>;
}
