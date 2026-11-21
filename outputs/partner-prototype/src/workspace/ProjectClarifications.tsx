import { projectSourceError } from "./projectErrors";
import { useEffect, useRef, useState } from "react";
import { ApiError, isRecord, type AuthClient } from "../api/client";
import type { ProjectAnswerInput, ProjectFieldAnswerInput } from "../api/chats";
import "./project-questions.css";
import { draftKey, readDraft, saveDraft, removeDraft } from './clarificationDraft';

interface Question {
  field: string;
  text: string;
  target_id: string | null;
  side_network_id: string | null;
  kind: string;
  reason?: string;
  choices: { label: string; value: unknown }[];
}
interface Page {
  revision_id: string;
  current: boolean;
  questions: Question[];
  total: number;
  next_offset: number | null;
}

function onlyFreeText(question: Question): boolean {
  return question.kind === "unsupported" ||
    ((question.kind === "choice" || question.kind === "multiple") && !question.choices.length);
}
function parse(value: unknown, id: string): Page {
  if (
    !isRecord(value) ||
    value.revision_id !== id ||
    typeof value.current !== "boolean" ||
    !Array.isArray(value.questions) ||
    value.questions.length > 6 ||
    typeof value.total !== "number" ||
    !value.questions.every(
      (item) =>
        isRecord(item) &&
        typeof item.field === "string" &&
        typeof item.text === "string" &&
        typeof item.kind === "string" &&
        Array.isArray(item.choices) &&
        item.choices.length <= 256 &&
        item.choices.every(
          (choice) => isRecord(choice) && typeof choice.label === "string",
        ),
    )
  )
    throw new ApiError(502, "invalid_response");
  return value as unknown as Page;
}

function fieldAnswer(
  question: Question,
  input: string,
  upperInput: string,
  selected: string[],
  unknown: boolean,
): ProjectFieldAnswerInput | null {
  const field = {
    path: question.field,
    target_id: question.target_id,
    side_network_id: question.side_network_id,
  };
  if (unknown) return { ...field, unknown: true };
  switch (question.kind) {
    case "choice": {
      const choice = input ? question.choices[Number(input)] : undefined;
      return choice ? { ...field, value: choice.value } : null;
    }
    case "multiple":
      return selected.length ? { ...field, value: selected } : null;
    case "number": {
      const value = Number(input);
      return Number.isInteger(value) && value >= 1 && value <= 65535
        ? { ...field, value }
        : null;
    }
    case "range": {
      const lower = Number(input);
      const upper = upperInput ? Number(upperInput) : null;
      return input && Number.isInteger(lower) && lower >= 0 &&
        (upper === null || (Number.isInteger(upper) && upper >= lower))
        ? { ...field, value: { lower, upper } }
        : null;
    }
    case "text":
      return input.trim() ? { ...field, value: input.trim() } : null;
    default:
      return null;
  }
}
export function ProjectClarifications({
  ownerId,
  auth,
  chatId,
  branchId,
  revisionId,
  newer,
  disabled,
  refresh,
  onAnswer,
  onReady,
  onExpired,
}: {
  ownerId: string;
  auth: AuthClient;
  chatId: string;
  branchId: string;
  revisionId: string;
  newer: boolean;
  disabled: boolean;
  refresh: number;
  onAnswer: (message: string, answer: ProjectAnswerInput) => Promise<boolean>;
  onReady: () => void;
  onExpired: () => void;
}) {
  const storageKey = draftKey(ownerId, chatId, branchId, revisionId);
  const [initialDraft] = useState(() => readDraft(storageKey));
  const [draftStatus, setDraftStatus] = useState<'saved' | 'saving' | 'unavailable' | null>(initialDraft ? 'saved' : null);
  const [submitted, setSubmitted] = useState(false);
  const [page, setPage] = useState<Page | null>(null),
    [error, setError] = useState(""),
    [retry, setRetry] = useState(0),
    [offset, setOffset] = useState(0);
  const [values, setValues] = useState<Record<string, string>>(initialDraft?.values ?? {}),
    [selected, setSelected] = useState<Record<string, string[]>>(initialDraft?.selected ?? {}),
    [visited, setVisited] = useState<Record<string, Question>>({}),
    [unknowns, setUnknowns] = useState<Record<string, boolean>>(initialDraft?.unknowns ?? {}),
    [other, setOther] = useState<Record<string, boolean>>(initialDraft?.other ?? {}),
    [freeText, setFreeText] = useState<Record<string, string>>(initialDraft?.freeText ?? {}),
    [sending, setSending] = useState(false);
  const ready = useRef(onReady);
  ready.current = onReady;
  const expired = useRef(onExpired);
  expired.current = onExpired;
  const draft = { values, selected, unknowns, other, freeText };
  const currentDraft = useRef(draft);
  currentDraft.current = draft;
  useEffect(() => {
    if (newer || submitted) { removeDraft(storageKey); return; }
    if (![values, selected, unknowns, other, freeText].some(value => Object.keys(value).length)) return;
    setDraftStatus('saving');
    const timer = window.setTimeout(() => {
      setDraftStatus(saveDraft(storageKey, currentDraft.current) ? 'saved' : 'unavailable');
    }, 350);
    return () => window.clearTimeout(timer);
  }, [storageKey, values, selected, unknowns, other, freeText, newer, submitted]);
  function flushDraft(): void {
    if (!newer && !submitted) setDraftStatus(saveDraft(storageKey, currentDraft.current) ? 'saved' : 'unavailable');
  }
  useEffect(() => {
    const flush = () => { if (!newer && !submitted) saveDraft(storageKey, currentDraft.current); };
    window.addEventListener('pagehide', flush);
    return () => window.removeEventListener('pagehide', flush);
  }, [storageKey, newer, submitted]);
  useEffect(() => {
    let live = true;
    if (newer) {
      setPage({
        revision_id: revisionId,
        current: false,
        questions: [],
        total: 0,
        next_offset: null,
      });
      return;
    }
    setPage(null);
    setError("");
    void auth
      .request(
        `/chats/${chatId}/designs/${revisionId}/questions?branch_id=${encodeURIComponent(branchId)}&offset=${offset}`,
      )
      .then((value) => {
        const parsed = parse(value, revisionId);
        if (live) {
          setPage(parsed);
          setVisited((old) => ({ ...old, ...Object.fromEntries(
            parsed.questions.map((question) => [question.field, question]),
          ) }));
        }
      })
      .catch((failure) => {
        if (!live) return;
        if (failure instanceof ApiError && failure.status === 401)
          expired.current();
        else setError(projectSourceError(failure) ?? "Не удалось загрузить варианты уточнений.");
      });
    return () => {
      live = false;
    };
  }, [auth, chatId, branchId, revisionId, offset, retry, newer, refresh]);
  useEffect(() => {
    if (!page) return;
    let second = 0;
    const first = requestAnimationFrame(() => {
      second = requestAnimationFrame(() => ready.current());
    });
    return () => {
      cancelAnimationFrame(first);
      cancelAnimationFrame(second);
    };
  }, [page]);
  const showsFreeText = (question: Question) => onlyFreeText(question) || other[question.field];
  const answers = Object.values(visited).flatMap((question) => {
    if (showsFreeText(question)) return [];
    const answer = fieldAnswer(
      question, values[question.field] ?? "", values[question.field + ":upper"] ?? "",
      selected[question.field] ?? [], unknowns[question.field] ?? false,
    );
    return answer ? [answer] : [];
  });
  const remarks = Object.values(visited).flatMap((question) => {
    const text = freeText[question.field];
    return showsFreeText(question) && text?.trim() ? [{
      path: question.field, target_id: question.target_id, side_network_id: question.side_network_id,
      text,
    }] : [];
  });
  const count = answers.length + remarks.length;
  const invalid = Object.values(visited).some((question) => {
    const path = question.field;
    return !unknowns[path] && !showsFreeText(question) &&
      (question.kind === "number" || question.kind === "range") &&
      Boolean(values[path] || values[path + ":upper"]) &&
      !answers.some((answer) => answer.path === path);
  });
  async function apply(): Promise<void> {
    if (!page?.current || disabled || sending || !count || count > 64 || invalid) return;
    setSending(true);
    setError("");
    flushDraft();
    try {
      const message = ["Ответы на уточнения проекта:",
        ...answers.map(answer => `${visited[answer.path]?.text ?? 'Условие'}: ${answer.unknown ? 'Не знаю' : JSON.stringify(answer.value)}`),
        ...remarks.map(remark => `${visited[remark.path]?.text ?? 'Условие'}\nОтвет: ${remark.text}`),
      ].join("\n\n");
      if (!(await onAnswer(message, {
        base_revision_id: revisionId, answers, ...(remarks.length ? { remarks } : {}),
      }))) {
        setError("Ответы не подтверждены. Проверьте состояние отправки или откройте актуальный проект.");
      } else {
        setSubmitted(true);
        removeDraft(storageKey);
      }
    } finally {
      setSending(false);
    }
  }
  if (!page)
    return (
      <section className="project-questions" role="status">
        {error || "Загружаем варианты уточнений…"}
        {error && (
          <button onClick={() => setRetry((value) => value + 1)}>
            Повторить загрузку уточнений
          </button>
        )}
      </section>
    );
  if (!page.current)
    return (
      <p>{submitted && <span className="answer-saved">✓ Ответы приняты сервером. </span>}Уточнения относятся к прежней версии. Откройте актуальный проект.</p>
    );
  if (!page.total) return null;
  return (
    <section className="project-questions" aria-label="Уточнения проекта" onBlur={flushDraft}>
      <h4>
        Уточним условия{" "}
        <span>
          ({offset + 1}–{Math.min(offset + 6, page.total)} из {page.total})
        </span>
      </h4>
      <p>
        Примените готовые варианты и свои ответы вместе. Свободный текст
        будет разобран как условия проекта; вопросы и ответы сохранятся в чате.
      </p>
      {error && <p role="alert">{error}</p>}
      <p className={`draft-save-status ${draftStatus === 'saved' ? 'answer-saved' : ''}`} role="status" aria-live="polite">
        {submitted ? '✓ Ответы приняты сервером. Проверяем применение к проекту.'
          : draftStatus === 'saved' ? '✓ Черновик сохранён в этой вкладке. Для применения нажмите «Применить ответы».'
          : draftStatus === 'saving' ? 'Сохраняем черновик…'
          : draftStatus === 'unavailable' ? 'Не удалось сохранить черновик в браузере. Оставьте вкладку открытой до отправки.' : ''}
      </p>
      {page.questions.map((question) => (
          <fieldset key={question.field} disabled={disabled || sending}>
            <legend>{question.text}</legend>
            {draftStatus === 'saved' && (values[question.field] || freeText[question.field]?.trim()
              || selected[question.field]?.length || unknowns[question.field]) && !submitted &&
              <span className="field-saved-check" title="Черновик ответа сохранён в этой вкладке" aria-label="Черновик ответа сохранён">✓</span>}
            {showsFreeText(question) ? (
              <label>
                Свой ответ
                <textarea
                  maxLength={500}
                  rows={2}
                  value={freeText[question.field] ?? ""}
                  onChange={(event) =>
                    setFreeText((old) => ({
                      ...old,
                      [question.field]: event.target.value,
                    }))
                  }
                />
              </label>
            ) : unknowns[question.field] ? (
              <p>Выбран ответ «Не знаю».</p>
            ) : question.kind === "choice" ? (
              <label>
                Выберите вариант
                <select
                  required
                  value={values[question.field] ?? ""}
                  onChange={(event) =>
                    setValues((old) => ({
                      ...old,
                      [question.field]: event.target.value,
                    }))
                  }
                >
                  <option value="">Выберите…</option>
                  {question.choices.map((choice, n) => (
                    <option key={n} value={n}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              </label>
            ) : question.kind === "multiple" ? (
              <>
                <div className="question-checks">
                  {question.choices.map((choice) => (
                    <label key={String(choice.value)}>
                      <input
                        type="checkbox"
                        checked={(selected[question.field] ?? []).includes(
                          String(choice.value),
                        )}
                        onChange={(event) =>
                          setSelected((old) => ({
                            ...old,
                            [question.field]: event.target.checked
                              ? [
                                  ...(old[question.field] ?? []),
                                  String(choice.value),
                                ]
                              : (old[question.field] ?? []).filter(
                                  (item) => item !== choice.value,
                                ),
                          }))
                        }
                      />
                      {choice.label}
                    </label>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() =>
                    setSelected((old) => ({
                      ...old,
                      [question.field]: question.choices.map((choice) =>
                        String(choice.value),
                      ),
                    }))
                  }
                >
                  Выбрать все
                </button>
              </>
            ) : question.kind === "range" ? (
              <div className="question-range">
                <label>
                  Минимум
                  <input
                    type="number"
                    required
                    min={0}
                    step={1}
                    value={values[question.field] ?? ""}
                    onChange={(event) =>
                      setValues((old) => ({
                        ...old,
                        [question.field]: event.target.value,
                      }))
                    }
                  />
                </label>
                <label>
                  Максимум, если известен
                  <input
                    type="number"
                    min={Number(values[question.field] ?? 0)}
                    step={1}
                    value={values[question.field + ":upper"] ?? ""}
                    onChange={(event) =>
                      setValues((old) => ({
                        ...old,
                        [question.field + ":upper"]: event.target.value,
                      }))
                    }
                  />
                </label>
              </div>
            ) : (
              <label>
                Ответ
                <input
                  required
                  type={question.kind === "number" ? "number" : "text"}
                  min={question.kind === "number" ? 1 : undefined}
                  max={question.kind === "number" ? 65535 : undefined}
                  step={1}
                  maxLength={question.field.endsWith("/name") ? 120 : 500}
                  value={values[question.field] ?? ""}
                  onChange={(event) =>
                    setValues((old) => ({
                      ...old,
                      [question.field]: event.target.value,
                    }))
                  }
                />
              </label>
            )}
            <div className="question-actions">
              {!showsFreeText(question) && question.reason !== "conflict" && (
                <button
                  type="button"
                  onClick={() => {
                    setUnknowns((old) => ({ ...old, [question.field]: !old[question.field] }));
                  }}
                >
                  {unknowns[question.field] ? "Отменить «Не знаю»" : "Не знаю"}
                </button>
              )}
              {!onlyFreeText(question) && (
                <button
                  type="button"
                  onClick={() => {
                    setOther((old) => ({ ...old, [question.field]: !old[question.field] }));
                  }}
                >
                  {other[question.field] ? "К вариантам" : "Другое"}
                </button>
              )}
            </div>
          </fieldset>
      ))}
      <div className="question-actions">
        {offset > 0 && (
          <button
            disabled={disabled || sending}
            onClick={() => setOffset((value) => Math.max(0, value - 6))}
          >
            Предыдущие вопросы
          </button>
        )}
        {page.next_offset !== null && (
          <button
            disabled={disabled || sending}
            onClick={() => setOffset(page.next_offset!)}
          >
            Следующие вопросы
          </button>
        )}
      </div>
      {invalid && <p role="alert">Проверьте введённые числа и границы диапазона.</p>}
      {count > 64 && <p role="alert">За один раз можно применить до 64 ответов.</p>}
      <div className="question-actions">
        <button
          type="button"
          disabled={disabled || sending || invalid || !count || count > 64}
          onClick={() => { void apply(); }}
        >
          Применить ответы ({count})
        </button>
      </div>
    </section>
  );
}
