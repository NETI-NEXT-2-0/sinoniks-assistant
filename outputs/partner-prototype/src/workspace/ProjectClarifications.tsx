import { projectSourceError } from "./projectErrors";
import { useEffect, useRef, useState } from "react";
import { ApiError, isRecord, type AuthClient } from "../api/client";
import type { ProjectAnswerInput, SubmittedWishInput } from "../api/chats";
import "./project-questions.css";

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
export function ProjectClarifications({
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
  auth: AuthClient;
  chatId: string;
  branchId: string;
  revisionId: string;
  newer: boolean;
  disabled: boolean;
  refresh: number;
  onAnswer: (message: string, answer?: ProjectAnswerInput, wish?: SubmittedWishInput) => Promise<boolean>;
  onReady: () => void;
  onExpired: () => void;
}) {
  const [page, setPage] = useState<Page | null>(null),
    [error, setError] = useState(""),
    [retry, setRetry] = useState(0),
    [offset, setOffset] = useState(0);
  const [values, setValues] = useState<Record<string, string>>({}),
    [selected, setSelected] = useState<Record<string, string[]>>({}),
    [other, setOther] = useState<string | null>(null),
    [sending, setSending] = useState(false);
  const ready = useRef(onReady);
  ready.current = onReady;
  const expired = useRef(onExpired);
  expired.current = onExpired;
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
        if (live) setPage(parsed);
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
  async function answer(question: Question, unknown = false): Promise<void> {
    if (!page?.current || disabled || sending) return;
    let value: unknown = null,
      label = "Не знаю";
    if (!unknown) {
      if (other === question.field) {
        setSending(true);
        try {
          if (
            !(await onAnswer(
              `${question.text} Другое: ${values[question.field] ?? ""}`,
              undefined,
              { kind: "free_text", text: values[question.field] ?? "" },
            ))
          )
            setError("Ответ не подтверждён. Проверьте состояние отправки.");
        } finally {
          setSending(false);
        }
        return;
      }
      if (question.kind === "choice") {
        const choice = question.choices[Number(values[question.field])];
        if (
          !choice ||
          values[question.field] === undefined ||
          values[question.field] === ""
        )
          return;
        value = choice.value;
        label = choice.label;
      } else if (question.kind === "multiple") {
        value = selected[question.field] ?? [];
        label = question.choices
          .filter((choice) => (value as unknown[]).includes(choice.value))
          .map((choice) => choice.label)
          .join(", ");
      } else if (question.kind === "number") {
        value = Number(values[question.field]);
        label = String(value);
      } else if (question.kind === "range") {
        const lower = Number(values[question.field] ?? 0);
        const upper = values[question.field + ":upper"];
        value = { lower, upper: upper ? Number(upper) : null };
        label = `от ${lower} до ${upper || "неизвестной верхней границы"}`;
      } else {
        value = (values[question.field] ?? "").trim();
        label = String(value);
      }
    }
    setSending(true);
    setError("");
    try {
      const accepted = await onAnswer(`${question.text} Ответ: ${label}`, {
        base_revision_id: revisionId,
        path: question.field,
        target_id: question.target_id,
        side_network_id: question.side_network_id,
        value,
        unknown,
      });
      if (!accepted)
        setError(
          "Ответ не подтверждён. Проверьте состояние отправки или откройте актуальный проект.",
        );
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
      <p>Уточнения относятся к прежней версии. Откройте актуальный проект.</p>
    );
  if (!page.total) return null;
  return (
    <section className="project-questions" aria-label="Уточнения проекта">
      <h4>
        Уточним условия{" "}
        <span>
          ({offset + 1}–{Math.min(offset + 6, page.total)} из {page.total})
        </span>
      </h4>
      <p>
        Выбор варианта сохраняется без обращения к модели. «Другое» сохранит
        свободный ответ в сессии и отправит его модели.
      </p>
      {error && <p role="alert">{error}</p>}
      {page.questions.map((question) => (
        <form
          key={question.field}
          onSubmit={(event) => {
            event.preventDefault();
            void answer(question);
          }}
        >
          <fieldset disabled={disabled || sending}>
            <legend>{question.text}</legend>
            {other === question.field ? (
              <label>
                Свой ответ
                <input
                  maxLength={500}
                  required
                  value={values[question.field] ?? ""}
                  onChange={(event) =>
                    setValues((old) => ({
                      ...old,
                      [question.field]: event.target.value,
                    }))
                  }
                />
              </label>
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
            ) : question.kind === "unsupported" ? (
              <p>
                Дополните это условие в редакторе проекта или напишите свой
                ответ.
              </p>
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
              {(question.kind !== "unsupported" ||
                other === question.field) && (
                <button
                  type="submit"
                  disabled={
                    other !== question.field &&
                    question.kind === "multiple" &&
                    !selected[question.field]?.length
                  }
                >
                  {other === question.field
                    ? "Отправить свой ответ"
                    : "Сохранить ответ"}
                </button>
              )}
              {question.kind !== "unsupported" && question.reason !== "conflict" && (
                <button
                  type="button"
                  onClick={() => {
                    void answer(question, true);
                  }}
                >
                  Не знаю
                </button>
              )}
              <button
                type="button"
                onClick={() => {
                  setOther((old) =>
                    old === question.field ? null : question.field,
                  );
                  setValues((old) => ({ ...old, [question.field]: "" }));
                }}
              >
                {other === question.field ? "К вариантам" : "Другое"}
              </button>
            </div>
          </fieldset>
        </form>
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
    </section>
  );
}
