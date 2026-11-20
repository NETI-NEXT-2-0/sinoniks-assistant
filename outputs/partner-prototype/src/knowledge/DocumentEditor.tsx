import { Fragment, useRef, useState } from 'react';
import { sensitiveSpans } from './review-tools.mjs';

interface Props { original: string; value: string; disabled: boolean; onChange: (value: string) => void }
interface Snapshot { text: string; start: number; end: number }
function Highlight({ text }: { text: string }) {
  const parts = []; let offset = 0;
  for (const span of sensitiveSpans(text)) {
    parts.push(<Fragment key={`t-${offset}`}>{text.slice(offset, span.start)}</Fragment>);
    parts.push(<mark key={`m-${span.start}`}>{text.slice(span.start, span.end)}</mark>);
    offset = span.end;
  }
  parts.push(<Fragment key={`t-${offset}`}>{text.slice(offset)}</Fragment>);
  return <>{parts}</>;
}
export function DocumentEditor({ original, value, disabled, onChange }: Props) {
  const input = useRef<HTMLTextAreaElement>(null), backdrop = useRef<HTMLPreElement>(null);
  const past = useRef<Snapshot[]>([]), future = useRef<Snapshot[]>([]);
  const lastEdit = useRef(0);
  const [revision, setRevision] = useState(0), [selected, setSelected] = useState(false);
  const [notice, setNotice] = useState('');
  const hints = sensitiveSpans(value);
  function snapshot(): Snapshot { return { text: value, start: input.current?.selectionStart ?? 0, end: input.current?.selectionEnd ?? 0 }; }
  function save(): void {
    past.current.push(snapshot());
    let size = past.current.reduce((sum, item) => sum + item.text.length, 0);
    while (past.current.length > 30 || (size > 2_000_000 && past.current.length > 1)) size -= past.current.shift()!.text.length;
    future.current = []; setRevision(revision + 1);
  }
  function focus(start: number, end = start): void {
    requestAnimationFrame(() => { input.current?.focus(); input.current?.setSelectionRange(start, end); setSelected(start !== end); });
  }
  function replace(start: number, end: number, text: string): void {
    if (disabled) return;
    save(); lastEdit.current = 0; onChange(value.slice(0, start) + text + value.slice(end)); focus(start + text.length);
  }
  function travel(redo: boolean): void {
    if (disabled) return;
    const from = redo ? future : past, to = redo ? past : future;
    const next = from.current.pop(); if (!next) return;
    to.current.push(snapshot()); onChange(next.text); focus(next.start, next.end); lastEdit.current = 0; setRevision(revision + 1);
  }
  function removeSelection(): void {
    const area = input.current; if (area && area.selectionStart !== area.selectionEnd) replace(area.selectionStart, area.selectionEnd, '');
  }
  return <section className="document-editor" aria-label="Редактор проверенной копии">
    <div className="document-editor-toolbar" role="toolbar" aria-label="Действия с текстом">
      <button className="knowledge-secondary" disabled={disabled} onClick={() => focus(0, value.length)}>Выбрать весь текст</button>
      <button className="knowledge-secondary" disabled={disabled || !selected} onMouseDown={event => event.preventDefault()} onClick={removeSelection}>Удалить выделенное</button>
      <button className="knowledge-secondary" disabled={disabled || !past.current.length} onClick={() => travel(false)}>Отменить</button>
      <button className="knowledge-secondary" disabled={disabled || !future.current.length} onClick={() => travel(true)}>Повторить</button>
      <button className="knowledge-secondary" disabled={disabled || value === original} onClick={() => { replace(0, value.length, original); setNotice('Исходная распознанная копия восстановлена.'); }}>Восстановить текст</button>
    </div>
    <div className="document-editor-help">Редактируйте как обычный документ. Delete удаляет только выделенный текст. Ctrl+Z — отмена, Ctrl+Shift+Z — повтор.</div>
    <div className="document-copy-shell">
      <pre ref={backdrop} className="document-copy-highlights" aria-hidden="true"><Highlight text={value + '\n'} /></pre>
      <textarea ref={input} className="document-copy-input" aria-label="Текст документа для модерации" value={value} disabled={disabled} spellCheck={false} maxLength={1_000_000}
        onSelect={event => setSelected(event.currentTarget.selectionStart !== event.currentTarget.selectionEnd)}
        onScroll={event => { if (backdrop.current) { backdrop.current.scrollTop = event.currentTarget.scrollTop; backdrop.current.scrollLeft = event.currentTarget.scrollLeft; } }}
        onChange={event => { const now = Date.now(); if (now - lastEdit.current > 700) save(); lastEdit.current = now; onChange(event.target.value); }}
        onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && ['z', 'y'].includes(event.key.toLowerCase())) { event.preventDefault(); travel(event.shiftKey || event.key.toLowerCase() === 'y'); } }} />
    </div>
    <div className="document-editor-status" role="status"><span>{Array.from(value).length.toLocaleString('ru-RU')} символов · {value === original ? 'Исходная копия' : 'Есть изменения'}</span><span>{notice || 'В базу попадёт только этот текст. Оригинал остаётся отдельно.'}</span></div>
    {hints.length > 0 && <div className="document-copy-hints"><div><strong>Возможные чувствительные данные · {hints.length}</strong><button className="knowledge-secondary" disabled={disabled} onClick={() => { let text = value; for (const span of [...hints].reverse()) text = text.slice(0, span.start) + text.slice(span.end); replace(0, value.length, text); setNotice('Удалены только подсвеченные значения. Проверьте результат.'); }}>Удалить подсвеченные значения</button></div><p>Подсказки могут ошибаться. Если оставить значение в копии, оно будет добавлено в базу.</p>{hints.slice(0, 12).map(span => <button type="button" key={span.start} disabled={disabled} onClick={() => focus(span.start, span.end)}>{span.label} · показать в тексте</button>)}</div>}
  </section>;
}
