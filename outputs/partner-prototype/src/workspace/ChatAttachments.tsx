import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ApiError, AuthClient, isRecord } from '../api/client';
import { Icon } from '../ui';
import { registerFileDrop } from './file-drop.mjs';

export interface ChatFile { id: string; name: string; status: 'pending' | 'ready' | 'failed'; size_bytes: number; error_code: string | null; warnings: string[] }
interface Props { auth: AuthClient; chatId: string; files: ChatFile[]; change: (files: ChatFile[]) => void; disabled: boolean; busy: (value: boolean) => void; onExpired: () => void; active: boolean; children: ReactNode }
const formats: Record<string, string> = { pdf: 'application/pdf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', txt: 'text/plain', md: 'text/markdown', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg' };
function parseFile(raw: unknown): ChatFile {
  if (!isRecord(raw) || typeof raw.id !== 'string' || typeof raw.name !== 'string' || !['pending', 'ready', 'failed'].includes(String(raw.status)) || typeof raw.size_bytes !== 'number' || !Array.isArray(raw.warnings)) throw new ApiError(502, 'invalid_response');
  return raw as unknown as ChatFile;
}
export function ChatAttachments({ auth, chatId, files, change, disabled, busy, onExpired, active, children }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const live = useRef(true);
  const current = useRef(files); current.current = files;
  const changeRef = useRef(change); changeRef.current = change;
  const [uploading, setUploading] = useState(false);
  const uploadLock = useRef(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState<{ file: File; key: string }[]>([]);
  const [dragging, setDragging] = useState(false);
  useEffect(() => { live.current = true; return () => { live.current = false; busy(false); }; }, [busy]);
  const pending = files.some(file => file.status === 'pending');
  useEffect(() => {
    if (!pending) return;
    let active = true, running = false;
    const poll = async (): Promise<void> => {
      if (running) return; running = true;
      try {
        const result = await auth.request(`/chats/${chatId}/attachments`);
        if (!active || !live.current) return;
        if (!isRecord(result) || !Array.isArray(result.items)) throw new ApiError(502, 'invalid_response');
        const updated = result.items.map(parseFile);
        changeRef.current(current.current.map(file => updated.find(item => item.id === file.id) ?? { ...file, status: 'failed' }));
      } catch (failure) { if (active && failure instanceof ApiError && failure.status === 401) onExpired(); }
      finally { running = false; }
    };
    const timer = window.setInterval(() => { void poll(); }, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [auth, chatId, pending, onExpired]);
  async function uploadMany(entries: { file: File; key: string }[]): Promise<void> {
    if (disabled || uploadLock.current) return;
    if (entries.length + current.current.length > 5) { setError('Можно прикрепить до пяти файлов на сообщение.'); return; }
    if (entries.some(({ file }) => !formats[file.name.split('.').at(-1)?.toLowerCase() ?? ''] || !file.size || file.size > 25 * 1024 * 1024)) { setError('Выберите PDF, DOCX, изображение, TXT или MD до 25 МиБ.'); return; }
    uploadLock.current = true; setUploading(true); busy(true); setError(''); setRetry([]);
    const failures: { file: File; key: string }[] = [];
    try {
      for (const entry of entries) {
        if (!live.current) break;
        try {
          const extension = entry.file.name.split('.').at(-1)!.toLowerCase();
          const value = parseFile(await auth.upload(`/chats/${chatId}/attachments?name=${encodeURIComponent(entry.file.name)}`, entry.file, formats[extension], 'POST', entry.key));
          if (!live.current) break;
          const next = [...current.current.filter(item => item.id !== value.id), value];
          current.current = next; changeRef.current(next);
        } catch (failure) {
          if (!live.current) break;
          failures.push(entry);
          if (failure instanceof ApiError && failure.status === 401) onExpired();
          setError(failure instanceof ApiError && failure.code === 'chat_file_limit' ? 'В чате уже 20 файлов. Создайте новый чат.' : 'Часть файлов не загружена. Повтор использует те же файлы и ключи.');
        }
      }
    } finally { uploadLock.current = false; if (live.current) { setRetry(failures); setUploading(false); busy(false); } }
  }
  const dropRef = useRef(uploadMany); dropRef.current = uploadMany;
  const available = useRef(false); available.current = !disabled && !uploading;
  useEffect(() => {
    if (!active) { setDragging(false); return; }
    return registerFileDrop(window, { onActive: setDragging, isAvailable: () => available.current,
      onFiles: files => { void dropRef.current(files.map(file => ({ file, key: crypto.randomUUID() }))); } });
  }, [active]);
  async function retryProcessing(file: ChatFile): Promise<void> {
    try {
      const value = parseFile(await auth.mutate(`/chats/${chatId}/attachments/${file.id}/retry`, 'POST'));
      if (live.current) changeRef.current(current.current.map(item => item.id === file.id ? value : item));
    } catch { if (live.current) setError('Не удалось повторить обработку файла.'); }
  }
  return <div className="chat-composer-content">
    {dragging && <div className="chat-drop-overlay" role="status"><div className="chat-drop-card"><span className="chat-drop-icon"><Icon name="attach" /></span><strong>{disabled || uploading ? 'Дождитесь готовности чата' : 'Отпустите файлы здесь'}</strong><p>Добавим их к сообщению · до 5 файлов по 25 МиБ</p><small>PDF, DOCX, TXT, MD, PNG и JPEG · доступны только вам</small></div></div>}
    <div className="chat-attachments">{files.map(file => <div className={`chat-file chat-file-${file.status}`} key={file.id}><Icon name="doc" /><span><strong>{file.name}</strong><small>{file.status === 'ready' ? 'Готов · только в этом чате' : file.status === 'failed' ? 'Не удалось прочитать файл' : file.error_code ? 'Повторяем обработку…' : 'Читаем документ…'}{file.warnings.length > 0 && ' · проверьте качество распознавания'}</small></span>{file.status === 'failed' && <button type="button" disabled={disabled} onClick={() => { void retryProcessing(file); }}>Повторить</button>}<button type="button" aria-label={`Убрать из сообщения ${file.name}`} disabled={disabled || uploading} onClick={() => change(files.filter(item => item.id !== file.id))}><Icon name="close" /></button></div>)}
    {error && <p className="chat-file-error" role="alert">{error}{retry.length > 0 && <button type="button" disabled={disabled || uploading} onClick={() => { void uploadMany(retry); }}>Повторить загрузку</button>}</p>}
    {files.length > 0 && <p className="chat-file-caption">До пяти файлов на сообщение. Вложения используются в этом чате и не публикуются в общей базе знаний.</p>}
    </div>
    <div className="chat-input-row">
      <input ref={input} className="visually-hidden" type="file" multiple accept=".pdf,.docx,.png,.jpg,.jpeg,.txt,.md" aria-label="Выбрать документы для чата" disabled={disabled || uploading || files.length >= 5} onChange={event => { const entries = Array.from(event.target.files ?? []).map(file => ({ file, key: crypto.randomUUID() })); event.target.value = ''; if (entries.length) void uploadMany(entries); }} />
      <button type="button" className={`chat-attach-button ${uploading ? 'is-uploading' : ''}`} aria-label={uploading ? 'Загружаем документы' : 'Прикрепить документ'} title="Прикрепить документ или перетащить файлы в чат" disabled={disabled || uploading || files.length >= 5} onClick={() => input.current?.click()}><Icon name="attach" /></button>
      {children}
    </div>
  </div>;
}
