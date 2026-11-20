import { projectSourceError } from "./projectErrors";
import { useEffect, useRef, useState } from "react";
import { ApiError, type AuthClient } from "../api/client";
import {
  emptyRequirements,
  isRequirements,
  loadAnyDesign,
  upgradeDesign,
  type AnyRevision,
  type RequirementsInput,
} from "../api/requirements";
import { DesignPanel } from "./DesignPanel";
import { RequirementsPanel } from "./RequirementsPanel";

interface Props {
  auth: AuthClient;
  chatId: string;
  branchId: string;
  revisionId?: string | null;
  close: () => void;
  onExpired: () => void;
}
export function ProjectPanel(props: Props) {
  const { auth, chatId, branchId, close, onExpired } = props;
  const [selection, setSelection] = useState<string | null>(
    props.revisionId ?? null,
  );
  const [loaded, setLoaded] = useState<{
    selected: AnyRevision | null;
    latest: AnyRevision | null;
  } | null>(null);
  const [reload, setReload] = useState(0),
    [fresh, setFresh] = useState<RequirementsInput | null>(null);
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const pending = useRef<{ id: string; key: string } | null>(null);
  const initial = useRef(emptyRequirements());
  const expired = useRef(onExpired); expired.current = onExpired;
  useEffect(() => {
    let live = true;
    setLoaded(null);
    setError("");
    void loadAnyDesign(auth, chatId, branchId, selection)
      .then((value) => {
        if (live) setLoaded(value);
      })
      .catch((failure) => {
        if (!live) return;
        if (failure instanceof ApiError && failure.status === 401) expired.current();
        else setError(projectSourceError(failure) ?? "Не удалось открыть сохранённый проект.");
      });
    return () => {
      live = false;
    };
  }, [auth, chatId, branchId, selection, reload]);
  function latest(): void {
    setSelection(null);
    setFresh(null);
    setReload((value) => value + 1);
    pending.current = null;
  }
  async function upgrade(id: string): Promise<void> {
    if (busy) return;
    setBusy(true);
    setError("");
    pending.current ??= { id, key: crypto.randomUUID() };
    try {
      const row = await upgradeDesign(
        auth,
        chatId,
        pending.current.id,
        branchId,
        pending.current.key,
      );
      setLoaded({ selected: row, latest: row });
      setSelection(null);
      pending.current = null;
    } catch (failure) {
      if (
        failure instanceof ApiError &&
        failure.status > 0 &&
        failure.status < 500
      )
        pending.current = null;
      if (failure instanceof ApiError && failure.status === 401) expired.current();
      else
        setError(
          projectSourceError(failure) ?? (failure instanceof ApiError && failure.code === "design_kind_unknown"
            ? "Сначала укажите в старом проекте тип каждого обмена: данные или файлы."
            : "Переход не подтверждён. Повторите действие или загрузите свежую версию."),
        );
    } finally {
      setBusy(false);
    }
  }
  if (!loaded)
    return (
      <div className="design-overlay">
        <section
          className="design-panel"
          role="dialog"
          aria-modal="true"
          aria-label="Открытие проекта"
        >
          <div className="design-content">
            <p role="status">{error || "Открываем проект…"}</p>
            <button onClick={close}>К чату</button>
            {error && <button onClick={latest}>Повторить загрузку</button>}
          </div>
        </section>
      </div>
    );
  const selected = loaded.selected;
  if (fresh || (selected && isRequirements(selected)))
    return (
      <RequirementsPanel
        key={selected?.id ?? "new19"}
        {...props}
        initial={selected && isRequirements(selected) ? selected : null}
        empty={fresh ?? initial.current}
        historical={!!selected && selected.id !== loaded.latest?.id}
        reload={latest}
        openRevision={(id) => {
          setSelection(id);
          setFresh(null);
        }}
      />
    );
  return (
    <DesignPanel
      {...props}
      initial={selected}
      latestId={loaded.latest?.id ?? null}
      onReload={latest}
      requirementsAction={(id) => {
        if (id) void upgrade(id);
        else setFresh(initial.current);
      }}
      upgrading={busy}
      upgradeError={error}
    />
  );
}
