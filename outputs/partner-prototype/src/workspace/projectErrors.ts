import { ApiError } from "../api/client";

export function projectSourceError(error: unknown): string | null {
  return error instanceof ApiError && error.code === "source_context_changed"
    ? "Источники проекта изменились или стали недоступны. Проверьте доступ к материалам и повторите загрузку проекта."
    : null;
}
