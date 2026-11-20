import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, type AuthClient } from '../api/client';
import { getDesign, latestDesign, saveDesign, type DesignCommand, type DesignInput, type DesignRevision, type ExchangeInput } from '../api/designs';
import './design.css';

interface Props { auth: AuthClient; chatId: string; branchId: string; revisionId?: string | null; close: () => void; onExpired: () => void }
const empty: DesignInput = { schema_version: 2, network_a: null, network_b: null, firewall: null, exchanges: [] };
const modes = { transport: 'Сетевая сессия с ответными данными', diode: 'Односторонние данные', filetransfer: 'Передача файлов' };

function rehearsalExample(): DesignInput {
  const telemetry: ExchangeInput = {
    requirement_id: 'telemetry', sender_side: 'A', sender: 'Сборщик показаний', receiver: 'Архив',
    mode: 'diode', service: 'Показания датчиков', policy: 'Только телеметрия', protocol: 'UDP', destination_port: 9200,
  };
  return {
    schema_version: 2, network_a: 'Измерительный контур', network_b: 'Отчётный контур', firewall: 'B',
    exchanges: [
      telemetry,
      { ...telemetry, requirement_id: 'telemetry-repeat' },
      {
        requirement_id: 'status', sender_side: 'A', sender: 'Диагностика', receiver: 'Сервис статуса',
        mode: 'transport', service: 'Состояние оборудования', policy: 'Только чтение', protocol: 'TCP', destination_port: 443,
      },
      {
        requirement_id: 'archive-ack', sender_side: 'B', sender: 'Архив', receiver: 'Сборщик показаний',
        mode: 'diode', service: 'Квитанции архива', policy: 'Только квитанции', protocol: 'UDP', destination_port: 9300,
      },
      {
        requirement_id: 'reports', sender_side: 'A', sender: 'Выгрузка', receiver: null,
        mode: 'filetransfer', service: 'Отчёты', policy: 'Только PDF',
      },
    ],
  };
}

function failureText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'design_revision_conflict') return 'Проект уже изменён в другом окне. Обновите сохранённую версию.';
    if (error.status === 422) return 'Проверьте поля. Порт должен быть целым числом от 1 до 65535, названия — до 120 символов.';
    if (error.status === 404) return 'Чат или проект недоступен.';
  }
  return 'Сохранение не подтверждено. Повторите тот же запрос.';
}

export function DesignPanel({ auth, chatId, branchId, revisionId, close, onExpired }: Props) {
  const panel = useRef<HTMLElement>(null);
  const pending = useRef<{ body: DesignCommand; key: string } | null>(null);
  const [input, setInput] = useState<DesignInput>(empty);
  const [saved, setSaved] = useState<DesignRevision | null>(null);
  const [busy, setBusy] = useState(true);
  const [uncertain, setUncertain] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [historical, setHistorical] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const previous = document.activeElement;
    panel.current?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    let live = true;
    setBusy(true);
    void latestDesign(auth, chatId, branchId).then(async (latest) => {
      const revision = revisionId && reload === 0 && revisionId !== latest?.id
        ? await getDesign(auth, chatId, revisionId) : latest;
      if (!live) return;
      setSaved(revision); setInput(revision?.input ?? empty); setDirty(false);
      setLoaded(true);
      setHistorical(revision?.id !== latest?.id);
      pending.current = null; setUncertain(false); setError('');
    }).catch((failure: unknown) => {
      if (!live) return;
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError(failureText(failure));
    }).finally(() => { if (live) setBusy(false); });
    return () => { live = false; };
  }, [auth, chatId, branchId, onExpired, reload, revisionId]);

  function change(next: DesignInput): void { setInput(next); setDirty(true); setError(''); }
  function changeFlow(index: number, patch: Partial<ExchangeInput>): void {
    change({ ...input, exchanges: input.exchanges.map((item, position) => position === index ? { ...item, ...patch } : item) });
  }
  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (busy || !loaded || historical) return;
    pending.current ??= { body: { branch_id: branchId, parent_id: saved?.id ?? null, input }, key: crypto.randomUUID() };
    setBusy(true); setError('');
    try {
      const revision = await saveDesign(auth, chatId, pending.current.body, pending.current.key);
      setSaved(revision); setInput(revision.input); setDirty(false); setUncertain(false); pending.current = null;
    } catch (failure) {
      const unknown = !(failure instanceof ApiError) || failure.status === 0 || failure.status >= 500;
      setUncertain(unknown);
      if (!unknown) pending.current = null;
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError(failureText(failure));
    } finally { setBusy(false); }
  }
  async function download(extension: string): Promise<void> {
    if (!saved || busy) return;
    setBusy(true); setError('');
    try {
      const result = await auth.download(`/chats/${chatId}/designs/${saved.id}/download/${extension}`);
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement('a'); link.href = url;
      link.download = `sinoniks-v${saved.revision}.${extension}`;
      document.body.append(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError('Не удалось скачать сохранённый файл. Повторите попытку.');
    } finally { setBusy(false); }
  }
  async function previous(): Promise<void> {
    if (!saved?.parent_id || busy || dirty || uncertain) return;
    setBusy(true); setError('');
    try {
      const revision = await getDesign(auth, chatId, saved.parent_id);
      setSaved(revision); setInput(revision.input); setHistorical(true);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError('Не удалось открыть предыдущую версию.');
    } finally { setBusy(false); }
  }
  const disabled = busy || uncertain || !loaded || historical;
  const result = saved?.result;
  const names = new Map([...(result?.network_nodes ?? []), ...(result?.device_nodes ?? [])].map((node) => [node.id, node.name]));
  const path = result?.connections.length ? [result.connections[0].source, ...result.connections.map((edge) => edge.target)] : [];

  return <div className="design-overlay"><section ref={panel} className="design-panel" role="dialog" aria-modal="true" aria-labelledby="design-title" tabIndex={-1} onKeyDown={(event) => {
    if (event.key === 'Escape' && !busy) close();
    if (event.key !== 'Tab') return;
    const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')];
    const first = controls[0], last = controls.at(-1);
    if (document.activeElement === panel.current) { event.preventDefault(); (event.shiftKey ? last : first)?.focus(); }
    else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}>
    <header className="design-header"><div><h2 id="design-title">Проект Синоникса</h2><p>Условия и расчёты сохраняются в текущей ветке чата.</p></div><button disabled={busy} onClick={close}>К чату</button></header>
    <div className="design-content">
      {error && <div role="alert" className="design-error">{error}<button disabled={busy} onClick={() => {
        if (!dirty || window.confirm('Загрузить сохранённую версию и заменить текущие правки?')) setReload((value) => value + 1);
      }}>Обновить сохранённую версию</button></div>}
      <form onSubmit={(event) => { void submit(event); }}>
        {!disabled && !saved && !dirty && <div className="design-actions">
          <button type="button" onClick={() => change(rehearsalExample())}>Загрузить учебный пример</button>
        </div>}
        <fieldset disabled={disabled}><legend>Сети и размещение</legend><div className="design-fields">
          <label>Контур A<input maxLength={120} value={input.network_a ?? ''} onChange={(event) => change({ ...input, network_a: event.target.value || null })} /></label>
          <label>Контур B<input maxLength={120} value={input.network_b ?? ''} onChange={(event) => change({ ...input, network_b: event.target.value || null })} /></label>
          <label>Требуемое размещение МЭ<select value={input.firewall ?? ''} onChange={(event) => change({ ...input, firewall: (event.target.value || null) as DesignInput['firewall'] })}>
            <option value="">Уточнить</option><option value="A">На стороне A</option><option value="B">На стороне B</option><option value="both">На обеих сторонах</option>
          </select></label>
        </div></fieldset>
        {input.exchanges.map((flow, index) => <fieldset disabled={disabled} key={flow.requirement_id ?? index}>
          <legend>Обмен {index + 1}</legend><div className="design-fields">
            <label>Откуда начинается обмен<select value={flow.sender_side ?? ''} onChange={(event) => changeFlow(index, { sender_side: (event.target.value || null) as ExchangeInput['sender_side'] })}>
              <option value="">Уточнить</option><option value="A">Из A в B</option><option value="B">Из B в A</option>
            </select></label>
            <label>Отправитель<input maxLength={120} value={flow.sender ?? ''} onChange={(event) => changeFlow(index, { sender: event.target.value || null })} /></label>
            <label>Получатель<input maxLength={120} value={flow.receiver ?? ''} onChange={(event) => changeFlow(index, { receiver: event.target.value || null })} /></label>
            <label>Режим<select value={flow.mode ?? ''} onChange={(event) => changeFlow(index, { mode: (event.target.value || null) as ExchangeInput['mode'], protocol: null, destination_port: null })}>
              <option value="">Уточнить</option>{Object.entries(modes).map(([mode, label]) => <option key={mode} value={mode}>{label}</option>)}
            </select></label>
            <label>Сервис или категория файлов<input maxLength={120} value={flow.service ?? ''} onChange={(event) => changeFlow(index, { service: event.target.value || null })} /></label>
            <label>Ограничения обмена<input maxLength={120} value={flow.policy ?? ''} onChange={(event) => changeFlow(index, { policy: event.target.value || null })} /></label>
            {flow.mode !== 'filetransfer' && <><label>Протокол<select value={flow.protocol ?? ''} onChange={(event) => changeFlow(index, { protocol: (event.target.value || null) as ExchangeInput['protocol'] })}>
              <option value="">Уточнить</option><option>TCP</option><option>UDP</option>
            </select></label><label>Порт получателя<input type="number" min={1} max={65535} step={1} value={flow.destination_port ?? ''} onChange={(event) => changeFlow(index, { destination_port: event.target.value ? Number(event.target.value) : null })} /></label></>}
          </div><button type="button" onClick={() => change({ ...input, exchanges: input.exchanges.filter((_, position) => position !== index) })}>Удалить обмен {index + 1}</button>
        </fieldset>)}
        <div className="design-actions"><button type="button" disabled={disabled || input.exchanges.length >= 24} onClick={() => change({ ...input, exchanges: [...input.exchanges, {
          requirement_id: `req-${crypto.randomUUID()}`, sender_side: null, sender: null, receiver: null, mode: null, service: null, policy: null,
        }] })}>Добавить обмен</button><button type="submit" disabled={busy || !loaded || historical}>{busy ? 'Сохраняем…' : uncertain ? 'Повторить сохранение' : 'Сохранить и рассчитать'}</button></div>
      </form>
      {saved && <section className="design-result" aria-labelledby="design-result-title"><h3 id="design-result-title">Сохранённая версия {saved.revision}</h3>
        {saved.branch_id !== branchId && <p role="status">Исходная версия унаследована из предыдущей ветки. Изменения сохранятся в текущей ветке.</p>}
        <div className="design-actions">{saved.parent_id && <button disabled={busy || dirty || uncertain} onClick={() => { void previous(); }}>Предыдущая версия</button>}{historical && <button disabled={busy} onClick={() => setReload((value) => value + 1)}>К актуальной версии</button>}</div>
        {dirty && <p role="status">Есть новые правки. Результат ниже относится к сохранённой версии.</p>}
        {!!saved.questions.length && <><p>Для продолжения уточните условия.</p><ul>{saved.questions.map((question) => <li key={question.field}>{question.text}</li>)}</ul></>}
        {result && <><p>Концептуальная схема по заданным условиям.</p><ol className="design-path" aria-label="Размещение устройств">{path.map((id) => <li key={id}>{names.get(id)}</li>)}</ol>
          <p>Логических обменов — {result.counts.logical_exchanges}. Сетевых пар слотов — {result.counts.network_slot_pairs}. Настроек узлов — {result.counts.network_node_settings}. Файловых политик — {result.counts.file_transfer_policies}.</p>
          <ul className="design-warnings">{result.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
          <div className="design-table">
            <table>
              <thead><tr><th>Обмен</th><th>Режим</th><th>Условия</th></tr></thead>
              <tbody>{result.rules.map((rule) => (
                <tr key={rule.id}>
                  <td>
                    {rule.sender_side === 'A' ? 'A → B' : 'B → A'}<br />
                    {[rule.sender, ...(rule.additional_senders ?? [])].join(', ')} → {rule.receiver}
                  </td>
                  <td>{rule.mode && modes[rule.mode]}</td>
                  <td>{rule.service}<br />{rule.policy}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <details><summary>Обоснование и ограничения</summary><p className="design-explanation">{result.explanation}</p></details>
          <div className="design-actions">{['drawio', 'json', 'txt'].map((extension) => <button key={extension} disabled={busy} onClick={() => { void download(extension); }}>Скачать {extension === 'drawio' ? 'схему draw.io' : extension.toUpperCase()}</button>)}</div>
        </>}
      </section>}
    </div>
  </section></div>;
}
