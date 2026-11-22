import { projectSourceError } from "./projectErrors";
import { useEffect, useId, useRef, useState } from 'react';
import { ApiError, isRecord, type AuthClient } from '../api/client';
import './diagram.css';

interface Box { id: string; text: string; x: number; y: number; width: number; height: number;
  kind: string; fill: string; stroke: string; color: string; dashed: boolean; font_size: number; radius?: number; bold?: boolean; image?: string | null }
interface Edge { id: string; source: string; target: string; points: number[][]; color: string;
  rule_id: string; start_arrow: string; end_arrow: string; dashed: boolean }
interface Page { id: string; title: string; width: number; height: number; boxes: Box[]; edges: Edge[] }
interface RuleRow { id: string; sender_side: string; senders: string[]; receiver: string;
  mode: string; protocol: string | null; destination_port: number | null; service: string; policy: string;
  two_way?: boolean; provisional?: boolean }
interface Scene { schema_version: number; background: string; pages: Page[]; rule_rows?: RuleRow[] }

function parse(value: unknown): Scene {
  const revision19 = isRecord(value) && value.profile === 'calculated-sinoniks-19';
  if (!isRecord(value) || value.schema_version !== 1 || value.background !== '#303E48'
    || !Array.isArray(value.pages) || !value.pages.length || value.pages.length > (revision19 ? 86 : 8)) throw new ApiError(502, 'invalid_response');
  if (value.rule_rows !== undefined && (!Array.isArray(value.rule_rows) || value.rule_rows.length > (revision19 ? 256 : 24)
    || !value.rule_rows.every(row => isRecord(row) && ['id', 'sender_side', 'receiver', 'mode', 'service', 'policy'].every(key => typeof row[key] === 'string')
      && (row.protocol == null || typeof row.protocol === 'string')
      && (row.destination_port == null || (typeof row.destination_port === 'number'
        && Number.isInteger(row.destination_port) && row.destination_port >= 1 && row.destination_port <= 65535))
      && (row.two_way === undefined || typeof row.two_way === 'boolean')
      && (row.provisional === undefined || typeof row.provisional === 'boolean')
      && Array.isArray(row.senders) && row.senders.every(sender => typeof sender === 'string')))) throw new ApiError(502, 'invalid_response');
  for (const page of value.pages) {
    if (!isRecord(page) || typeof page.id !== 'string' || typeof page.title !== 'string'
      || !Array.isArray(page.boxes) || !Array.isArray(page.edges) || page.boxes.length > 500 || page.edges.length > 500
      || ![page.width, page.height].every(n => typeof n === 'number' && Number.isFinite(n) && n > 0 && n <= 50000)) throw new ApiError(502, 'invalid_response');
    const color = (v: unknown): boolean => typeof v === 'string' && /^(#[0-9A-Fa-f]{6}|none)$/.test(v);
    for (const box of page.boxes) {
      if (!isRecord(box) || typeof box.id !== 'string' || typeof box.text !== 'string' || box.text.length > 40000
        || ![box.x, box.y, box.width, box.height, box.font_size].every(n => typeof n === 'number' && Number.isFinite(n))
        || ![box.fill, box.stroke, box.color].every(color)
        || (box.image != null && (typeof box.image !== 'string' || box.image.length > 4000 || !/^[A-Za-z0-9+/=]+$/.test(box.image)))) throw new ApiError(502, 'invalid_response');
    }
    for (const edge of page.edges) {
      if (!isRecord(edge) || typeof edge.id !== 'string' || !color(edge.color)
        || !Array.isArray(edge.points) || edge.points.length < 2 || edge.points.length > 20
        || !edge.points.every(p => Array.isArray(p) && p.length === 2 && p.every(n => typeof n === 'number' && Number.isFinite(n)))) throw new ApiError(502, 'invalid_response');
    }
  }
  return value as unknown as Scene;
}

export function DiagramViewer({ auth, chatId, revisionId, onExpired, onReady }: {
  auth: AuthClient; chatId: string; revisionId: string; onExpired: () => void; onReady?: () => void;
}) {
  const [scene, setScene] = useState<Scene | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [index, setIndex] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [fullscreen, setFullscreen] = useState(false);
  const frame = useRef<HTMLElement>(null);
  const uid = useId().replace(/:/g, '');
  const ready = useRef(onReady); ready.current = onReady;
  const expired = useRef(onExpired); expired.current = onExpired;
  const drag = useRef<{ x: number; y: number; px: number; py: number; scale: number } | null>(null);
  useEffect(() => {
    const changed = (): void => setFullscreen(document.fullscreenElement === frame.current);
    document.addEventListener('fullscreenchange', changed);
    return () => document.removeEventListener('fullscreenchange', changed);
  }, []);
  useEffect(() => {
    let live = true;
    setScene(null); setError('');
    void auth.request(`/chats/${chatId}/designs/${revisionId}/diagram`).then(raw => {
      if (!isRecord(raw) || raw.revision_id !== revisionId) throw new ApiError(502, 'invalid_response');
      const parsed = parse(raw.scene);
      if (live) { setScene(parsed); setIndex(0); setZoom(1); setPan({ x: 0, y: 0 }); }
    }).catch(failure => {
      if (!live) return;
      if (failure instanceof ApiError && failure.status === 401) expired.current();
      else setError(projectSourceError(failure) ?? (failure instanceof ApiError && failure.code === 'diagram_not_available'
        ? 'Архивная версия: просмотр недоступен. Сохранённый draw.io можно скачать.'
        : 'Не удалось загрузить схему.'));
    });
    return () => { live = false; };
  }, [auth, chatId, revisionId, retry]);
  useEffect(() => {
    if (!scene) return;
    let second = 0;
    const first = requestAnimationFrame(() => { second = requestAnimationFrame(() => ready.current?.()); });
    return () => { cancelAnimationFrame(first); cancelAnimationFrame(second); };
  }, [scene]);
  const page = scene?.pages[index];
  function fit(): void { setZoom(1); setPan({ x: 0, y: 0 }); }
  function scale(factor: number): void { setZoom(old => Math.max(1, Math.min(8, old * factor))); }
  function box(item: Box) {
    return <g key={item.id} data-node-id={item.id}><title>{item.text}</title>
      <rect x={item.x} y={item.y} width={item.width} height={item.height} rx={item.radius ?? 10}
        fill={item.fill} stroke={item.stroke} strokeWidth={2} strokeDasharray={item.dashed ? '8 6' : undefined} />
      {item.image && <image x={item.x} y={item.y} width={item.width} height={item.height} href={`data:image/svg+xml;base64,${item.image}`} />}
      <text fill={item.color} fontSize={item.font_size} fontWeight={item.bold ? 700 : 400} fontFamily="Open Sans, sans-serif">
        {item.text.split('\n').map((line, n) => <tspan key={n} x={item.x + 12} y={item.y + 12 + item.font_size + n * (item.font_size + 6)}>{line}</tspan>)}
      </text></g>;
  }
  if (!page || !scene) return <div className="diagram-status" role="status">{error || 'Загружаем схему…'}{error && <button type="button" onClick={() => setRetry(old => old + 1)}>Повторить загрузку</button>}</div>;
  const markers = [...new Set(page.edges.map(edge => edge.color))];
  const marker = (edge: Edge, start: boolean): string | undefined => {
    const kind = start ? edge.start_arrow : edge.end_arrow;
    return kind === 'none' ? undefined : `url(#${uid}-${kind}-${markers.indexOf(edge.color)})`;
  };
  const width = page.width / zoom, height = page.height / zoom;
  const view = { x: (page.width - width) / 2 + pan.x, y: (page.height - height) / 2 + pan.y };
  return <section ref={frame} className="diagram-viewer" aria-label="Просмотр схемы" onKeyDown={event => {
    if (event.key === 'Escape' && document.fullscreenElement === frame.current) event.stopPropagation();
  }}>
    <div className="diagram-toolbar"><button type="button" aria-label="Уменьшить схему" onClick={() => scale(1 / 1.4)} disabled={zoom === 1}>−</button>
      <span>{Math.round(zoom * 100)}%</span><button type="button" aria-label="Увеличить схему" onClick={() => scale(1.4)} disabled={zoom >= 8}>+</button>
      <button type="button" onClick={fit}>Вся схема</button>
      {document.fullscreenEnabled && <button type="button" onClick={() => {
        const action = fullscreen ? document.exitFullscreen() : frame.current?.requestFullscreen();
        void action?.catch(() => {});
      }}>{fullscreen ? 'Свернуть' : 'На весь экран'}</button>}
      <label>Лист <select value={index} onChange={event => { setIndex(Number(event.target.value)); fit(); }}>
        {scene.pages.map((item, n) => <option key={item.id} value={n}>{item.title}</option>)}
      </select></label></div>
    <svg role="img" aria-label={`Схема Синоникса · ${page.title}`} tabIndex={0}
      viewBox={`${view.x} ${view.y} ${width} ${height}`} style={{ background: scene.background }}
      onKeyDown={event => { const keys: Record<string, number[]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
        if (keys[event.key]) { event.preventDefault(); const [x, y] = keys[event.key]; setPan(old => ({ x: old.x + x * width / 10, y: old.y + y * height / 10 })); } }}
      onPointerDown={event => { if (event.button !== 0) return; const rect = event.currentTarget.getBoundingClientRect();
        drag.current = { x: event.clientX, y: event.clientY, px: pan.x, py: pan.y, scale: Math.min(rect.width / width, rect.height / height) }; event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={event => { const d = drag.current; if (d) setPan({ x: d.px - (event.clientX - d.x) / d.scale, y: d.py - (event.clientY - d.y) / d.scale }); }}
      onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
      <defs>{markers.flatMap((color, n) => ['block', 'open'].map(kind => <marker key={`${n}-${kind}`} id={`${uid}-${kind}-${n}`} viewBox="0 0 8 8" refX={8} refY={4} markerWidth={8} markerHeight={8} orient="auto-start-reverse">
        <path d={kind === 'block' ? 'M0,0 L8,4 L0,8 Z' : 'M0,0 L8,4 L0,8'} stroke={color} fill={kind === 'block' ? color : 'none'} />
      </marker>))}</defs>
      {page.boxes.filter(item => ['zone', 'appliance'].includes(item.kind)).map(box)}
      {page.edges.map(edge => <polyline key={edge.id} data-edge-id={edge.id} points={edge.points.map(p => p.join(',')).join(' ')} fill="none" stroke={edge.color} strokeWidth={2} strokeDasharray={edge.dashed ? '5 5' : undefined} markerStart={marker(edge, true)} markerEnd={marker(edge, false)}><title>{edge.rule_id}</title></polyline>)}
      {page.boxes.filter(item => !['zone', 'appliance'].includes(item.kind)).sort((a, b) => Number(a.kind === 'detail') - Number(b.kind === 'detail')).map(box)}
    </svg><p className="diagram-hint">Потяните схему для перемещения. Масштаб указан относительно полного вида. Текст элементов доступен при наведении.</p>
    {scene.rule_rows && scene.rule_rows.length > 0 && <div className="diagram-rule-table" role="region" aria-label="Правила сохранённой схемы" tabIndex={0}><table>
      <thead><tr><th>№</th><th>Отправители → получатель</th><th>Режим</th><th>Протокол / порт</th><th>Назначение и условия</th></tr></thead>
      <tbody>{scene.rule_rows.map((row, n) => {
        const arrow = row.two_way ? '↔' : '→';
        return <tr key={row.id}>
          <td title={row.id}>{n + 1}</td><td>{row.sender_side} {arrow} {row.sender_side === 'A' ? 'B' : 'A'}<br />{row.senders.join(', ')} {arrow} {row.receiver}</td>
          <td>{{ diode: 'Односторонний', transport: 'Сессия с ответом', filetransfer: 'Файлы' }[row.mode] ?? row.mode}</td>
          <td>{row.mode === 'filetransfer' ? (row.protocol ?? 'Метод не указан') : `${row.protocol ?? 'Не указан'} / ${row.destination_port ?? 'Не указан'}`}</td>
          <td>{row.service}{row.provisional === true && <small> · Предварительно</small>}<br />{row.policy}</td>
        </tr>;
      })}</tbody></table></div>}
  </section>;
}
