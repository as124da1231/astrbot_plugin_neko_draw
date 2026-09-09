const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const icon = (name, size = 18) => `<svg class="icon icon-${name}" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

const state = {
  bridge: null,
  history: [],
  stats: null,
  page: 1,
  pageSize: 10,
  total: 0,
  schema: {},
  draft: {},
  dirty: false,
  configCategory: "api",
};

const CATEGORIES = [
  { key: "api", title: "API 密钥", description: "分别管理 WaveSpeed、RunningHub 与 OpenAI 兼容服务的访问凭据。", groups: [
    ["WaveSpeed", ["api_key", "base_url"]], ["RunningHub", ["runninghub_api_key", "runninghub_base_url"]], ["OpenAI 兼容", ["openapi_api_key", "openapi_base_url"]],
  ]},
  { key: "model", title: "模型模板", description: "模板类型决定使用哪一组 API URL 与 Key；可通过 --model 模板名切换具体模型。", groups: [
    ["默认模型", ["default_text_model", "default_edit_model"]], ["模型模板列表", ["model_templates"]],
  ]},
  { key: "prompt", title: "预设提示词", description: "每个预设对应一个触发词；模板中的 {{user_text}} 会被替换为用户输入，也可以附带风格参考图。", groups: [
    ["触发词预设", ["prompt"]],
  ]},
  { key: "send", title: "发送增强", description: "生成提示、APNG 包装、图片金句和合并转发均可独立开关。", groups: [
    ["生成中提示", ["enable_drawing_message", "drawing_message"]],
    ["APNG 动图包装", ["enable_apng_wrap", "apng_first_frame_path", "apng_first_frame_duration", "apng_second_frame_duration", "apng_loop", "apng_optimize"]],
    ["图片外显金句", ["enable_image_summary", "image_summary_quotes", "image_summary_quotes_files"]],
    ["合并转发", ["enable_forward_message", "forward_node_name"]], ["其他", ["enable_at_sender"]],
  ]},
  { key: "limit", title: "限流与白名单", description: "限流按用户维度统计并支持多规则；启用插件白名单后，仅名单中的用户或群组可使用绘图。", groups: [
    ["限流设置", ["enable_rate_limit", "rate_limit_rules", "rate_limit_whitelist", "rate_limit_message"]],
    ["白名单设置", ["whitelist_enabled", "group_whitelist", "user_whitelist"]],
  ]},
  { key: "network", title: "网络与超时", description: "调整代理、并发、重试和任务轮询策略。", groups: [
    ["网络", ["proxy"]], ["并发与重试", ["max_concurrency", "max_429_retries", "retry_429_delay"]], ["超时与轮询", ["timeout", "poll_interval"]],
  ]},
];

const PROVIDERS = {
  seedream_text: "wavespeed", seedream_edit: "wavespeed",
  runninghub_text: "runninghub", runninghub_edit: "runninghub",
  openapi_text: "openapi", openapi_edit: "openapi",
};

function toast(message, type = "success") {
  let stack = $(".toast-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.className = "toast-stack";
    document.body.append(stack);
  }
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  stack.append(item);
  setTimeout(() => item.remove(), 3600);
}

function safeStoreGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function safeStoreSet(key, value) { try { localStorage.setItem(key, value); } catch { /* sandbox may deny storage */ } }
function clone(value) { return JSON.parse(JSON.stringify(value ?? null)); }
function text(value) { return value === null || value === undefined || value === "" ? "—" : String(value); }

function unwrap(response) {
  let value = response;
  for (let i = 0; i < 3; i += 1) {
    if (!value || typeof value !== "object") break;
    if (value.status === "error" || value.error) throw new Error(value.message || value.error || "请求失败");
    if (!("data" in value)) break;
    value = value.data;
  }
  return value;
}

async function apiGet(path, params) {
  if (!state.bridge) throw new Error("AstrBot 页面桥接未连接");
  return unwrap(await state.bridge.apiGet(path, params));
}

async function apiPost(path, body) {
  if (!state.bridge) throw new Error("AstrBot 页面桥接未连接");
  return unwrap(await state.bridge.apiPost(path, body));
}

function showPage(page) {
  $$(".page").forEach(node => node.classList.toggle("is-active", node.id === `page-${page}`));
  $$(".nav-item").forEach(node => node.classList.toggle("is-active", node.dataset.page === page));
  $("#crumb").textContent = page === "history" ? "生成历史" : "插件配置";
  $("#sidebar").classList.remove("is-open");
  if (page === "config" && !Object.keys(state.schema).length) loadConfig();
}

function applyTheme(theme) {
  const names = { atelier: "奶油画室", midnight: "午夜霓虹", forest: "森林实验室" };
  const selected = names[theme] ? theme : "atelier";
  document.documentElement.dataset.theme = selected;
  $("#theme-label").textContent = names[selected];
  $$(".theme-option").forEach(node => node.classList.toggle("is-active", node.dataset.themeValue === selected));
  safeStoreSet("neko-draw-theme", selected);
}

function initChrome(context = {}) {
  const saved = safeStoreGet("neko-draw-theme");
  applyTheme(saved || (context.isDark ? "midnight" : "atelier"));
  $$(".nav-item").forEach(node => node.addEventListener("click", () => showPage(node.dataset.page)));
  $("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("is-open"));
  $("#theme-trigger").addEventListener("click", event => {
    event.stopPropagation();
    const open = $("#theme-menu").classList.toggle("is-open");
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  $$(".theme-option").forEach(node => node.addEventListener("click", () => {
    applyTheme(node.dataset.themeValue);
    $("#theme-menu").classList.remove("is-open");
    $("#theme-trigger").setAttribute("aria-expanded", "false");
  }));
  document.addEventListener("click", () => $("#theme-menu").classList.remove("is-open"));
}

function formatTime(timestamp) {
  if (!timestamp) return "—";
  return new Date(Number(timestamp) * 1000).toLocaleString("zh-CN", { hour12: false });
}

function formatDuration(ms) {
  const value = Number(ms || 0);
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
}

function renderStats() {
  const stats = state.stats || { total: 0, success: 0, failed: 0 };
  const rate = stats.total ? `${((stats.success / stats.total) * 100).toFixed(1)}%` : "—";
  const values = { total: stats.total || 0, success: stats.success || 0, failed: stats.failed || 0, rate };
  Object.entries(values).forEach(([key, value]) => { const node = $(`[data-stat="${key}"]`); if (node) node.textContent = value; });
}

function extensionForMime(mime = "") {
  return { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif" }[mime] || "png";
}

async function downloadHistoryImage(item, index = 0) {
  try {
    const result = await apiGet(`history/image/${item.id}/${index}`);
    if (!result?.data_url) throw new Error("图片数据为空");
    const link = document.createElement("a");
    link.href = result.data_url;
    link.download = `neko-draw-${item.id}-${index + 1}.${extensionForMime(result.mime)}`;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    toast("图片下载已开始");
  } catch (error) {
    toast(`下载失败：${error.message}`, "error");
  }
}

function renderHistory() {
  const body = $("#history-body");
  body.replaceChildren();
  $("#history-count").textContent = `${state.total} 条`;
  $("#history-empty").classList.toggle("is-visible", state.history.length === 0);
  state.history.forEach(item => {
    const row = document.createElement("tr");
    const timeCell = document.createElement("td");
    timeCell.innerHTML = `<span class="cell-main"></span><small class="cell-sub"></small>`;
    $(".cell-main", timeCell).textContent = formatTime(item.timestamp);
    $(".cell-sub", timeCell).textContent = `#${item.id} · 用户 ${text(item.user_id)}`;

    const promptCell = document.createElement("td");
    promptCell.innerHTML = `<span class="cell-main"></span><small class="cell-sub"></small>`;
    $(".cell-main", promptCell).textContent = text(item.prompt);
    $(".cell-main", promptCell).title = text(item.prompt);
    $(".cell-sub", promptCell).textContent = item.trigger_word ? `触发词 · ${item.trigger_word}` : "自由提示词";

    const modelCell = document.createElement("td");
    modelCell.innerHTML = `<span class="cell-main"></span><small class="cell-sub"></small>`;
    $(".cell-main", modelCell).textContent = text(item.model_template);
    $(".cell-sub", modelCell).textContent = text(item.provider);

    const durationCell = document.createElement("td");
    durationCell.textContent = formatDuration(item.generation_time_ms);
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `tag ${item.status === "success" ? "success" : "failed"}`;
    badge.textContent = item.status === "success" ? "成功" : "失败";
    statusCell.append(badge);

    const actionCell = document.createElement("td");
    actionCell.className = "right";
    actionCell.innerHTML = `<div class="row-actions"><button class="view-row" title="查看详情" aria-label="查看详情">${icon("eye")}</button>${item.status === "success" && (item.image_paths || []).length ? `<button class="download-row" title="下载图片" aria-label="下载图片">${icon("download")}</button>` : ""}<button class="delete-row" title="删除记录" aria-label="删除记录">${icon("trash")}</button></div>`;
    $(".view-row", actionCell).addEventListener("click", () => openHistoryDetail(item));
    $(".download-row", actionCell)?.addEventListener("click", () => downloadHistoryImage(item));
    $(".delete-row", actionCell).addEventListener("click", async () => {
      if (await confirmAction("删除这条记录？", "删除后无法恢复，但不会影响已发送到聊天中的图片。")) {
        try { await apiPost("history/delete", { id: item.id }); toast("记录已删除"); await loadHistory(); await loadStats(); }
        catch (error) { toast(`删除失败：${error.message}`, "error"); }
      }
    });
    row.append(timeCell, promptCell, modelCell, durationCell, statusCell, actionCell);
    body.append(row);
  });
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("#page-summary").textContent = `第 ${state.page} / ${pages} 页`;
  $("#prev-page").disabled = state.page <= 1;
  $("#next-page").disabled = state.page >= pages;
}

function historyParams() {
  const values = Object.fromEntries(new FormData($("#history-filters")).entries());
  const params = { page: state.page, page_size: state.pageSize };
  ["user_id", "status"].forEach(key => { if (values[key]) params[key] = values[key]; });
  if (values.start_date) params.start_time = new Date(`${values.start_date}T00:00:00`).getTime() / 1000;
  if (values.end_date) params.end_time = new Date(`${values.end_date}T23:59:59`).getTime() / 1000;
  return params;
}

async function loadStats() {
  try { state.stats = await apiGet("history/stats"); renderStats(); }
  catch (error) { toast(`统计加载失败：${error.message}`, "error"); }
}

async function loadHistory() {
  const body = $("#history-body");
  body.innerHTML = `<tr><td colspan="6"><div class="loading-card"><span class="spinner"></span>正在整理创作记录…</div></td></tr>`;
  $("#history-empty").classList.remove("is-visible");
  try {
    const result = await apiGet("history", historyParams());
    state.history = result.items || [];
    state.total = Number(result.total || 0);
    renderHistory();
  } catch (error) {
    state.history = []; state.total = 0; renderHistory();
    toast(`历史加载失败：${error.message}`, "error");
  }
}

function makeDialog(className = "modal") {
  const dialog = document.createElement("dialog");
  dialog.className = className;
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  return dialog;
}

async function openHistoryDetail(item) {
  const dialog = makeDialog("modal");
  dialog.innerHTML = `<button class="modal-close" aria-label="关闭">${icon("close")}</button><div class="detail-layout"><h2>生成记录 #${item.id}</h2><div class="detail-image loading-card"><span class="spinner"></span>正在读取作品…</div><div class="detail-grid"></div></div>`;
  $(".modal-close", dialog).addEventListener("click", () => dialog.close());
  const grid = $(".detail-grid", dialog);
  const details = [
    ["生成时间", formatTime(item.timestamp)], ["状态", item.status === "success" ? "成功" : "失败"],
    ["用户 / 群组", `${text(item.user_id)} / ${text(item.group_id)}`], ["耗时", formatDuration(item.generation_time_ms)],
    ["模型模板", text(item.model_template)], ["提供商", text(item.provider)],
    ["模型", text(item.model)], ["参考图", `${Number(item.refer_image_count || 0)} 张`],
    ["提示词", text(item.prompt), "detail-prompt"],
  ];
  details.forEach(([label, value, extra = ""]) => {
    const node = document.createElement("div"); node.className = `detail-item ${extra}`;
    const name = document.createElement("span"); name.textContent = label;
    const content = document.createElement("strong"); content.textContent = value;
    node.append(name, content); grid.append(node);
  });
  dialog.showModal();
  const imageBox = $(".detail-image", dialog);
  if (item.status === "success" && (item.image_paths || []).length) {
    try {
      const result = await apiGet(`history/image/${item.id}/0`);
      const imageWrap = document.createElement("div"); imageWrap.className = "detail-preview";
      const image = new Image(); image.className = "detail-image"; image.alt = `生成作品 ${item.id}`; image.src = result.data_url;
      const download = document.createElement("button"); download.type = "button"; download.className = "primary-button detail-download"; download.innerHTML = `${icon("download", 17)}下载图片`;
      download.addEventListener("click", () => downloadHistoryImage(item));
      imageWrap.append(image, download); imageBox.replaceWith(imageWrap);
    } catch { imageBox.textContent = "图片暂时无法读取"; }
  } else { imageBox.textContent = item.error_message || "此记录没有图片"; }
}

function confirmAction(title, description) {
  return new Promise(resolve => {
    const dialog = makeDialog("modal confirm-modal");
    dialog.innerHTML = `<div class="confirm-icon">!</div><h2></h2><p></p><div class="modal-actions"><button class="soft-button cancel">取消</button><button class="danger-button confirm">确认</button></div>`;
    $("h2", dialog).textContent = title; $("p", dialog).textContent = description;
    $(".cancel", dialog).addEventListener("click", () => { resolve(false); dialog.close(); });
    $(".confirm", dialog).addEventListener("click", () => { resolve(true); dialog.close(); });
    dialog.addEventListener("cancel", event => { event.preventDefault(); resolve(false); dialog.close(); }, { once: true });
    dialog.showModal();
  });
}

function initHistory() {
  $("#history-filters").addEventListener("submit", event => { event.preventDefault(); state.page = 1; loadHistory(); });
  $("#reset-filters").addEventListener("click", () => { $("#history-filters").reset(); state.page = 1; loadHistory(); });
  $("#refresh-history").addEventListener("click", () => { loadHistory(); loadStats(); });
  $("#prev-page").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadHistory(); } });
  $("#next-page").addEventListener("click", () => { if (state.page * state.pageSize < state.total) { state.page += 1; loadHistory(); } });
  $("#clear-history").addEventListener("click", async () => {
    if (!(await confirmAction("清空全部历史？", "所有生成记录都将被删除，此操作无法恢复。"))) return;
    try { const result = await apiPost("history/clear", {}); state.page = 1; toast(`已清空 ${result.deleted || 0} 条记录`); await loadHistory(); await loadStats(); }
    catch (error) { toast(`清空失败：${error.message}`, "error"); }
  });
}

function setDirty(value = true) {
  state.dirty = value;
  const indicator = $("#dirty-indicator");
  if (!indicator) return;
  indicator.classList.toggle("is-dirty", value);
  $("strong", indicator).textContent = value ? "有未保存的修改" : "配置已同步";
  $("#save-config").disabled = !value;
}

function parseScalar(raw) {
  const value = String(raw).trim();
  if (value === "true") return true;
  if (value === "false") return false;
  if (value === "null") return null;
  if (value !== "" && Number.isFinite(Number(value))) return Number(value);
  if (/^[\[{]/.test(value)) { try { return JSON.parse(value); } catch { /* keep text */ } }
  return raw;
}

function fieldDefault(spec = {}) {
  if (spec.default !== undefined) return clone(spec.default);
  if (spec.type === "bool") return false;
  if (spec.type === "list" || spec.type === "template_list") return [];
  if (spec.type === "object") return {};
  if (["int", "float", "number"].includes(spec.type)) return 0;
  return "";
}

function inputField(spec, value, onChange) {
  const type = spec.type || "string";
  if (type === "bool") {
    const row = document.createElement("div"); row.className = "switch-row";
    const label = document.createElement("span"); label.className = "switch-state"; label.textContent = value ? "已启用" : "已关闭";
    const button = document.createElement("button"); button.type = "button"; button.className = `switch ${value ? "is-on" : ""}`; button.setAttribute("role", "switch"); button.setAttribute("aria-checked", String(!!value));
    button.addEventListener("click", () => { value = !value; button.classList.toggle("is-on", value); button.setAttribute("aria-checked", String(value)); label.textContent = value ? "已启用" : "已关闭"; onChange(value); });
    row.append(label, button); return row;
  }
  if (type === "list") return listField(Array.isArray(value) ? value : [], onChange);
  if (type === "object") return objectField(value && typeof value === "object" && !Array.isArray(value) ? value : {}, onChange);
  const multiline = ["text", "textarea"].includes(type) || (typeof value === "string" && value.length > 100);
  const input = document.createElement(multiline ? "textarea" : "input");
  if (!multiline) input.type = ["int", "float", "number"].includes(type) ? "number" : (spec.secret ? "password" : "text");
  if (spec.options && Array.isArray(spec.options)) {
    const select = document.createElement("select");
    spec.options.forEach(option => select.add(new Option(String(option), String(option))));
    select.value = value ?? ""; select.addEventListener("change", () => onChange(select.value)); return select;
  }
  input.value = value ?? "";
  if (spec.min !== undefined) input.min = spec.min; if (spec.max !== undefined) input.max = spec.max;
  const eventName = multiline ? "input" : "change";
  input.addEventListener(eventName, () => onChange(["int", "float", "number"].includes(type) ? Number(input.value) : input.value));
  if (spec.secret && !multiline) {
    const wrap = document.createElement("div"); wrap.className = "input-wrap";
    const reveal = document.createElement("button"); reveal.type = "button"; reveal.className = "input-action"; reveal.innerHTML = icon("eye", 17); reveal.title = "显示或隐藏";
    reveal.addEventListener("click", () => { input.type = input.type === "password" ? "text" : "password"; });
    wrap.append(input, reveal); return wrap;
  }
  return input;
}

function listField(initial, onChange) {
  const root = document.createElement("div"); root.className = "list-editor";
  let values = [...initial];
  const render = () => {
    root.replaceChildren();
    values.forEach((value, index) => {
      const row = document.createElement("div"); row.className = "list-row";
      const input = document.createElement("input"); input.value = typeof value === "string" ? value : JSON.stringify(value);
      input.addEventListener("change", () => { values[index] = parseScalar(input.value); onChange([...values]); });
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "mini-delete"; remove.innerHTML = "×";
      remove.addEventListener("click", () => { values.splice(index, 1); onChange([...values]); render(); });
      row.append(input, remove); root.append(row);
    });
    const add = document.createElement("button"); add.type = "button"; add.className = "add-row"; add.innerHTML = `${icon("plus", 15)} 添加一项`;
    add.addEventListener("click", () => { values.push(""); onChange([...values]); render(); }); root.append(add);
  };
  render(); return root;
}

function objectField(initial, onChange) {
  const root = document.createElement("div"); root.className = "object-editor";
  let entries = Object.entries(initial || {});
  const emit = () => onChange(Object.fromEntries(entries.filter(([key]) => key)));
  const render = () => {
    root.replaceChildren();
    entries.forEach(([key, value], index) => {
      const row = document.createElement("div"); row.className = "object-row";
      const keyInput = document.createElement("input"); keyInput.value = key; keyInput.placeholder = "参数名";
      const valueInput = document.createElement("input"); valueInput.value = value && typeof value === "object" ? JSON.stringify(value) : String(value ?? ""); valueInput.placeholder = "参数值";
      keyInput.addEventListener("change", () => { entries[index][0] = keyInput.value; emit(); });
      valueInput.addEventListener("change", () => { entries[index][1] = parseScalar(valueInput.value); emit(); });
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "mini-delete"; remove.textContent = "×";
      remove.addEventListener("click", () => { entries.splice(index, 1); emit(); render(); }); row.append(keyInput, valueInput, remove); root.append(row);
    });
    const add = document.createElement("button"); add.type = "button"; add.className = "add-row"; add.innerHTML = `${icon("plus", 15)} 添加参数`;
    add.addEventListener("click", () => { let key = "param"; let i = 1; const keys = new Set(entries.map(entry => entry[0])); while (keys.has(key)) key = `param${i++}`; entries.push([key, ""]); emit(); render(); }); root.append(add);
  };
  render(); return root;
}

function uploadField(spec, value, onChange, configKey) {
  const root = document.createElement("div"); root.className = "file-control";
  const input = document.createElement("input"); input.value = value || ""; input.placeholder = "文件路径"; input.addEventListener("change", () => onChange(input.value));
  const button = document.createElement("button"); button.type = "button"; button.className = "soft-button"; button.innerHTML = `${icon("upload", 16)}上传`;
  button.addEventListener("click", () => {
    const picker = $("#file-upload-picker");
    picker.accept = (spec.file_types || []).map(ext => `.${ext}`).join(",");
    picker.onchange = async () => {
      const file = picker.files?.[0]; if (!file) return;
      try {
        const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
        const result = await apiPost("config/upload_file", { config_key: configKey, filename: file.name, data });
        input.value = result.path; onChange(result.path); toast("文件上传成功");
      } catch (error) { toast(`上传失败：${error.message}`, "error"); }
      picker.value = "";
    };
    picker.click();
  });
  root.append(input, button); return root;
}

function createField(key, spec, value, onChange, nested = false) {
  const field = document.createElement("div");
  const wide = ["template_list", "list", "file", "object", "text", "textarea"].includes(spec.type);
  field.className = `field ${wide ? "is-wide" : ""}`;
  const title = document.createElement("div"); title.className = "field-title";
  const name = document.createElement("span"); name.textContent = spec.description || key;
  const code = document.createElement("span"); code.className = "field-key"; code.textContent = key;
  title.append(name, code); field.append(title);
  let control;
  if (spec.type === "template_list") control = templateListField(key, spec, Array.isArray(value) ? value : [], onChange);
  else if (spec.type === "file") control = uploadField(spec, value, onChange, key);
  else control = inputField(spec, value === undefined ? fieldDefault(spec) : value, onChange);
  field.append(control);
  if (spec.hint) { const hint = document.createElement("p"); hint.className = "field-hint"; hint.textContent = spec.hint; field.append(hint); }
  return field;
}

function templateDialogTitle(configKey, editing) {
  const action = editing ? "修改" : "新建";
  return {
    model_templates: `${action}模型模板`,
    prompt: `${action}预设提示词`,
    rate_limit_rules: `${action}限流规则`,
  }[configKey] || `${action}模板`;
}

function promptTrigger(item) {
  if (item && typeof item === "object") return String(item.trigger || "").trim();
  const raw = String(item || "").trim();
  return raw ? raw.split(/\s+/, 1)[0] : "";
}

function validateUniqueCollections(config) {
  const modelNames = (config.model_templates || []).map(item => item && typeof item === "object" ? String(item.name || "").trim() : "").filter(Boolean);
  const duplicateModel = modelNames.find((name, index) => modelNames.indexOf(name) !== index);
  if (duplicateModel) return `模型模板名称「${duplicateModel}」已存在，请使用唯一名称`;
  const triggers = (config.prompt || []).map(promptTrigger).filter(Boolean);
  const duplicateTrigger = triggers.find((trigger, index) => triggers.indexOf(trigger) !== index);
  if (duplicateTrigger) return `预设触发词「${duplicateTrigger}」已存在，请使用唯一触发词`;
  return "";
}

function openTemplateDialog(configKey, templateKey, templateSpec, initialValue, editing, validate, onSubmit, dialogOptions = {}) {
  const dialog = makeDialog("modal template-editor-modal");
  let candidate = clone(initialValue);
  let activeTemplateKey = templateKey;
  let activeTemplateSpec = templateSpec;
  if (configKey === "model_templates" && dialogOptions.deriveProvider) candidate.provider = dialogOptions.deriveProvider(activeTemplateKey);
  dialog.innerHTML = `<div class="template-modal-head"><div><span class="eyebrow">IMAGE LAB / ${editing ? "EDIT ITEM" : "NEW ITEM"}</span><h2></h2><p></p></div><button class="modal-close" type="button" aria-label="关闭">${icon("close")}</button></div><div class="template-modal-body"><div class="template-modal-error" role="alert"></div><div class="field-grid"></div></div><div class="template-modal-foot"><button class="soft-button cancel" type="button">取消</button><button class="primary-button confirm" type="button">${icon(editing ? "save" : "plus", 16)}${editing ? "保存修改" : "添加"}</button></div>`;
  $("h2", dialog).textContent = templateDialogTitle(configKey, editing);
  const subtitle = $(".template-modal-head p", dialog);
  const grid = $(".field-grid", dialog);
  const renderDialogFields = () => {
    grid.replaceChildren();
    subtitle.textContent = activeTemplateSpec?.name || activeTemplateKey;
    if (configKey === "model_templates" && dialogOptions.templates) {
      const typeField = document.createElement("div"); typeField.className = "field is-wide template-type-field";
      const title = document.createElement("div"); title.className = "field-title"; title.innerHTML = `<span>模板类型</span><span class="field-key">__template_key</span>`;
      const select = document.createElement("select");
      Object.entries(dialogOptions.templates).forEach(([key, value]) => select.add(new Option(value.name || key, key)));
      select.value = activeTemplateKey;
      select.addEventListener("change", () => {
        const preserved = { name: candidate.name || "", enabled: candidate.enabled, enabled_as_default: candidate.enabled_as_default, fallback_order: candidate.fallback_order };
        activeTemplateKey = select.value;
        activeTemplateSpec = dialogOptions.templates[activeTemplateKey];
        candidate = { ...dialogOptions.makeDefaults(activeTemplateKey), ...preserved, __template_key: activeTemplateKey, provider: dialogOptions.deriveProvider(activeTemplateKey) };
        renderDialogFields();
      });
      const hint = document.createElement("p"); hint.className = "field-hint"; hint.textContent = "使用对应的 API URL 和 Key";
      typeField.append(title, select, hint); grid.append(typeField);
    }
    Object.entries(activeTemplateSpec?.items || {}).filter(([fieldKey]) => fieldKey !== "provider").forEach(([fieldKey, fieldSpec]) => {
      grid.append(createField(fieldKey, fieldSpec, candidate[fieldKey], next => { candidate[fieldKey] = next; }, true));
    });
  };
  renderDialogFields();
  const close = () => dialog.close();
  $(".modal-close", dialog).addEventListener("click", close);
  $(".cancel", dialog).addEventListener("click", close);
  $(".confirm", dialog).addEventListener("click", () => {
    const error = validate(candidate);
    const errorBox = $(".template-modal-error", dialog);
    errorBox.textContent = error;
    errorBox.classList.toggle("is-visible", !!error);
    if (error) return;
    onSubmit(candidate); dialog.close();
  });
  dialog.showModal();
}

function templateListField(configKey, spec, initial, onChange) {
  const root = document.createElement("div"); root.className = "template-list";
  let items = clone(initial) || [];
  const templates = spec.templates || {};
  const templateKeys = Object.keys(templates);
  const deriveProvider = key => PROVIDERS[key] || (key.includes("runninghub") ? "runninghub" : key.includes("openapi") ? "openapi" : "wavespeed");
  const emit = () => onChange(clone(items));
  const itemLabel = (item, key) => key === "rate_limit_rule" ? `${item.window_seconds || 0} 秒内 ${item.max_count || 0} 次` : (item.name || item.trigger || templates[key]?.name || "未命名模板");
  const makeDefaults = key => {
    const result = { __template_key: key, provider: deriveProvider(key) };
    Object.entries(templates[key]?.items || {}).forEach(([fieldKey, fieldSpec]) => { if (fieldKey !== "provider") result[fieldKey] = fieldDefault(fieldSpec); });
    return result;
  };
  const editableItem = (item, key) => {
    if (item && typeof item === "object") return clone(item);
    const raw = String(item || "").trim();
    const [trigger = "", ...rest] = raw.split(/\s+/);
    return { ...makeDefaults(key), trigger, prompt: rest.join(" ") };
  };
  const validateCandidate = (candidate, editingIndex = -1) => {
    if (configKey === "model_templates" && !String(candidate.name || "").trim()) return "模板名称不能为空";
    if (configKey === "prompt" && !String(candidate.trigger || "").trim()) return "预设触发词不能为空";
    const nextItems = items.map((item, index) => index === editingIndex ? candidate : item);
    if (editingIndex < 0) nextItems.push(candidate);
    return validateUniqueCollections({
      model_templates: configKey === "model_templates" ? nextItems : (state.draft.model_templates || []),
      prompt: configKey === "prompt" ? nextItems : (state.draft.prompt || []),
    });
  };
  const commitAndKeepPosition = callback => {
    const scrollTop = document.scrollingElement?.scrollTop || 0;
    callback(); emit(); render();
    requestAnimationFrame(() => window.scrollTo({ top: scrollTop, behavior: "auto" }));
  };
  const render = () => {
    root.replaceChildren();
    items.forEach((item, index) => {
      const templateKey = item.__template_key || templateKeys[0] || "";
      const template = templates[templateKey] || templates[templateKeys[0]] || { items: {} };
      const details = document.createElement("div"); details.className = "template-card";
      const summary = document.createElement("div"); summary.className = "template-card-head"; summary.tabIndex = 0; summary.setAttribute("role", "button"); summary.setAttribute("aria-label", `修改${itemLabel(item, templateKey)}`);
      const label = document.createElement("span"); label.className = "template-name"; label.textContent = itemLabel(item, templateKey);
      const provider = document.createElement("span"); provider.className = "provider-chip"; provider.textContent = deriveProvider(templateKey);
      summary.append(label); if (configKey === "model_templates") summary.append(provider);
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "template-remove"; remove.title = "删除"; remove.innerHTML = icon("trash", 16);
      remove.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); commitAndKeepPosition(() => items.splice(index, 1)); }); summary.append(remove);
      const openEditor = event => {
        if (event.target.closest("select, button")) return;
        openTemplateDialog(configKey, templateKey, template, editableItem(item, templateKey), true, candidate => validateCandidate(candidate, index), candidate => commitAndKeepPosition(() => { items[index] = candidate; }), { templates, makeDefaults, deriveProvider });
      };
      summary.addEventListener("click", openEditor);
      summary.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openEditor(event); } });
      details.append(summary); root.append(details);
    });
    if (templateKeys.length) {
      const addRow = document.createElement("div"); addRow.className = "template-add";
      const addLabel = { model_templates: "添加模板", prompt: "添加预设", rate_limit_rules: "添加规则" }[configKey] || "添加模板";
      const add = document.createElement("button"); add.type = "button"; add.className = "soft-button"; add.innerHTML = `${icon("plus", 15)}${addLabel}`;
      add.addEventListener("click", () => {
        const templateKey = configKey === "model_templates" && templates.openapi_text ? "openapi_text" : templateKeys[0];
        const initialValue = makeDefaults(templateKey);
        if (configKey === "model_templates") initialValue.name = "";
        openTemplateDialog(configKey, templateKey, templates[templateKey], initialValue, false, candidate => validateCandidate(candidate), candidate => commitAndKeepPosition(() => items.push(candidate)), { templates, makeDefaults, deriveProvider });
      }); addRow.append(add); root.append(addRow);
    }
  };
  render(); return root;
}

function renderConfig() {
  const page = $("#page-config");
  page.querySelectorAll(":scope > .config-ui, :scope > .config-toolbar, :scope > .config-layout").forEach(node => node.remove());
  const ui = document.createElement("div"); ui.className = "config-ui";
  ui.innerHTML = `<div class="panel config-toolbar"><div id="dirty-indicator" class="dirty-indicator"><span></span><div><strong>配置已同步</strong><small>保存后请在 AstrBot 中重启插件</small></div></div><div><button class="soft-button" id="reload-config">${icon("refresh", 16)}重新加载</button><button class="primary-button" id="save-config" disabled>${icon("save", 16)}保存配置</button></div></div><div class="config-layout"><nav class="config-nav" aria-label="配置分类"></nav><div class="config-content"></div></div>`;
  page.append(ui);
  const nav = $(".config-nav", ui); const content = $(".config-content", ui);
  CATEGORIES.filter(category => category.groups.some(([, keys]) => keys.some(key => state.schema[key]))).forEach((category, catIndex) => {
    const button = document.createElement("button"); button.type = "button"; button.textContent = category.title; button.dataset.category = category.key;
    button.classList.toggle("is-active", state.configCategory === category.key || (!CATEGORIES.some(c => c.key === state.configCategory) && catIndex === 0));
    nav.append(button);
    const section = document.createElement("section"); section.className = `config-section ${button.classList.contains("is-active") ? "is-active" : ""}`; section.dataset.category = category.key;
    const intro = document.createElement("div"); intro.className = "section-intro"; intro.textContent = category.description; section.append(intro);
    category.groups.forEach(([title, keys]) => {
      const available = keys.filter(key => state.schema[key]); if (!available.length) return;
      const group = document.createElement("div"); group.className = "field-group";
      const heading = document.createElement("h3"); heading.className = "group-heading"; heading.textContent = title;
      const grid = document.createElement("div"); grid.className = "field-grid";
      available.forEach(key => grid.append(createField(key, state.schema[key], state.draft[key], next => { state.draft[key] = next; setDirty(true); })));
      group.append(heading, grid); section.append(group);
    });
    content.append(section);
    button.addEventListener("click", () => {
      state.configCategory = category.key;
      $$("button", nav).forEach(node => node.classList.toggle("is-active", node === button));
      $$(".config-section", content).forEach(node => node.classList.toggle("is-active", node.dataset.category === category.key));
    });
  });
  $("#reload-config").addEventListener("click", async () => { if (!state.dirty || await confirmAction("放弃未保存的修改？", "页面将重新读取当前配置。")) loadConfig(); });
  $("#save-config").addEventListener("click", saveConfig);
  setDirty(false);
}

async function loadConfig() {
  const page = $("#page-config");
  page.querySelectorAll(":scope > .config-ui, :scope > .config-toolbar, :scope > .config-layout").forEach(node => node.remove());
  const loading = document.createElement("div"); loading.className = "config-ui loading-card"; loading.innerHTML = `<span class="spinner"></span>正在读取插件配置…`; page.append(loading);
  try {
    const result = await apiGet("config"); state.schema = result.schema || {}; state.draft = clone(result.config || {}); renderConfig();
  } catch (error) { loading.textContent = `配置加载失败：${error.message}`; toast(`配置加载失败：${error.message}`, "error"); }
}

async function saveConfig() {
  const uniquenessError = validateUniqueCollections(state.draft);
  if (uniquenessError) {
    toast(uniquenessError, "error");
    return;
  }
  const button = $("#save-config"); button.disabled = true; button.innerHTML = `<span class="spinner"></span>保存中`;
  try { await apiPost("config", { config: state.draft }); setDirty(false); toast("配置已保存，重启插件后完全生效"); }
  catch (error) { setDirty(true); toast(`保存失败：${error.message}`, "error"); }
  finally { button.innerHTML = `${icon("save", 16)}保存配置`; button.disabled = !state.dirty; }
}

function addConfigHero() {
  const page = $("#page-config");
  const hero = page.querySelector(".hero");
  hero.insertAdjacentHTML("afterend", "");
  const filePicker = document.createElement("input"); filePicker.id = "file-upload-picker"; filePicker.type = "file"; filePicker.hidden = true; document.body.append(filePicker);
}

async function boot() {
  initChrome(); initHistory(); addConfigHero();
  const bridgeState = $("#bridge-state");
  try {
    if (!window.AstrBotPluginPage) throw new Error("请从 AstrBot 插件页面打开控制台");
    state.bridge = window.AstrBotPluginPage;
    const context = await state.bridge.ready();
    initChromeContext(context);
    bridgeState.className = "bridge-state is-ready"; $("span", bridgeState).textContent = "已连接";
    state.bridge.onContext?.(ctx => { if (!safeStoreGet("neko-draw-theme")) applyTheme(ctx.isDark ? "midnight" : "atelier"); });
    await Promise.all([loadHistory(), loadStats()]);
  } catch (error) {
    bridgeState.className = "bridge-state is-error"; $("span", bridgeState).textContent = "连接失败";
    toast(error.message, "error"); state.history = []; state.total = 0; renderHistory(); renderStats();
  }
}

function initChromeContext(context) {
  if (!safeStoreGet("neko-draw-theme")) applyTheme(context?.isDark ? "midnight" : "atelier");
}

window.addEventListener("beforeunload", event => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
boot();
