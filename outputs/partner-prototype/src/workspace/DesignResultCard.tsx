import { useEffect, useRef, useState } from "react";
import { ApiError, type AuthClient } from "../api/client";
import type { ProjectAnswerInput } from "../api/chats";
import { DiagramViewer } from "./DiagramViewer";
import { ProjectClarifications } from "./ProjectClarifications";

interface Props {
  auth: AuthClient;
  chatId: string;
  id: string;
  revision: number;
  ready: boolean;
  requirements?: boolean;
  calculated?: boolean;
  questions?: boolean;
  branchId?: string;
  newer?: boolean;
  disabled?: boolean;
  questionsRefresh?: number;
  onAnswer?: (message: string, answer?: ProjectAnswerInput) => Promise<boolean>;
  open: () => void;
  onExpired: () => void;
  onDiagramReady?: () => void;
}
export function DesignResultCard({
  auth,
  chatId,
  id,
  revision,
  ready,
  requirements,
  calculated,
  questions,
  branchId,
  newer,
  disabled,
  questionsRefresh,
  onAnswer,
  open,
  onExpired,
  onDiagramReady,
}: Props) {
  const hasQuestions = Boolean(questions && branchId && onAnswer);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [sceneReady, setSceneReady] = useState(!ready),
    [questionsReady, setQuestionsReady] = useState(!hasQuestions);
  const callback = useRef(onDiagramReady);
  callback.current = onDiagramReady;
  useEffect(() => {
    setSceneReady(!ready);
    setQuestionsReady(!hasQuestions);
  }, [id, ready, hasQuestions]);
  useEffect(() => {
    if (!sceneReady || !questionsReady) return;
    let second = 0;
    const first = requestAnimationFrame(() => {
      second = requestAnimationFrame(() => callback.current?.());
    });
    return () => {
      cancelAnimationFrame(first);
      cancelAnimationFrame(second);
    };
  }, [sceneReady, questionsReady]);
  async function download(extension: string): Promise<void> {
    setBusy(true);
    setError("");
    try {
      const file = await auth.download(
        `/chats/${chatId}/designs/${id}/download/${extension}`,
      );
      const url = URL.createObjectURL(file.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `sinoniks-v${revision}.${extension}`;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError("Не удалось скачать файл. Повторите попытку.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="design-chat-card"
      aria-label={`Проект Синоникса · версия ${revision}`}
    >
      <strong>Проект Синоникса · версия {revision}</strong>
      <p>
        {requirements && calculated
          ? "Расчёт Синоникса 1.9 сохранён. Откройте проект для пояснения, правил и ограничений."
          : requirements
            ? "Требования Синоникса 1.9 сохранены. Архитектура и число правил ещё не рассчитаны."
            : ready
              ? "Схема и расчёт сохранены. Можно скачать файлы или изменить условия."
              : "Условия сохранены. Ответьте на вопросы в чате или дополните проект вручную."}
      </p>
      {ready && (
        <DiagramViewer
          key={id}
          auth={auth}
          chatId={chatId}
          revisionId={id}
          onExpired={onExpired}
          onReady={() => setSceneReady(true)}
        />
      )}
      <div>
        <button onClick={open}>Открыть проект</button>
        {(ready || calculated) &&
          [...(ready ? ["drawio"] : []), "json", "txt"].map((extension) => (
            <button
              key={extension}
              disabled={busy}
              onClick={() => {
                void download(extension);
              }}
            >
              {extension === "drawio"
                ? "Схема draw.io"
                : extension.toUpperCase()}
            </button>
          ))}
      </div>
      {hasQuestions && branchId && onAnswer && (
        <ProjectClarifications
          auth={auth}
          chatId={chatId}
          branchId={branchId}
          revisionId={id}
          newer={!!newer}
          disabled={!!disabled}
          refresh={questionsRefresh ?? 0}
          onAnswer={onAnswer}
          onReady={() => setQuestionsReady(true)}
          onExpired={onExpired}
        />
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
