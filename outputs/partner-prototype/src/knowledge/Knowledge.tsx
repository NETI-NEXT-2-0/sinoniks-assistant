import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, AuthClient } from '../api/client';
import { type WebSource } from '../api/web-source';
import { createDocument, document as getDocument, documents, publicationAclReview, publish, reviewPreview, unpublish, versions,
  type AclReview, type KnowledgeDocument, type KnowledgeVersion, type ReviewPage } from '../api/knowledge';
import { Icon } from '../ui';
import { CorpusSummary } from './CorpusSummary';
import { SearchPreview } from './SearchPreview';
import { WebSearch } from './WebSearch';
import { Access } from './Access';

import { DocumentEditor } from './DocumentEditor';
import './knowledge.css';

interface Props { auth: AuthClient; close: () => void; onExpired: () => void; openGroups: () => void }
const formats: Record<string, string> = {
  pdf: 'application/pdf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', txt: 'text/plain', md: 'text/markdown',
};
function message(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Не удалось выполнить действие. Попробуйте ещё раз.';
  if (error.code === 'upload_uncertain') return 'Приём не подтверждён. Повторите тот же запрос или проверьте список версий.';
  if (error.code === 'disclosure_context_too_large') return 'Фрагмент вместе с примечаниями слишком велик для текущей выдачи. Разделите исходник и загрузите новую версию.';
  if (error.code === 'disclosure_unit_too_large') return 'Фрагмент длиннее 1600 символов и не может быть показан целиком. Разделите исходник и загрузите новую версию.';
  if (error.code === 'deny_review_required') return 'В предыдущей версии есть индивидуальные запреты. Проверьте ограничения новой версии до публикации.';
  if (error.code === 'invalid_web_source') return 'Укажите HTTPS-адрес страницы и дату снимка.';
  if (error.code === 'source_checksum_mismatch') return 'Файл отличается от исходной версии. Выберите исходный файл.';
  if (error.code === 'canonical_review_required') return 'Нельзя согласовать эти фрагменты: проверьте предупреждения и связанные примечания.';
  if (error.code === 'canonical_digest_mismatch' || error.code === 'canonical_review_changed') return 'Документ изменился. Откройте проверку заново.';
  if (error.code === 'revision_conflict' || error.status === 409) return 'Состояние изменилось. Обновите документ и повторите действие.';
  if (error.status === 413) return 'Файл пустой или превышает 25 МиБ.';
  if (error.status === 415 || error.status === 422) return 'Проверьте формат файла или выбранные фрагменты.';
  if (error.status === 401) return 'Сессия истекла. Войдите снова.';
  return 'Не удалось подтвердить действие. Обновите данные и проверьте результат.';
}
function warning(code: string): string {
  if (code === 'png_dpi_assumed') return 'У PNG нет данных о разрешении. Для распознавания использовано 150 DPI. Сверьте таблицы с оригиналом.';
  if (code === 'native_text_order_uncertain') return 'Порядок текста в PDF может быть неверным. Согласование заблокировано до исправления источника.';
  if (code === 'element_locator_without_page_bbox') return 'Для некоторых фрагментов нет координат страницы.';
  return 'При обработке обнаружено предупреждение. Проверьте текст и примечания.';
}
function ingestionFailureHint(version: KnowledgeVersion): string {
  if (version.status === 'failed') return 'Лимит попыток исчерпан. Проверьте источник и сервис обработки, затем повторите.';
  const retryAt = version.text_retry_at ?? version.parser_retry_at;
  if (retryAt) {
    const time = new Date(retryAt);
    if (!Number.isNaN(time.getTime())) return `Следующая попытка после ${time.toLocaleString('ru-RU')}.`;
  }
  return 'Проверьте источник или повторите обработку.';
}
function indexFailureHint(version: KnowledgeVersion): string {
  if (version.index_attempts >= 5) return 'Лимит попыток исчерпан. Исправьте причину и повторите подготовку.';
  if (version.index_retry_at) {
    const time = new Date(version.index_retry_at);
    if (!Number.isNaN(time.getTime())) return `Следующая попытка после ${time.toLocaleString('ru-RU')}.`;
  }
  return 'Проверьте источник и состояние сервисов. При повреждении загрузите новую версию.';
}
function stateLabel(version: KnowledgeVersion): string {
  if (version.needs_reingest) return 'Требует повторной загрузки';
  if (version.index_error) return 'Не удалось подготовить поиск';
  if (version.review_available) return 'Ожидает проверки';
  if (version.status === 'pending') return version.error_code ? 'Обработка требует внимания' : 'Ожидает обработки';
  if (version.status === 'processing') return 'Подготавливается к публикации';
  if (version.status === 'failed') return 'Не удалось обработать';
  return 'Готова к публикации';
}

function SourceSnapshot({ source }: { source: WebSource }) {
  return <p className="knowledge-snapshot">
    <a href={source.url} target="_blank" rel="noopener noreferrer">{source.url}</a>
    <span>Снимок от <time dateTime={source.captured_on}>{source.captured_on.split('-').reverse().join('.')}</time></span>
  </p>;
}
export function Knowledge({ auth, close, onExpired, openGroups }: Props) {
  const panel = useRef<HTMLElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const aclPanel = useRef<HTMLElement>(null);
  const withdrawPanel = useRef<HTMLElement>(null);
  const [showAccess, setShowAccess] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [showWebSearch, setShowWebSearch] = useState(false);
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [docCursor, setDocCursor] = useState<string | null>(null);
  const [current, setCurrent] = useState<KnowledgeDocument | null>(null);
  const [items, setItems] = useState<KnowledgeVersion[]>([]);
  const [versionCursor, setVersionCursor] = useState<number | null>(null);
  const [page, setPage] = useState<ReviewPage | null>(null);
  const [reviewVersion, setReviewVersion] = useState<KnowledgeVersion | null>(null);
  const [reindexTarget, setReindexTarget] = useState<KnowledgeVersion | null>(null);
  const [aclReview, setAclReview] = useState<{ version: KnowledgeVersion; report: AclReview } | null>(null);
  const [aclConfirmed, setAclConfirmed] = useState(false);
  const [withdrawRevision, setWithdrawRevision] = useState<number | null>(null);
  const [approvalConfirmed, setApprovalConfirmed] = useState(false);
  const [syncError, setSyncError] = useState(false);
  const [reviewText, setReviewText] = useState('');
  const [title, setTitle] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState('');
  const [captureDate, setCaptureDate] = useState('');
  const [uploadKey, setUploadKey] = useState<string | null>(null);
  const [uploadUncertain, setUploadUncertain] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const generation = useRef(0);
  const currentId = useRef<string | null>(null);
  currentId.current = current?.id ?? null;

  const fail = useCallback((failure: unknown): void => {
    if (failure instanceof ApiError && failure.status === 401) onExpired();
    else setError(failure);
  }, [onExpired]);
  useEffect(() => {
    const previous = window.document.activeElement;
    panel.current?.querySelector<HTMLElement>('button')?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  useEffect(() => { if (aclReview) aclPanel.current?.focus(); }, [aclReview]);
  useEffect(() => { if (withdrawRevision !== null) withdrawPanel.current?.focus(); }, [withdrawRevision]);
  useEffect(() => {
    let live = true;
    void documents(auth).then((result) => {
      if (live) { setDocs(result.items); setDocCursor(result.next); }
    }).catch((failure: unknown) => { if (live) fail(failure); });
    return () => { live = false; ++generation.current; };
  }, [auth, fail]);
  async function refresh(id: string): Promise<void> {
    const token = ++generation.current;
    const [doc, list] = await Promise.all([getDocument(auth, id), versions(auth, id)]);
    if (token !== generation.current) return;
    setCurrent(doc); setItems(list.items); setVersionCursor(list.next);
    setDocs((old) => old.map((item) => item.id === doc.id ? doc : item));
  }
  function resetUpload(): void {
    setFile(null); setUploadKey(null); setUploadUncertain(false);
    setSourceUrl(''); setCaptureDate('');
    if (fileInput.current) fileInput.current.value = '';
  }
  async function openDoc(item: KnowledgeDocument): Promise<void> {
    setShowAccess(false); setWithdrawRevision(null); setBusy(true); setError(null); setNotice(''); setPage(null); setReviewVersion(null); setReindexTarget(null); setAclReview(null); setAclConfirmed(false); setReviewText(''); resetUpload();
    try { await refresh(item.id); } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function create(event: FormEvent): Promise<void> {
    event.preventDefault(); if (!title.trim() || busy) return;
    setShowAccess(false); setWithdrawRevision(null); setBusy(true); setError(null); setNotice('');
    try {
      const item = await createDocument(auth, title.trim());
      setDocs((old) => [item, ...old]); setTitle(''); setPage(null); setReviewVersion(null); setReindexTarget(null); setAclReview(null); setAclConfirmed(false); setReviewText('');
      resetUpload();
      await refresh(item.id);
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function upload(): Promise<void> {
    if (!current || !file || !uploadKey || busy) return;
    const extension = file.name.split('.').at(-1)?.toLowerCase() ?? '';
    if (!formats[extension] || file.size === 0 || file.size > 25 * 1024 * 1024) { setError(new ApiError(415, 'unsupported_media_type')); return; }
    setBusy(true); setError(null); setNotice('');
    try {
      const params = new URLSearchParams({ expected_revision: String(current.revision) });
      if (sourceUrl.trim() || captureDate) {
        params.set('source_url', sourceUrl.trim());
        params.set('source_captured_on', captureDate);
      }
      await auth.upload(`/knowledge/documents/${current.id}/versions?${params}`, file, formats[extension], 'POST', uploadKey);
      resetUpload();
      await refresh(current.id); setNotice('Версия загружена. Состояние обновится автоматически после обработки.');
    } catch (failure) {
      // Keep the same file and key: an explicit retry must identify the same admission.
      if (failure instanceof ApiError && (failure.status === 0 || failure.status >= 500)) {
        setUploadUncertain(true);
        fail(new ApiError(failure.status, 'upload_uncertain', failure.requestId, failure.retryable));
      } else fail(failure);
      try { await refresh(current.id); } catch { /* Preserve the original error. */ }
    } finally { setBusy(false); }
  }
  async function loadReview(version: KnowledgeVersion): Promise<void> {
    setBusy(true); setError(null); setNotice(''); setAclReview(null); setAclConfirmed(false); setApprovalConfirmed(false);
    try {
      const result = await reviewPreview(auth, version.id);
      setReviewText(result.blocks.map(block => block.text).join('\n\n'));
      setReviewVersion(version); setPage(result);
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function approve(): Promise<void> {
    if (!current || !reviewVersion || !page || !reviewText.trim() || !approvalConfirmed || page.next_offset !== null || busy) return;
    setBusy(true); setError(null); setNotice('');
    try {
      await auth.mutate(`/knowledge/versions/${reviewVersion.id}/approve`, 'POST', {
        expected_revision: current.revision, canonical_sha256: page.canonical_sha256, reviewed_text: reviewText,
      });
      setPage(null); setReviewVersion(null); setReviewText(''); await refresh(current.id);
      setNotice('Фрагменты согласованы. Дождитесь подготовки версии к публикации.');
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function publishVersion(version: KnowledgeVersion): Promise<void> {
    if (!current || busy) return;
    setBusy(true); setError(null); setNotice('');
    try { await publish(auth, version.id, current.revision); await refresh(current.id); setNotice('Версия опубликована.'); }
    catch (failure) {
      if (failure instanceof ApiError && failure.code === 'deny_review_required') {
        try {
          const report = await publicationAclReview(auth, current.id, version.id);
          setReindexTarget(null); setAclConfirmed(false); setAclReview({ version, report });
        } catch (reviewFailure) { fail(reviewFailure); }
      } else fail(failure);
    } finally { setBusy(false); }
  }
  async function publishAfterReview(): Promise<void> {
    if (!current || !aclReview || !aclConfirmed || aclReview.report.incomplete || busy) return;
    setBusy(true); setError(null); setNotice('');
    try {
      await publish(auth, aclReview.version.id, aclReview.report.revision, true);
      setAclReview(null); setAclConfirmed(false); await refresh(current.id);
      setNotice('Версия опубликована после проверки ограничений.');
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function withdraw(): Promise<void> {
    if (!current || withdrawRevision === null || busy) return;
    setBusy(true); setError(null); setNotice('');
    try {
      await unpublish(auth, current.id, withdrawRevision);
      setWithdrawRevision(null); await refresh(current.id);
      setNotice('Публикация снята. Документ больше не участвует в поиске.');
    } catch (failure) {
      setWithdrawRevision(null); fail(failure);
      try { await refresh(current.id); } catch { /* Preserve the original error. */ }
    } finally { setBusy(false); }
  }
  async function retryIndex(version: KnowledgeVersion): Promise<void> {
    if (!current || busy) return;
    setBusy(true); setError(null); setNotice('');
    try {
      await auth.mutate(`/knowledge/versions/${version.id}/retry-index`, 'POST', { expected_revision: current.revision });
      await refresh(current.id); setNotice('Подготовка поиска запущена повторно.');
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function rebuildIndex(): Promise<void> {
    if (!current || !reindexTarget || busy) return;
    setBusy(true); setError(null); setNotice('');
    try {
      await auth.mutate(`/knowledge/versions/${reindexTarget.id}/reindex`, 'POST', { expected_revision: current.revision });
      setReindexTarget(null); await refresh(current.id);
      setNotice('Поиск перестраивается. Готовность обновится автоматически.');
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function downloadOriginal(version: KnowledgeVersion): Promise<void> {
    if (busy) return;
    setBusy(true); setError(null); setNotice('');
    try {
      const { blob, filename } = await auth.download(`/knowledge/versions/${version.id}/original/download`);
      const url = URL.createObjectURL(blob);
      const link = window.document.createElement('a');
      link.href = url; link.download = filename;
      try { window.document.body.appendChild(link); link.click(); }
      finally { link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 30_000); }
      setNotice('Оригинал подготовлен к скачиванию.');
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function restoreOriginal(version: KnowledgeVersion, source: File): Promise<void> {
    if (!current || busy) return;
    const extension = source.name.split('.').at(-1)?.toLowerCase() ?? '';
    if (!formats[extension] || source.size === 0 || source.size > 25 * 1024 * 1024) {
      setError(new ApiError(415, 'unsupported_media_type')); return;
    }
    setBusy(true); setError(null); setNotice('');
    try {
      await auth.upload(`/knowledge/versions/${version.id}/original?expected_revision=${current.revision}`, source, formats[extension], 'PUT');
      setNotice('Оригинал восстановлен. Повторите обработку версии.');
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function retryVersion(version: KnowledgeVersion): Promise<void> {
    if (!current || busy) return;
    setBusy(true); setError(null); setNotice('');
    try {
      await auth.mutate(`/knowledge/versions/${version.id}/retry`, 'POST', { expected_revision: current.revision });
      await refresh(current.id); setNotice('Версия снова ожидает обработки.');
    } catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function moreDocs(): Promise<void> {
    if (!docCursor || busy) return; setBusy(true); setError(null);
    try { const result = await documents(auth, docCursor); setDocs((old) => [...old, ...result.items]); setDocCursor(result.next); }
    catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  async function moreVersions(): Promise<void> {
    if (!current || versionCursor === null || busy) return; setBusy(true); setError(null);
    try { const result = await versions(auth, current.id, versionCursor); setItems((old) => [...old, ...result.items]); setVersionCursor(result.next); }
    catch (failure) { fail(failure); } finally { setBusy(false); }
  }
  useEffect(() => {
    if (!current || busy || page || showAccess || aclReview || withdrawRevision !== null || reindexTarget) return;
    let live = true;
    let pending = false;
    const id = current.id;
    const update = async (): Promise<void> => {
      if (pending || document.visibilityState === 'hidden') return;
      pending = true;
      const token = generation.current;
      try {
        const [doc, list] = await Promise.all([getDocument(auth, id), versions(auth, id)]);
        if (!live || token !== generation.current || currentId.current !== id) return;
        setCurrent(doc);
        setItems(old => [...list.items, ...old.filter(item => !list.items.some(next => next.id === item.id))]);
        setDocs(old => old.map(item => item.id === id ? doc : item));
        setSyncError(false);
      } catch (failure) {
        if (live && token === generation.current) {
          setSyncError(true);
          if (failure instanceof ApiError && failure.status === 401) fail(failure);
        }
      } finally { pending = false; }
    };
    const timer = window.setInterval(() => { void update(); }, 2000);
    const visible = (): void => { void update(); };
    document.addEventListener('visibilitychange', visible);
    return () => { live = false; window.clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [auth, current?.id, busy, page, showAccess, aclReview, withdrawRevision, reindexTarget, fail]);
  const accessGeneration = generation.current;
  return <div className="knowledge-page"><section ref={panel} className="knowledge-panel" aria-labelledby="knowledge-title">
    <header className="knowledge-header"><div><span className="knowledge-kicker">РАБОЧИЕ МАТЕРИАЛЫ</span><h2 id="knowledge-title">Документы и база знаний</h2>{busy && <span className="knowledge-busy" role="status">Выполняем…</span>}</div><nav className="knowledge-top-nav" aria-label="Разделы управления">
      <button className="knowledge-secondary" disabled={busy} aria-expanded={showSearch} onClick={() => setShowSearch(!showSearch)}>Проверить поиск</button>
      <button className="knowledge-secondary" disabled={busy} aria-expanded={showWebSearch} onClick={() => setShowWebSearch(!showWebSearch)}>Веб-поиск</button>
      <button className="knowledge-secondary" disabled={busy} aria-expanded={showSummary} onClick={() => setShowSummary(!showSummary)}>Сводка</button>
      <button className="knowledge-secondary" disabled={busy} onClick={openGroups}>Пространства и группы</button><button className="knowledge-secondary" disabled={busy} onClick={close}>← К чатам</button></nav></header>
    <div className="knowledge-layout"><aside className="knowledge-rail" aria-label="Документы"><div className="knowledge-section-title">Документы <span>{docs.length}</span></div>
      <form className="knowledge-create" onSubmit={(event) => { void create(event); }}><label className="visually-hidden" htmlFor="knowledge-title-input">Название нового документа</label><input id="knowledge-title-input" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={200} placeholder="Название документа" disabled={busy} /><button type="submit" aria-label="Создать документ" disabled={busy || !title.trim()}><Icon name="plus" /></button></form>
      <div className="knowledge-doc-list">{docs.map((item) => <button key={item.id} className={`knowledge-doc ${current?.id === item.id ? 'selected' : ''}`} disabled={busy} onClick={() => { void openDoc(item); }}><Icon name="doc" /><span>{item.title}</span></button>)}{!docs.length && <p className="knowledge-empty-rail">Создайте документ, чтобы добавить первую версию.</p>}{docCursor && <button className="knowledge-link" disabled={busy} onClick={() => { void moreDocs(); }}>Показать ещё</button>}</div>
    </aside><main className="knowledge-content">{error !== null && <div className="knowledge-message error" role="alert">{message(error)}</div>}{notice && <div className="knowledge-message" role="status">{notice}</div>}
      {showSearch && <SearchPreview auth={auth} onExpired={onExpired} />}
      {showWebSearch && <WebSearch auth={auth} onExpired={onExpired} />}
      {showSummary && <CorpusSummary auth={auth} onExpired={onExpired} />}
      {!current ? <div className="knowledge-empty"><span><Icon name="doc" /></span><h3>Материалы команды</h3><p>Выберите документ или создайте новый. Перед публикацией просмотрите текст и исключите фрагменты, которые не должны попадать в ответы.</p></div> : <>
        <div className="knowledge-document-head"><div><span className="knowledge-kicker">ДОКУМЕНТ</span><h3>{current.title}</h3><p className="knowledge-sync" role="status">{syncError ? 'Не удалось обновить статус. Повторяем подключение…' : 'Статусы обновляются автоматически'}</p></div><button className="knowledge-secondary" disabled={busy} onClick={() => { setBusy(true); setError(null); setAclReview(null); setAclConfirmed(false); setWithdrawRevision(null); void refresh(current.id).catch(fail).finally(() => setBusy(false)); }}>Обновить</button></div>
        {showAccess ? <Access key={current.id} auth={auth} document={current} versions={items} refresh={() => currentId.current === current.id && generation.current === accessGeneration ? refresh(current.id) : Promise.resolve()} close={() => setShowAccess(false)} onExpired={onExpired} /> : !page ? <><div className="knowledge-access-entry"><div><strong>Доступ участников</strong><p>Разрешения на ответы и оригинал, ограничения отдельных выдержек.</p></div><button className="knowledge-secondary" disabled={busy} onClick={() => { setAclReview(null); setAclConfirmed(false); setReindexTarget(null); setWithdrawRevision(null); setShowAccess(true); }}>Настроить доступ</button></div><div className="knowledge-upload"><div><strong>Новая версия</strong><p>PDF, DOCX, PNG, JPEG, TXT или Markdown · до 25 МиБ</p></div><label className="knowledge-file">Выбрать файл<input ref={fileInput} type="file" accept=".pdf,.docx,.png,.jpg,.jpeg,.txt,.md" disabled={busy} onChange={(event) => { const next = event.target.files?.[0] ?? null; setFile(next); setUploadKey(next ? crypto.randomUUID() : null); setUploadUncertain(false); }} /></label><span className="knowledge-file-name">{file?.name ?? 'Файл не выбран'}</span><button className="knowledge-primary" disabled={busy || !file || !uploadKey || (!!sourceUrl.trim() !== !!captureDate)} onClick={() => { void upload(); }}>{uploadUncertain ? 'Повторить запрос' : 'Загрузить'}</button></div>
          <details className="knowledge-web-source">
            <summary>Источник в интернете</summary>
            <p>Для сохранённой страницы или публичного файла укажите адрес и дату снимка.</p>
            <div className="knowledge-web-fields">
              <label>HTTPS-адрес<input type="url" value={sourceUrl} maxLength={2048} placeholder="https://example.org/page"
                disabled={busy || uploadUncertain} onChange={(event) => setSourceUrl(event.target.value)} /></label>
              <label>Дата снимка<input type="date" value={captureDate} disabled={busy || uploadUncertain}
                onChange={(event) => setCaptureDate(event.target.value)} /></label>
            </div>
          </details>
          {current.active_version_id && <div className="knowledge-publication"><div><strong>Документ опубликован</strong><p>Снятие публикации сразу исключит версию из поиска.</p></div><button className="knowledge-secondary" disabled={busy || withdrawRevision !== null} onClick={() => { setAclReview(null); setAclConfirmed(false); setReindexTarget(null); setWithdrawRevision(current.revision); }}>Снять с публикации</button></div>}
          {withdrawRevision !== null && <section ref={withdrawPanel} tabIndex={-1} className="knowledge-reindex-confirm knowledge-withdraw-confirm" aria-labelledby="withdraw-title"><div><strong id="withdraw-title">Снять публикацию?</strong><p>Участники больше не смогут получать выдержки и скачивать опубликованный оригинал. Уже скачанные файлы останутся у получателей.</p></div><div><button className="knowledge-secondary" disabled={busy} onClick={() => setWithdrawRevision(null)}>Отмена</button><button className="knowledge-primary" disabled={busy} onClick={() => { void withdraw(); }}>Снять публикацию</button></div></section>}
          <div className="knowledge-versions-head"><h4>Версии</h4><span>Новые сверху</span></div>{aclReview && <section ref={aclPanel} tabIndex={-1} className="knowledge-acl-review" aria-labelledby="acl-review-title"><h4 id="acl-review-title">Проверьте ограничения перед публикацией</h4><p>Ограничения не переносятся автоматически. Сравните пользователей и фрагменты, при необходимости настройте запреты новой версии и обновите данные.</p><p>Запрет относится к ответам. Отдельное разрешение на оригинал открывает документ целиком.</p><div className="knowledge-acl-columns">{([['Прежняя версия', aclReview.report.previous], ['Новая версия', aclReview.report.target]] as const).map(([heading, entries]) => <div key={heading}><strong>{heading}</strong>{entries.length ? entries.map((entry) => <article key={`${entry.subject_type}:${entry.unit_id}:${entry.user_id}`}><span>{entry.subject_type === 'group' ? 'Группа: ' : 'Пользователь: '}{entry.login} · {entry.user_id}</span><p>{entry.excerpt}</p></article>) : <p>Индивидуальных запретов нет.</p>}</div>)}</div>{aclReview.report.incomplete && <p className="knowledge-acl-incomplete">Список или фрагменты неполные. Проверьте ограничения через API или с администратором.</p>}<label className="knowledge-acl-confirm"><input type="checkbox" checked={aclConfirmed} disabled={busy || aclReview.report.incomplete} onChange={(event) => setAclConfirmed(event.target.checked)} />Я проверил ограничения новой версии и подтверждаю публикацию.</label><div className="knowledge-acl-actions"><button className="knowledge-secondary" disabled={busy} onClick={() => { setAclReview(null); setAclConfirmed(false); }}>Отмена</button><button className="knowledge-primary" disabled={busy || !aclConfirmed || aclReview.report.incomplete} onClick={() => { void publishAfterReview(); }}>Опубликовать</button></div></section>}{reindexTarget && <div className="knowledge-reindex-confirm" role="status"><p>Перестроить поиск для версии {reindexTarget.generation}? Пока идёт перестройка, версия не участвует в поиске.</p><div><button className="knowledge-secondary" disabled={busy} onClick={() => setReindexTarget(null)}>Отмена</button><button className="knowledge-primary" disabled={busy} onClick={() => { void rebuildIndex(); }}>Перестроить</button></div></div>}{items.length === 0 ? <p className="knowledge-muted">Версий пока нет.</p> : <div className="knowledge-versions">{items.map((version) => <div className="knowledge-version" key={version.id}><div><strong>Версия {version.generation}</strong><span className={`knowledge-state ${version.status === 'failed' ? 'failed' : ''}`}>{current.active_version_id === version.id && version.status === 'processing' && !version.index_error ? 'Перестраивается поиск' : current.active_version_id === version.id && version.status === 'ready_for_review' && !version.needs_reingest ? 'Опубликована' : stateLabel(version)}</span>{version.web_source && <SourceSnapshot source={version.web_source} />}{version.error_code && <small>{ingestionFailureHint(version)}</small>}{version.index_error && <small>{indexFailureHint(version)}</small>}{version.needs_reingest && <small>Загрузите и согласуйте новую версию, чтобы сохранить связанные примечания в выдаче.</small>}{version.oversized_units && <small>Длинные выдержки исключены из поиска. Разделите исходник и загрузите новую версию.</small>}{current.active_version_id === version.id && version.status === 'processing' && <small>Версия временно не участвует в поиске.</small>}</div><div className="knowledge-version-actions">{((version.status === 'pending' && !version.review_available) || version.status === 'failed') && <label className="knowledge-file">Восстановить файл<input type="file" accept=".pdf,.docx,.png,.jpg,.jpeg,.txt,.md" disabled={busy} onChange={(event) => { const source = event.target.files?.[0]; event.target.value = ''; if (source) void restoreOriginal(version, source); }} /></label>}{version.error_code && <button className="knowledge-secondary" disabled={busy} onClick={() => { void retryVersion(version); }}>Повторить</button>}{version.index_error && <button className="knowledge-secondary" disabled={busy} onClick={() => { void retryIndex(version); }}>Повторить подготовку</button>}{version.review_available && <button className="knowledge-secondary" disabled={busy} onClick={() => { void loadReview(version); }}>Проверить</button>}{version.status === 'ready_for_review' && !version.needs_reingest && <button className="knowledge-secondary" disabled={busy} onClick={() => { setAclReview(null); setAclConfirmed(false); setReindexTarget(version); }}>Перестроить поиск</button>}{version.status === 'ready_for_review' && !version.needs_reingest && !version.oversized_units && version.generation === items[0]?.generation && current.active_version_id !== version.id && <button className="knowledge-primary" disabled={busy} onClick={() => { void publishVersion(version); }}>Опубликовать</button>}</div></div>)}</div>}{versionCursor !== null && <button className="knowledge-link" disabled={busy} onClick={() => { void moreVersions(); }}>Показать ещё версии</button>}</> : <section className="knowledge-review"><div className="knowledge-review-heading"><div><button className="knowledge-back" disabled={busy} onClick={() => { setPage(null); setReviewVersion(null); setReviewText(''); }}>← К версиям</button><h4>Проверка версии {reviewVersion?.generation}</h4><p>Предпросмотр распознанного документа. Работайте с текстовой копией: удаляйте выделенные слова, исправляйте и добавляйте текст. В базу попадёт только проверенная копия. Оригинал файла сохраняется отдельно.</p>{reviewVersion?.web_source && <SourceSnapshot source={reviewVersion.web_source} />}{reviewVersion && <button className="knowledge-secondary" disabled={busy} onClick={() => { void downloadOriginal(reviewVersion); }}>Скачать оригинал для сверки</button>}</div><span className="knowledge-count">Символов: {Array.from(reviewText).length}</span></div>
          {(page.warnings.length > 0 || page.quality_block || page.unresolved_notes || page.required_notes.length > 0) && <div className="knowledge-warnings" role="status">
            {page.warnings.map((item) => <p key={item}>{warning(item)}</p>)}
            {page.quality_block === 'unlinked_footnote_review_required' && <p>Не удалось надёжно связать примечание с текстом. Исправьте источник перед согласованием.</p>}
            {page.quality_block && !['unlinked_footnote_review_required', 'manual_quality_review_required'].includes(page.quality_block) && <p>Качество документа требует проверки перед согласованием.</p>}
            {page.unresolved_notes && <p>Не удалось найти обязательное примечание. Исправьте источник перед согласованием.</p>}
            {page.required_notes.length > 0 && <p>Обязательные примечания: фрагменты {page.required_notes.map((number) => number + 1).join(', ')}. Включите их в редакторе.</p>}
          </div>}
          <div className="knowledge-review-hint"><strong>Подсвечены возможные чувствительные данные</strong><p>Подсказки помогают найти возможные чувствительные сведения. Удаляйте только нужные символы или исправляйте текст прямо в копии.</p><details><summary>О проверке и хранении оригинала</summary><p>Контакты, реквизиты и ключи доступа определяются локальными правилами. Проверка может ошибаться и не находит все чувствительные сведения. Редактирование копии не изменяет оригинал; право на его скачивание открывает файл целиком.</p></details></div>
          {page.next_offset !== null && <div className="knowledge-warnings" role="alert">В документе больше 1000 фрагментов. Показаны первые 1000 из {page.total_blocks}. Разделите исходник на несколько файлов перед согласованием.</div>}
          <DocumentEditor key={reviewVersion?.id} original={page.blocks.map(block => block.text).join('\n\n')} value={reviewText} disabled={busy} onChange={next => { setReviewText(next); setApprovalConfirmed(false); }} />
          <div className="knowledge-review-footer"><label className="knowledge-approval-confirm"><input type="checkbox" checked={approvalConfirmed} disabled={busy || page.next_offset !== null} onChange={event => setApprovalConfirmed(event.target.checked)} />Я проверил редакцию текста и подтверждаю её добавление в базу знаний. Цитаты будут взяты из этой копии.</label><button className="knowledge-primary" disabled={busy || !reviewText.trim() || !approvalConfirmed || page.next_offset !== null || !!page.quality_block || page.unresolved_notes} onClick={() => { void approve(); }}>Подтвердить и подготовить к публикации</button></div>
        </section>}</>}
    </main></div>
  </section></div>;
}
