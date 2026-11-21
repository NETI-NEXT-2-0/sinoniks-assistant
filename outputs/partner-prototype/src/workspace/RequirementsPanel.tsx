import { projectSourceError } from "./projectErrors";
import { useEffect, useRef, useState } from "react";
import { ApiError, type AuthClient } from "../api/client";
import { networkProtocols } from "../api/designs";
import {
  saveRequirements,
  type Check19,
  type FileSide19,
  type Flow19,
  type RequirementsInput,
  type RequirementsRevision,
} from "../api/requirements";
import "./design.css";
import { CalculatedProject } from "./CalculatedProject";

interface Props {
  auth: AuthClient;
  chatId: string;
  branchId: string;
  initial: RequirementsRevision | null;
  empty: RequirementsInput;
  historical: boolean;
  close: () => void;
  reload: () => void;
  openRevision: (id: string) => void;
  onExpired: () => void;
}
const boolValue = (value: string): boolean | null =>
  value === "" ? null : value === "yes";
const boolString = (value: boolean | null | undefined): string =>
  value == null ? "" : value ? "yes" : "no";
function Bool({
  label,
  value,
  change,
}: {
  label: string;
  value: boolean | null | undefined;
  change: (value: boolean | null) => void;
}) {
  return (
    <label>
      {label}
      <select
        value={boolString(value)}
        onChange={(event) => change(boolValue(event.target.value))}
      >
        <option value="">Уточнить</option>
        <option value="yes">Да</option>
        <option value="no">Нет</option>
      </select>
    </label>
  );
}
function failureText(error: unknown): string {
  const sourceError = projectSourceError(error);
  if (sourceError) return sourceError;
  if (error instanceof ApiError) {
    if (error.code === "design_revision_conflict")
      return "Проект изменён в другом окне или в чате. Загрузите свежую версию перед сохранением.";
    if (error.code === "design_identity_conflict")
      return "Ссылки проекта не согласованы. Загрузите сохранённую версию.";
    if (error.status === 422)
      return "Проверьте поля и связанные системы. Порт — целое число от 1 до 65535; названия — до 120 символов.";
    if (error.status === 404) return "Этот проект недоступен.";
  }
  return "Сохранение не подтверждено. Повторите тот же запрос.";
}

export function RequirementsPanel({
  auth,
  chatId,
  branchId,
  initial,
  empty,
  historical,
  close,
  reload,
  openRevision,
  onExpired,
}: Props) {
  const [saved, setSaved] = useState(initial);
  const [section, setSection] = useState<'overview' | 'requirements'>(initial?.result ? 'overview' : 'requirements');
  const [input, setInput] = useState(initial?.input ?? empty);
  const [dirty, setDirty] = useState(false),
    [busy, setBusy] = useState(false),
    [uncertain, setUncertain] = useState(false),
    [error, setError] = useState("");
  const frame = useRef<HTMLElement>(null);
  const pending = useRef<{
    body: {
      branch_id: string;
      parent_id: string | null;
      input: RequirementsInput;
    };
    key: string;
  } | null>(null);
  useEffect(() => {
    const previous = document.activeElement;
    frame.current?.focus();
    return () => {
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus();
    };
  }, []);
  function leave(action: () => void): void {
    if (
      !busy &&
      (!dirty ||
        window.confirm("Есть несохранённые изменения. Продолжить без них?"))
    )
      action();
  }
  function change(next: RequirementsInput, path?: string): void {
    // Keep an explicit unknown when an unrelated control changes. Remove it
    // only once its own value is supplied (or its owner no longer exists).
    const pendingValue = (pointer: string): boolean => {
      let current: unknown = next;
      for (const raw of pointer.split("/").slice(1)) {
        const key = raw.replace(/~1/g, "/").replace(/~0/g, "~");
        if (Array.isArray(current)) current = current[Number(key)];
        else if (typeof current === "object" && current !== null) current = (current as Record<string, unknown>)[key];
        else return false;
      }
      return current == null || (Array.isArray(current) && current.length === 0);
    };
    next = {
      ...next,
      unknowns: (next.unknowns ?? []).filter(note => pendingValue(note.path)),
      assumptions: (next.assumptions ?? []).filter(note => !path || (note.path !== path && !note.path.startsWith(path + "/"))),
    };
    setInput(next);
    setDirty(true);
    setError("");
  }
  function flowChange(
    index: number,
    patch: Partial<Flow19>,
    field?: string,
  ): void {
    change(
      {
        ...input,
        flows: input.flows.map((flow, n) =>
          n === index ? { ...flow, ...patch } : flow,
        ),
      },
      `/flows/${index}${field ? "/" + field : ""}`,
    );
  }
  function sideChange(
    index: number,
    sideIndex: number,
    patch: Partial<FileSide19>,
  ): void {
    const sides = [...(input.flows[index].sides ?? [])];
    sides[sideIndex] = { ...sides[sideIndex], ...patch };
    let next = { ...input, flows: input.flows.map((flow, n) => n === index ? { ...flow, sides } : flow) };
    const paths = Object.keys(patch).map(field => `/flows/${index}/sides/${sideIndex}/${field}`);
    next = { ...next, assumptions: (next.assumptions ?? []).filter(note => !paths.some(path => note.path === path || note.path.startsWith(path + "/"))) };
    change(next);
  }
  function removeFlow(index: number): void {
    const flows = input.flows.filter((_, n) => n !== index);
    function retarget<T extends { path: string; target_id: string | null }>(
      notes: T[],
    ): T[] {
      return notes.flatMap((note) => {
        if (!note.path.startsWith("/flows/")) return [note];
        const n = flows.findIndex((flow) => flow.id === note.target_id);
        return n < 0
          ? []
          : [
              {
                ...note,
                path: note.path.replace(/^\/flows\/\d+/, `/flows/${n}`),
              },
            ];
      });
    }
    change({
      ...input,
      flows,
      unknowns: retarget(input.unknowns ?? []),
      assumptions: retarget(input.assumptions ?? []),
      requests_for_vendor: (input.requests_for_vendor ?? []).filter(
        (value) =>
          typeof value !== "object" ||
          value === null ||
          !("flow_id" in value) ||
          value.flow_id !== input.flows[index].id,
      ),
    });
  }
  async function submit(): Promise<void> {
    if (busy || historical) return;
    pending.current ??= {
      body: { branch_id: branchId, parent_id: saved?.id ?? null, input },
      key: crypto.randomUUID(),
    };
    setBusy(true);
    setError("");
    try {
      const row = await saveRequirements(
        auth,
        chatId,
        pending.current.body,
        pending.current.key,
      );
      setSaved(row);
      setInput(row.input);
      setDirty(false);
      setUncertain(false);
      pending.current = null;
    } catch (failure) {
      const unknown =
        !(failure instanceof ApiError) ||
        failure.status === 0 ||
        failure.status >= 500;
      setUncertain(unknown);
      if (!unknown) pending.current = null;
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError(failureText(failure));
    } finally {
      setBusy(false);
    }
  }
  async function download(): Promise<void> {
    if (!saved || busy) return;
    setBusy(true);
    try {
      const file = await auth.download(
        `/chats/${chatId}/designs/${saved.id}/download/${saved.artifact_sha256?.requirements ? "requirements" : "json"}`,
      );
      const url = URL.createObjectURL(file.blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `sinoniks-requirements-v${saved.revision}.json`;
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setError(projectSourceError(failure) ?? "Не удалось скачать требования.");
    } finally {
      setBusy(false);
    }
  }
  const disabled = busy || historical || uncertain;
  const selectedSystems = (ids: string[]): string[] =>
    input.systems
      .filter((system) => ids.includes(system.id))
      .map((system) => system.name ?? "Без названия");
  const labels: Record<string, string> = {
    name_mask: "Маска имени",
    type_extension: "Тип и расширение",
    size: "Размер",
    signature: "Цифровая подпись",
    icap: "Внешний сервис ICAP",
    unsupported: "Пожелание к проверке",
  };
  return (
    <div className="design-overlay">
      <section
        className="design-panel requirements-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="requirements-title"
        ref={frame}
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") leave(close);
          if (event.key !== "Tab") return;
          const controls = [
            ...event.currentTarget.querySelectorAll<HTMLElement>(
              "button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)",
            ),
          ].filter(control => !control.closest('[hidden]') && control.getClientRects().length > 0);
          const first = controls[0],
            last = controls.at(-1);
          if (
            document.activeElement === frame.current ||
            (event.shiftKey && document.activeElement === first)
          ) {
            event.preventDefault();
            (event.shiftKey ? last : first)?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
        }}
      >
        <header className="design-header">
          <div>
            <h2 id="requirements-title">Проект Синоникса</h2>
            <p>
              {saved ? `Версия ${saved.revision} · ` : 'Новый проект · '}
              {historical ? 'Архив только для чтения' : 'Схема, правила обмена и исходные условия'}
            </p>
          </div>
          <div className="project-header-actions">
            {section === 'requirements' && !historical && <button type="submit" form="project-requirements-form" className="project-save" disabled={busy}>{busy ? 'Сохраняем…' : uncertain ? 'Повторить сохранение' : 'Сохранить и рассчитать'}</button>}
            <button disabled={busy} onClick={() => leave(close)}>К чату</button>
          </div>
        </header>
        <div className="design-content project-workbench">
          {error && (
            <p role="alert" className="design-error">
              {error}
            </p>
          )}
          <p role="status" className={`project-version-status ${dirty ? 'has-changes' : ''}`}>
            {dirty
              ? "Есть несохранённые изменения."
              : saved
                ? `Показана сохранённая версия ${saved.revision}.`
                : "Опишите сети и обмены — после сохранения появится расчёт."}{" "}
            {historical && "Архивная версия доступна только для чтения."}
          </p>
          <p>
            {saved?.result ? "Расчёт сохранён вместе с этой версией. Уточните недостающие условия здесь или в чате." : "Архитектура и число правил ещё не рассчитаны. Неизвестные параметры можно уточнить здесь или следующим сообщением в чате."}
          </p>
          <div className="design-actions">
            <button disabled={busy || uncertain} onClick={() => leave(reload)}>
              Загрузить актуальную версию
            </button>
            {saved?.parent_id && (
              <button
                disabled={busy || dirty || uncertain}
                onClick={() => openRevision(saved.parent_id!)}
              >
                Предыдущая версия
              </button>
            )}
            {saved && (
              <button
                disabled={busy}
                onClick={() => {
                  void download();
                }}
              >
                Скачать требования JSON
              </button>
            )}
          </div>
          <nav className="project-sections" aria-label="Раздел проекта">
            <button type="button" aria-pressed={section === 'overview'} aria-controls="project-overview" onClick={() => setSection('overview')}>Схема и правила</button>
            <button type="button" aria-pressed={section === 'requirements'} aria-controls="project-requirements" onClick={() => setSection('requirements')}>Редактировать условия</button>
          </nav>
          <section id="project-overview" hidden={section !== 'overview'}>
            {dirty && <p className="project-unsaved-note">Схема ниже относится к сохранённой версии. Сохраните изменённые условия, чтобы обновить расчёт.</p>}
            {!!saved?.questions.length && <div className="project-next-steps"><h3>Есть параметры для уточнения</h3><p>В сохранённой версии {saved.questions.length} пунктов для проверки. Посмотрите их в условиях или уточните следующим сообщением в чате.</p><button type="button" onClick={() => setSection('requirements')}>Посмотреть условия</button></div>}
            {saved?.result ? <CalculatedProject auth={auth} chatId={chatId} revision={saved} onExpired={onExpired} /> : <div className="project-empty-result"><h3>Расчёта пока нет</h3><p>Укажите сети, системы и направления обмена. Неизвестные параметры можно оставить для уточнения в чате.</p><button type="button" onClick={() => setSection('requirements')}>Перейти к условиям</button></div>}
          </section>
          <section id="project-requirements" hidden={section !== 'requirements'}>
            <div className="project-editor-intro"><h3>Условия проекта</h3><p>Сначала проверьте сети, затем системы и обмены. «Уточнить» означает, что параметр пока неизвестен. Сохранение создаёт новую версию и пересчитывает правила.</p></div>
      <form id="project-requirements-form"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            {input.networks.map((network, index) => (
              <fieldset key={network.id} disabled={disabled}>
                <legend>Сеть {index + 1}</legend>
                <div className="design-fields">
                  <label>
                    Название сети
                    <input
                      maxLength={120}
                      value={network.name ?? ""}
                      onChange={(event) =>
                        change(
                          {
                            ...input,
                            networks: input.networks.map((item, n) =>
                              n === index
                                ? { ...item, name: event.target.value || null }
                                : item,
                            ),
                          },
                          `/networks/${index}/name`,
                        )
                      }
                    />
                  </label>
                  <label>
                    Доверие сети
                    <select
                      value={network.trust ?? ""}
                      onChange={(event) =>
                        change(
                          {
                            ...input,
                            networks: input.networks.map((item, n) =>
                              n === index
                                ? {
                                    ...item,
                                    trust: (event.target.value ||
                                      null) as typeof network.trust,
                                  }
                                : item,
                            ),
                          },
                          `/networks/${index}/trust`,
                        )
                      }
                    >
                      <option value="">Уточнить</option>
                      <option value="open">Открытая, менее доверенная</option>
                      <option value="closed">Закрытая, защищаемая</option>
                    </select>
                  </label>
                </div>
              </fieldset>
            ))}
            {((input.networks.length === 2 && input.networks.every(network => network.trust === "open")) || input.protected_network_id) && (
              <fieldset disabled={disabled}>
                <legend>Защищаемая сторона проекта</legend>
                <p>Выбор стороны — допущение проекта. Фактическое доверие сетей не меняется.</p>
                <label>
                  Считать защищаемой
                  <select value={input.protected_network_id ?? ""} onChange={event => change(
                    { ...input, protected_network_id: event.target.value || null }, "/protected_network_id",
                  )}>
                    <option value="">Не согласована</option>
                    {input.networks.map(network => <option key={network.id} value={network.id}>{network.name || network.id}</option>)}
                  </select>
                </label>
                {input.unknowns?.some(note => note.path === "/protected_network_id" && note.reason === "user_unknown") && <p>Ответ «не знаю» сохранён. Защищаемая сторона пока не согласована.</p>}
              </fieldset>
            )}
            <fieldset disabled={disabled}>
              <legend>Системы и хранилища</legend>
              {input.systems.map((system, index) => (
                <div className="design-fields" key={system.id}>
                  <label>
                    Название системы
                    <input
                      maxLength={120}
                      value={system.name ?? ""}
                      onChange={(event) =>
                        change(
                          {
                            ...input,
                            systems: input.systems.map((item, n) =>
                              n === index
                                ? { ...item, name: event.target.value || null }
                                : item,
                            ),
                          },
                          `/systems/${index}/name`,
                        )
                      }
                    />
                  </label>
                  <label>
                    Сеть системы
                    <select
                      disabled={system.kind === "usb"}
                      value={system.network_id ?? ""}
                      onChange={(event) =>
                        change(
                          {
                            ...input,
                            systems: input.systems.map((item, n) =>
                              n === index
                                ? {
                                    ...item,
                                    network_id: event.target.value || null,
                                  }
                                : item,
                            ),
                          },
                          `/systems/${index}/network_id`,
                        )
                      }
                    >
                      <option value="">Уточнить</option>
                      {input.networks.map((network) => (
                        <option key={network.id} value={network.id}>
                          {network.name ?? network.id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Тип системы
                    <select
                      value={system.kind}
                      onChange={(event) =>
                        change(
                          {
                            ...input,
                            systems: input.systems.map((item, n) =>
                              n === index
                                ? {
                                    ...item,
                                    kind: event.target
                                      .value as typeof system.kind,
                                    network_id:
                                      event.target.value === "usb"
                                        ? null
                                        : item.network_id,
                                    attachment:
                                      event.target.value === "usb"
                                        ? "node-A"
                                        : null,
                                  }
                                : item,
                            ),
                          },
                          `/systems/${index}`,
                        )
                      }
                    >
                      <option value="system">Система</option>
                      <option value="storage">Хранилище</option>
                      <option value="usb">USB-носитель на Узле А</option>
                    </select>
                  </label>
                </div>
              ))}
              <button
                type="button"
                disabled={input.systems.length >= 256}
                onClick={() =>
                  change({
                    ...input,
                    systems: [
                      ...input.systems,
                      {
                        id: `system-${crypto.randomUUID()}`,
                        name: null,
                        network_id: null,
                        kind: "system",
                      },
                    ],
                  })
                }
              >
                Добавить систему
              </button>
            </fieldset>
            {input.flows.map((flow, index) => (
              <fieldset key={flow.id} disabled={disabled}>
                <legend>
                  Обмен {index + 1}
                  {flow.purpose ? ` · ${flow.purpose}` : ""}
                </legend>
                <div className="design-fields">
                  <label>
                    Назначение
                    <input
                      maxLength={500}
                      value={flow.purpose ?? ""}
                      onChange={(event) =>
                        flowChange(
                          index,
                          { purpose: event.target.value || null },
                          "purpose",
                        )
                      }
                    />
                  </label>
                  <label>
                    Сеть отправителя
                    <select
                      value={flow.sender_network_id ?? ""}
                      onChange={(event) =>
                        flowChange(index, {
                          sender_network_id: event.target.value || null,
                          sender_ids: [],
                        })
                      }
                    >
                      <option value="">Уточнить</option>
                      {input.networks.map((network) => (
                        <option key={network.id} value={network.id}>
                          {network.name ?? network.id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Сеть получателя
                    <select
                      value={flow.receiver_network_id ?? ""}
                      onChange={(event) =>
                        flowChange(index, {
                          receiver_network_id: event.target.value || null,
                          receiver_ids: [],
                        })
                      }
                    >
                      <option value="">Уточнить</option>
                      {input.networks.map((network) => (
                        <option key={network.id} value={network.id}>
                          {network.name ?? network.id}
                        </option>
                      ))}
                    </select>
                  </label>
                  {(["sender_ids", "receiver_ids"] as const).map((field) => (
                    <label key={field}>
                      {field === "sender_ids" ? "Отправители" : "Получатели"}
                      <select
                        multiple
                        value={flow[field]}
                        onChange={(event) =>
                          flowChange(
                            index,
                            {
                              [field]: [...event.target.selectedOptions].map(
                                (option) => option.value,
                              ),
                            },
                            field,
                          )
                        }
                      >
                        {input.systems
                          .filter((system) =>
                            system.kind === "usb"
                              ? flow.kind === "file"
                              : !system.network_id ||
                                system.network_id ===
                                  (field === "sender_ids"
                                    ? flow.sender_network_id
                                    : flow.receiver_network_id),
                          )
                          .map((system) => (
                            <option key={system.id} value={system.id}>
                              {system.name ?? "Без названия"}
                            </option>
                          ))}
                      </select>
                      <small>
                        {selectedSystems(flow[field]).join(", ") ||
                          "Не выбраны"}
                      </small>
                    </label>
                  ))}
                  <label>
                    Условия
                    <input
                      maxLength={500}
                      value={flow.policy ?? ""}
                      onChange={(event) =>
                        flowChange(
                          index,
                          { policy: event.target.value || null },
                          "policy",
                        )
                      }
                    />
                  </label>
                  {flow.kind === "data" ? (
                    <>
                      <label>
                        Режим
                        <select
                          value={flow.mode ?? ""}
                          onChange={(event) =>
                            flowChange(
                              index,
                              {
                                mode: (event.target.value ||
                                  null) as Flow19["mode"],
                              },
                              "mode",
                            )
                          }
                        >
                          <option value="">Уточнить</option>
                          <option value="one-way">Односторонний</option>
                          <option value="two-way">С ответом</option>
                        </select>
                      </label>
                      <label>
                        Протокол
                        <select
                          value={flow.protocol ?? ""}
                          onChange={(event) =>
                            flowChange(
                              index,
                              { protocol: event.target.value || null },
                              "protocol",
                            )
                          }
                        >
                          <option value="">Уточнить</option>
                          {networkProtocols.map((protocol) => (
                            <option key={protocol}>{protocol}</option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Порт получателя
                        <input
                          type="number"
                          min={1}
                          max={65535}
                          step={1}
                          value={flow.destination_port ?? ""}
                          onChange={(event) =>
                            flowChange(
                              index,
                              {
                                destination_port: event.target.value
                                  ? Number(event.target.value)
                                  : null,
                              },
                              "destination_port",
                            )
                          }
                        />
                      </label>
                      <label>
                        Назначение протокола
                        <select
                          value={flow.application ?? "generic"}
                          onChange={(event) =>
                            flowChange(
                              index,
                              {
                                application: event.target
                                  .value as Flow19["application"],
                              },
                              "application",
                            )
                          }
                        >
                          <option value="generic">Обычный поток</option>
                          <option value="ntp">Синхронизация времени NTP</option>
                        </select>
                      </label>
                    </>
                  ) : (
                    <label>
                      Направление файлов
                      <select
                        value={flow.direction ?? ""}
                        onChange={(event) =>
                          flowChange(
                            index,
                            {
                              direction: (event.target.value ||
                                null) as Flow19["direction"],
                            },
                            "direction",
                          )
                        }
                      >
                        <option value="">Уточнить</option>
                        <option value="forward">
                          От отправителя к получателю
                        </option>
                        <option value="both">В обе стороны</option>
                      </select>
                    </label>
                  )}
                </div>
                {flow.kind === "file" && (
                  <>
                    <button
                      type="button"
                      onClick={() =>
                        flowChange(
                          index,
                          {
                            sides: input.networks.map(
                              (network) =>
                                (flow.sides ?? []).find(
                                  (side) => side.network_id === network.id,
                                ) ?? {
                                  network_id: network.id,
                                  manual: null,
                                  usb: null,
                                  checks: null,
                                },
                            ),
                          },
                          "sides",
                        )
                      }
                    >
                      Настроить обе стороны
                    </button>
                    {(flow.sides ?? []).map((side, sideIndex) => (
                      <fieldset key={side.network_id}>
                        <legend>
                          Файлы ·{" "}
                          {input.networks.find(
                            (network) => network.id === side.network_id,
                          )?.name ?? side.network_id}
                        </legend>
                        <div className="design-fields">
                          <Bool
                            label="Ручная передача SFTP"
                            value={side.manual}
                            change={(value) =>
                              sideChange(index, sideIndex, {
                                manual: value,
                                manual_protocol: value ? "SFTP" : null,
                              })
                            }
                          />
                          <Bool
                            label="Передача через USB"
                            value={side.usb}
                            change={(value) =>
                              sideChange(index, sideIndex, { usb: value })
                            }
                          />
                          <Bool
                            label="Общие принятые файлы"
                            value={side.received_files_shared}
                            change={(value) =>
                              sideChange(index, sideIndex, {
                                received_files_shared: value,
                              })
                            }
                          />
                          <label>
                            Группа доступа
                            <input
                              maxLength={80}
                              pattern="[a-zA-Z0-9_-]+"
                              value={side.access_group ?? ""}
                              onChange={(event) =>
                                sideChange(index, sideIndex, {
                                  access_group: event.target.value || null,
                                })
                              }
                            />
                          </label>
                        </div>
                        {(side.automations ?? []).map((automatic, n) => (
                          <div className="design-fields" key={n}>
                            <label>
                              Автоматическая операция
                              <select
                                value={automatic.operation}
                                onChange={(event) =>
                                  sideChange(index, sideIndex, {
                                    automations: side.automations!.map(
                                      (item, m) =>
                                        m === n
                                          ? {
                                              ...item,
                                              operation: event.target.value as
                                                | "PULL"
                                                | "PUSH",
                                            }
                                          : item,
                                    ),
                                  })
                                }
                              >
                                <option value="PULL">
                                  PULL · забор из хранилища
                                </option>
                                <option value="PUSH">
                                  PUSH · доставка в хранилище
                                </option>
                              </select>
                            </label>
                            <label>
                              Протокол файлов
                              <select
                                value={automatic.protocol ?? ""}
                                onChange={(event) =>
                                  sideChange(index, sideIndex, {
                                    automations: side.automations!.map(
                                      (item, m) =>
                                        m === n
                                          ? {
                                              ...item,
                                              protocol: (event.target.value ||
                                                null) as typeof item.protocol,
                                            }
                                          : item,
                                    ),
                                  })
                                }
                              >
                                <option value="">Уточнить</option>
                                {["SFTP", "FTP", "SMB"].map((protocol) => (
                                  <option key={protocol}>{protocol}</option>
                                ))}
                              </select>
                            </label>
                            <label>
                              Хранилище
                              <select
                                value={automatic.storage_id ?? ""}
                                onChange={(event) =>
                                  sideChange(index, sideIndex, {
                                    automations: side.automations!.map(
                                      (item, m) =>
                                        m === n
                                          ? {
                                              ...item,
                                              storage_id:
                                                event.target.value || null,
                                            }
                                          : item,
                                    ),
                                  })
                                }
                              >
                                <option value="">Уточнить</option>
                                {input.systems
                                  .filter(
                                    (system) =>
                                      system.kind === "storage" &&
                                      system.network_id === side.network_id,
                                  )
                                  .map((system) => (
                                    <option key={system.id} value={system.id}>
                                      {system.name ?? system.id}
                                    </option>
                                  ))}
                              </select>
                            </label>
                            <button
                              type="button"
                              onClick={() =>
                                sideChange(index, sideIndex, {
                                  automations: side.automations!.filter(
                                    (_, m) => m !== n,
                                  ),
                                })
                              }
                            >
                              Убрать операцию
                            </button>
                          </div>
                        ))}
                        <button
                          type="button"
                          disabled={(side.automations?.length ?? 0) >= 8}
                          onClick={() =>
                            sideChange(index, sideIndex, {
                              automations: [
                                ...(side.automations ?? []),
                                {
                                  operation: "PULL",
                                  protocol: null,
                                  storage_id: null,
                                },
                              ],
                            })
                          }
                        >
                          Добавить автоматическую операцию
                        </button>
                        <label>
                          Проверки файлов
                          <select
                            value={side.checks == null ? "unknown" : "known"}
                            onChange={(event) =>
                              sideChange(index, sideIndex, {
                                checks:
                                  event.target.value === "unknown"
                                    ? null
                                    : (side.checks ?? []),
                              })
                            }
                          >
                            <option value="unknown">Нужно уточнить</option>
                            <option value="known">
                              Указаны ниже; пустой список — без проверок
                            </option>
                          </select>
                        </label>
                        {(side.checks ?? []).map((check, n) => (
                          <div className="design-fields" key={n}>
                            <label>
                              Вид проверки
                              <select
                                value={check.kind}
                                onChange={(event) =>
                                  sideChange(index, sideIndex, {
                                    checks: side.checks!.map((item, m) =>
                                      m === n
                                        ? {
                                            ...item,
                                            kind: event.target
                                              .value as Check19["kind"],
                                            icap_service: null,
                                          }
                                        : item,
                                    ),
                                  })
                                }
                              >
                                {Object.entries(labels).map(
                                  ([value, label]) => (
                                    <option key={value} value={value}>
                                      {label}
                                    </option>
                                  ),
                                )}
                              </select>
                            </label>
                            <label>
                              Параметры проверки
                              <input
                                maxLength={500}
                                required={check.kind === "unsupported"}
                                value={check.details ?? ""}
                                onChange={(event) =>
                                  sideChange(index, sideIndex, {
                                    checks: side.checks!.map((item, m) =>
                                      m === n
                                        ? {
                                            ...item,
                                            details: event.target.value || null,
                                          }
                                        : item,
                                    ),
                                  })
                                }
                              />
                            </label>
                            {check.kind === "icap" && (
                              <label>
                                Внешний сервис
                                <select
                                  value={check.icap_service ?? ""}
                                  onChange={(event) =>
                                    sideChange(index, sideIndex, {
                                      checks: side.checks!.map((item, m) =>
                                        m === n
                                          ? {
                                              ...item,
                                              icap_service: (event.target
                                                .value ||
                                                null) as Check19["icap_service"],
                                            }
                                          : item,
                                      ),
                                    })
                                  }
                                >
                                  <option value="">Уточнить</option>
                                  <option value="antivirus">Антивирус</option>
                                  <option value="sandbox">Песочница</option>
                                  <option value="dlp">DLP</option>
                                </select>
                              </label>
                            )}
                            <button
                              type="button"
                              onClick={() =>
                                sideChange(index, sideIndex, {
                                  checks: side.checks!.filter(
                                    (_, m) => m !== n,
                                  ),
                                })
                              }
                            >
                              Убрать проверку
                            </button>
                          </div>
                        ))}
                        <button
                          type="button"
                          disabled={(side.checks?.length ?? 0) >= 20}
                          onClick={() =>
                            sideChange(index, sideIndex, {
                              checks: [
                                ...(side.checks ?? []),
                                { kind: "name_mask", details: null },
                              ],
                            })
                          }
                        >
                          Добавить проверку
                        </button>
                      </fieldset>
                    ))}
                  </>
                )}
                <button type="button" onClick={() => removeFlow(index)}>
                  Удалить обмен {index + 1}
                </button>
              </fieldset>
            ))}
            <fieldset disabled={disabled}>
              <legend>Ручные пользователи передачи файлов</legend>
              <p>
                Один человек учитывается один раз. Автоматические
                PULL-пользователи будут рассчитаны отдельно.
              </p>
              <Bool
                label="Число ручных пользователей известно"
                value={input.manual_users != null}
                change={(value) =>
                  change(
                    {
                      ...input,
                      manual_users: value ? { lower: 0, upper: null } : null,
                    },
                    "/manual_users",
                  )
                }
              />
              {input.manual_users && (
                <div className="design-fields">
                  <label>
                    Минимум
                    <input
                      type="number"
                      min={0}
                      step={1}
                      value={input.manual_users.lower}
                      onChange={(event) =>
                        change(
                          {
                            ...input,
                            manual_users: {
                              ...input.manual_users!,
                              lower: Number(event.target.value),
                            },
                          },
                          "/manual_users",
                        )
                      }
                    />
                  </label>
                  <label>
                    Максимум (пусто — неизвестен)
                    <input
                      type="number"
                      min={input.manual_users.lower}
                      step={1}
                      value={input.manual_users.upper ?? ""}
                      onChange={(event) =>
                        change(
                          {
                            ...input,
                            manual_users: {
                              ...input.manual_users!,
                              upper: event.target.value
                                ? Number(event.target.value)
                                : null,
                            },
                          },
                          "/manual_users",
                        )
                      }
                    />
                  </label>
                </div>
              )}
            </fieldset>
            <div className="design-actions">
              {(["data", "file"] as const).map((kind) => (
                <button
                  type="button"
                  key={kind}
                  disabled={disabled || input.flows.length >= 256}
                  onClick={() =>
                    change({
                      ...input,
                      flows: [
                        ...input.flows,
                        {
                          id: `flow-${crypto.randomUUID()}`,
                          kind,
                          sender_network_id: null,
                          receiver_network_id: null,
                          sender_ids: [],
                          receiver_ids: [],
                          purpose: null,
                          ...(kind === "file"
                            ? { direction: null, sides: [] }
                            : {
                                mode: null,
                                protocol: null,
                                destination_port: null,
                              }),
                        },
                      ],
                    })
                  }
                >
                  {kind === "data"
                    ? "Добавить поток данных"
                    : "Добавить обмен файлами"}
                </button>
              ))}
              <button type="submit" disabled={busy || historical}>
                {busy
                  ? "Сохраняем…"
                  : uncertain
                    ? "Повторить сохранение"
                    : "Сохранить и рассчитать"}
              </button>
            </div>
          </form>
          {saved && (
            <section className="design-result" aria-label="Уточнения проекта">
              <h3>
                {saved.status === "needs_clarification"
                  ? "Нужны уточнения"
                  : saved.status === "preliminary"
                    ? "Предварительные требования"
                    : "Основные сведения собраны"}
              </h3>
              {saved.questions.map((question) => (
                <p key={`${question.field}:${question.reason}`}>
                  {question.text}{" "}
                  {question.answered_unknown ? (
                    <span>Ответ «не знаю» сохранён.</span>
                  ) : (
                    ["missing", "optional"].includes(question.reason) && (
                      <button
                        disabled={disabled || dirty}
                        onClick={() =>
                          change({
                            ...input,
                            unknowns: [
                              ...(input.unknowns ?? []).filter(
                                (note) => note.path !== question.field,
                              ),
                              {
                                path: question.field,
                                target_id: question.target_id,
                                reason: "user_unknown",
                              },
                            ],
                          })
                        }
                      >
                        Не знаю
                      </button>
                    )
                  )}
                </p>
              ))}
            </section>
          )}
          </section>
        </div>
      </section>
    </div>
  );
}
