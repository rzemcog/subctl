import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const MUTATION_HEADERS = { "Content-Type": "application/json", "X-Subctl-UI": "1" };

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Ошибка HTTP ${response.status}`);
  return body;
}

function formatTime(value) {
  if (!value) return "—";
  return new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

function formatAge(value) {
  if (!value) return "нет данных";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds} сек. назад`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин. назад`;
  return `${Math.floor(seconds / 3600)} ч. назад`;
}

function statusLabel(status) {
  return {
    queued: "в очереди", running: "выполняется", succeeded: "готово", failed: "ошибка",
    interrupted: "прервано", not_generated: "не генерировался",
  }[status] || status || "—";
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function App() {
  const [users, setUsers] = useState([]);
  const [provider, setProvider] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [settings, setSettings] = useState(null);
  const [selected, setSelected] = useState(null);
  const [form, setForm] = useState({ name: "", xui_subscription: "" });
  const [editing, setEditing] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [settingsTab, setSettingsTab] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);

  const activeJobs = useMemo(() => jobs.some((job) => ["queued", "running"].includes(job.status)), [jobs]);

  async function loadData() {
    const [userData, providerData, jobData, settingsData] = await Promise.all([
      api("/api/users"), api("/api/provider/status"), api("/api/jobs?limit=100"), api("/api/settings"),
    ]);
    setUsers(userData.users);
    setProvider(providerData);
    setJobs(jobData.jobs);
    setSettings(settingsData);
  }

  useEffect(() => { loadData().catch((error) => setNotice({ type: "error", text: error.message })); }, []);
  useEffect(() => {
    if (!activeJobs) return undefined;
    const timer = setInterval(() => loadData().catch(() => {}), 1500);
    return () => clearInterval(timer);
  }, [activeJobs]);

  function notify(text, type = "ok") {
    setNotice({ type, text });
    setTimeout(() => setNotice(null), 6000);
  }

  function openCreate() {
    setSelected(null); setEditing(false); setForm({ name: "", xui_subscription: "" }); setShowForm(true);
  }

  async function openEdit(user) {
    try {
      const data = await api(`/api/users/${encodeURIComponent(user.name)}`);
      setSelected(user); setEditing(true); setShowForm(true);
      setForm({ name: data.user.name, xui_subscription: data.user.xui_subscription });
    } catch (error) { notify(error.message, "error"); }
  }

  async function submitForm(event) {
    event.preventDefault(); setBusy(true);
    try {
      const path = editing ? `/api/users/${encodeURIComponent(selected.name)}` : "/api/users";
      await api(path, { method: editing ? "PATCH" : "POST", headers: MUTATION_HEADERS, body: JSON.stringify(form) });
      await loadData(); setEditing(false); setShowForm(false); setForm({ name: "", xui_subscription: "" });
      notify(editing ? "Пользователь обновлён, генерация запущена" : "Пользователь создан, генерация запущена");
    } catch (error) { notify(error.message, "error"); } finally { setBusy(false); }
  }

  async function rotate(user) {
    if (!window.confirm(`Ротировать token пользователя «${user.name}»? Старые ссылки перестанут работать.`)) return;
    try {
      await api(`/api/users/${encodeURIComponent(user.name)}/rotate-token`, { method: "POST", headers: MUTATION_HEADERS });
      await loadData(); notify("Token обновлён, старые файлы удалены");
    } catch (error) { notify(error.message, "error"); }
  }

  async function remove(user) {
    const confirmation = window.prompt(`Для удаления введите имя пользователя: ${user.name}`);
    if (confirmation !== user.name) return;
    try {
      await api(`/api/users/${encodeURIComponent(user.name)}`, { method: "DELETE", headers: MUTATION_HEADERS, body: JSON.stringify({ confirm_name: confirmation }) });
      if (selected?.name === user.name) setSelected(null);
      await loadData(); notify("Пользователь удалён, публичные файлы удалены");
    } catch (error) { notify(error.message, "error"); }
  }

  async function enqueue(path, text) {
    try { await api(path, { method: "POST", headers: MUTATION_HEADERS }); await loadData(); notify(text); }
    catch (error) { notify(error.message, "error"); }
  }

  async function renderUser(user) {
    await enqueue(`/api/users/${encodeURIComponent(user.name)}/render`, `Генерация для ${user.name} поставлена в очередь`);
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div><div className="eyebrow">VPN subscription control</div><h1>subctl <span>подписки</span></h1></div>
        <div className="top-actions">
          <button className="button ghost" title="Показать итоговый YAML и безопасные параметры склейки" onClick={() => setSettingsTab("template")}><span className="button-icon">⚙</span>Настройки шаблона</button>
          <button className="button ghost" title="Заново создать YAML и raw-файлы для всех пользователей" onClick={() => enqueue("/api/render", "Полная генерация поставлена в очередь")}><span className="button-icon">↻</span>Перегенерировать всё</button>
          <button className="button primary" onClick={openCreate}><span className="button-icon">＋</span>Пользователь</button>
        </div>
      </header>

      {notice && <div className={`notice ${notice.type}`}>{notice.text}</div>}

      <main>
        <section className="metrics">
          <div className="metric-card"><span>Пользователи</span><strong>{users.length}</strong><small>активные записи registry</small></div>
          <div className="metric-card" title="Общий provider кэшируется на сервере и добавляется в пользовательские профили."><span>Общий provider</span><strong>{provider?.node_count ?? "—"}</strong><small>{provider?.cache_present ? `узлов · ${formatAge(provider.cache_updated_at)}` : "кэш отсутствует"}</small></div>
          <div className="metric-card"><span>Последний refresh</span><strong className={provider?.refresh?.status === "failed" ? "warn" : "good"}>{provider?.refresh?.status === "succeeded" ? "OK" : provider?.refresh?.status === "failed" ? "Ошибка" : provider?.refresh?.status === "running" ? "Работает" : "—"}</strong><small>{provider?.refresh?.finished_at ? `${formatTime(provider.refresh.finished_at)} · ${provider.refresh.duration_ms ?? 0} мс` : "результат ещё не записан"}</small></div>
          <div className="metric-card action-card"><span>Синхронизация</span><strong>{activeJobs ? "Работает" : "Готово"}</strong><button className="button small ghost" title="Скачать, проверить и опубликовать новый общий provider, затем обновить всех пользователей" onClick={() => enqueue("/api/provider/refresh", "Refresh provider поставлен в очередь")}><span className="button-icon">↻</span>Обновить provider</button></div>
        </section>

        <section className="panel provider-panel">
          <div className="panel-heading"><div><div className="eyebrow">Shared source</div><h2>Provider</h2></div><div className="provider-heading-actions"><span className={`state-badge ${provider?.public_feed_present ? "good" : "warn"}`}>{provider?.public_feed_present ? "feed опубликован" : "feed отсутствует"}</span><button className="button small ghost" onClick={() => setSettingsTab("provider")}><span className="button-icon">✎</span>Изменить provider</button><a className="button small ghost" href="http://127.0.0.1:19090/ui/" target="_blank" rel="noreferrer" title="Открывается через SSH-туннель 19090:19090"><span className="button-icon">↗</span>Mihomo-панель</a></div></div>
          <div className="provider-grid"><div><strong>{provider?.shared_provider_count ?? 1} общий источник</strong><p>{provider?.shared_provider_note || "Один источник provider сейчас используется всеми профилями."}</p></div><div><span>Последний результат</span><strong>{provider?.refresh?.status ? statusLabel(provider.refresh.status) : "нет запусков"}</strong><p>{provider?.refresh?.node_count_before ?? "—"} → {provider?.refresh?.node_count_after ?? "—"} узлов · {provider?.refresh?.error || "ошибок не зафиксировано"}</p></div><div><span>Кэш</span><strong>{provider?.cache_updated_at ? formatTime(provider.cache_updated_at) : "—"}</strong><p>Возраст: {formatAge(provider?.cache_updated_at)}</p></div></div>
        </section>

        <div className="content-grid">
          <section className="panel users-panel">
            <div className="panel-heading"><div><div className="eyebrow">Registry</div><h2>Пользователи</h2></div><span className="count-pill">{users.length}</span></div>
            {users.length === 0 ? <div className="empty"><div className="empty-icon">＋</div><h3>Пока нет пользователей</h3><p>Добавьте первую подписку, чтобы сгенерировать YAML и raw-ссылки.</p><button className="button primary" onClick={openCreate}><span className="button-icon">＋</span>Добавить пользователя</button></div> : <div className="user-list">{users.map((user) => <UserCard key={user.name} user={user} onEdit={openEdit} onRotate={rotate} onDelete={remove} onRender={renderUser} />)}</div>}
          </section>

          <aside className="panel operations-panel"><div className="panel-heading"><div><div className="eyebrow">Activity</div><h2>Операции</h2></div><span className="live-dot">● live</span></div>{jobs.length === 0 ? <p className="muted">Операций пока нет.</p> : <div className="job-list">{jobs.slice(0, 16).map((job) => <div className="job" key={job.id}><span className={`job-status ${job.status}`}></span><div className="job-body"><div className="job-title">{job.kind === "refresh_provider" ? "Refresh provider" : job.kind === "render_all" ? "Полная генерация" : `Генерация · ${job.target}`}</div><div className="job-message">{job.message || statusLabel(job.status)}</div></div><time>{formatTime(job.finished_at || job.created_at)}</time></div>)}</div>}</aside>
        </div>
      </main>

      {showForm && <UserForm form={form} setForm={setForm} editing={editing} busy={busy} onSubmit={submitForm} onCancel={() => { setEditing(false); setShowForm(false); setForm({ name: "", xui_subscription: "" }); }} />}
      {settingsTab && <SettingsModal tab={settingsTab} settings={settings} users={users} onClose={() => setSettingsTab(null)} onChanged={loadData} notify={notify} />}
    </div>
  );
}

function UserCard({ user, onEdit, onRotate, onDelete, onRender }) {
  const render = user.activity?.render;
  const status = user.status || {};
  return <article className="user-card">
    <div className="user-main"><div className="avatar">{user.name.slice(0, 1).toUpperCase()}</div><div><h3>{user.name}</h3><div className="token">token · {user.token_masked}</div></div><span className={`state-badge ${render?.status === "failed" ? "bad" : status.ready ? "good" : "warn"}`}>{render ? statusLabel(render.status) : status.ready ? "файлы готовы" : "нет файлов"}</span></div>
    <div className="links"><LinkRow label="YAML профиль" url={user.yaml_url} /><LinkRow label="Raw подписка" url={user.raw_url} /></div>
    <div className="user-observability"><div><span>Конфиг</span><strong>{status.last_regenerated_at ? formatTime(status.last_regenerated_at) : "—"}</strong><small>{render?.config_version ? `версия шаблона ${render.config_version}` : render?.error || "последняя генерация неизвестна"}</small></div><div><span>Последнее скачивание YAML</span><strong>{formatTime(user.activity?.fetch?.yaml?.fetched_at)}</strong><small>{user.activity?.fetch?.yaml ? `HTTP ${user.activity.fetch.yaml.http_status} · ${formatAge(user.activity.fetch.yaml.fetched_at)}` : "ещё не скачивался"}</small></div><div><span>Последнее скачивание raw</span><strong>{formatTime(user.activity?.fetch?.raw?.fetched_at)}</strong><small>{user.activity?.fetch?.raw ? `HTTP ${user.activity.fetch.raw.http_status} · ${formatAge(user.activity.fetch.raw.fetched_at)}` : "ещё не скачивался"}</small></div></div>
    <div className="card-actions"><button className="button small" title="Изменить имя или личную 3x-ui URL. Token и ссылки сохраняются" onClick={() => onEdit(user)}><span className="button-icon">✎</span>Изменить</button><button className="button small ghost" title="Заново создать YAML и raw-файлы с тем же token. Ссылки не меняются" onClick={() => onRender(user)}><span className="button-icon">↻</span>Перегенерировать</button><button className="button small ghost" title="Создать новый token и сразу инвалидировать старые ссылки" onClick={() => onRotate(user)}><span className="button-icon">⟳</span>Ротировать token</button><button className="button small danger-button" title="Удалить пользователя и его публичные файлы" onClick={() => onDelete(user)}><span className="button-icon">×</span>Удалить</button></div>
  </article>;
}

function LinkRow({ label, url }) {
  async function copy() { try { await navigator.clipboard.writeText(url); } catch { window.prompt("Скопируйте ссылку", url); } }
  return <div className="link-row"><div><span>{label}</span><code>{url}</code></div><div className="link-actions"><button className="button small link-button icon-only" title="Копировать ссылку" aria-label="Копировать ссылку" onClick={copy}><span className="button-icon">⧉</span></button><a className="button small link-button icon-only" title="Открыть ссылку" aria-label="Открыть ссылку" href={url} target="_blank" rel="noreferrer"><span className="button-icon">↗</span></a></div></div>;
}

function UserForm({ form, setForm, editing, busy, onSubmit, onCancel }) {
  return <div className="modal-backdrop"><div className="modal"><div className="modal-heading"><div><div className="eyebrow">{editing ? "Edit user" : "New user"}</div><h2>{editing ? "Изменить пользователя" : "Новый пользователь"}</h2></div><button className="button small ghost" onClick={onCancel}><span className="button-icon">×</span>Закрыть</button></div><form onSubmit={onSubmit}><label>Имя пользователя<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="например, alice" required pattern="[A-Za-z0-9_.-]+" /></label><label>Личная 3x-ui subscription URL<input type="url" value={form.xui_subscription} onChange={(event) => setForm({ ...form, xui_subscription: event.target.value })} placeholder="https://panel.example/sub/..." required /></label><p className="form-hint">URL хранится в registry и используется только сервером для получения личной подписки.</p><div className="modal-actions"><button type="button" className="button ghost" onClick={onCancel}><span className="button-icon">←</span>Отмена</button><button type="submit" className="button primary" disabled={busy}><span className="button-icon">{busy ? "…" : editing ? "✓" : "＋"}</span>{busy ? "Сохраняю…" : editing ? "Сохранить" : "Создать"}</button></div></form></div></div>;
}

function SettingsModal({ tab, settings, users, onClose, onChanged, notify }) {
  const source = settings?.draft || settings?.settings || defaultSettings();
  const [form, setForm] = useState(clone(source));
  const [preview, setPreview] = useState(null);
  const [previewUser, setPreviewUser] = useState(users[0]?.name || "");
  const [working, setWorking] = useState(false);
  const providerTab = tab === "provider";

  function update(path, value) {
    setForm((current) => { const next = clone(current); let target = next; path.slice(0, -1).forEach((key) => { target = target[key]; }); target[path[path.length - 1]] = value; return next; });
  }

  async function previewForm() {
    setWorking(true);
    try { const data = await api("/api/settings/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings: form, user_name: previewUser || null }) }); setPreview(data); }
    catch (error) { notify(error.message, "error"); } finally { setWorking(false); }
  }

  async function saveDraft() {
    setWorking(true);
    try { await api("/api/settings/draft", { method: "POST", headers: MUTATION_HEADERS, body: JSON.stringify({ settings: form }) }); notify("Черновик сохранён"); await onChanged(); }
    catch (error) { notify(error.message, "error"); } finally { setWorking(false); }
  }

  async function publish() {
    setWorking(true);
    try {
      const path = providerTab ? "/api/provider/settings" : "/api/settings/publish";
      const body = providerTab ? { upstream_url: form.provider.upstream_url, refresh_interval_seconds: form.provider.refresh_interval_seconds, exclude_keywords: form.render.provider_exclude_keywords } : { settings: form };
      await api(path, { method: "POST", headers: MUTATION_HEADERS, body: JSON.stringify(body) });
      notify(providerTab ? "Provider сохранён, refresh и генерация поставлены в очередь" : "Настройки опубликованы, полная генерация поставлена в очередь"); await onChanged(); onClose();
    } catch (error) { notify(error.message, "error"); } finally { setWorking(false); }
  }

  async function rollback(version) {
    if (!window.confirm(`Откатить настройки к версии ${version} и перегенерировать всех?`)) return;
    setWorking(true);
    try { await api(`/api/settings/rollback/${version}`, { method: "POST", headers: MUTATION_HEADERS }); notify(`Создана новая версия из отката ${version}`); await onChanged(); }
    catch (error) { notify(error.message, "error"); } finally { setWorking(false); }
  }

  const render = form.render;
  const composition = render.composition;
  return <div className="modal-backdrop"><div className="modal wide-modal"><div className="modal-heading"><div><div className="eyebrow">{providerTab ? "Shared provider" : "Template versions"}</div><h2>{providerTab ? "Настройки provider" : "Шаблон и склейка"}</h2></div><button className="button small ghost" onClick={onClose}><span className="button-icon">×</span>Закрыть</button></div>
    <div className="settings-tabs"><button className={providerTab ? "tab" : "tab active"} onClick={() => {}}>Шаблон</button><span className="tab-note">{providerTab ? "Один общий источник для всех профилей" : `Опубликовано: v${settings?.version || 0}`}</span></div>
    <div className="settings-grid">
      {providerTab && <section className="settings-section"><h3>Общий provider</h3><p className="form-hint">Сейчас используется один provider-блок. Он не размножается по пользователям: изменяется URL источника, а общий кэш затем используется всеми профилями.</p><label>Upstream URL<input value={form.provider.upstream_url} onChange={(e) => update(["provider", "upstream_url"], e.target.value)} /></label><label>Интервал обновления, сек.<input type="number" min="1" value={form.provider.refresh_interval_seconds} onChange={(e) => update(["provider", "refresh_interval_seconds"], Number(e.target.value))} /></label><label>Исключения по названию узла<input value={render.provider_exclude_keywords.join(", ")} onChange={(e) => update(["render", "provider_exclude_keywords"], e.target.value.split(",").map((v) => v.trim()).filter(Boolean))} placeholder="test, expired" /></label></section>}
      {!providerTab && <><section className="settings-section"><h3>Периодичность</h3><label>Обновление личного профиля, сек.<input type="number" min="1" value={render.profile_update_interval_seconds} onChange={(e) => update(["render", "profile_update_interval_seconds"], Number(e.target.value))} /></label><label>Обновление provider в профиле, сек.<input type="number" min="1" value={render.provider_update_interval_seconds} onChange={(e) => update(["render", "provider_update_interval_seconds"], Number(e.target.value))} /></label><label>URL health-check<input value={render.healthcheck_url} onChange={(e) => update(["render", "healthcheck_url"], e.target.value)} /></label><label>Интервал health-check, сек.<input type="number" min="1" value={render.healthcheck_interval_seconds} onChange={(e) => update(["render", "healthcheck_interval_seconds"], Number(e.target.value))} /></label><label>Timeout health-check, мс.<input type="number" min="1" value={render.healthcheck_timeout_milliseconds} onChange={(e) => update(["render", "healthcheck_timeout_milliseconds"], Number(e.target.value))} /></label></section><section className="settings-section"><h3>Склейка</h3><label className="check-row"><input type="checkbox" checked={composition.include_private} onChange={(e) => update(["render", "composition", "include_private"], e.target.checked)} />Личная группа PRIVATE</label><label className="check-row"><input type="checkbox" checked={composition.include_provider} onChange={(e) => update(["render", "composition", "include_provider"], e.target.checked)} />Общий provider PROVIDER-AUTO</label><label className="check-row"><input type="checkbox" checked={composition.provider_first} onChange={(e) => update(["render", "composition", "provider_first"], e.target.checked)} />Provider-узлы первыми в raw</label><label>Префикс личных узлов<input value={composition.private_prefix} onChange={(e) => update(["render", "composition", "private_prefix"], e.target.value)} /></label><label>Префикс provider-узлов<input value={composition.provider_prefix} onChange={(e) => update(["render", "composition", "provider_prefix"], e.target.value)} /></label><label>Исключения provider<input value={render.provider_exclude_keywords.join(", ")} onChange={(e) => update(["render", "provider_exclude_keywords"], e.target.value.split(",").map((v) => v.trim()).filter(Boolean))} placeholder="test, expired" /></label></section></>}
    </div>
    <div className="preview-toolbar"><label>Пользователь для preview<select value={previewUser} onChange={(e) => setPreviewUser(e.target.value)}><option value="">первый доступный</option>{users.map((user) => <option key={user.name} value={user.name}>{user.name}</option>)}</select></label><div className="modal-actions"><button className="button ghost" disabled={working} onClick={previewForm}><span className="button-icon">◉</span>Preview</button><button className="button ghost" disabled={working} onClick={saveDraft}><span className="button-icon">▣</span>Сохранить черновик</button><button className="button primary" disabled={working} onClick={publish}><span className="button-icon">✓</span>{providerTab ? "Сохранить и обновить" : "Publish и render all"}</button></div></div>
    {preview && <div className="preview-grid"><div><h3>Итоговый YAML · секреты скрыты</h3><pre>{preview.yaml || "Нет пользователя для preview"}</pre></div><div><h3>Raw-структура</h3><pre>{JSON.stringify(preview.raw, null, 2)}</pre></div></div>}
    {!providerTab && <div className="versions"><h3>История версий</h3>{(settings?.versions || []).length === 0 ? <p className="muted">Опубликованных версий пока нет.</p> : settings.versions.map((version) => <div className="version-row" key={version.version}><span>v{version.version}</span><time>{formatTime(version.published_at)}</time>{version.rolled_back_from && <small>rollback из v{version.rolled_back_from}</small>}<button className="button small ghost" disabled={working} onClick={() => rollback(version.version)}><span className="button-icon">↶</span>Откатить</button></div>)}</div>}
  </div></div>;
}

function defaultSettings() {
  return { provider: { upstream_url: "", refresh_interval_seconds: 900 }, render: { profile_update_interval_seconds: 3600, provider_update_interval_seconds: 900, healthcheck_url: "https://www.gstatic.com/generate_204", healthcheck_interval_seconds: 15, healthcheck_timeout_milliseconds: 3000, healthcheck_max_failed_times: 2, healthcheck_tolerance_milliseconds: 50, healthcheck_lazy: true, provider_exclude_keywords: [], composition: { include_private: true, include_provider: true, provider_first: false, private_prefix: "PRIVATE | ", provider_prefix: "PROVIDER | " } } };
}

createRoot(document.getElementById("root")).render(<App />);
