import { projectSourceError } from "./projectErrors";
import { useState } from "react";
import { ApiError, type AuthClient } from "../api/client";
import type { RequirementsRevision } from "../api/requirements";
import { DiagramViewer } from "./DiagramViewer";

export function CalculatedProject({
  auth,
  chatId,
  revision,
  onExpired,
}: {
  auth: AuthClient;
  chatId: string;
  revision: RequirementsRevision;
  onExpired: () => void;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const result = revision.result;
  if (!result) return null;
  const network = (id: string): string =>
    result.networks.find((item) => item.id === id)?.name ?? "Сеть без названия";
  async function download(extension: string): Promise<void> {
    setBusy(true);
    setError("");
    try {
      const file = await auth.download(
        `/chats/${chatId}/designs/${revision.id}/download/${extension}`,
      );
      const url = URL.createObjectURL(file.blob),
        a = document.createElement("a");
      a.href = url;
      a.download = `sinoniks-v${revision.revision}.${extension}`;
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError(projectSourceError(failure) ?? "Не удалось скачать сохранённый результат.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="design-result" aria-label="Расчёт Синоникса 1.9">
      <h3>
        {result.status === "completed"
          ? "Расчёт по заданным условиям"
          : result.status === "unsupported"
            ? "Ограничения проекта"
            : "Предварительный расчёт"}
      </h3>
      <p>
        {result.confirmed_rules_count === null
          ? `Полный минимум не подтверждён. Нижняя оценка: ${result.rules_lower_bound}; в предварительном наборе: ${result.rules_count}.`
          : `Минимальное число правил Синоникса: ${result.confirmed_rules_count}.`}
      </p>
      <p>
        {result.file_users_count === null
          ? result.file_users_range
            ? `Пользователей передачи файлов: ${result.file_users_range.min}–${result.file_users_range.max ?? "верхняя граница неизвестна"}.`
            : "Число пользователей передачи файлов не определено."
          : `Пользователей передачи файлов: ${result.file_users_count}.`}
      </p>
      {revision.artifact_sha256?.diagram ? (
        <DiagramViewer
          auth={auth}
          chatId={chatId}
          revisionId={revision.id}
          onExpired={onExpired}
        />
      ) : (
        <p>
          У этой ревизии нет сохранённой схемы. Доступны расчёт и пояснение.
        </p>
      )}
      <div
        className="diagram-rule-table"
        role="region"
        aria-label="Рассчитанные правила 1.9"
        tabIndex={0}
      >
        <table>
          <thead>
            <tr>
              <th>№</th>
              <th>Назначение</th>
              <th>Направление и режим</th>
              <th>Протокол и проверки</th>
            </tr>
          </thead>
          <tbody>
            {result.rules.map((rule) => (
              <tr key={rule.rule_id}>
                <td>{rule.id}</td>
                <td>{rule.description}</td>
                <td>
                  {network(rule.from)} {rule.mode === "two-way" ? "↔" : "→"}{" "}
                  {network(rule.to)}
                  <br />
                  {rule.mode === "two-way" ? "С ответом" : "Односторонний"}
                </td>
                <td>
                  {rule.type === "data"
                    ? `${typeof rule.protocol === "string" ? rule.protocol : "Не уточнён"} / ${rule.port ?? "порт не задан"}`
                    : Object.entries(
                        typeof rule.protocol === "object" && rule.protocol
                          ? rule.protocol
                          : {},
                      )
                        .map(
                          ([id, method]) =>
                            `${network(id)}: ${method ?? "не уточнён"}`,
                        )
                        .join("; ")}
                  {rule.checks?.length ? (
                    <>
                      <br />
                      {rule.checks.join(", ")}
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Пояснение архитектуры и ограничений</summary>
        <p className="prose" style={{ whiteSpace: "pre-wrap" }}>
          {result.explanation}
        </p>
      </details>
      <div className="design-actions">
        {[
          "json",
          "txt",
          ...(revision.artifact_sha256?.drawio ? ["drawio"] : []),
        ].map((extension) => (
          <button
            type="button"
            key={extension}
            disabled={busy}
            onClick={() => {
              void download(extension);
            }}
          >
            {extension === "json"
              ? "Экспертный JSON"
              : extension === "drawio"
                ? "Схема draw.io"
                : "Скачать пояснение"}
          </button>
        ))}
      </div>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
