import { inferredProtocol, recommendedModelConfig } from "./model-profiles.js?v=3.0.1";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const icon = (name, size = 18) => `<svg class="icon icon-${name}" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const PAGE_SIZE_KEY = "neko.draw.page.size";

function readPageSize() {
  try {
    const saved = Number(window.localStorage.getItem(PAGE_SIZE_KEY));
    return PAGE_SIZE_OPTIONS.includes(saved) ? saved : 10;
  } catch { return 10; }
}

const state = {
  bridge: null,
  bridgePromise: null,
  history: [],
  historyLayout: "table",
  stats: null,
  page: 1,
  pageSize: readPageSize(),
  total: 0,
  apngHistory: [],
  apngPage: 1,
  apngTotal: 0,
  astrbotProviders: [],
  providerModels: {},
  presetParamRoles: {},
  selectedProvider: 0,
  schema: {},
  draft: {},
  dirty: false,
  configCategory: "model",
  commandPresets: { expand: [], extract: [] },
  commandLoaded: { expand: false, extract: false },
  commandBase: { expand: [], extract: [] },
  commandProviders: [],
  configRetried: false,
};

const CATEGORIES = [
  { key: "model", title: "默认模型选择", description: "文生图与图片编辑分别选择默认模型；既可使用插件内启用的模型，也可直接选择 AstrBot 已有模型。", groups: [
    ["默认模型", ["default_text_model_v2", "default_edit_model_v2"]],
  ]},
  { key: "prompt", title: "预设提示词", description: "每个预设对应一个触发词；模板中的 {{user_text}} 会被替换为用户输入，也可以附带风格参考图。", groups: [
    ["触发词预设", ["prompt"]],
  ]},
  { key: "comfyui", title: "ComfyUI 工作流", description: "上传 RunningHub 的 API 格式工作流 JSON，自动识别参数并确认用途；用触发词或 --model 调用，工作流复用 RunningHub 提供商的连接。", groups: [
    ["工作流列表", ["comfyui_workflows"]],
  ]},
  { key: "send", title: "生图设置", description: "独立管理模型生图的提示、金句、转发方式、默认首帧和 @ 触发者。", groups: [
    ["生成中提示", ["enable_drawing_message", "drawing_message"]],
    ["生图外显金句", ["enable_image_summary", "image_summary_quotes", "image_summary_quotes_files"]],
    ["生图合并转发", ["enable_forward_message", "forward_node_name", "enable_at_sender"]],
    ["生图默认首帧", ["enable_apng_wrap", "drawing_first_frame_path", "drawing_first_frame_duration", "drawing_second_frame_duration", "drawing_apng_loop", "drawing_apng_optimize"]],
    ["生图历史与清理", ["drawing_cleanup_after_send", "drawing_history_limit"]],
  ]},
  { key: "apng", title: "APNG 设置", description: "独立管理 APNG 指令的金句、转发方式、默认首帧、制作限制和文件清理。", groups: [
    ["生成中提示", ["apng_enable_drawing_message", "apng_drawing_message"]],
    ["APNG 外显金句", ["apng_enable_image_summary", "apng_image_summary_quotes", "apng_image_summary_quotes_files"]],
    ["APNG 合并转发", ["apng_enable_forward_message", "apng_forward_node_name", "apng_enable_at_sender"]],
    ["APNG 默认首帧", ["apng_first_frame_path", "apng_first_frame_duration"]],
    ["动画播放与优化", ["apng_default_interval_seconds", "apng_loop", "apng_optimize"]],
    ["多图指令制作", ["apng_maker_min_frames", "apng_maker_max_frames", "apng_maker_max_dimension"]],
    ["APNG 历史与清理", ["apng_cleanup_after_send", "apng_history_limit"]],
  ]},
  { key: "limit", title: "限流与白名单", description: "限流按用户维度统计并支持多规则；启用插件白名单后，仅名单中的用户或群组可使用绘图。", groups: [
    ["限流设置", ["enable_rate_limit", "rate_limit_rules", "rate_limit_whitelist", "rate_limit_message"]],
    ["白名单设置", ["whitelist_enabled", "group_whitelist", "user_whitelist"]],
  ]},
  { key: "network", title: "网络与超时", description: "调整代理、并发、重试和任务轮询策略。", groups: [
    ["网络", ["proxy"]], ["并发与重试", ["max_concurrency", "max_429_retries", "retry_429_delay", "download_retries", "download_retry_delay", "download_retry_backoff"]], ["超时与轮询", ["timeout", "poll_interval"]],
  ]},
];

const PROVIDER_DEFAULT_URLS = {
  WaveSpeed: "https://api.wavespeed.ai/api/v3",
  RunningHub: "https://www.runninghub.cn/openapi/v2",
  OpenAI: "https://api.openai.com/v1",
  AstrBot: "",
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

function clone(value) { return JSON.parse(JSON.stringify(value ?? null)); }
function text(value) { return value === null || value === undefined || value === "" ? "—" : String(value); }

// 放进 innerHTML 的用户数据必须先转义：提供商名、模型名、工作流字段名、错误信息都是用户可填的
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(/[&<>"']/g, ch => ESCAPES[ch]);
}

// 复制到剪贴板：优先用异步剪贴板 API，不可用（非 https、旧 WebView）时退回 execCommand
async function copyText(value) {
  const content = String(value ?? "");
  if (!content) { toast("没有可复制的内容", "error"); return false; }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(content);
      return true;
    }
  } catch { /* 落到下面的兜底方案 */ }
  try {
    const area = document.createElement("textarea");
    area.value = content;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

function unwrap(response) {  let value = response;
  for (let i = 0; i < 3; i += 1) {
    if (!value || typeof value !== "object") break;
    if (value.status === "error" || value.error) throw new Error(value.message || value.error || "请求失败");
    if (!("data" in value)) break;
    value = value.data;
  }
  return value;
}

function connectBridge() {
  if (state.bridgePromise) return state.bridgePromise;
  state.bridgePromise = (async () => {
    for (let attempt = 0; attempt < 100 && !window.AstrBotPluginPage; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    if (!window.AstrBotPluginPage) throw new Error("AstrBot 页面桥接加载超时，请刷新插件页面");
    state.bridge = window.AstrBotPluginPage;
    return (await Promise.race([
      state.bridge.ready(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("AstrBot 页面桥接初始化超时，请确认从插件详情页打开并更新 AstrBot")), 12000)),
    ])) || {};
  })();
  return state.bridgePromise;
}

async function apiGet(path, params) {
  await connectBridge();
  return unwrap(await state.bridge.apiGet(path, params));
}

async function apiPost(path, body) {
  await connectBridge();
  return unwrap(await state.bridge.apiPost(path, body));
}

function showPage(page) {
  state.pageScroll ||= {};
  const previousPage = $(".nav-item.is-active")?.dataset.page;
  if (previousPage) state.pageScroll[previousPage] = window.scrollY;
  $$(".page").forEach(node => node.classList.toggle("is-active", node.id === `page-${page}`));
  $$(".nav-item").forEach(node => node.classList.toggle("is-active", node.dataset.page === page));
  $("#crumb").textContent = { providers: "模型提供商", config: "插件配置", history: "生成历史", apng: "APNG 作品", textlab: "文字加工" }[page] || "工作空间";
  $("#sidebar").classList.remove("is-open");
  $("#sidebar-backdrop")?.classList.remove("is-visible");
  if (page === "config" && !$("#page-config .config-ui")) {
    if (Object.keys(state.schema).length) renderConfig(); else loadConfig();
  } else if (page === "config" && Object.keys(state.schema).length) {
    renderConfig();
  }
  if (page === "providers" && !$("#page-providers .providers-ui")) {
    if (Object.keys(state.schema).length) renderProvidersPage(); else loadProvidersPage();
  }
  if (page === "apng") loadApngHistory();
  if (page === "textlab") {
    setLabSection(state.labSection || "command");
    // 每次进入都重新读一遍：群里改过的指令能立刻看到；编辑中的草稿不会被冲掉
    if (!state.commandLoaded.expand && !draftDirty(state.commandDraft)) loadCommandPage();
  }
  requestAnimationFrame(() => window.scrollTo(0, state.pageScroll[page] || 0));
}

function keepViewPosition(action) {
  const top = window.scrollY;
  const modal = document.querySelector("dialog[open] .template-modal-body");
  const modalTop = modal?.scrollTop || 0;
  action();
  requestAnimationFrame(() => {
    window.scrollTo(0, top);
    if (modal) modal.scrollTop = modalTop;
  });
}

function configuredImageChoices(mode = "") {
  const rows = [];
  (state.draft.image_providers || []).forEach(provider => {
    if (!provider || provider.enabled === false) return;
    (provider.models || []).forEach(model => {
      if (!model || model.enabled === false || (mode && (model.mode || "text") !== mode)) return;
      const alias = String(model.name || model.model || "").trim();
      if (alias) rows.push({ value: alias, label: `${provider.name} / ${alias}`, group: "猫娘画图" });
    });
  });
  (state.draft.comfyui_workflows || []).forEach(item => {
    if (!item || item.enabled === false) return;
    const itemMode = String(item.refer_field || "").trim() ? "edit" : "text";
    if (mode && itemMode !== mode) return;
    const alias = String(item.name || "").trim();
    if (alias) rows.push({ value: alias, label: `${item.provider || "ComfyUI"} / ${alias}`, group: "猫娘画图" });
  });
  // AstrBot 的对话模型目录不是图片生成能力声明，不再无条件混入生图下拉。
  return rows;
}

function applyTheme(theme) {
  const names = { atelier: "奶油画室", midnight: "午夜霓虹", forest: "森林实验室" };
  const selected = names[theme] ? theme : "forest";
  document.documentElement.dataset.theme = selected;
  $("#theme-label").textContent = names[selected];
  $$(".theme-option").forEach(node => node.classList.toggle("is-active", node.dataset.themeValue === selected));
}

function initChrome(context = {}) {
  applyTheme("forest");
  initLabChrome();
  $$(".nav-item").forEach(node => node.addEventListener("click", () => showPage(node.dataset.page)));
  const setSidebar = open => {
    $("#sidebar").classList.toggle("is-open", open);
    $("#sidebar-backdrop")?.classList.toggle("is-visible", open);
    $("#nav-launcher")?.setAttribute("aria-expanded", String(open));
  };
  const toggleSidebar = () => setSidebar(!$("#sidebar").classList.contains("is-open"));
  $("#mobile-menu").addEventListener("click", toggleSidebar);
  $("#nav-launcher")?.addEventListener("click", toggleSidebar);
  $("#sidebar-backdrop")?.addEventListener("click", () => setSidebar(false));
  document.addEventListener("click", event => {
    $$(".config-nav.is-open").forEach(nav => {
      const launcher = nav.parentElement?.querySelector(".category-launcher");
      if (!nav.contains(event.target) && !(launcher && launcher.contains(event.target))) {
        nav.classList.remove("is-open");
        launcher?.setAttribute("aria-expanded", "false");
      }
    });
  });
  $("#theme-trigger").addEventListener("click", event => {
    event.stopPropagation();
    const open = $("#theme-menu").classList.toggle("is-open");
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  $$(".theme-option").forEach(node => node.addEventListener("click", () => {
    const selected = node.dataset.themeValue;
    $("#theme-menu").classList.remove("is-open");
    $("#theme-trigger").setAttribute("aria-expanded", "false");
    applyTheme(selected);
  }));
  document.addEventListener("click", () => $("#theme-menu").classList.remove("is-open"));
  $("#global-save").addEventListener("click", event => saveConfig(event.currentTarget));
  $("#global-reload").addEventListener("click", async () => {
    if ((state.dirty || draftDirty(state.commandDraft)) && !await confirmAction("放弃未保存的修改？", "页面将重新读取当前配置。")) return;
    try {
      await fetchConfigData();
      state.dirty = false;
      const active = $(".nav-item.is-active")?.dataset.page;
      if (active === "providers") renderProvidersPage();
      if (active === "config") renderConfig();
      if (active === "textlab") {
        state.commandDraft = null; state.commandOpenKey = "";
        state.commandLoaded = { expand: false, extract: false };
        await loadCommandPage(); setLabSection(state.labSection);
      }
      setDirty(false);
      toast("配置已重新加载");
    } catch (error) { toast(`重新加载失败：${readableError(error)}`, "error"); }
  });
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
  // 排队中的记录还没出结果，不算进成功率的分母，否则成功率会被拉低
  const settled = Math.max(0, (stats.total || 0) - (stats.pending || 0));
  const rate = settled ? `${((stats.success / settled) * 100).toFixed(1)}%` : "—";
  const values = { total: stats.total || 0, success: stats.success || 0, failed: stats.failed || 0, rate };
  Object.entries(values).forEach(([key, value]) => { const node = $(`[data-stat="${key}"]`); if (node) node.textContent = value; });
  syncHistoryModelFilter();
}

// 用统计里的「按模型模板分组」填充筛选下拉，省得自己记模板名
function syncHistoryModelFilter() {
  const select = $("#history-model-filter");
  if (!select) return;
  const groups = Array.isArray(state.stats?.by_model) ? state.stats.by_model : [];
  const signature = groups.map(row => `${row.model}:${row.count}`).join("|");
  if (select.dataset.signature === signature) return;
  const current = select.value;
  select.replaceChildren();
  select.append(new Option("全部模型", ""));
  groups.forEach(row => {
    const name = text(row.model) || "（未记录模板）";
    select.append(new Option(`${name} · ${Number(row.count) || 0} 条`, text(row.model)));
  });
  select.dataset.signature = signature;
  // 如果之前选的模板已经没记录了，就退回「全部模型」，不要留一个查不到东西的选项
  select.value = [...select.options].some(option => option.value === current) ? current : "";
}

function extensionForMime(mime = "") {
  return { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif" }[mime] || "png";
}

async function downloadAsset(endpoint, filename) {
  try {
    const result = await apiGet(endpoint);
    if (!result?.data_url) throw new Error("图片数据为空");
    const link = document.createElement("a");
    link.href = result.data_url;
    link.download = `${filename}.${extensionForMime(result.mime)}`;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    toast("图片下载已开始");
  } catch (error) {
    toast(`下载失败：${readableError(error)}`, "error");
  }
}

async function downloadHistoryImage(item, index = 0) {
  return downloadAsset(`history/image/${esc(item.id)}/${index}`, `neko-draw-${esc(item.id)}-output-${index + 1}`);
}

async function openFullAsset(endpoint, title, filename, allowDownload = true) {
  const dialog = makeDialog("modal asset-viewer");
  dialog.innerHTML = `<button class="modal-close" aria-label="关闭">${icon("close")}</button><div class="detail-layout"><h2></h2><div class="detail-image loading-card"><span class="spinner"></span>正在读取原图…</div></div>`;
  $("h2", dialog).textContent = title;
  $(".modal-close", dialog).addEventListener("click", () => dialog.close()); dialog.showModal();
  try {
    const result = await apiGet(endpoint); const wrap = document.createElement("div"); wrap.className = "detail-preview";
    const image = new Image(); image.className = "detail-image"; image.src = result.data_url; image.alt = title;
    wrap.append(image);
    if (allowDownload) { const button = document.createElement("button"); button.className = "primary-button detail-download"; button.innerHTML = `${icon("download",17)}下载原图`; button.addEventListener("click", () => downloadAsset(endpoint, filename)); wrap.append(button); }
    $(".detail-image", dialog).replaceWith(wrap);
  } catch (error) { $(".detail-image", dialog).textContent = `原图读取失败：${readableError(error)}`; }
}

function renderAssetGallery(root, item, kind, count, routeBase, thumbBase, label, allowDownload = true) {
  if (!count) return;
  const section = document.createElement("section"); section.className = "asset-section";
  section.innerHTML = `<h3>${esc(label)} <small>${esc(count)} 张</small></h3><div class="asset-gallery"></div>`; root.append(section);
  const gallery = $(".asset-gallery", section);
  for (let index = 0; index < count; index += 1) {
    const button = document.createElement("button"); button.className = "asset-thumb-button"; button.innerHTML = `<span class="spinner"></span><span>${index + 1}</span>`;
    button.addEventListener("click", () => openFullAsset(`${routeBase}/${esc(item.id)}/${index}`, `${label} ${index + 1}`, `neko-draw-${esc(item.id)}-${esc(kind)}-${index + 1}`, allowDownload)); gallery.append(button);
    const thumbEndpoint = thumbBase === "history/thumbnail" ? `${thumbBase}/${esc(item.id)}/${esc(kind)}/${index}` : `${thumbBase}/${esc(item.id)}/${index}`;
    apiGet(thumbEndpoint).then(result => { const image = new Image(); image.className="asset-thumb"; image.src=result.data_url; image.alt=`${label}缩略图 ${index+1}`; button.replaceChildren(image); }).catch(() => { button.classList.add("no-thumb"); button.textContent=`${index+1} · 点击读取`; });
  }
}

function renderHistoryRows() {
  const body = $("#history-body");
  body.replaceChildren();
  $("#history-count").textContent = `${state.total} 条`;
  $("#history-empty").classList.toggle("is-visible", state.history.length === 0);
  state.history.forEach(item => {
    const row = document.createElement("tr");
    const timeCell = document.createElement("td");
    timeCell.innerHTML = `<div class="history-record-cell"><span class="history-cover">${icon("image",16)}</span><span><span class="cell-main"></span><small class="cell-sub"></small></span></div>`;
    $(".cell-main", timeCell).textContent = formatTime(item.timestamp);
    $(".cell-sub", timeCell).textContent = `#${item.id} · 用户 ${text(item.user_id)}`;
    const outputCover = (item.image_thumbnail_paths || []).findIndex(Boolean); const inputCover = (item.source_thumbnail_paths || []).findIndex(Boolean);
    const coverKind = outputCover >= 0 ? "output" : (inputCover >= 0 ? "input" : ""); const coverIndex = outputCover >= 0 ? outputCover : inputCover;
    if (coverKind) apiGet(`history/thumbnail/${esc(item.id)}/${coverKind}/${coverIndex}`).then(result => { const img=new Image(); img.src=result.data_url; img.alt="记录缩略图"; $(".history-cover",timeCell).replaceChildren(img); }).catch(()=>{});

    const promptCell = document.createElement("td");
    promptCell.innerHTML = `<span class="cell-main"></span><small class="cell-sub"></small>`;
    $(".cell-main", promptCell).textContent = text(item.prompt);
    $(".cell-main", promptCell).title = text(item.prompt);
    // 失败的记录把原因顺手带在列表里，不用逐条点开详情才知道为什么没图
    const subText = item.status !== "success" && item.error_message
      ? `失败原因 · ${item.error_message}`
      : (item.trigger_word ? `触发词 · ${item.trigger_word}` : "自由提示词");
    $(".cell-sub", promptCell).textContent = subText;
    $(".cell-sub", promptCell).title = subText;

    const durationCell = document.createElement("td");
    durationCell.textContent = formatDuration(item.generation_time_ms);
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    const pending = item.pending === true || item.status === "pending";
    badge.className = `tag ${pending ? "pending" : (item.status === "success" ? "success" : "failed")}`;
    badge.textContent = pending ? "排队中" : (item.status === "success" ? "成功" : "失败");
    statusCell.append(badge);

    const actionCell = document.createElement("td");
    actionCell.className = "right";
    actionCell.innerHTML = `<div class="row-actions"><button class="view-row" title="查看详情" aria-label="查看详情">${icon("eye")}</button>${item.task_id ? `<button class="recall-row" title="找回 / 补发到原会话" aria-label="找回补发">${icon("refresh")}</button>` : ""}${item.status === "success" && (item.image_paths || []).length ? `<button class="download-row" title="下载图片" aria-label="下载图片">${icon("download")}</button>` : ""}<button class="delete-row" title="删除记录" aria-label="删除记录">${icon("trash")}</button></div>`;
    $(".view-row", actionCell).addEventListener("click", () => openHistoryDetail(item));
    $(".recall-row", actionCell)?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      if (!(await confirmAction("找回并补发这条记录？", "插件会按任务 ID 从平台取回成品，并把图片重新发到原来生成它的会话（群 / 私聊）。"))) return;
      button.disabled = true;
      try {
        const result = await apiPost("history/recall", { id: item.id, deliver: true });
        toast(result.message || "已找回", "success");
        await loadHistory(); await loadStats();
      } catch (error) {
        toast(`找回失败：${readableError(error)}`, "error");
      } finally {
        button.disabled = false;
      }
    });
    $(".download-row", actionCell)?.addEventListener("click", () => downloadHistoryImage(item));
    $(".delete-row", actionCell).addEventListener("click", async () => {
      if (await confirmAction("删除这条记录？", "删除后无法恢复，但不会影响已发送到聊天中的图片。")) {
        try { await apiPost("history/delete", { id: item.id }); toast("记录已删除"); await loadHistory(); await loadStats(); }
        catch (error) { toast(`删除失败：${readableError(error)}`, "error"); }
      }
    });
    row.append(timeCell, promptCell, durationCell, statusCell, actionCell);
    body.append(row);
  });
  renderHistoryPagination();
}

// ---------------- 生成历史：排列方式（列表 / 一行 4 个 / 一行 6 个） ----------------
const HISTORY_LAYOUTS = ["table", "grid4", "grid6"];
const HISTORY_LAYOUT_KEY = "neko.draw.history.layout";

function readHistoryLayout() {
  try {
    const saved = window.localStorage.getItem(HISTORY_LAYOUT_KEY);
    return HISTORY_LAYOUTS.includes(saved) ? saved : "table";
  } catch { return "table"; }
}

function applyHistoryLayout() {
  const layout = HISTORY_LAYOUTS.includes(state.historyLayout) ? state.historyLayout : "table";
  const tableWrap = $("#history-table-wrap");
  const gridWrap = $("#history-grid-wrap");
  const grid = $("#history-grid");
  if (tableWrap) tableWrap.hidden = layout !== "table";
  if (gridWrap) gridWrap.hidden = layout === "table";
  if (grid) { grid.classList.toggle("is-4", layout === "grid4"); grid.classList.toggle("is-6", layout === "grid6"); }
  $$("#history-layout-switch .layout-button").forEach(button => {
    const active = button.dataset.layout === layout;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function setHistoryLayout(layout) {
  if (!HISTORY_LAYOUTS.includes(layout)) return;
  state.historyLayout = layout;
  try { window.localStorage.setItem(HISTORY_LAYOUT_KEY, layout); } catch { /* 隐私模式写不进去就只用本次会话的 */ }
  applyHistoryLayout();
  renderHistory();
}

function renderHistoryPagination() {
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("#page-summary").textContent = `第 ${state.page} / ${pages} 页`;
  $("#prev-page").disabled = state.page <= 1;
  $("#next-page").disabled = state.page >= pages;
}

function renderHistory() {
  const layout = HISTORY_LAYOUTS.includes(state.historyLayout) ? state.historyLayout : "table";
  $("#history-count").textContent = `${state.total} 条`;
  $("#history-empty").classList.toggle("is-visible", state.history.length === 0);
  if (layout === "table") renderHistoryRows(); else renderHistoryGrid();
  const gridWrap = $("#history-grid-wrap");
  if (gridWrap) gridWrap.hidden = layout === "table" || state.history.length === 0;
  renderHistoryPagination();
}

async function recallHistoryRecord(item, button) {
  if (!(await confirmAction("找回并补发这条记录？", "插件会按任务 ID 从平台取回成品，并把图片重新发到原来生成它的会话（群 / 私聊）。"))) return;
  if (button) button.disabled = true;
  try {
    const result = await apiPost("history/recall", { id: item.id, deliver: true });
    toast(result.message || "已找回", "success");
    await loadHistory(); await loadStats();
  } catch (error) {
    toast(`找回失败：${readableError(error)}`, "error");
  } finally { if (button) button.disabled = false; }
}

async function deleteHistoryRecord(item) {
  if (!(await confirmAction("删除这条记录？", "删除后无法恢复，但不会影响已发送到聊天中的图片。"))) return;
  try { await apiPost("history/delete", { id: item.id }); toast("记录已删除"); await loadHistory(); await loadStats(); }
  catch (error) { toast(`删除失败：${readableError(error)}`, "error"); }
}

function historyStatusBadge(item) {
  const pending = item.pending === true || item.status === "pending";
  const label = pending ? "排队中" : (item.status === "success" ? "成功" : "失败");
  const tone = pending ? "pending" : (item.status === "success" ? "success" : "failed");
  return `<span class="tag ${tone}">${label}</span>`;
}

function historyCoverOf(item) {
  const outputCover = (item.image_thumbnail_paths || []).findIndex(Boolean);
  const inputCover = (item.source_thumbnail_paths || []).findIndex(Boolean);
  if (outputCover >= 0) return { kind: "output", index: outputCover };
  if (inputCover >= 0) return { kind: "input", index: inputCover };
  return null;
}

function renderHistoryGrid() {
  const grid = $("#history-grid");
  if (!grid) return;
  grid.replaceChildren();
  state.history.forEach(item => {
    const card = document.createElement("article");
    card.className = "history-card";
    card.tabIndex = 0;

    const cover = document.createElement("div");
    cover.className = "history-card-cover";
    cover.innerHTML = `<span class="spinner"></span>`;
    const which = historyCoverOf(item);
    if (which) {
      apiGet(`history/thumbnail/${esc(item.id)}/${esc(which.kind)}/${which.index}`)
        .then(result => { const image = new Image(); image.src = result.data_url; image.alt = "记录缩略图"; cover.replaceChildren(image); })
        .catch(() => { cover.textContent = "无预览"; });
    } else {
      cover.textContent = "无预览";
    }

    const body = document.createElement("div");
    body.className = "history-card-body";
    const head = document.createElement("div");
    head.className = "history-card-head";
    head.innerHTML = `${historyStatusBadge(item)}<small></small>`;
    $("small", head).textContent = `#${item.id}`;
    const prompt = document.createElement("p");
    prompt.className = "history-card-prompt";
    prompt.textContent = text(item.prompt) || "（无提示词）";
    prompt.title = text(item.prompt);
    const sub = document.createElement("small");
    sub.className = "history-card-sub";
    const subText = item.status !== "success" && item.error_message
      ? `失败原因 · ${item.error_message}`
      : (item.model_template ? `${item.model_template}` : (item.trigger_word ? `触发词 · ${item.trigger_word}` : "自由提示词"));
    sub.textContent = subText;
    sub.title = subText;
    const meta = document.createElement("div");
    meta.className = "history-card-meta";
    meta.innerHTML = `<span></span><span></span>`;
    const metas = $$("span", meta);
    metas[0].textContent = formatDuration(item.generation_time_ms);
    metas[1].textContent = formatTime(item.timestamp);
    body.append(head, prompt, sub, meta);

    const actions = document.createElement("div");
    actions.className = "history-card-actions";
    // 四个位置永远都在：不适用的按钮只隐藏不删除，这样每张卡的操作列都对齐同一格
    const slot = (name, title, glyph, enabled) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `${name}-row`;
      button.title = title;
      button.setAttribute("aria-label", title);
      button.innerHTML = icon(glyph, 15);
      if (!enabled) { button.classList.add("is-empty"); button.disabled = true; button.tabIndex = -1; }
      return button;
    };
    const viewButton = slot("view", "查看详情", "eye", true);
    const recallButton = slot("recall", "找回 / 补发到原会话", "refresh", Boolean(item.task_id));
    const downloadButton = slot("download", "下载图片", "download", item.status === "success" && (item.image_paths || []).length > 0);
    const deleteButton = slot("delete", "删除记录", "trash", true);
    actions.append(viewButton, recallButton, downloadButton, deleteButton);

    const open = () => openHistoryDetail(item);
    card.append(cover, body, actions);
    card.addEventListener("click", open);
    card.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
    actions.addEventListener("click", event => event.stopPropagation());
    viewButton.addEventListener("click", open);
    if (!recallButton.disabled) recallButton.addEventListener("click", event => recallHistoryRecord(item, event.currentTarget));
    if (!downloadButton.disabled) downloadButton.addEventListener("click", () => downloadHistoryImage(item));
    deleteButton.addEventListener("click", () => deleteHistoryRecord(item));
    grid.append(card);
  });
}

function historyParams() {
  const values = Object.fromEntries(new FormData($("#history-filters")).entries());
  const params = { page: state.page, page_size: state.pageSize };
  ["user_id", "status", "model_template", "order"].forEach(key => { if (values[key]) params[key] = values[key]; });
  if (String(values.keyword || "").trim()) params.keyword = String(values.keyword).trim();
  if (values.start_date) params.start_time = new Date(`${values.start_date}T00:00:00`).getTime() / 1000;
  if (values.end_date) params.end_time = new Date(`${values.end_date}T23:59:59`).getTime() / 1000;
  return params;
}

// 快捷时间区间：直接写进表单的日期框，再走同一条查询路径，避免两套参数
function applyHistoryRange(days) {
  const form = $("#history-filters");
  if (!form) return;
  const today = new Date();
  const start = new Date(today);
  if (days > 0) start.setDate(start.getDate() - (days - 1));
  const stamp = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  form.elements.start_date.value = stamp(start);
  form.elements.end_date.value = stamp(today);
  state.page = 1;
  loadHistory();
}

async function loadStats() {
  try { state.stats = await apiGet("history/stats"); renderStats(); }
  catch (error) { toast(`统计加载失败：${readableError(error)}`, "error"); }
}

async function loadHistory() {
  const layout = HISTORY_LAYOUTS.includes(state.historyLayout) ? state.historyLayout : "table";
  const body = $("#history-body");
  const grid = $("#history-grid");
  const loading = `<div class="loading-card"><span class="spinner"></span>正在整理创作记录…</div>`;
  if (layout === "table") { if (body) body.innerHTML = `<tr><td colspan="5">${loading}</td></tr>`; }
  else if (grid) { grid.innerHTML = loading; }
  $("#history-empty").classList.remove("is-visible");
  try {
    const result = await apiGet("history", historyParams());
    state.history = result.items || [];
    state.total = Number(result.total || 0);
    renderHistory();
  } catch (error) {
    state.history = []; state.total = 0; renderHistory();
    toast(`历史加载失败：${readableError(error)}`, "error");
  }
}

function makeDialog(className = "modal") {
  const scrollTop = window.scrollY;
  const dialog = document.createElement("dialog");
  dialog.className = className;
  document.body.append(dialog);
  dialog.addEventListener("close", () => { dialog.remove(); requestAnimationFrame(() => window.scrollTo(0, scrollTop)); }, { once: true });
  return dialog;
}

async function openHistoryDetail(item) {
  const dialog = makeDialog("modal");
  dialog.innerHTML = `<button class="modal-close" aria-label="关闭">${icon("close")}</button><div class="detail-layout"><h2>生成记录 #${esc(item.id)}</h2><div class="detail-grid"></div></div>`;
  $(".modal-close", dialog).addEventListener("click", () => dialog.close());
  const grid = $(".detail-grid", dialog);
  const opts = item.options && typeof item.options === "object" ? item.options : {};
  const apngLabel = opts.apng === "on" ? "强制开启" : (opts.apng === "off" ? "强制关闭" : "跟随生图设置");
  // 优先用后端记录的预处理结局（success / fallback / disabled）；
  // 老记录没有该字段时按 decomposed / decompose_requested 推断。
  const decomposeStatus = opts.decompose_status
    || (opts.decomposed ? "success" : (opts.decompose_requested ? "fallback" : "disabled"));
  const decomposeError = opts.decompose_error ? `：${opts.decompose_error}` : "";
  const decomposeLabel = decomposeStatus === "success"
    ? "已预处理（用扩写结果出图）"
    : (decomposeStatus === "fallback"
      ? `已请求，但预处理失败${decomposeError}，已回退用原文出图`
      : "未预处理");
  // 排队中的记录单独标出来：还在等平台出图，后台拿到后会回填这条
  const pending = item.pending === true || item.status === "pending";
  // 排队中的记录耗时只是“提交用了多久”，标出来免得被当成总耗时
  const options = opts.params && typeof opts.params === "object" ? opts.params : {};
  // 只展示真正会影响出图的参数，路由内部字段藏在“生成参数”里给需要的人看
  const hiddenParams = new Set(["model", "trigger", "prompt"]);
  const paramText = Object.entries(options)
    .filter(([key, value]) => !hiddenParams.has(key) && value !== "" && value !== null && value !== undefined)
    .map(([key, value]) => `${key}=${value}`).join("，");
  const referCount = Number(item.refer_image_count || opts.refer_images || 0);
  const details = [
    ["生成时间", formatTime(item.timestamp)],
    ["状态", pending ? "排队中（后台等结果）" : (item.status === "success" ? "成功" : "失败")],
    ["用户 / 群组", `${esc(text(item.user_id))} / ${esc(text(item.group_id))}`],
    ["耗时", pending ? `${formatDuration(item.generation_time_ms)}（提交耗时）` : formatDuration(item.generation_time_ms)],
    ["模型模板", text(item.model_template)],
    ["模型提供商", text(item.provider)],
    ["模型", text(item.model)],
    ["参考图", `${referCount} 张`],
    ["任务 ID", text(item.task_id)],
    ["提示词（原始消息）", text(item.prompt), "detail-prompt"],
  ];
  // 实际发给模型的提示词优先取 resolved_prompt；老记录没有这一列时用预处理结果兜底，
  // 否则详情页只剩“原始消息”，看到底用了什么提示词就无从判断。
  const resolved = item.resolved_prompt || (opts.decomposed && opts.tags ? opts.tags : "");
  if (resolved) details.push(["实际发给模型的提示词", text(resolved), "detail-prompt"]);
  else if (opts.decompose_requested) details.push(["实际发给模型的提示词", "（旧记录未保存）", "detail-prompt"]);
  if (opts.decomposed && opts.tags && opts.tags !== resolved) details.push(["预处理结果", text(opts.tags), "detail-prompt"]);
  details.push(["生成参数", paramText || "（使用模板默认值）", paramText ? "detail-prompt" : ""]);
  details.push(["APNG 包装", `${apngLabel}${opts.apng_enabled === true ? "（已包装）" : (opts.apng_enabled === false ? "（未包装）" : "")}`]);
  details.push(["生图预处理", decomposeLabel]);
  // 失败原因以前只在“一张图都没有”时才显示，出图但下载失败会误显示成正常记录
  if (item.error_message) details.push(["失败原因", text(item.error_message), "detail-prompt"]);
  const hasImages = (item.image_paths || []).length > 0 || (item.source_image_paths || []).length > 0;
  details.forEach(([label, value, extra = ""]) => {
    const node = document.createElement("div"); node.className = `detail-item ${extra}`;
    const name = document.createElement("span"); name.textContent = label;
    const content = document.createElement("strong"); content.textContent = value;
    node.append(name, content); grid.append(node);
  });
  dialog.showModal();
  const root = $(".detail-layout", dialog);
  renderAssetGallery(root, item, "input", (item.source_image_paths || []).length, "history/source", "history/thumbnail", "输入原图");
  renderAssetGallery(root, item, "output", (item.image_paths || []).length, "history/image", "history/thumbnail", "生成图片");
  if (!hasImages) {
    const note = document.createElement("p"); note.className = "empty-note";
    note.textContent = pending ? "任务已提交，还在等平台出图；拿到结果后会自动补发到原会话，并回填这条记录。" : (item.error_message ? "这次没有生成图片。" : "此记录没有图片");
    root.append(note);
  }
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

async function viewApng(item) {
  const dialog = makeDialog("modal");
  dialog.innerHTML = `<button class="modal-close">${icon("close")}</button><div class="detail-layout"><h2>APNG 记录 #${esc(item.id)}</h2><p class="cleanup-note">APNG 成品发送后已自动清理；这里只保留制作时的输入原图。</p><div class="detail-grid"><div class="detail-item"><span>帧数</span><strong>${esc(item.frame_count)}</strong></div><div class="detail-item"><span>间隔</span><strong>${formatDuration(item.duration_ms)}</strong></div><div class="detail-item"><span>循环</span><strong>${Number(item.loop) === 0 ? "无限" : item.loop + " 次"}</strong></div><div class="detail-item"><span>制作时间</span><strong>${formatTime(item.timestamp)}</strong></div></div></div>`;
  $(".modal-close", dialog).addEventListener("click", () => dialog.close()); dialog.showModal();
  renderAssetGallery($(".detail-layout",dialog), item, "input", Number(item.source_count||0), "apng-history/source", "apng-history/thumbnail", "输入原图", false);
}

function renderApngHistory() {
  const body = $("#apng-body"); body.replaceChildren();
  $("#apng-count").textContent = `${state.apngTotal} 条`;
  $("#apng-empty").classList.toggle("is-visible", state.apngHistory.length === 0);
  state.apngHistory.forEach(item => {
    const row = document.createElement("tr");
    row.innerHTML = `<td><div class="history-record-cell"><span class="history-cover">${icon("image",16)}</span><span><span class="cell-main"></span><small class="cell-sub"></small></span></div></td><td>${esc(item.frame_count)} 帧</td><td>${formatDuration(item.duration_ms)}</td><td>${Number(item.loop) === 0 ? "无限" : item.loop + " 次"}</td><td>${Number(item.source_count||0)} 张</td><td class="right"><div class="row-actions"><button class="view-row" title="查看原图">${icon("eye")}</button><button class="delete-row" title="删除">${icon("trash")}</button></div></td>`;
    $(".cell-main", row).textContent = formatTime(item.timestamp); $(".cell-sub", row).textContent = `#${item.id} · 用户 ${text(item.user_id)}`;
    const coverIndex = (item.source_thumbnail_paths || []).findIndex(Boolean);
    if (coverIndex >= 0) apiGet(`apng-history/thumbnail/${esc(item.id)}/${coverIndex}`).then(result => { const img=new Image(); img.src=result.data_url; img.alt="原图缩略图"; $(".history-cover",row).replaceChildren(img); }).catch(()=>{});
    $(".view-row", row).addEventListener("click", () => viewApng(item));
    $(".delete-row", row).addEventListener("click", async () => {
      if (!await confirmAction("删除这条 APNG 记录？", "记录、全部输入原图副本和缩略图都会彻底删除，已发送到 QQ 的消息不受影响。")) return;
      try { await apiPost("apng-history/delete", { id: item.id }); toast("APNG 已删除"); loadApngHistory(); }
      catch (error) { toast(`删除失败：${readableError(error)}`, "error"); }
    });
    body.append(row);
  });
  const pages = Math.max(1, Math.ceil(state.apngTotal / state.pageSize));
  $("#apng-page-summary").textContent = `第 ${state.apngPage} / ${pages} 页`;
  $("#apng-prev").disabled = state.apngPage <= 1; $("#apng-next").disabled = state.apngPage >= pages;
}

async function loadApngHistory() {
  $("#apng-body").innerHTML = `<tr><td colspan="6"><div class="loading-card"><span class="spinner"></span>正在读取 APNG 作品…</div></td></tr>`;
  try {
    const result = await apiGet("apng-history", { page: state.apngPage, page_size: state.pageSize });
    state.apngHistory = result.items || []; state.apngTotal = Number(result.total || 0); renderApngHistory();
  } catch (error) { state.apngHistory = []; state.apngTotal = 0; renderApngHistory(); toast(`APNG 记录加载失败：${readableError(error)}`, "error"); }
}

function initApngHistory() {
  if (!$("#refresh-apng")) return;
  $("#refresh-apng").addEventListener("click", loadApngHistory);
  $("#apng-prev").addEventListener("click", () => { if (state.apngPage > 1) { state.apngPage -= 1; loadApngHistory(); } });
  $("#apng-next").addEventListener("click", () => { if (state.apngPage * state.pageSize < state.apngTotal) { state.apngPage += 1; loadApngHistory(); } });
  $("#clear-apng").addEventListener("click", async () => {
    if (!await confirmAction("清空全部 APNG？", "所有独立 APNG 记录及服务器文件都会被删除。")) return;
    try { const result = await apiPost("apng-history/clear", {}); state.apngPage = 1; toast(`已清理 ${result.deleted || 0} 个作品`); loadApngHistory(); }
    catch (error) { toast(`清理失败：${readableError(error)}`, "error"); }
  });
}

// 面向用户的错误只保留可读部分：去掉 HTTP 状态码、错误码和括号里的技术细节
// 侧栏版本号：由后端 config 接口顺带返回
function showVersion(version) {
  const slot = $("#sidebar-version");
  const value = String(version || "").trim();
  if (!slot || !value) return;
  slot.textContent = `已连接 · v${value}`;
}

function readableError(error) {
  const raw = String((error && error.message) || error || "").trim();
  const cleaned = raw
    .replace(/\b(?:HTTP|status|code|error)[\s:=]*\d{3,5}\b/gi, "")
    .replace(/\b[45]\d{2}\b/g, "")
    .replace(/[(（][^)）]{0,40}\d{3,5}[^)）]{0,40}[)）]/g, "")
    .replace(/[\s:：，,、-]{2,}/g, " ")
    .replace(/^[\s:：，,、-]+|[\s:：，,、-]+$/g, "")
    .trim();
  return cleaned || "请稍后重试";
}

function confirmAction(title, description) {
  return new Promise(resolve => {
    document.querySelectorAll("dialog.confirm-modal").forEach(node => node.close());
    const dialog = makeDialog("modal confirm-modal");
    dialog.innerHTML = `<div class="confirm-icon">!</div><h2></h2><p></p><div class="modal-actions"><button class="soft-button cancel">取消</button><button class="danger-button confirm">确认</button></div>`;
    $("h2", dialog).textContent = title; $("p", dialog).textContent = description;
    $(".cancel", dialog).addEventListener("click", () => { resolve(false); dialog.close(); });
    const settle = value => { resolve(value); dialog.close(); };
    $(".confirm", dialog).addEventListener("click", () => settle(true));
    // Enter = 确认；取消按钮默认获得焦点，键盘用户按 Tab 或 Esc 处理
    dialog.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); settle(true); }
    });
    requestAnimationFrame(() => $(".cancel", dialog).focus());
    dialog.addEventListener("cancel", event => { event.preventDefault(); resolve(false); dialog.close(); }, { once: true });
    // 点遮罩也算取消，否则只能去找小按钮
    dialog.addEventListener("click", event => { if (event.target === dialog) { resolve(false); dialog.close(); } });
    dialog.showModal();
  });
}

function initHistory() {
  if (!$("#history-filters")) return;
  state.historyLayout = readHistoryLayout();
  applyHistoryLayout();
  $("#history-filters").addEventListener("submit", event => { event.preventDefault(); state.page = 1; loadHistory(); });
  $("#reset-filters").addEventListener("click", () => { $("#history-filters").reset(); state.page = 1; loadHistory(); });
  $$(".filter-quick [data-range]").forEach(button => {
    button.addEventListener("click", () => applyHistoryRange(Number(button.dataset.range) || 0));
  });
  $$("#history-layout-switch .layout-button").forEach(button => {
    button.addEventListener("click", () => setHistoryLayout(button.dataset.layout));
  });
  const pageSizeSelect = $("#history-page-size");
  if (pageSizeSelect) {
    pageSizeSelect.value = String(state.pageSize);
    pageSizeSelect.addEventListener("change", () => {
      const next = Number(pageSizeSelect.value);
      state.pageSize = PAGE_SIZE_OPTIONS.includes(next) ? next : 10;
      try { window.localStorage.setItem(PAGE_SIZE_KEY, String(state.pageSize)); } catch { /* 隐私模式忽略 */ }
      state.page = 1;
      loadHistory();
    });
  }
  $("#refresh-history").addEventListener("click", () => { loadHistory(); loadStats(); });
  $("#prev-page").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadHistory(); } });
  $("#next-page").addEventListener("click", () => { if (state.page * state.pageSize < state.total) { state.page += 1; loadHistory(); } });
  $("#clear-history").addEventListener("click", async () => {
    if (!(await confirmAction("清空全部历史？", "所有生成记录都将被删除，此操作无法恢复。"))) return;
    try { const result = await apiPost("history/clear", {}); state.page = 1; toast(`已清空 ${result.deleted || 0} 条记录`); await loadHistory(); await loadStats(); }
    catch (error) { toast(`清空失败：${readableError(error)}`, "error"); }
  });
}

function setDirty(value = true) {
  state.dirty = value;
  $$(".dirty-indicator").forEach(indicator => {
    indicator.classList.toggle("is-dirty", value);
    $("strong", indicator).textContent = value ? "有未保存的修改" : "配置已同步";
  });
  $$('[data-save-config]').forEach(button => { button.disabled = !value; });
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
    spec.options.forEach(option => select.add(new Option(String(spec.option_labels?.[option] || option), String(option))));
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

function modelTemplateSelectField(key, value, onChange) {
  const select = document.createElement("select");
  const imageMode = key === "default_edit_model";
  const templates = (state.draft.model_templates || []).filter(item => (
    item && typeof item === "object" && item.enabled !== false
    && item.enabled_as_default !== false
    && Boolean(String(item.refer_field || "").trim()) === imageMode
    && String(item.name || "").trim()
  ));
  if (!templates.length) select.add(new Option("没有可用模板，请先配置模型模板", ""));
  templates.forEach(item => select.add(new Option(String(item.name), String(item.name))));
  const current = String(value || "");
  if (current && !templates.some(item => String(item.name) === current)) {
    select.add(new Option(`${current}（当前不可用）`, current));
  }
  select.value = current;
  select.disabled = !templates.length && !current;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function astrbotProviderSelectField(value, onChange) {
  const select = document.createElement("select");
  const items = state.astrbotProviders || [];
  if (!items.length) select.add(new Option("没有已启用的 AstrBot 模型提供商", ""));
  items.forEach(item => {
    const detail = [item.model, item.type].filter(Boolean).join(" · ");
    select.add(new Option(detail ? `${esc(item.id)}（${detail}）` : item.id, item.id));
  });
  const current = String(value || "");
  if (current && !items.some(item => item.id === current)) {
    select.add(new Option(`${current}（当前未加载）`, current));
  }
  select.value = current;
  select.disabled = !items.length && !current;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

// 旧「拆解」用过的模型下拉：这组配置项已停用，保留函数只为兼容旧页面引用
function llmTagModelSelectField(value, onChange) {
  const select = document.createElement("select");
  const providerId = String(state.draft.llm_tag_provider_id || "");
  const provider = (state.astrbotProviders || []).find(item => item.id === providerId);
  const models = provider && Array.isArray(provider.models) ? provider.models : [];
  select.add(new Option(providerId ? "提供商默认模型" : "（先选提供商，或用当前会话默认模型）", ""));
  models.forEach(model => select.add(new Option(model, model)));
  const current = String(value || "");
  if (current && !models.includes(current)) select.add(new Option(`${current}（当前未列出）`, current));
  select.value = current;
  select.disabled = !models.length && !current;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

// 生图预处理用哪条扩写：下拉列出「文字加工 → 扩写指令编辑」里的触发词。
// 预设对话框可能比「文字加工」页更早打开，这里按需拉一次并就地刷新选项。
function expandTriggerSelectField(value, onChange) {
  const select = document.createElement("select");
  const current = String(value || "");

  const fill = () => {
    const items = (state.commandPresets && state.commandPresets.expand) || [];
    select.replaceChildren();
    select.add(new Option("跟随全局（用第一条启用的扩写指令）", ""));
    items.forEach(item => {
      const trigger = String(item && item.trigger || "");
      if (!trigger) return;
      const disabled = item.enabled === false ? "（已停用）" : "";
      select.add(new Option(`${trigger}${disabled}`, trigger));
    });
    if (current && !items.some(item => item && item.trigger === current)) {
      select.add(new Option(`${current}（已不存在）`, current));
    }
    select.value = current;
  };

  fill();
  if (!state.commandLoaded.expand) {
    apiGet("commands/presets").then(data => {
      ["expand", "extract"].forEach(section => {
        state.commandPresets[section] = Array.isArray(data[section]) ? clone(data[section]) : [];
      });
      state.commandLoaded.expand = true;
      state.commandLoaded.extract = true;
      fill();
    }).catch(() => {});
  }
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

// 扩写 / 提取的全局模型：跟随所选的 AstrBot 提供商（留空用提供商默认模型）
function sectionModelSelectField(providerKey, value, onChange) {
  const select = document.createElement("select");
  const providerId = String(state.draft[providerKey] || "");
  const provider = (state.astrbotProviders || []).find(item => item.id === providerId);
  const models = provider && Array.isArray(provider.models) ? provider.models : [];
  select.add(new Option(providerId ? "提供商默认模型" : "（先选提供商，留空则用当前会话默认模型）", ""));
  models.forEach(model => select.add(new Option(model, model)));
  const current = String(value || "");
  if (current && !models.includes(current)) select.add(new Option(`${current}（当前未列出）`, current));
  select.value = current;
  select.disabled = !models.length && !current;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function integratedDefaultSelectField(key, value, onChange) {
  const select = document.createElement("select");
  select.add(new Option("无（不设置默认模型）", ""));
  const mode = key === "default_edit_model_v2" ? "edit" : "text";
  const choices = configuredImageChoices(mode).filter(row => {
    const models = (state.draft.image_providers || []).flatMap(p => p.models || []);
    const item = models.find(m => String(m.name || m.model || "").trim() === row.value)
      || (state.draft.comfyui_workflows || []).find(w => w.name === row.value);
    return item?.enabled_as_default !== false;
  });
  /* Legacy enumeration is intentionally bypassed: only explicitly configured image models qualify.
  (state.draft.image_providers || []).forEach(provider => {
    if (!provider || provider.enabled === false) return;
    (provider.models || []).forEach(model => {
      if (!model || model.enabled === false || String(model.mode || "text") !== mode) return;
      const alias = String(model.name || model.model || "").trim();
      if (alias) choices.push({ value: alias, label: `${provider.name} / ${alias}`, group: "猫娘画图" });
    });
  });
  (state.astrbotProviders || []).forEach(provider => {
    const models = provider.models?.length ? provider.models : (provider.model ? [provider.model] : []);
    models.forEach(model => choices.push({ value: `@astrbot:${provider.id}:${model}`, label: `${provider.id} / ${model}`, group: "AstrBot 已有模型" }));
  });
  (state.draft.comfyui_workflows || []).forEach(item => {
    if (!item || item.enabled === false || item.enabled_as_default === false) return;
    // 工作流按 refer_field 判定用途：有参考图字段算图生图（edit），否则算文生图。
    const workflowMode = String(item.refer_field || "").trim() ? "edit" : "text";
    if (workflowMode !== mode) return;
    const alias = String(item.name || "").trim();
    if (alias) choices.push({ value: alias, label: `${item.provider || "ComfyUI"} / ${alias}`, group: "猫娘画图" });
  });
  */
  if (!choices.length) select.options[0].textContent = mode === "edit" ? "暂无已启用的图片编辑模型，请先到模型提供商添加" : "暂无已启用的文生图模型，请先到模型提供商添加";
  ["猫娘画图", "AstrBot 已有模型"].forEach(groupName => {
    const rows = choices.filter(item => item.group === groupName); if (!rows.length) return;
    const group = document.createElement("optgroup"); group.label = groupName;
    rows.forEach(item => group.append(new Option(item.label, item.value))); select.append(group);
  });
  const current = String(value || "");
  if (current && !choices.some(item => item.value === current)) {
    const stale = new Option(`${current}（原配置，当前未启用；可清除）`, current);
    stale.disabled = true; select.add(stale);
  }
  select.value = current; select.disabled = !choices.length && !current;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

// —— 预设提示词：默认模型选择 + 从所选模型调取参数 ——

function presetModelSelect(value, onChange) {
  const select = document.createElement("select");
  select.add(new Option("跟随全局默认模型（不指定）", ""));
  const groups = { "猫娘画图": configuredImageChoices(), "AstrBot 已有模型": [] };
  /* Legacy unfiltered provider enumeration.
  (state.draft.image_providers || []).forEach(provider => {
    if (!provider || provider.enabled === false) return;
    (provider.models || []).forEach(model => {
      if (!model || model.enabled === false) return;
      const alias = String(model.name || model.model || "").trim();
      if (alias) groups["猫娘画图"].push({ value: alias, label: `${provider.name || "模型"} / ${alias}` });
    });
  });
  (state.draft.comfyui_workflows || []).forEach(item => {
    if (!item || item.enabled === false) return;
    const alias = String(item.name || "").trim();
    if (alias) groups["猫娘画图"].push({ value: alias, label: `${item.provider || "ComfyUI"} / ${alias}` });
  });
  (state.astrbotProviders || []).forEach(provider => {
    const models = provider.models?.length ? provider.models : (provider.model ? [provider.model] : []);
    models.forEach(model => groups["AstrBot 已有模型"].push({ value: `@astrbot:${provider.id}:${model}`, label: `${provider.id} / ${model}` }));
  });
  */
  Object.entries(groups).forEach(([name, rows]) => {
    if (!rows.length) return;
    const optgroup = document.createElement("optgroup"); optgroup.label = name;
    rows.forEach(row => optgroup.append(new Option(row.label, row.value)));
    select.append(optgroup);
  });
  const current = String(value || "");
  if (current && ![...select.options].some(option => option.value === current)) select.add(new Option(`${current}（当前不可用）`, current));
  select.value = current;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function jsParamType(value) {
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return Number.isInteger(value) ? "int" : "float";
  return "text";
}

// 从所选模型反推「可调参数」清单：
// - ComfyUI 工作流：取该工作流映射里所有生效用途（role），用后端标签渲染；
// - 插件内模型：取模型自定义 params 的键；
// - AstrBot 模型：无法枚举，只能手动自定义。
function presetAvailableParams(modelValue) {
  const alias = String(modelValue || "").trim();
  if (!alias || alias.startsWith("@astrbot:")) return [];
  const workflow = (state.draft.comfyui_workflows || []).find(item => item && String(item.name || "").trim() === alias);
  if (workflow) {
    const seen = new Set(); const rows = [];
    (workflow.nodes || []).forEach(node => {
      const role = node && node.role ? String(node.role) : "";
      if (!role || role === "ignore" || role === "prompt" || role === "image" || seen.has(role)) return;
      seen.add(role);
      const meta = (state.presetParamRoles || {})[role] || {};
      rows.push({ key: role, label: meta.label || role, type: meta.type || "text" });
    });
    return rows;
  }
  let found = null;
  (state.draft.image_providers || []).forEach(provider => {
    if (!provider || provider.enabled === false) return;
    (provider.models || []).forEach(model => {
      if (model && String(model.name || model.model || "").trim() === alias) found = model;
    });
  });
  if (found) return Object.entries(found.params || {}).map(([key, value]) => ({ key, label: key, type: jsParamType(value) }));
  return [];
}

function presetParamControlType(type) {
  return ["int", "float", "bool"].includes(type) ? type : "string";
}

function presetParamsEditor(getParams, getModel, onChange) {
  const root = document.createElement("div"); root.className = "preset-params-editor";
  const rowsBox = document.createElement("div"); rowsBox.className = "preset-param-rows";
  const addBox = document.createElement("div"); addBox.className = "preset-param-add";
  root.append(rowsBox, addBox);
  const metaOf = key => (state.presetParamRoles || {})[key] || {};

  const renderRows = () => {
    const params = getParams();
    rowsBox.replaceChildren();
    const keys = Object.keys(params || {});
    if (!keys.length) {
      const empty = document.createElement("p"); empty.className = "preset-param-empty";
      empty.textContent = getModel()
        ? "尚未添加参数。点下方下拉框从该模型调取可用参数，或自定义参数。"
        : "先选择「默认模型」，即可从该模型调取可用参数。";
      rowsBox.append(empty);
      return;
    }
    keys.forEach(key => {
      const params0 = getParams();
      const row = document.createElement("div"); row.className = "preset-param-row";
      const meta = metaOf(key);
      const info = document.createElement("div"); info.className = "preset-param-info";
      info.innerHTML = `<strong>${esc(text(meta.label || key))}</strong><code>${esc(text(key))}</code>`;
      const valueBox = document.createElement("div"); valueBox.className = "preset-param-value";
      const type = meta.type || jsParamType(params0[key]);
      valueBox.append(inputField({ type: presetParamControlType(type) }, params0[key], next => { params0[key] = next; onChange(); }));
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "icon-button"; remove.title = "删除该参数"; remove.innerHTML = icon("trash", 15);
      remove.addEventListener("click", () => { delete params0[key]; onChange(); renderRows(); renderAdd(); });
      row.append(info, valueBox, remove);
      rowsBox.append(row);
    });
  };

  const renderAdd = () => {
    addBox.replaceChildren();
    const used = new Set(Object.keys(getParams() || {}));
    const options = presetAvailableParams(getModel()).filter(item => !used.has(item.key));
    const select = document.createElement("select"); select.className = "param-add-select";
    select.add(new Option(options.length ? "＋ 添加参数…" : (getModel() ? "该模型参数已全部添加" : "＋ 添加参数…"), ""));
    options.forEach(item => select.add(new Option(`${item.label}（${item.key}）`, item.key)));
    select.disabled = !options.length;
    select.addEventListener("change", () => {
      const key = select.value; if (!key) return;
      const type = (presetAvailableParams(getModel()).find(item => item.key === key) || {}).type || (metaOf(key).type) || "text";
      getParams()[key] = type === "bool" ? false : "";
      onChange(); renderRows(); renderAdd();
    });
    const custom = document.createElement("button"); custom.type = "button"; custom.className = "text-button";
    custom.innerHTML = `${icon("plus", 14)}自定义参数`;
    custom.addEventListener("click", renderCustom);
    addBox.append(select, custom);
  };

  const renderCustom = () => {
    addBox.replaceChildren();
    const keyInput = document.createElement("input"); keyInput.type = "text"; keyInput.className = "param-key-input"; keyInput.placeholder = "参数名，如 steps / cfg / lora_strength";
    const confirm = document.createElement("button"); confirm.type = "button"; confirm.className = "soft-button"; confirm.textContent = "添加";
    const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "text-button"; cancel.textContent = "取消";
    const commit = () => {
      const key = keyInput.value.trim().replace(/^--/, "").trim();
      if (key && !(key in getParams())) { getParams()[key] = ""; onChange(); }
      renderRows(); renderAdd();
    };
    confirm.addEventListener("click", commit);
    cancel.addEventListener("click", renderAdd);
    keyInput.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); commit(); } });
    addBox.append(keyInput, confirm, cancel);
    keyInput.focus();
  };

  renderRows(); renderAdd();
  return root;
}

function modelEditor(provider, initial, editing, onDone) {
  const dialog = makeDialog("modal template-editor-modal");
  const candidate = clone(initial || { name: "", model: "", mode: "text", enabled: true, enabled_as_default: true, fallback_order: 20, refer_field: "", max_refer_images: 0, min_prompt_length: 0, params: {}, custom_model: true });
  let recommendation = recommendedModelConfig(provider.base_url, candidate.model, candidate.mode);
  dialog.innerHTML = `<div class="template-modal-head"><div><span class="eyebrow">IMAGE LAB / MODEL</span><h2>${editing ? "修改模型" : "添加模型"}</h2><p>${esc(provider.name || "模型提供商")}</p></div><button class="modal-close" type="button">${icon("close")}</button></div><div class="template-modal-body"><div class="template-modal-error"></div><div class="model-recommendation"><span>可添加参数：<b></b><small>不会自动修改；点击后只补充尚未设置的推荐项</small></span><button class="soft-button restore-params" type="button">${icon("plus",15)}添加推荐参数</button></div><div class="field-grid model-editor-grid"></div></div><div class="template-modal-foot"><button class="soft-button model-test" type="button">${icon("refresh",16)}测试模型连接</button><button class="soft-button cancel" type="button">取消</button><button class="primary-button confirm" type="button">${icon("save",16)}保存模型</button></div>`;
  const grid = $(".model-editor-grid", dialog);
  const specs = {
    model: { type: "string", description: "模型 ID", hint: candidate.custom_model ? "自定义模型允许手工填写服务商模型 ID" : "从连接返回的模型列表中选择" },
    name: { type: "string", description: "显示名称", hint: "供 --model 和默认模型选择使用，必须全局唯一" },
    mode: { type: "string", description: "模型用途", options: ["text", "edit"], option_labels: { text: "文生图", edit: "图片编辑" } },
    enabled: { type: "bool", description: "启用模型" }, enabled_as_default: { type: "bool", description: "允许作为默认模型" },
    fallback_order: { type: "int", description: "回退优先级", hint: "数字越小优先级越高" },
    refer_field: { type: "string", description: "参考图字段名", hint: "文生图留空；图片编辑常见为 images" },
    max_refer_images: { type: "int", description: "参考图数量上限" }, min_prompt_length: { type: "int", description: "提示词最小长度" },
    params: { type: "object", description: "自定义请求参数", hint: "保留原有自由参数能力；OpenAI 兼容接口会自动使用 images/generations 端点" },
  };
  const renderFields = () => { grid.replaceChildren(); recommendation = recommendedModelConfig(provider.base_url, candidate.model, candidate.mode); $(".model-recommendation b", dialog).textContent = recommendation.label;
  Object.entries(specs).forEach(([fieldKey, spec]) => {
    if (fieldKey === "model" && !candidate.custom_model) {
      const field = document.createElement("div"); field.className = "field fixed-model-field"; field.dataset.configKey = fieldKey;
      field.innerHTML = `<div class="field-title"><span>已选模型</span><span class="field-key">model</span></div><div class="fixed-model-id">${esc(text(candidate.model))}</div><p class="field-hint">模型已从提供商列表选定，无需再次选择</p>`;
      grid.append(field);
      return;
    }
    const field = createField(fieldKey, spec, candidate[fieldKey], next => {
      candidate[fieldKey] = next;
      if (fieldKey === "model" && !candidate.name) candidate.name = next;
      if (fieldKey === "mode") { recommendation = recommendedModelConfig(provider.base_url, candidate.model, next); $(".model-recommendation b", dialog).textContent = recommendation.label; }
      if (fieldKey === "model" && !editing) { recommendation = recommendedModelConfig(provider.base_url, next, candidate.mode); renderFields(); }
    }, true);
    grid.append(field);
  });
  };
  renderFields();
  $(".restore-params", dialog).onclick = () => { recommendation = recommendedModelConfig(provider.base_url, candidate.model, candidate.mode); candidate.params = { ...clone(recommendation.params), ...(candidate.params || {}) }; if (!candidate.refer_field) candidate.refer_field = recommendation.refer_field; if (!candidate.max_refer_images) candidate.max_refer_images = recommendation.max_refer_images; renderFields(); toast(`已添加${recommendation.label}推荐参数`); };
  const close = () => dialog.close(); $(".modal-close", dialog).onclick = close; $(".cancel", dialog).onclick = close;
  $(".model-test", dialog).onclick = async event => {
    const button = event.currentTarget; button.disabled = true;
    const status = $(".template-modal-head p", dialog);
    try { const result = await apiPost("models/test", { provider, model: candidate }); status.textContent = `✓ 测试成功 · ${result.message || "模型连接正常"}`; status.className = "model-dialog-status is-success"; }
    catch (error) { status.textContent = `✕ 模型测试失败：${readableError(error)}`; status.className = "model-dialog-status is-error"; } finally { button.disabled = false; }
  };
  $(".confirm", dialog).onclick = () => {
    const error = !String(candidate.model || "").trim() ? "请选择或填写模型 ID" : !String(candidate.name || "").trim() ? "请填写显示名称" : "";
    const box = $(".template-modal-error", dialog); box.textContent = error; box.classList.toggle("is-visible", !!error); if (error) return;
    onDone(candidate); dialog.close();
  };
  dialog.showModal();
}

function providerEditor(initial, editing, onDone) {
  const dialog = makeDialog("modal template-editor-modal provider-editor-modal");
  const candidate = clone(initial || { __template_key: "image_provider", name: "", source: "custom", api_key: "", api_keys: [], base_url: "https://api.openai.com/v1", astrbot_provider_id: "", enabled: true, models: [] });
  dialog.innerHTML = `<div class="template-modal-head"><div><span class="eyebrow">IMAGE LAB / PROVIDER</span><h2>${editing ? "修改模型提供商" : "新建模型提供商"}</h2><p>连接与模型集中配置</p></div><button class="modal-close" type="button">${icon("close")}</button></div><div class="template-modal-body"><div class="template-modal-error"></div><div class="field-grid provider-fields"></div><div class="provider-model-section"><div class="provider-model-toolbar"><div><h3>模型</h3><small class="provider-status">连接后获取可使用的生图模型</small></div><button class="soft-button add-model" type="button">${icon("plus",16)}自定义模型</button></div><div class="integrated-model-list"></div></div></div><div class="template-modal-foot"><button class="soft-button provider-fetch" type="button">${icon("refresh",16)}测试连接并获取模型</button><button class="soft-button cancel" type="button">取消</button><button class="primary-button confirm" type="button">${icon("save",16)}${editing ? "保存修改" : "添加提供商"}</button></div>`;
  const fields = $(".provider-fields", dialog); const modelList = $(".integrated-model-list", dialog);
  const renderModels = () => {
    modelList.replaceChildren();
    (candidate.models || []).forEach((model, index) => {
      const row = document.createElement("div"); row.className = "integrated-model-row";
      row.innerHTML = `<div><strong>${esc(text(model.name || model.model))}</strong><small>${esc(text(model.model))} · ${model.mode === "edit" ? "图片编辑" : "文生图"}</small></div><span class="provider-chip">${model.enabled === false ? "已停用" : "已启用"}</span><button class="icon-button edit-model" title="设置">${icon("settings",16)}</button><button class="icon-button delete-model" title="删除">${icon("trash",16)}</button>`;
      $(".edit-model", row).onclick = () => modelEditor(candidate, model, true, next => { candidate.models[index] = next; renderModels(); });
      $(".delete-model", row).onclick = () => { candidate.models.splice(index, 1); renderModels(); }; modelList.append(row);
    });
    if (!(candidate.models || []).length) { const empty = document.createElement("div"); empty.className = "model-empty"; empty.textContent = "尚未启用模型。先测试连接获取列表，或添加自定义模型。"; modelList.append(empty); }
  };
  const renderFields = () => {
    fields.replaceChildren();
    const specs = {
      name: { type: "string", description: "提供商名称", hint: "必须唯一，例如 硅基流动" },
      source: { type: "string", description: "来源", options: ["custom", "astrbot"], option_labels: { custom: "插件内配置", astrbot: "AstrBot 已有提供商" } },
      api_key: { type: "string", secret: true, description: "API Key" }, api_keys: { type: "list", description: "备用 API Key", hint: "可选；主 Key 失败时按顺序自动切换" }, base_url: { type: "string", description: "API Base URL", hint: "只填根地址；例如 https://api.siliconflow.cn/v1" },
      astrbot_provider_id: { type: "string", description: "AstrBot 提供商" }, enabled: { type: "bool", description: "启用提供商" },
    };
    Object.entries(specs).filter(([key]) => candidate.source === "astrbot" ? !["api_key", "api_keys", "base_url"].includes(key) : key !== "astrbot_provider_id").forEach(([key, spec]) => fields.append(createField(key, spec, candidate[key], next => { candidate[key] = next; if (key === "source") renderFields(); }, true)));
  };
  renderFields(); renderModels();
  $(".add-model", dialog).onclick = () => modelEditor(candidate, null, false, model => { candidate.models ||= []; candidate.models.push(model); renderModels(); });
  $(".provider-fetch", dialog).onclick = async event => {
    const button = event.currentTarget; button.disabled = true; button.innerHTML = `<span class="spinner"></span>正在连接`;
    try {
      const result = await apiPost("providers/models", { provider: candidate }); const models = result.models || []; state.providerModels[candidate.name] = models;
      $(".provider-status", dialog).textContent = `${result.message}；点击模型即可加入`; $(".provider-status", dialog).classList.remove("is-error");
      candidate.available_models = models; modelList.replaceChildren(); models.forEach(id => { const row = document.createElement("button"); row.type = "button"; row.className = "discovered-model"; row.innerHTML = `<span>${esc(id)}</span><b>＋ 启用</b>`; row.onclick = () => modelEditor(candidate, { model: id, name: id, mode: "text", enabled: true, enabled_as_default: true, fallback_order: 20, refer_field: "", max_refer_images: 0, min_prompt_length: 0, params: {}, custom_model: false }, false, model => { candidate.models ||= []; candidate.models.push(model); renderModels(); }); modelList.append(row); });
      if (!models.length) renderModels();
    } catch (error) { $(".provider-status", dialog).textContent = `连接失败：${readableError(error)}`; $(".provider-status", dialog).classList.add("is-error"); } finally { button.disabled = false; button.innerHTML = `${icon("refresh",16)}测试连接并获取模型`; }
  };
  const close = () => dialog.close(); $(".modal-close", dialog).onclick = close; $(".cancel", dialog).onclick = close;
  $(".confirm", dialog).onclick = () => {
    const error = !String(candidate.name || "").trim() ? "提供商名称不能为空" : candidate.source === "custom" && !String(candidate.base_url || "").startsWith("http") ? "请填写有效的 API Base URL" : candidate.source === "astrbot" && !candidate.astrbot_provider_id ? "请选择 AstrBot 提供商" : "";
    const box = $(".template-modal-error", dialog); box.textContent = error; box.classList.toggle("is-visible", !!error); if (error) return; onDone(candidate); dialog.close();
  };
  dialog.showModal();
}

function imageProvidersField(initial, onChange) {
  const root = document.createElement("div"); root.className = "provider-console";
  let items = clone(initial || []).filter(item => String(item?.source || "custom").toLowerCase() !== "astrbot");
  let selected = Math.min(Math.max(0, Number(state.selectedProvider || 0)), Math.max(0, items.length - 1));
  const emit = (rerender = true) => { state.selectedProvider = selected; onChange(clone(items)); if (rerender) render(); };
  const setting = (label, hint, control) => { const row = document.createElement("div"); row.className = "provider-setting-row"; const meta = document.createElement("div"); meta.innerHTML = `<strong>${esc(label)}</strong>${hint ? `<small>${esc(hint)}</small>` : ""}`; row.append(meta, control); return row; };
  const input = (value, secret, changed) => { const wrap = document.createElement("div"); wrap.className = secret ? "input-wrap" : ""; const control = document.createElement("input"); control.type = secret ? "password" : "text"; control.value = value ?? ""; control.onchange = () => changed(control.value); wrap.append(control); if (secret) { const reveal = document.createElement("button"); reveal.type = "button"; reveal.className = "input-action"; reveal.innerHTML = icon("eye", 17); reveal.onclick = () => { control.type = control.type === "password" ? "text" : "password"; }; wrap.append(reveal); } return wrap; };
  const render = () => {
    root.replaceChildren();
    const sidebar = document.createElement("aside"); sidebar.className = "provider-console-sidebar";
    const sideHead = document.createElement("div"); sideHead.className = "provider-console-head"; sideHead.innerHTML = `<h3>提供商源</h3><button type="button">＋ 新增</button>`;
    $("button", sideHead).onclick = () => { items.push({ __template_key: "image_provider", name: "", source: "custom", api_key: "", api_keys: [], base_url: "https://api.openai.com/v1", enabled: true, timeout: 300, proxy: "", custom_headers: {}, models: [] }); selected = items.length - 1; emit(); };
    sidebar.append(sideHead);
    const sourceList = document.createElement("div"); sourceList.className = "provider-source-list";
    items.forEach((provider, index) => { const card = document.createElement("button"); card.type = "button"; card.className = `provider-source-card ${index === selected ? "is-active" : ""}`; card.innerHTML = `<span class="provider-source-logo">${String(provider.name || "?").slice(0, 1).toUpperCase()}</span><span><strong>${esc(text(provider.name))}</strong><small>${esc(text(provider.base_url))}</small></span><i>${icon("trash", 16)}</i>`; card.onclick = event => { if (event.target.closest("i")) { items.splice(index, 1); selected = Math.min(selected, Math.max(0, items.length - 1)); emit(); } else { selected = index; state.selectedProvider = index; render(); } }; sourceList.append(card); });
    sidebar.append(sourceList); root.append(sidebar);
    const detail = document.createElement("section"); detail.className = "provider-console-detail"; root.append(detail);
    if (!items.length) { detail.innerHTML = `<div class="provider-console-empty"><b>尚未添加模型提供商</b><span>点击左侧“新增”，填写 API Base URL 和 Key。</span></div>`; return; }
    const provider = items[selected]; provider.models ||= []; provider.source = "custom";
    const detected = inferredProtocol(provider.base_url);
    detail.innerHTML = `<div class="provider-detail-title"><div><h2>${esc(text(provider.name))}</h2><p>${esc(text(provider.base_url))}</p><small class="detected-protocol">已自动识别：${detected === "OpenAI" ? "OpenAI 兼容" : esc(detected)}</small></div></div><div class="provider-settings"><h3>设置</h3><div class="provider-setting-list"></div><details class="provider-advanced"><summary>高级配置…</summary><div class="provider-setting-list advanced-list"></div></details></div><div class="provider-models"><div class="provider-model-header"><div><h3>模型</h3><small>已配置 ${(provider.models || []).length} 个</small></div><div class="provider-model-actions"><input class="model-search" type="search" placeholder="搜索已配置模型"><button class="soft-button fetch-models" type="button">${icon("refresh", 16)}${detected === "WaveSpeed" ? "测试连接" : "获取模型列表"}</button><button class="text-button custom-model" type="button">＋ 自定义模型</button></div></div><div class="provider-inline-status" role="status">${detected === "WaveSpeed" ? "WaveSpeed 模型需要自定义添加，模型 ID 会自动拼接到请求路径。" : ""}</div><div class="provider-configured-models"></div><div class="provider-discovered-models"></div><div class="model-search-empty" hidden>没有匹配的模型</div></div>`;
    const list = $(".provider-setting-list", detail);
    list.append(setting("ID", "提供商唯一 ID（不是模型 ID）", input(provider.name, false, value => { provider.name = value.trim(); emit(); })));
    const keyPool = document.createElement("div"); keyPool.className = "provider-key-pool";
    if (!Array.isArray(provider.api_keys) || !provider.api_keys.length) provider.api_keys = provider.api_key ? [provider.api_key] : [];
    const syncPrimary = () => { provider.api_key = (provider.api_keys || [])[0] || ""; };
    const drawKeyPool = () => {
      keyPool.replaceChildren();
      provider.api_keys.forEach((keyValue, index) => {
        const row = document.createElement("div"); row.className = "provider-key-row";
        const tag = document.createElement("span"); tag.className = "provider-key-tag"; tag.textContent = index === 0 ? "主 Key" : `备用 #${index}`;
        const control = input(keyValue || "", true, value => { provider.api_keys[index] = value.trim(); syncPrimary(); emit(false); });
        const balance = document.createElement("span"); balance.className = "provider-key-balance"; balance.dataset.index = String(index);
        const remove = document.createElement("button"); remove.type = "button"; remove.className = "icon-button provider-key-remove"; remove.title = "删除该 Key"; remove.innerHTML = icon("trash", 15);
        remove.onclick = () => { provider.api_keys.splice(index, 1); syncPrimary(); emit(false); drawKeyPool(); };
        row.append(tag, control, balance, remove);
        keyPool.append(row);
      });
      const actions = document.createElement("div"); actions.className = "provider-key-actions";
      const addKey = document.createElement("button"); addKey.type = "button"; addKey.className = "soft-button"; addKey.innerHTML = `${icon("plus", 15)}添加 Key`;
      addKey.onclick = () => { provider.api_keys.push(""); emit(false); drawKeyPool(); };
      const check = document.createElement("button"); check.type = "button"; check.className = "soft-button provider-key-check"; check.innerHTML = `${icon("refresh", 15)}查询余额`;
      check.onclick = async () => { const status = $(".provider-inline-status", detail); check.disabled = true; syncPrimary(); status.textContent = "正在查询各 Key 余额…"; status.className = "provider-inline-status is-testing"; try { const result = await apiPost("providers/balance", { provider }); const items = result.items || []; $$(".provider-key-balance", keyPool).forEach(el => { el.textContent = ""; el.className = "provider-key-balance"; }); items.forEach(entry => { const el = keyPool.querySelector(`.provider-key-balance[data-index="${entry.index}"]`); if (!el) return; const currency = entry.currency ? ` ${entry.currency}` : ""; if (entry.ok) { el.textContent = `${entry.balance} 币${currency}`; el.classList.add("is-ok"); if (String(entry.balance) === "0") el.classList.add("is-empty"); } else { el.textContent = entry.message || "查询失败"; el.classList.add("is-error"); } }); const okCount = items.filter(entry => entry.ok).length; status.textContent = `✓ 已查询 ${items.length} 个 Key（站点 ${result.site || "未知"}），可用 ${okCount} 个`; status.className = "provider-inline-status is-success"; } catch (error) { status.textContent = `✕ 余额查询失败 · ${readableError(error)}`; status.className = "provider-inline-status is-error"; } finally { check.disabled = false; } };
      actions.append(addKey, check);
      keyPool.append(actions);
    };
    drawKeyPool();
    list.append(setting("API Key 池", "主 Key 在前，其后按顺序备用；主 Key 失败时自动切换。多个 Key 需来自不同账号，可逐条查询余额", keyPool));
    list.append(setting("API Base URL", "填写后自动识别接口协议", input(provider.base_url, false, value => { provider.base_url = value.trim().replace(/\/$/, ""); delete provider.protocol; emit(); })));
    const advanced = $(".advanced-list", detail);
    advanced.append(setting("超时时间", "单位为秒", input(provider.timeout ?? 300, false, value => { provider.timeout = Number(value) || 300; emit(false); })));
    advanced.append(setting("代理地址", "仅对该提供商的 API 请求生效", input(provider.proxy || "", false, value => { provider.proxy = value.trim(); emit(false); })));
    advanced.append(createField("custom_headers", { type: "object", description: "自定义请求头" }, provider.custom_headers || {}, value => { provider.custom_headers = value; emit(false); }, true));
    const rows = $(".provider-configured-models", detail);
    const drawRows = filter => { rows.replaceChildren(); (provider.models || []).forEach((model, index) => { if (filter && !`${text(model.name)} ${text(model.model)}`.toLowerCase().includes(filter)) return; const row = document.createElement("div"); row.className = "provider-model-row"; row.innerHTML = `<div><strong>${esc(text(model.name || model.model))}</strong><small>${esc(text(model.model))}</small><span>${model.mode === "edit" ? "图片编辑" : "文生图"}</span><b class="model-test-badge" hidden>✓ 测试通过</b></div><button class="switch ${model.enabled === false ? "" : "is-on"}" role="switch" aria-label="启用模型"></button><button class="icon-button test-one" title="测试连接">${icon("refresh", 15)}</button><button class="icon-button configure-one" title="模型设置">${icon("settings", 16)}</button><button class="icon-button remove-one" title="删除">${icon("trash", 16)}</button>`; $(".switch", row).onclick = () => { model.enabled = model.enabled === false; emit(); }; $(".test-one", row).onclick = async event => { const button = event.currentTarget; const status = $(".provider-inline-status", detail); const badge = $(".model-test-badge", row); button.disabled = true; row.classList.remove("is-tested"); badge.hidden = true; status.textContent = "正在测试模型连接…"; status.className = "provider-inline-status is-testing"; try { const result = await apiPost("models/test", { provider, model }); status.textContent = `✓ 测试成功 · ${esc(result.message || "模型可用")}`; status.className = "provider-inline-status is-success"; row.classList.add("is-tested"); badge.hidden = false; } catch (error) { status.textContent = `✕ 模型测试失败 · ${readableError(error)}`; status.className = "provider-inline-status is-error"; } finally { button.disabled = false; } }; $(".configure-one", row).onclick = () => modelEditor(provider, model, true, next => { provider.models[index] = next; emit(); }); $(".remove-one", row).onclick = () => { provider.models.splice(index, 1); emit(); }; rows.append(row); }); };
    const applyModelFilter = () => { const filter = $(".model-search", detail).value.trim().toLowerCase(); drawRows(filter); let visible = rows.children.length; $$(".discovered-model", detail).forEach(row => { row.hidden = Boolean(filter) && !row.textContent.toLowerCase().includes(filter); if (!row.hidden) visible += 1; }); $(".model-search-empty", detail).hidden = visible > 0; };
    applyModelFilter(); $(".model-search", detail).oninput = applyModelFilter;
    $(".custom-model", detail).onclick = () => modelEditor(provider, null, false, model => { provider.models.push(model); emit(); });
    $(".fetch-models", detail).onclick = async event => { const button = event.currentTarget; button.disabled = true; const status = $(".provider-inline-status", detail); const waveSpeed = inferredProtocol(provider.base_url) === "WaveSpeed"; status.textContent = waveSpeed ? "正在测试连接…" : "正在连接并读取模型…"; status.className = "provider-inline-status is-testing"; try { const result = waveSpeed ? await apiPost("providers/test", { provider }) : await apiPost("providers/models", { provider }); const models = result.models || []; state.providerModels[provider.name] = models; provider.available_models = models; status.textContent = waveSpeed ? `✓ 连接成功 · ${esc(result.message || "请使用自定义模型")}` : `✓ 连接成功 · ${esc(result.message || `发现 ${models.length} 个模型`)}`; status.className = "provider-inline-status is-success"; const discovered = $(".provider-discovered-models", detail); discovered.replaceChildren(); const configured = new Set(provider.models.map(model => model.model)); models.filter(id => !configured.has(id)).forEach(id => { const row = document.createElement("button"); row.type = "button"; row.className = "discovered-model"; row.innerHTML = `<span>${esc(id)}</span><b>＋ 启用</b>`; row.onclick = () => modelEditor(provider, { model: id, name: id, mode: "text", enabled: true, enabled_as_default: true, fallback_order: 20, refer_field: "", max_refer_images: 0, min_prompt_length: 0, params: {}, custom_model: false }, false, model => { provider.models.push(model); emit(); }); discovered.append(row); }); applyModelFilter(); emit(false); } catch (error) { status.textContent = `✕ 连接失败 · ${readableError(error)}`; status.className = "provider-inline-status is-error"; } finally { button.disabled = false; } };
  };
  render(); return root;
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

const COMFYUI_ROLES = [
  ["prompt", "正向提示词"],
  ["negative", "负面提示词"],
  ["width", "宽度"],
  ["height", "高度"],
  ["seed", "随机种子"],
  ["steps", "采样步数"],
  ["cfg", "CFG 强度"],
  ["sampler", "采样器"],
  ["scheduler", "调度器"],
  ["count", "生成数量"],
  ["image", "参考图"],
  ["artist_style", "画师风格"],
  ["artist_style_weight", "画师风格权重"],
  ["artist_prefix", "画师前缀开关"],
  ["gelbooru_style", "Gelbooru 风格"],
  ["gelbooru_style_weight", "Gelbooru 风格权重"],
  ["positive_prefix", "正向前缀"],
  ["positive_suffix", "正向后缀"],
  ["prompt_part_3", "提示词分区 3"],
  ["prompt_part_4", "提示词分区 4"],
  ["prompt_part_5", "提示词分区 5"],
  ["prompt_part_6", "提示词分区 6"],
  ["delimiter", "拼接分隔符"],
  ["merge_separator", "合并分隔符"],
  ["clean_whitespace", "清理空白字符"],
  ["model_name", "底模（Checkpoint）"],
  ["lora_name", "LoRA 名称"],
  ["lora_strength", "LoRA 强度"],
  ["lora_strength_clip", "LoRA 文本强度"],
  ["clip_name", "文本编码器（CLIP）"],
  ["vae_name", "VAE"],
  ["denoise", "重绘强度"],
  ["shift", "Flow Shift"],
  ["teacache_threshold", "加速缓存阈值"],
  ["teacache_adaptive", "自适应加速"],
  ["sage_attention", "SageAttention"],
  ["filename_prefix", "输出文件名前缀"],
  ["text_const", "常量文本"],
  ["fixed", "固定值"],
  ["ignore", "忽略"],
];

// —— 工作流三种调用方式 ——
const RUN_MODE_LABELS = { workflow: "工作流", ai_app: "AI 应用", proxy: "原生代理" };

function isProxyMode(runMode) {
  return ["proxy", "native", "comfyui"].includes(String(runMode || "").trim().toLowerCase());
}

function runModeLabel(runMode) {
  return RUN_MODE_LABELS[String(runMode || "workflow").trim().toLowerCase()] || "工作流";
}

const UPLOAD_ENDPOINTS = {
  reference_image: "config/upload/reference_image",
  drawing_first_frame_path: "config/upload/drawing_first_frame",
  apng_first_frame_path: "config/upload/apng_first_frame",
};

async function apiUpload(path, file) {
  await connectBridge();
  const uploader = state.bridge && state.bridge.upload;
  if (typeof uploader !== "function") throw new Error("当前 AstrBot 版本不支持文件上传桥接，请更新 AstrBot");
  return unwrap(await uploader.call(state.bridge, path, file));
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
}

async function uploadFileForConfig(configKey, file) {
  const endpoint = UPLOAD_ENDPOINTS[configKey];
  await connectBridge();
  if (endpoint && state.bridge && typeof state.bridge.upload === "function") {
    return apiUpload(endpoint, file);
  }
  // 旧版 AstrBot 无 upload 桥接时回退到 base64 JSON
  const data = await readFileAsDataURL(file);
  return apiPost("config/upload_file", { config_key: configKey, filename: file.name, data });
}

function uploadField(spec, value, onChange, configKey) {
  const root = document.createElement("div"); root.className = "file-control";
  const input = document.createElement("input"); input.value = value || ""; input.placeholder = "文件路径"; input.addEventListener("change", () => onChange(input.value));
  const zone = document.createElement("div"); zone.className = "file-drop"; zone.tabIndex = 0; zone.setAttribute("role", "button");
  zone.innerHTML = `${icon("upload", 16)}<span class="file-drop-title">点击选择 · 拖入 · Ctrl+V 粘贴</span><small>${(spec.file_types || []).map(ext => `.${ext}`).join(" ")}</small>`;
  const busy = document.createElement("span"); busy.className = "file-drop-busy"; busy.hidden = true; busy.innerHTML = `<span class="spinner"></span>上传中`;
  zone.append(busy);
  let uploading = false;
  const doUpload = async file => {
    if (!file || uploading) return;
    uploading = true; busy.hidden = false; zone.classList.add("is-busy");
    try {
      const result = await uploadFileForConfig(configKey, file);
      input.value = result.path; onChange(result.path); toast("文件上传成功");
    } catch (error) { toast(`上传失败：${readableError(error)}`, "error"); }
    finally { uploading = false; busy.hidden = true; zone.classList.remove("is-busy"); }
  };
  const pick = () => {
    // boot() 可能还没跑完，选择器按需创建，避免拿到 null
    const picker = ensureFilePicker();
    picker.accept = (spec.file_types || []).map(ext => `.${ext}`).join(",");
    picker.onchange = () => { const file = picker.files && picker.files[0]; picker.value = ""; if (file) doUpload(file); };
    picker.click();
  };
  zone.addEventListener("click", pick);
  zone.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(); } });
  zone.addEventListener("dragover", event => { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = "copy"; zone.classList.add("is-hover"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("is-hover"));
  zone.addEventListener("drop", event => { event.preventDefault(); zone.classList.remove("is-hover"); const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0]; if (file) doUpload(file); });
  zone.addEventListener("paste", event => { const file = event.clipboardData && event.clipboardData.files && event.clipboardData.files[0]; if (file) { event.preventDefault(); doUpload(file); } });
  root.append(input, zone); return root;
}

function runningHubProviderNames() {
  return (state.draft.image_providers || [])
    .filter(provider => provider && provider.enabled !== false && inferredProtocol(provider.base_url) === "RunningHub")
    .map(provider => String(provider.name || "").trim())
    .filter(Boolean);
}

function comfyuiWorkflowsField(initial, onChange) {
  const root = document.createElement("div"); root.className = "comfyui-console";
  let items = clone(initial) || [];
  const emit = () => onChange(clone(items));
  const itemLabel = item => String(item?.name || "").trim() || "未命名工作流";
  const activeCount = item => Array.isArray(item?.nodes) ? item.nodes.filter(node => node && node.role && node.role !== "ignore").length : 0;
  const render = () => {
    root.replaceChildren();
    const hint = document.createElement("p"); hint.className = "field-hint comfyui-console-hint";
    hint.textContent = "上传“API 格式”的工作流 JSON 后自动识别可设置参数，确认用途后即可用触发词或 --model 调用。工作流复用 RunningHub 提供商的 Key 与地址。";
    root.append(hint);
    if (!runningHubProviderNames().length) {
      const warn = document.createElement("p"); warn.className = "field-hint comfyui-console-warn";
      warn.textContent = "尚未配置 RunningHub 提供商，请先在“模型提供商”中添加一个 RunningHub 连接。";
      root.append(warn);
    }
    items.forEach((item, index) => {
      const card = document.createElement("div"); card.className = "comfyui-card";
      const main = document.createElement("div"); main.className = "comfyui-card-main";
      const kind = runModeLabel(item.run_mode);
      const idText = isProxyMode(item.run_mode) ? "整份 JSON" : text(item.workflow_id);
      main.innerHTML = `<strong>${esc(text(itemLabel(item)))}</strong><small>${esc(text(item.provider))} · ${esc(kind)} ${esc(idText)} · 已映射 ${activeCount(item)} 项</small>`;
      const toggle = document.createElement("button"); toggle.type = "button"; toggle.className = `switch ${item.enabled === false ? "" : "is-on"}`; toggle.setAttribute("role", "switch"); toggle.setAttribute("aria-label", "启用工作流");
      toggle.addEventListener("click", () => { item.enabled = item.enabled === false; emit(); render(); });
      const edit = document.createElement("button"); edit.type = "button"; edit.className = "icon-button"; edit.title = "编辑"; edit.innerHTML = icon("settings", 16);
      edit.addEventListener("click", () => comfyuiWorkflowEditor(item, true, next => { items[index] = next; emit(); render(); }, items));
      const test = document.createElement("button"); test.type = "button"; test.className = "icon-button"; test.title = "测试（只检查配置与连接，不生成、不消耗额度）"; test.innerHTML = icon("refresh", 15);
      test.addEventListener("click", async () => {
        const original = test.innerHTML; test.disabled = true; test.innerHTML = `<span class="spinner"></span>`;
        try { const result = await apiPost("comfyui/test-workflow", { provider: item.provider, workflow: item }); toast(result.message || "工作流配置正常"); }
        catch (error) { toast(`工作流测试失败：${readableError(error)}`, "error"); }
        finally { test.disabled = false; test.innerHTML = original; }
      });
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "icon-button"; remove.title = "删除"; remove.innerHTML = icon("trash", 16);
      remove.addEventListener("click", () => { items.splice(index, 1); emit(); render(); });
      const download = document.createElement("button"); download.type = "button"; download.className = "icon-button"; download.innerHTML = icon("download", 15);
      if (item.raw_json) {
        download.title = "下载这个条目当前使用的 API 格式 JSON";
        download.addEventListener("click", () => downloadWorkflowJson(itemLabel(item)));
      } else {
        download.title = "还没有保存工作流 JSON，重新上传一次 API 格式 JSON 即可补齐";
        download.disabled = true;
        download.style.opacity = "0.4";
      }
      card.append(main, toggle, test, download, edit, remove);
      root.append(card);
    });
    const add = document.createElement("button"); add.type = "button"; add.className = "soft-button comfyui-add"; add.innerHTML = `${icon("plus", 15)}添加工作流`;
    add.addEventListener("click", () => comfyuiWorkflowEditor(null, false, next => { items.push(next); emit(); render(); }, items));
    root.append(add);
  };
  render(); return root;
}

// 重新上传后给一句人话总结：沿用了多少项、新增多少、移除多少
function describeMappingSummary(summary, fallbackCount) {
  if (!summary) return `已识别 ${fallbackCount} 个可设置字段`;
  if (summary.rebuilt) {
    const ratio = summary.overlap != null ? `（节点重合 ${Math.round(Number(summary.overlap) * 100)}%）` : "";
    return `检测到这是另一份工作流${ratio}，映射表已重建，共 ${fallbackCount} 个字段，请重新确认用途`;
  }
  const parts = [`已识别 ${fallbackCount} 个字段`];
  if (summary.kept) parts.push(`沿用 ${summary.kept} 项用途`);
  if (summary.added) parts.push(`新增 ${summary.added} 项`);
  if (summary.removed) parts.push(`移除 ${summary.removed} 项已不存在的字段`);
  if (summary.value_changed) parts.push(`${summary.value_changed} 项默认值随 JSON 更新`);
  return parts.join("，");
}

// 把文本存成本地文件（JSON 下载走这条，不经过图片那套 data_url）
function downloadJsonText(filename, text) {
  try {
    const blob = new Blob([text], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = filename; link.hidden = true;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast("已开始下载工作流 JSON");
  } catch (error) {
    toast(`下载失败：${readableError(error)}`, "error");
  }
}

async function downloadWorkflowJson(name) {
  if (!String(name || "").trim()) { toast("请先给工作流起个名字", "error"); return; }
  try {
    const data = await apiGet("comfyui/download-workflow", { name: String(name).trim() });
    downloadJsonText(data.filename || `${name}.json`, data.text || "");
  } catch (error) { toast(`下载失败：${readableError(error)}`, "error"); }
}

function comfyuiWorkflowEditor(initial, editing, onDone, existingItems) {  const dialog = makeDialog("modal template-editor-modal comfyui-editor-modal");
  const candidate = clone(initial) || {};
  candidate.name = String(candidate.name || "");
  candidate.provider = String(candidate.provider || "");
  candidate.workflow_id = String(candidate.workflow_id || "");
  candidate.run_mode = String(candidate.run_mode || "workflow");
  candidate.instance_type = String(candidate.instance_type || "default").trim().toLowerCase();
  candidate.use_personal_queue = Boolean(candidate.use_personal_queue);
  candidate.enabled = candidate.enabled !== false;
  candidate.enabled_as_default = candidate.enabled_as_default !== false;
  candidate.fallback_order = Number(candidate.fallback_order ?? 30);
  candidate.refer_field = String(candidate.refer_field || "");
  candidate.max_refer_images = Number(candidate.max_refer_images ?? 0);
  candidate.nodes = Array.isArray(candidate.nodes) ? clone(candidate.nodes) : [];
  candidate.raw_json = String(candidate.raw_json || "");
  const providers = runningHubProviderNames();
  dialog.innerHTML = `<div class="template-modal-head"><div><span class="eyebrow">IMAGE LAB / COMFYUI</span><h2>${editing ? "编辑工作流" : "添加工作流"}</h2><p>RunningHub ComfyUI 工作流 API</p></div><button class="modal-close" type="button" aria-label="关闭">${icon("close")}</button></div><div class="template-modal-body"><div class="template-modal-error" role="alert"></div><details class="comfyui-base-details"><summary>基础信息 <small class="comfyui-base-summary"></small></summary><div class="field-grid comfyui-editor-fields"></div></details><div class="comfyui-mapping"><div class="comfyui-mapping-head"><h3>参数映射</h3><div class="comfyui-mapping-tools"><small class="comfyui-mapping-status"></small><label class="comfyui-toggle"><input type="checkbox" class="comfyui-show-all"> 显示全部字段</label></div></div><p class="field-hint comfyui-mapping-hint">「默认值」在 QQ 指令没有提供对应 <code>--参数</code>（如 <code>--width</code>、<code>--steps</code>）时生效，因此常用的尺寸、步数等只需在这里填一次。</p><div class="file-drop comfyui-workflow-drop" role="button" tabindex="0"><b>点击选择 · 拖入 · Ctrl+V 粘贴 API 工作流 JSON</b><small>ComfyUI 中“导出（API 格式）”得到的 .json</small></div><p class="field-hint comfyui-raw-hint"></p><div class="comfyui-node-table"></div></div></div><div class="template-modal-foot"><button class="soft-button comfyui-download" type="button">${icon("download", 15)}下载 JSON</button><button class="soft-button comfyui-test" type="button">${icon("refresh", 15)}测试</button><button class="soft-button cancel" type="button">取消</button><button class="primary-button confirm" type="button">${icon("save", 16)}保存</button></div>`;
  const fields = $(".comfyui-editor-fields", dialog);
  const status = $(".comfyui-mapping-status", dialog);
  const table = $(".comfyui-node-table", dialog);
  const baseSummary = $(".comfyui-base-summary", dialog);
  const rawHint = $(".comfyui-raw-hint", dialog);
  const updateRawHint = () => {
    if (!rawHint) return;
    rawHint.textContent = candidate.raw_json
      ? `已保存整份 API JSON（${candidate.raw_json}），可用「下载 JSON」取回，也可切到原生代理直接提交。`
      : "还没有保存工作流 JSON：点击选择 · 拖入 · Ctrl+V 粘贴「导出（API 格式）」的 .json。";
  };
  const showAllBox = $(".comfyui-show-all", dialog);
  let showAll = false;
  showAllBox.addEventListener("change", () => { showAll = showAllBox.checked; renderTable(); });
  const specs = {
    name: { type: "string", description: "模板名称", hint: "供 --model 与默认模型选择使用，必须全局唯一" },
    provider: { type: "string", description: "模型提供商", options: providers, hint: providers.length ? "选择一个 RunningHub 提供商" : "请先添加 RunningHub 提供商" },
    workflow_id: { type: "string", description: "RunningHub 工作流 / AI 应用 ID", hint: "工作流页取 /workflow/<id>，AI 应用页取 /ai-detail/<id>，填数字 ID" },
    run_mode: { type: "string", description: "调用接口", options: ["workflow", "ai_app", "proxy"], option_labels: { workflow: "工作流接口", ai_app: "AI 应用接口", proxy: "原生代理（整份 JSON）" }, hint: "工作流接口填 /workflow/<id>；AI 应用接口填 /ai-detail/<id>；原生代理不用 ID，直接提交已上传的整份 JSON 到 /proxy/{KEY}/prompt" },
    instance_type: { type: "string", description: "实例类型", options: ["default", "plus", "ultra"], option_labels: { default: "默认实例", plus: "plus · 48G 显存", ultra: "ultra · 84G 显存" }, hint: "显存更大的档位出图更快，但计费更高；报错“未找到任务用户 API 实例”时通常是显存档位选低了" },
    refer_field: { type: "string", description: "参考图字段名", hint: "文生图留空；图生图填写 LoadImage 的 image 字段" },
    max_refer_images: { type: "int", description: "参考图数量上限" },
    fallback_order: { type: "int", description: "回退优先级", hint: "数字越小优先级越高" },
    use_personal_queue: { type: "bool", description: "使用个人队列" },
    enabled_as_default: { type: "bool", description: "允许作为默认模型" },
    enabled: { type: "bool", description: "启用工作流" },
  };
  const updateBaseSummary = () => { baseSummary.textContent = `${candidate.name || "未命名工作流"} · ${candidate.provider || "未选择提供商"}`; };
  const renderFields = () => {
    fields.replaceChildren();
    Object.entries(specs).forEach(([key, spec]) => {
      const value = candidate[key];
      fields.append(createField(key, spec, value === undefined ? fieldDefault(spec) : value, next => { candidate[key] = next; if (key === "name" || key === "provider") updateBaseSummary(); }, true));
    });
    updateBaseSummary();
  };
  const renderTable = () => {
    table.replaceChildren();
    const nodes = candidate.nodes || [];
    const active = nodes.filter(node => node && node.role && node.role !== "ignore").length;
    status.textContent = nodes.length ? `${nodes.length} 个字段，已映射 ${active} 项` : "尚未识别参数，请先上传工作流 JSON";
    if (!nodes.length) return;
    const visible = showAll ? nodes : nodes.filter(node => node && node.role && node.role !== "ignore");
    if (!visible.length) {
      const empty = document.createElement("p"); empty.className = "comfyui-node-more";
      empty.textContent = "还没有映射任何参数——勾选“显示全部字段”来挑选用途。";
      table.append(empty); return;
    }
    const head = document.createElement("div"); head.className = "comfyui-node-row is-head";
    head.innerHTML = `<span>节点</span><span>字段 / 节点类型</span><span>默认值</span><span>用途</span>`;
    table.append(head);
    visible.forEach(node => {
      const role = node.role || "ignore";
      const row = document.createElement("div"); row.className = "comfyui-node-row";
      if (role === "ignore") row.classList.add("is-muted");
      const nodeId = document.createElement("code"); nodeId.textContent = String(node.nodeId ?? "");
      const meta = document.createElement("span"); meta.className = "comfyui-node-meta"; meta.innerHTML = `<b>${esc(text(node.fieldName))}</b><small>${esc(text(node.classType))}${node.title ? " · " + esc(text(node.title)) : ""}</small>`;
      const valueCell = document.createElement("span"); valueCell.className = "comfyui-node-value";
      if (role === "ignore") {
        const note = document.createElement("span"); note.className = "comfyui-node-note"; note.textContent = "—"; valueCell.append(note);
      } else if (role === "seed") {
        const note = document.createElement("span"); note.className = "comfyui-node-note"; note.textContent = "每次随机"; valueCell.append(note);
      } else {
        const valueInput = document.createElement("input"); valueInput.type = "text";
        valueInput.value = node.value === null || node.value === undefined ? "" : String(node.value);
        valueInput.placeholder = role === "fixed" ? "固定值" : "默认值";
        valueInput.addEventListener("change", () => { node.value = parseScalar(valueInput.value); });
        valueCell.append(valueInput);
      }
      const select = document.createElement("select");
      COMFYUI_ROLES.forEach(([r, label]) => select.add(new Option(label, r)));
      select.value = role;
      select.addEventListener("change", () => { node.role = select.value; renderTable(); });
      row.append(nodeId, meta, valueCell, select);
      table.append(row);
    });
    const hidden = nodes.length - visible.length;
    if (!showAll && hidden > 0) {
      const more = document.createElement("p"); more.className = "comfyui-node-more";
      more.textContent = `还有 ${hidden} 个未映射字段，勾选“显示全部字段”查看。`;
      table.append(more);
    }
  };
  const loadWorkflow = async file => {
    if (!file) return;
    const drop = $(".comfyui-workflow-drop", dialog);
    drop.classList.add("is-busy"); status.textContent = "正在上传并识别参数…";
    try {
      await connectBridge();
      if (!(state.bridge && typeof state.bridge.upload === "function")) {
        throw new Error("当前 AstrBot 版本不支持文件上传桥接");
      }
      const result = await apiUpload("comfyui/parse-workflow", file);
      const fresh = Array.isArray(result.nodes) ? result.nodes : [];
      const previous = candidate.nodes || [];
      let merged = fresh;
      let summary = null;
      if (previous.length) {
        // 已经有映射时重新上传：把用户调过的用途搬过来，而不是整张表推倒重来
        try {
          const data = await apiPost("comfyui/merge-mapping", { old: previous, new: fresh });
          merged = Array.isArray(data.nodes) ? data.nodes : fresh;
          summary = data.summary || null;
        } catch (error) {
          toast(`映射迁移失败，已重建：${readableError(error)}`, "error");
        }
      }
      candidate.nodes = merged;
      if (result.raw_json) candidate.raw_json = result.raw_json;
      renderTable();
      updateRawHint();
      toast(describeMappingSummary(summary, merged.length));
    } catch (error) { status.textContent = `识别失败：${readableError(error)}`; toast(`工作流识别失败：${readableError(error)}`, "error"); }
    finally { drop.classList.remove("is-busy"); }
  };
  const drop = $(".comfyui-workflow-drop", dialog);
  const pickWorkflow = () => {
    const picker = ensureFilePicker();
    picker.accept = ".json,application/json";
    picker.onchange = () => { const picked = picker.files && picker.files[0]; picker.value = ""; if (picked) loadWorkflow(picked); };
    picker.click();
  };
  drop.addEventListener("click", pickWorkflow);
  drop.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pickWorkflow(); } });
  drop.addEventListener("dragover", event => { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = "copy"; drop.classList.add("is-hover"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("is-hover"));
  drop.addEventListener("drop", event => { event.preventDefault(); drop.classList.remove("is-hover"); const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0]; if (file) loadWorkflow(file); });
  drop.addEventListener("paste", event => { const file = event.clipboardData && event.clipboardData.files && event.clipboardData.files[0]; if (file) { event.preventDefault(); loadWorkflow(file); } });
  renderFields(); renderTable(); updateRawHint();
  const downloadButton = $(".comfyui-download", dialog);
  if (downloadButton) {
    downloadButton.disabled = !candidate.raw_json;
    if (!candidate.raw_json) downloadButton.title = "还没有保存工作流 JSON（旧条目重新上传一次即可）";
    downloadButton.onclick = async () => {
      if (!candidate.raw_json) return;
      try {
        const data = await apiGet("comfyui/download-workflow", { name: candidate.name || "" });
        downloadJsonText(data.filename || `${candidate.name || "workflow"}.json`, data.text || "");
      } catch (error) { toast(`下载失败：${readableError(error)}`, "error"); }
    };
  }
  const close = () => dialog.close();
  $(".modal-close", dialog).onclick = close; $(".cancel", dialog).onclick = close;
  $(".confirm", dialog).onclick = () => {
    const names = (existingItems || []).filter(item => item !== initial).map(item => String(item && item.name || "").trim());
    const proxy = isProxyMode(candidate.run_mode);
    const error = !candidate.name.trim() ? "请填写模板名称"
      : names.includes(candidate.name.trim()) ? "模板名称已存在，请使用唯一名称"
      : !candidate.provider.trim() ? "请选择模型提供商"
      : proxy && !candidate.raw_json ? "原生代理模式需要先上传一次 API 格式工作流 JSON"
      : !proxy && !candidate.workflow_id.trim() ? "请填写 RunningHub 工作流 ID"
      : !(candidate.nodes || []).some(node => node && node.role && node.role !== "ignore") ? "请至少映射一个参数（例如正向提示词）"
      : "";
    const box = $(".template-modal-error", dialog); box.textContent = error; box.classList.toggle("is-visible", !!error); if (error) return;
    onDone(candidate); dialog.close();
  };
  const testButton = $(".comfyui-test", dialog);
  if (testButton) testButton.onclick = async event => {
    const box = $(".template-modal-error", dialog);
    const proxyMode = isProxyMode(candidate.run_mode);
    const error = !candidate.provider.trim() ? "请选择模型提供商"
      : proxyMode && !candidate.raw_json ? "原生代理模式需要先上传一次 API 格式工作流 JSON"
      : !proxyMode && !candidate.workflow_id.trim() ? "请填写 RunningHub 工作流 / AI 应用 ID"
      : !(candidate.nodes || []).some(node => node && node.role && node.role !== "ignore") ? "请至少映射一个参数（例如正向提示词）"
      : "";
    box.textContent = error; box.classList.toggle("is-visible", !!error); if (error) return;
    const button = event.currentTarget; const original = button.innerHTML;
    button.disabled = true; button.innerHTML = `<span class="spinner"></span>测试中`;
    status.textContent = "正在检查配置与 RunningHub 连接…";
    try {
      const result = await apiPost("comfyui/test-workflow", { provider: candidate.provider, workflow: candidate });
      status.textContent = `✓ ${result.message || "工作流配置正常"}`;
      toast(result.message || "工作流配置正常");
    } catch (err) {
      status.textContent = `✕ 测试失败：${readableError(err)}`;
      toast(`工作流测试失败：${readableError(err)}`, "error");
    } finally { button.disabled = false; button.innerHTML = original; }
  };
  dialog.showModal();
}

// 取图来源的稳定顺序（新加的项按这个顺序追加到右侧，而不是按点击先后随便排）
const IMAGE_SOURCE_ORDER = ["message", "quoted", "forward", "avatar"];
const IMAGE_SOURCES = {
  message: { label: "本条消息的图", hint: "直接发在消息里的图片" },
  quoted: { label: "引用消息的图", hint: "回复别人那条消息里的图片" },
  forward: { label: "合并转发的图", hint: "合并转发聊天记录里的图片" },
  avatar: { label: "被 @ 的人的头像", hint: "取第一个被 @ 的人的头像" },
};

// 把逗号串拆成干净的来源列表：去空、去重、丢弃不认识的词，保持原有先后
function parseImagePriority(raw) {
  const seen = new Set();
  const list = [];
  String(raw || "").split(",").forEach(part => {
    const key = part.trim();
    if (!key || seen.has(key) || !IMAGE_SOURCES[key]) return;
    seen.add(key);
    list.push(key);
  });
  return list;
}

// 取图顺序：上方浮块＝已启用（从左到右即优先级），下方＝还没启用的，点一下加到右侧
function imagePriorityField(value, onChange) {
  const wrap = document.createElement("div");
  wrap.className = "priority-field";
  let current = parseImagePriority(value === undefined ? IMAGE_SOURCE_ORDER.join(",") : value);
  // 排序方式：buttons=用 ← → 按钮；drag=按住浮块拖动。二选一，互斥
  let sortMode = String(state.draft.extract_priority_sort_mode || "buttons") === "drag" ? "drag" : "buttons";

  const emit = next => {
    current = next;
    draw();
    onChange(current.join(","));
  };

  // 切换排序方式：只改显示方式，不动当前的顺序与启用集合
  const setSortMode = mode => {
    if (sortMode === mode) return;
    sortMode = mode;
    draw();
    onChange(current.join(","), { sortMode: mode });
  };

  // 拖动：把 from 位置的浮块插到 to 位置，其余顺延
  const moveTo = (from, to) => {
    if (from === to || from < 0 || to < 0 || from >= current.length || to >= current.length) return;
    const next = [...current];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    emit(next);
  };

  function draw() {
    wrap.replaceChildren();

    // ---- 排序方式切换 ----
    const modeRow = document.createElement("div");
    modeRow.className = "priority-mode";
    const modeLabel = document.createElement("span");
    modeLabel.className = "priority-mode-label";
    modeLabel.textContent = "排序方式";
    const modeGroup = document.createElement("div");
    modeGroup.className = "priority-mode-group";
    modeGroup.setAttribute("role", "radiogroup");
    modeGroup.setAttribute("aria-label", "排序方式");
    [["buttons", "按钮 ← →"], ["drag", "鼠标拖动"]].forEach(([mode, label]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `priority-mode-option ${sortMode === mode ? "is-active" : ""}`;
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", String(sortMode === mode));
      button.textContent = label;
      button.addEventListener("click", () => setSortMode(mode));
      modeGroup.append(button);
    });
    modeRow.append(modeLabel, modeGroup);
    wrap.append(modeRow);

    const activeLabel = document.createElement("div");
    activeLabel.className = "priority-label";
    activeLabel.textContent = sortMode === "drag"
      ? "已启用（从左到右依次尝试，取到即停；按住浮块左右拖动可调整先后）"
      : "已启用（从左到右依次尝试，取到即停）";
    wrap.append(activeLabel);

    const tray = document.createElement("div");
    tray.className = `priority-tray ${sortMode === "drag" ? "is-drag-mode" : ""}`;
    if (!current.length) {
      const empty = document.createElement("span");
      empty.className = "priority-empty";
      empty.textContent = "还没有启用任何来源，提取一定会失败";
      tray.append(empty);
    }

    // 拖动状态下用一条竖线标出会落到哪里
    const marker = document.createElement("span");
    marker.className = "priority-drop-marker";
    marker.hidden = true;
    const showMarker = (index, before) => {
      const chips = $$(".priority-chip", tray);
      if (!chips.length) { marker.hidden = true; return; }
      marker.hidden = false;
      const ref = index >= chips.length ? null : chips[index];
      if (ref) tray.insertBefore(marker, before ? ref : ref.nextSibling);
      else tray.append(marker);
    };

    current.forEach((key, index) => {
      const chip = document.createElement("span");
      chip.className = "priority-chip";
      chip.dataset.index = String(index);
      const name = document.createElement("span");
      name.className = "priority-chip-name";
      name.textContent = IMAGE_SOURCES[key].label;
      name.title = IMAGE_SOURCES[key].hint;
      const order = document.createElement("span");
      order.className = "priority-chip-order";
      order.textContent = `第 ${index + 1}`;
      chip.append(order, name);

      const tools = document.createElement("span");
      tools.className = "priority-chip-tools";
      // 按钮模式才显示 ← →
      if (sortMode === "buttons") {
        const move = (delta, title, symbol) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "priority-move";
          button.title = title;
          button.setAttribute("aria-label", title);
          button.textContent = symbol;
          button.disabled = (delta < 0 && index === 0) || (delta > 0 && index === current.length - 1);
          button.addEventListener("click", () => moveTo(index, index + delta));
          return button;
        };
        tools.append(move(-1, "往前挪一位", "←"), move(1, "往后挪一位", "→"));
      } else {
        // 拖动模式：给一个抓手，提示这里能按
        const grip = document.createElement("span");
        grip.className = "priority-grip";
        grip.setAttribute("aria-hidden", "true");
        grip.innerHTML = icon("menu", 13);
        tools.append(grip);
      }

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "priority-remove";
      remove.title = "取消启用";
      remove.setAttribute("aria-label", `取消启用${IMAGE_SOURCES[key].label}`);
      // 用全局 symbol 图标，和页面其它按钮一致（内联 svg 在部分环境下渲染不出来）
      remove.innerHTML = icon("close", 13);
      remove.addEventListener("click", () => emit(current.filter(item => item !== key)));
      tools.append(remove);

      chip.append(tools);

      // ---- 拖动排序 ----
      if (sortMode === "drag") {
        chip.classList.add("is-draggable");
        chip.draggable = true;
        let dropIndex = index;
        chip.addEventListener("dragstart", event => {
          // 只要不是点在 × 上，就允许整块拖动
          state.priorityDragFrom = index;
          chip.classList.add("is-dragging");
          if (event.dataTransfer) {
            event.dataTransfer.effectAllowed = "move";
            try { event.dataTransfer.setData("text/plain", String(index)); } catch { /* 部分环境不支持 */ }
          }
        });
        chip.addEventListener("dragend", () => {
          chip.classList.remove("is-dragging");
          marker.hidden = true;
          state.priorityDragFrom = null;
        });
        chip.addEventListener("dragover", event => {
          if (state.priorityDragFrom === null || state.priorityDragFrom === undefined) return;
          event.preventDefault();
          if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
          const rect = chip.getBoundingClientRect();
          const before = event.clientX < rect.left + rect.width / 2;
          // dropIndex 以「摘掉被拖块之后」的数组为准：原位置之后的块整体前移一位
          const from = state.priorityDragFrom;
          const insertAt = before ? index : index + 1;
          dropIndex = insertAt > from ? insertAt - 1 : insertAt;
          showMarker(dropIndex, true);
        });
        chip.addEventListener("drop", event => {
          event.preventDefault();
          const from = state.priorityDragFrom;
          marker.hidden = true;
          if (from === null || from === undefined) return;
          state.priorityDragFrom = null;
          moveTo(from, dropIndex);
        });
      }

      tray.append(chip);
    });
    wrap.append(tray);

    const rest = IMAGE_SOURCE_ORDER.filter(key => !current.includes(key));
    if (rest.length) {
      const addLabel = document.createElement("div");
      addLabel.className = "priority-label";
      addLabel.textContent = "点一下加到上面（追加到最右边，优先级最低）";
      wrap.append(addLabel);
      const pool = document.createElement("div");
      pool.className = "priority-pool";
      rest.forEach(key => {
        const add = document.createElement("button");
        add.type = "button";
        add.className = "priority-add";
        add.title = IMAGE_SOURCES[key].hint;
        add.innerHTML = `<span class="priority-add-plus">+</span><span></span>`;
        add.lastChild.textContent = IMAGE_SOURCES[key].label;
        // @ 头像总开关关掉时，头像不能再启用
        const avatarBlocked = key === "avatar" && state.draft.extract_at_avatar === false;
        if (avatarBlocked) {
          add.disabled = true;
          add.classList.add("is-blocked");
          add.title = "先打开上面的「提取形象时支持 @ 头像」";
        }
        add.addEventListener("click", () => { if (!add.disabled) emit([...current, key]); });
        pool.append(add);
      });
      wrap.append(pool);
    }
  }

  draw();
  return wrap;
}

function createField(key, spec, value, onChange, nested = false) {
  const field = document.createElement("div");
  field.dataset.configKey = key;
  const wide = ["template_list", "list", "file", "object", "text", "textarea"].includes(spec.type);
  field.className = `field ${wide ? "is-wide" : ""}`;
  const title = document.createElement("div"); title.className = "field-title";
  const name = document.createElement("span"); name.textContent = spec.description || key;
  const code = document.createElement("span"); code.className = "field-key"; code.textContent = key;
  title.append(name, code); field.append(title);
  let control;
  if (["default_text_model_v2", "default_edit_model_v2"].includes(key)) control = integratedDefaultSelectField(key, value, onChange);
  else if (key === "image_providers") control = imageProvidersField(Array.isArray(value) ? value : [], onChange);
  else if (["default_text_model", "default_edit_model"].includes(key)) control = modelTemplateSelectField(key, value, onChange);
  else if (key === "astrbot_provider_id") control = astrbotProviderSelectField(value, onChange);
  else if (key === "llm_tag_provider_id") control = astrbotProviderSelectField(value, onChange);
  else if (["expand_provider_id", "extract_provider_id"].includes(key)) control = astrbotProviderSelectField(value, onChange);
  else if (key === "llm_tag_model") control = llmTagModelSelectField(value, onChange);
  else if (key === "expand_model") control = sectionModelSelectField("expand_provider_id", value, onChange);
  else if (key === "extract_model") control = sectionModelSelectField("extract_provider_id", value, onChange);
  else if (key === "comfyui_workflows") control = comfyuiWorkflowsField(Array.isArray(value) ? value : [], onChange);
  else if (key === "extract_image_priority") control = imagePriorityField(value, onChange);
  else if (key === "tag_prompt") control = expandTriggerSelectField(value, onChange);
  else if (spec.type === "template_list") control = templateListField(key, spec, Array.isArray(value) ? value : [], onChange);
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
    model_providers: `${action}模型提供商`,
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
  const providerNames = (config.model_providers || []).map(item => item && typeof item === "object" ? String(item.name || "").trim() : "").filter(Boolean);
  const duplicateProvider = providerNames.find((name, index) => providerNames.indexOf(name) !== index);
  if (duplicateProvider) return `模型提供商名称「${duplicateProvider}」已存在，请使用唯一名称`;
  const modelNames = (config.model_templates || []).filter(item => !(item && typeof item === "object" && item.comfyui)).map(item => item && typeof item === "object" ? String(item.name || "").trim() : "").filter(Boolean);
  const duplicateModel = modelNames.find((name, index) => modelNames.indexOf(name) !== index);
  if (duplicateModel) return `模型模板名称「${duplicateModel}」已存在，请使用唯一名称`;
  const triggers = (config.prompt || []).map(promptTrigger).filter(Boolean);
  const duplicateTrigger = triggers.find((trigger, index) => triggers.indexOf(trigger) !== index);
  if (duplicateTrigger) return `预设触发词「${duplicateTrigger}」已存在，请使用唯一触发词`;
  const missingProvider = (config.model_templates || []).filter(item => !(item && typeof item === "object" && item.comfyui)).find(item => item && typeof item === "object" && !providerNames.includes(String(item.provider || "").trim()));
  if (missingProvider) return `模型模板「${missingProvider.name || "未命名"}」选择的模型提供商不存在`;
  const comfyuiNames = (config.comfyui_workflows || []).map(item => item && typeof item === "object" ? String(item.name || "").trim() : "").filter(Boolean);
  const duplicateComfyui = comfyuiNames.find((name, index) => comfyuiNames.indexOf(name) !== index);
  if (duplicateComfyui) return `ComfyUI 工作流名称「${duplicateComfyui}」已存在，请使用唯一名称`;
  const clashComfyui = comfyuiNames.find(name => modelNames.includes(name));
  if (clashComfyui) return `ComfyUI 工作流名称「${clashComfyui}」与模型模板名称重复，请改名`;
  return "";
}

function openTemplateDialog(configKey, templateKey, templateSpec, initialValue, editing, validate, onSubmit, dialogOptions = {}) {
  const dialog = makeDialog("modal template-editor-modal");
  let candidate = clone(initialValue);
  let activeTemplateKey = templateKey;
  let activeTemplateSpec = templateSpec;
  dialog.innerHTML = `<div class="template-modal-head"><div><span class="eyebrow">IMAGE LAB / ${editing ? "EDIT ITEM" : "NEW ITEM"}</span><h2></h2><p></p></div><button class="modal-close" type="button" aria-label="关闭">${icon("close")}</button></div><div class="template-modal-body"><div class="template-modal-error" role="alert"></div><div class="field-grid"></div></div><div class="template-modal-foot">${configKey === "model_providers" ? `<button class="soft-button provider-test" type="button">${icon("refresh", 16)}测试连接</button>` : ""}<button class="soft-button cancel" type="button">取消</button><button class="primary-button confirm" type="button">${icon(editing ? "save" : "plus", 16)}${editing ? "保存修改" : "添加"}</button></div>`;
  $("h2", dialog).textContent = templateDialogTitle(configKey, editing);
  const subtitle = $(".template-modal-head p", dialog);
  const grid = $(".field-grid", dialog);
  const renderDialogFields = () => {
    grid.replaceChildren();
    subtitle.textContent = configKey === "model_templates" ? (candidate.provider || "请选择模型提供商") : (activeTemplateSpec?.name || activeTemplateKey);
    if (configKey === "model_templates") {
      const typeField = document.createElement("div"); typeField.className = "field is-wide template-type-field";
      const title = document.createElement("div"); title.className = "field-title"; title.innerHTML = `<span>选择模型提供商</span><span class="field-key">provider</span>`;
      const select = document.createElement("select");
      const providerNames = dialogOptions.providerNames || [];
      providerNames.forEach(name => select.add(new Option(name, name)));
      if (!providerNames.length) select.add(new Option("请先添加模型提供商", ""));
      select.value = candidate.provider || "";
      select.disabled = !providerNames.length;
      select.addEventListener("change", () => { candidate.provider = select.value; subtitle.textContent = candidate.provider; });
      const hint = document.createElement("p"); hint.className = "field-hint"; hint.textContent = "使用所选提供商卡片中的类型、API URL 和 Key";
      typeField.append(title, select, hint); grid.append(typeField);
    }
    Object.entries(activeTemplateSpec?.items || {}).filter(([fieldKey]) => {
      if (fieldKey === "provider") return false;
      if (configKey === "prompt" && (fieldKey === "model" || fieldKey === "params")) return false;
      if (configKey !== "model_providers") return true;
      const astrbotType = String(candidate.type || "").toLowerCase() === "astrbot";
      if (["type", "name"].includes(fieldKey)) return true;
      return astrbotType ? ["astrbot_provider_id", "astrbot_protocol"].includes(fieldKey) : ["api_key", "base_url"].includes(fieldKey);
    }).forEach(([fieldKey, fieldSpec]) => {
      grid.append(createField(fieldKey, fieldSpec, candidate[fieldKey], next => {
        if (configKey === "model_providers" && fieldKey === "type") {
          const knownUrls = Object.values(PROVIDER_DEFAULT_URLS);
          if (!candidate.base_url || knownUrls.includes(candidate.base_url)) candidate.base_url = PROVIDER_DEFAULT_URLS[next] || "";
          candidate[fieldKey] = next;
          renderDialogFields();
          return;
        }
        candidate[fieldKey] = next;
      }, true));
    });
    if (configKey === "prompt") {
      if (!candidate.params || typeof candidate.params !== "object" || Array.isArray(candidate.params)) candidate.params = {};
      candidate.model = String(candidate.model || "");

      const modelField = document.createElement("div"); modelField.className = "field is-wide preset-model-field";
      modelField.innerHTML = `<div class="field-title"><span>默认模型</span><span class="field-key">model</span></div>`;
      modelField.append(presetModelSelect(candidate.model, next => { candidate.model = next; keepViewPosition(renderDialogFields); }));
      const modelHint = document.createElement("p"); modelHint.className = "field-hint"; modelHint.textContent = "触发此预设时使用该模型；留空则跟随「默认模型选择」的全局默认。选择后可在下方从该模型调取参数。";
      modelField.append(modelHint);
      grid.append(modelField);

      const paramsField = document.createElement("div"); paramsField.className = "field is-wide preset-params-field";
      paramsField.innerHTML = `<div class="field-title"><span>固定参数</span><span class="field-key">params</span></div>`;
      paramsField.append(presetParamsEditor(() => candidate.params, () => candidate.model, () => {}));
      const paramsHint = document.createElement("p"); paramsHint.className = "field-hint"; paramsHint.textContent = "触发此预设时自动附加，等效于 --参数 值；用户消息里的同名 --参数 会覆盖这里。ComfyUI 模型可直接从工作流映射调取参数。";
      paramsField.append(paramsHint);
      grid.append(paramsField);
    }
  };
  renderDialogFields();
  const close = () => dialog.close();
  $(".modal-close", dialog).addEventListener("click", close);
  $(".cancel", dialog).addEventListener("click", close);
  const testButton = $(".provider-test", dialog);
  if (testButton) testButton.addEventListener("click", async () => {
    const original = testButton.innerHTML;
    testButton.disabled = true;
    testButton.innerHTML = `<span class="spinner"></span>测试中`;
    try {
      const result = await apiPost("providers/test", { provider: candidate });
      toast(result.message || "连接成功");
    } catch (error) {
      toast(`连接失败：${readableError(error)}`, "error");
    } finally {
      testButton.disabled = false;
      testButton.innerHTML = original;
    }
  });
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
  let searchText = "";
  let statusFilter = "";
  const templates = spec.templates || {};
  const templateKeys = Object.keys(templates);
  const providerNames = () => (state.draft.model_providers || []).map(item => String(item?.name || "").trim()).filter(Boolean);
  const emit = () => onChange(clone(items));
  const itemLabel = (item, key) => key === "rate_limit_rule" ? `${item.window_seconds || 0} 秒内 ${item.max_count || 0} 次` : (item.name || item.trigger || templates[key]?.name || "未命名模板");
  const makeDefaults = key => {
    const result = { __template_key: key };
    Object.entries(templates[key]?.items || {}).forEach(([fieldKey, fieldSpec]) => { if (fieldKey !== "provider") result[fieldKey] = fieldDefault(fieldSpec); });
    if (configKey === "model_templates") {
      const names = providerNames();
      result.provider = names.includes("OpenAI") ? "OpenAI" : (names[0] || "");
    }
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
    if (configKey === "model_templates" && !String(candidate.provider || "").trim()) return "请选择模型提供商";
    if (configKey === "model_providers" && !String(candidate.name || "").trim()) return "提供商名称不能为空";
    if (configKey === "model_providers" && String(candidate.type || "").toLowerCase() === "astrbot" && !String(candidate.astrbot_provider_id || "").trim()) return "请选择 AstrBot 模型提供商";
    if (configKey === "prompt" && !String(candidate.trigger || "").trim()) return "预设触发词不能为空";
    const nextItems = items.map((item, index) => index === editingIndex ? candidate : item);
    if (editingIndex < 0) nextItems.push(candidate);
    return validateUniqueCollections({
      model_providers: configKey === "model_providers" ? nextItems : (state.draft.model_providers || []),
      model_templates: configKey === "model_templates" ? nextItems : (configKey === "model_providers" ? [] : (state.draft.model_templates || [])),
      prompt: configKey === "prompt" ? nextItems : (state.draft.prompt || []),
    });
  };
  const commitAndKeepPosition = callback => {
    keepViewPosition(() => { callback(); emit(); render(); });
  };
  const render = () => {
    root.replaceChildren();
    if (configKey === "prompt") {
      const toolbar = document.createElement("div"); toolbar.className = "preset-toolbar";
      const search = document.createElement("input"); search.type = "search";
      search.placeholder = "搜索触发词或提示词"; search.value = searchText;
      search.setAttribute("aria-label", "搜索预设");
      const status = document.createElement("select");
      [["", "全部状态"], ["on", "已启用"], ["off", "已停用"]].forEach(([value, label]) => status.add(new Option(label, value)));
      status.value = statusFilter; status.setAttribute("aria-label", "预设状态筛选");
      const filter = () => {
        $$(".template-card", root).forEach(card => {
          const enabled = card.dataset.enabled === "true";
          card.hidden = !card.dataset.search.includes(searchText) || (statusFilter === "on" && !enabled) || (statusFilter === "off" && enabled);
        });
      };
      search.addEventListener("input", () => { searchText = search.value.toLowerCase().trim(); filter(); });
      status.addEventListener("change", () => { statusFilter = status.value; filter(); });
      toolbar.append(search, status); root.append(toolbar);
      requestAnimationFrame(filter);
    }
    items.forEach((item, index) => {
      const templateKey = item.__template_key || templateKeys[0] || "";
      const template = templates[templateKey] || templates[templateKeys[0]] || { items: {} };
      const details = document.createElement("div"); details.className = "template-card";
      details.dataset.search = `${itemLabel(item, templateKey)} ${typeof item === "object" ? item.prompt || "" : item}`.toLowerCase();
      details.dataset.enabled = String(!(item && typeof item === "object" && item.enabled === false));
      const summary = document.createElement("div"); summary.className = "template-card-head"; summary.tabIndex = 0; summary.setAttribute("role", "button"); summary.setAttribute("aria-label", `修改${itemLabel(item, templateKey)}`);
      const label = document.createElement("span"); label.className = "template-name"; label.textContent = itemLabel(item, templateKey);
      const provider = document.createElement("span"); provider.className = "provider-chip"; provider.textContent = configKey === "model_providers" ? (item.type || "未选择类型") : (item.provider || "未选择提供商");
      summary.append(label); if (["model_templates", "model_providers"].includes(configKey)) summary.append(provider);
      if (configKey === "prompt") {
        const enabled = !(item && typeof item === "object" && item.enabled === false);
        details.classList.toggle("is-disabled", !enabled);
        const toggle = document.createElement("button");
        toggle.type = "button"; toggle.className = `switch ${enabled ? "is-on" : ""}`;
        toggle.setAttribute("role", "switch"); toggle.setAttribute("aria-checked", String(enabled));
        toggle.setAttribute("aria-label", `启用预设 ${itemLabel(item, templateKey)}`);
        toggle.addEventListener("click", event => {
          event.stopPropagation();
          commitAndKeepPosition(() => { const next = editableItem(item, templateKey); next.enabled = !enabled; items[index] = next; });
        });
        summary.append(toggle);
      }
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "template-remove"; remove.title = "删除"; remove.innerHTML = icon("trash", 16);
      remove.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); commitAndKeepPosition(() => items.splice(index, 1)); }); summary.append(remove);
      const openEditor = event => {
        if (event.target.closest("select, button")) return;
        openTemplateDialog(configKey, templateKey, template, editableItem(item, templateKey), true, candidate => validateCandidate(candidate, index), candidate => commitAndKeepPosition(() => { items[index] = candidate; }), { templates, makeDefaults, providerNames: providerNames() });
      };
      summary.addEventListener("click", openEditor);
      summary.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openEditor(event); } });
      details.append(summary); root.append(details);
    });
    if (templateKeys.length) {
      const addRow = document.createElement("div"); addRow.className = "template-add";
      const addLabel = { model_providers: "添加提供商", model_templates: "添加模板", prompt: "添加预设", rate_limit_rules: "添加规则" }[configKey] || "添加模板";
      const add = document.createElement("button"); add.type = "button"; add.className = "soft-button"; add.innerHTML = `${icon("plus", 15)}${esc(addLabel)}`;
      add.addEventListener("click", () => {
        const templateKey = configKey === "model_templates" && templates.openapi_text ? "openapi_text" : templateKeys[0];
        const initialValue = makeDefaults(templateKey);
        if (configKey === "model_templates") initialValue.name = "";
        openTemplateDialog(configKey, templateKey, templates[templateKey], initialValue, false, candidate => validateCandidate(candidate), candidate => commitAndKeepPosition(() => items.push(candidate)), { templates, makeDefaults, providerNames: providerNames() });
      }); addRow.append(add); root.append(addRow);
    }
  };
  render(); return root;
}

function renderProvidersPage() {
  const page = $("#page-providers");
  page.querySelectorAll(":scope > .providers-ui").forEach(node => node.remove());
  const ui = document.createElement("div"); ui.className = "providers-ui";
  ui.innerHTML = `<div class="provider-page-host"></div>`;
  page.append(ui);
  $(".provider-page-host", ui).append(imageProvidersField(state.draft.image_providers || [], next => { state.draft.image_providers = next; setDirty(true); }));
  setDirty(state.dirty);
}

// ---------------- 扩写与提取（同一页，页内切换） ----------------
const COMMAND_SECTIONS = {
  expand: {
    title: "扩写提示词",
    short: "扩写",
    addLabel: "添加扩写指令",
    triggerHint: "消息以该词开头时触发，例如「扩 一只晒太阳的猫」",
    promptHint: "{{user_text}} 会被替换为用户输入；只回复模型输出，不生成图片",
    needImage: false,
    empty: "还没有扩写指令。添加一个触发词，就能把一句话扩写成完整提示词。",
    intro: "用户发送「触发词 + 简短描述」时，用对话模型扩写并只回复文字；不生成图片，不消耗生图额度。",
    providerKey: "expand_provider_id",
    modelKey: "expand_model",
    timeoutKey: "expand_global_timeout",
  },
  extract: {
    title: "提取图片形象",
    short: "提取",
    addLabel: "添加提取指令",
    triggerHint: "消息以该词开头时触发，例如「形象」+ 图片",
    promptHint: "图片会一起发给模型，不需要 {{user_text}}；可补充侧重（只管发型/只管服装）",
    needImage: true,
    empty: "还没有形象提取指令。添加一个触发词，发图即可读出可复现的角色特征。",
    intro: "用户发送「触发词 + 图片」时，用对话模型读取图片并只回复文字；不生成图片，不消耗生图额度。",
    providerKey: "extract_provider_id",
    modelKey: "extract_model",
    timeoutKey: "extract_global_timeout",
  },
};

if (!state.commandSection) state.commandSection = "expand";
if (!state.labSection) state.labSection = "command";
// 当前展开的指令卡片（键为 `${section}:${trigger}`，同一时间只展开一张）
if (!state.commandOpenKey) state.commandOpenKey = "";

// 文字加工：四个页面，切换方式与「插件配置」完全一致（左下角分类按钮 + 向上展开的分类菜单）
const LAB_SECTIONS = [
  { key: "command", label: "文字加工配置" },
  { key: "expand", label: "扩写指令编辑" },
  { key: "extract", label: "提取指令编辑" },
  { key: "log", label: "文字加工历史" },
];

function setLabSection(key, options = {}) {
  const target = LAB_SECTIONS.some(item => item.key === key) ? key : "command";
  state.labSection = target;
  $$("#page-textlab .config-section").forEach(node => node.classList.toggle("is-active", node.dataset.labSection === target));
  $$("#lab-category-nav button").forEach(node => node.classList.toggle("is-active", node.dataset.labSection === target));
  const label = $("#page-textlab .category-launcher-label");
  const current = LAB_SECTIONS.find(item => item.key === target);
  if (label && current) label.textContent = current.label;
  $("#lab-category-nav")?.classList.remove("is-open");
  $("#lab-category-launcher")?.setAttribute("aria-expanded", "false");
  if (target === "command") renderLabConfig();
  if (target === "expand" || target === "extract") {
    // 首次进入（还没拿到数据）先显示加载态并去拉；拿到数据后由 loadCommandPage 重绘
    if (state.commandLoaded[target]) renderCommandPage(target);
    else {
      showCommandLoading(target);
      // 从侧边分类切换进来时 showPage 不一定经过这里，必须自己触发读取，
      // 否则会一直停在“正在读取指令”，且此时保存会把空列表当成用户意图。
      loadCommandPage();
    }
  }
  if (target === "log") { loadLlmHistory(); loadLlmStats(); }
}

function initLabChrome() {
  const nav = $("#lab-category-nav");
  const launcher = $("#lab-category-launcher");
  if (!nav || !launcher) return;
  if (!nav.children.length) {
    LAB_SECTIONS.forEach(item => {
      const button = document.createElement("button");
      button.type = "button"; button.dataset.labSection = item.key; button.textContent = item.label;
      button.classList.toggle("is-active", (state.labSection || "command") === item.key);
      button.addEventListener("click", () => guardUnsaved(() => setLabSection(item.key)));
      nav.append(button);
    });
    launcher.addEventListener("click", event => {
      event.stopPropagation();
      const open = nav.classList.toggle("is-open");
      launcher.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", event => {
      if (!nav.classList.contains("is-open")) return;
      if (nav.contains(event.target) || launcher.contains(event.target)) return;
      nav.classList.remove("is-open");
      launcher.setAttribute("aria-expanded", "false");
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape" || !nav.classList.contains("is-open")) return;
      nav.classList.remove("is-open");
      launcher.setAttribute("aria-expanded", "false");
    });
  }
}

async function ensureConfigLoaded() {
  if (Object.keys(state.schema).length) return true;
  try { await fetchConfigData(); return true; } catch { return false; }
}

async function loadCommandPage() {
  const target = state.labSection === "extract" ? "extract" : "expand";
  const panel = commandPanel(target);
  if (!panel) return;
  if (!panel.children.length) showCommandLoading(target);
  const needsConfig = !Object.keys(state.schema).length;
  try {
    const [data] = await Promise.all([apiGet("commands/presets"), needsConfig ? ensureConfigLoaded() : null]);
    ["expand", "extract"].forEach(section => {
      state.commandPresets[section] = Array.isArray(data[section]) ? clone(data[section]) : [];
    });
    state.commandProviders = Array.isArray(data.providers) ? data.providers : [];
  } catch (error) {
    if (state.commandLoaded[target]) { toast(`指令读取失败，仍在显示上次的内容：${readableError(error)}`, "error"); return; }
    const box = document.createElement("div"); box.className = "loading-card is-failed";
    const label = document.createElement("span"); label.textContent = `读取失败：${readableError(error)}`;
    const retry = document.createElement("button"); retry.type = "button"; retry.className = "soft-button"; retry.textContent = "重试";
    retry.addEventListener("click", () => { panel.replaceChildren(); loadCommandPage(); });
    box.append(label, retry);
    panel.replaceChildren(box);
    return;
  }
  state.commandOpenKey = "";
  state.commandDraft = null;
  state.commandLoaded.expand = true;
  state.commandLoaded.extract = true;
  // 记下这份页面“加载时看到的”触发词：保存时发给后端做基线对比，
  // 这样过期草稿（部署前就打开的标签页）不可能删掉它从没见过的指令。
  ["expand", "extract"].forEach(section => {
    state.commandBase[section] = (state.commandPresets[section] || [])
      .map(item => String((item && item.trigger) || "").trim())
      .filter(Boolean);
  });
  renderCommandPage(target);
}

function commandPanel(section) {
  return $(section === "extract" ? "#extract-panel" : "#expand-panel");
}

function showCommandLoading(section) {
  const panel = commandPanel(section);
  if (!panel) return;
  if (panel.querySelector(".loading-card")) return;
  const box = document.createElement("div");
  box.className = "loading-card";
  const spinner = document.createElement("span"); spinner.className = "spinner";
  const label = document.createElement("span"); label.textContent = "正在读取指令…";
  box.append(spinner, label);
  panel.replaceChildren(box);
}

// 页1：只放全局设置（表单直接写进 state.draft，右下角保存按钮统一提交）
const LAB_CONFIG_GROUPS = [
  {
    title: "生图预处理（扩写）",
    description: "出图前先用「扩写指令编辑」里的指令把用户原话处理成完整提示词。关掉总开关、或某条预设选「不预处理」时，用户原话会直接提交给画图模型。",
    section: "",
    fields: [["llm_tag_enable"], ["llm_tag_fallback"]],
  },
  {
    title: "扩写 · 模型、超时与独立会话",
    description: "群里发「触发词 + 一句话」时用这里的模型扩写成完整提示词。独立会话开启后，每次调用都带一次性会话标识，避免反代把不同请求串成同一段对话；单条指令可单独覆盖。",
    section: "expand",
    fields: [["expand_provider_id"], ["expand_model"], ["expand_global_timeout"], ["expand_isolation"]],
  },
  {
    title: "提取 · 模型、超时与独立会话",
    description: "群里发「触发词 + 图片」时用这里的模型读图，需要支持视觉的模型。独立会话开关只作用于提取形象，不影响扩写。",
    section: "extract",
    fields: [["extract_provider_id"], ["extract_model"], ["extract_global_timeout"], ["extract_isolation"]],
  },
  {
    title: "提取 · 找图顺序",
    description: "按顺序找图，找到就用；头像只在前面都没有图、并且开关打开时才用。",
    section: "",
    fields: [["extract_image_priority"], ["extract_at_avatar"]],
  },
  {
    title: "回复消息",
    description: "开始提示在调用前发一句，结束提示接在结果后面；{trigger} 会替换成触发词。",
    section: "",
    fields: [["expand_start_message"], ["extract_start_message"], ["expand_end_message"], ["extract_end_message"], ["command_end_at_sender"]],
  },
  {
    title: "调用历史",
    description: "「文字加工历史」里最多保留多少条记录。",
    section: "",
    fields: [["llm_history_limit"]],
  },
];

// 配置读不到时的失败卡：给出「重试」按钮，由用户决定是否再请求
function showLabConfigFailed(host) {
  if (!host) return;
  host.replaceChildren();
  const box = document.createElement("div");
  box.className = "loading-card is-failed";
  const label = document.createElement("span");
  label.textContent = "配置读取失败，暂时读不到可用的配置项";
  const retry = document.createElement("button");
  retry.type = "button"; retry.className = "soft-button"; retry.textContent = "重试";
  retry.addEventListener("click", () => {
    state.configRetried = false;
    host.replaceChildren();
    host.innerHTML = `<div class="loading-card"><span class="spinner"></span>正在读取配置…</div>`;
    ensureConfigLoaded().then(ok => { if (ok) renderLabConfig(); else showLabConfigFailed(host); });
  });
  box.append(label, retry);
  host.append(box);
}

function renderLabConfig() {
  const host = $("#lab-global-config");
  if (!host) return;
  // 配置还没读到：显示加载态（读到了但个别字段不存在，就只隐藏那几个字段）
  if (!Object.keys(state.schema).length) {
    // 只自动重试一次：接口正常返回但 schema 为空时，无脑递归会变成无限请求，把页面卡死
    if (!state.configRetried) {
      state.configRetried = true;
      host.replaceChildren();
      host.innerHTML = `<div class="loading-card"><span class="spinner"></span>正在读取配置…</div>`;
      ensureConfigLoaded().then(ok => { if (ok) renderLabConfig(); else showLabConfigFailed(host); });
      return;
    }
    showLabConfigFailed(host);
    return;
  }
  state.configRetried = false;
  host.replaceChildren();
  LAB_CONFIG_GROUPS.forEach(group => {
    const available = group.fields.filter(keys => keys.some(key => state.schema[key]));
    if (!available.length) return;
    const card = document.createElement("section");
    card.className = "lab-group";
    const head = document.createElement("div"); head.className = "lab-group-head";
    const title = document.createElement("h3"); title.textContent = group.title; head.append(title);
    if (group.description) {
      const desc = document.createElement("p"); desc.textContent = group.description; head.append(desc);
    }
    const grid = document.createElement("div"); grid.className = "field-grid";
    available.forEach(keys => {
      const keep = keys.filter(key => state.schema[key]);
      if (!keep.length) return;
      // 同一行里放几个字段：一个就占满整行，两个并排
      const row = document.createElement("div");
      row.className = `lab-field-row ${keep.length > 1 ? "is-pair" : ""}`;
      keep.forEach(key => row.append(createField(key, state.schema[key], state.draft[key], (next, meta) => {
        if (key === "extract_image_priority" && meta && meta.sortMode) {
          // 排序方式独立保存，不跟着取图顺序的值混在一起
          state.draft.extract_priority_sort_mode = meta.sortMode;
        }
        state.draft[key] = next;
        setDirty(true);
        if (["expand_provider_id", "extract_provider_id"].includes(key)) requestAnimationFrame(renderLabConfig);
        // @ 头像总开关会影响上面「取图顺序」里头像能不能启用，改完重绘这一页
        if (key === "extract_at_avatar") requestAnimationFrame(renderLabConfig);
      })));
      grid.append(row);
    });
    card.append(head, grid);
    if (group.section) {
      const spec = COMMAND_SECTIONS[group.section];
      const providerId = String(state.draft[spec.providerKey] || "");
      if (providerId) {
        const note = document.createElement("p"); note.className = "lab-group-note";
        note.textContent = `没有单独指定提供商 / 模型的指令，会跟着这里走（${providerId}）。`;
        card.append(note);
      }
    }
    host.append(card);
  });
  setDirty(state.dirty);
}

// 页2 / 页3：卡片只显示触发词，点一下就地展开，一条指令的全部字段都在展开区里
if (!state.commandDraft) state.commandDraft = null;   // {section, index(null=新增), item}

function renderCommandPage(section) {
  const scrollPosition = window.scrollY;
  const panel = commandPanel(section);
  if (!panel) return;
  // 重绘期间锁住高度，避免「展开卡片被移除 → 页面瞬间变矮 → 再长回来」的剧烈抖动
  const reservedHeight = panel.getBoundingClientRect().height;
  if (reservedHeight > 0) panel.style.minHeight = `${reservedHeight}px`;
  const release = () => { panel.style.minHeight = ""; };
  const spec = COMMAND_SECTIONS[section];
  const items = state.commandPresets[section] || [];
  panel.replaceChildren();

  const head = document.createElement("div"); head.className = "panel-title";
  const headLeft = document.createElement("div");
  const h2 = document.createElement("h2"); h2.textContent = spec.title;
  const pill = document.createElement("span"); pill.className = "count-pill"; pill.textContent = `${items.length} 条`;
  headLeft.append(h2, pill);
  const actions = document.createElement("div"); actions.className = "panel-actions";
  const reload = document.createElement("button"); reload.type = "button"; reload.className = "icon-button"; reload.title = "重新读取";
  reload.innerHTML = icon("refresh", 18);
  reload.addEventListener("click", () => guardUnsaved(() => { state.commandOpenKey = ""; state.commandDraft = null; loadCommandPage(); }));
  const add = document.createElement("button"); add.type = "button"; add.className = "primary-button";
  add.innerHTML = `${icon("plus", 16)}${esc(spec.addLabel)}`;
  add.addEventListener("click", () => guardUnsaved(() => openNewCommand(section)));
  actions.append(reload, add);
  head.append(headLeft, actions);
  panel.append(head);

  if (!items.length && !isNewDraft(section)) {
    const empty = document.createElement("div"); empty.className = "empty-state"; empty.style.display = "grid";
    empty.innerHTML = `<div class="empty-orbit">${icon("sparkles", 28)}</div><h3>${esc(spec.empty)}</h3>`;
    panel.append(empty);
    release();
    return;
  }

  const list = document.createElement("div"); list.className = "command-list";
  items.forEach((item, i) => list.append(commandCard(section, item, i)));
  if (isNewDraft(section)) list.append(commandCard(section, null, null));
  panel.append(list);
  requestAnimationFrame(() => {
    release();
    window.scrollTo(0, scrollPosition);
  });
}

function isNewDraft(section) {
  const draft = state.commandDraft;
  return Boolean(draft && draft.section === section && draft.index === null);
}

// 卡片右侧的小字：停用标记 / 跟随全局 / 具体提供商
function commandCardMeta(item) {
  if (!item) return "新指令";
  const parts = [];
  if (item.enabled === false) parts.push("已停用");
  if (item.provider_id) parts.push(item.provider_id);
  else parts.push("跟随全局");
  return parts.join(" · ");
}

function commandCard(section, item, index) {
  const key = item ? `${section}:${item.trigger}` : `${section}:__new__`;
  const isOpen = state.commandOpenKey === key;
  const card = document.createElement("div");
  card.className = "command-card";
  if (isOpen) card.classList.add("is-open");
  if (item && item.enabled === false) card.classList.add("is-disabled");

  const head = document.createElement("div");
  head.className = "command-card-head";
  head.tabIndex = 0;
  head.setAttribute("role", "button");
  head.setAttribute("aria-expanded", String(isOpen));
  const trigger = document.createElement("span"); trigger.className = "command-trigger";
  trigger.textContent = item ? String(item.trigger || "") : "新指令";
  const meta = document.createElement("span"); meta.className = "command-meta"; meta.textContent = commandCardMeta(item);
  const chevron = document.createElement("span"); chevron.className = "command-chevron"; chevron.innerHTML = icon("chevron", 16);
  head.append(trigger, meta, chevron);
  const toggle = () => {
    if (isOpen) {
      // 收起：有未保存修改先问一句
      guardUnsaved(() => { state.commandOpenKey = ""; state.commandDraft = null; renderCommandPage(section); });
      return;
    }
    guardUnsaved(() => { state.commandOpenKey = key; state.commandDraft = item ? { section, index, item: clone(item) } : state.commandDraft; renderCommandPage(section); });
  };
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", event => { if (event.target === head && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); toggle(); } });
  card.append(head);

  if (isOpen) {
    const draft = state.commandDraft && state.commandDraft.section === section ? state.commandDraft : { section, index, item: item ? clone(item) : null };
    state.commandDraft = draft;
    card.append(commandEditorBody(section, draft, () => {
      // 草稿里的提供商 / 启用状态一变，卡片头的小字立刻跟上
      meta.textContent = commandCardMeta(draft.item);
      card.classList.toggle("is-disabled", Boolean(draft.item && draft.item.enabled === false));
    }));
  }
  return card;
}

// 有未保存的编辑时先确认；确认=放弃修改并执行 action
function draftDirty(draft) {
  if (!draft || !draft.item) return false;
  if (draft.index === null) {                        // 新指令：填了触发词或正文才算有内容
    const item = draft.item || {};
    return Boolean(String(item.trigger || "").trim() || String(item.prompt || "").trim() || String(item.system_prompt || "").trim());
  }
  const original = (state.commandPresets[draft.section] || [])[draft.index];
  if (!original) return false;
  return JSON.stringify(normalizeDraft(draft.item)) !== JSON.stringify(normalizeDraft(original));
}

function normalizeDraft(item) {
  const pick = value => String(value || "").trim();
  return {
    trigger: pick(item.trigger),
    prompt: String(item.prompt || ""),
    system_prompt: String(item.system_prompt || ""),
    provider_id: pick(item.provider_id),
    model: pick(item.model),
    timeout: Number(item.timeout || 0),
    enabled: item.enabled !== false,
    isolation: ["on", "off"].includes(String(item.isolation || "")) ? String(item.isolation) : "",
  };
}

async function guardUnsaved(action) {
  if (!draftDirty(state.commandDraft)) { action(); return; }
  const ok = await confirmAction("将当前编辑保留到草稿并继续？", "确认后保留当前卡片内容，最后统一点击右下角保存。取消则留在当前编辑位置。");
  if (!ok) return;
  const draft = state.commandDraft;
  const next = Object.assign({}, draft.item, normalizeDraft(draft.item));
  if (!next.trigger) { toast("触发词不能为空", "error"); return; }
  const items = [...(state.commandPresets[draft.section] || [])];
  if (items.some((item, i) => i !== draft.index && item.trigger === next.trigger)) { toast("触发词重复", "error"); return; }
  if (draft.index === null) items.push(next); else items[draft.index] = next;
  if (!stageCommandItems(draft.section, items)) return;
  state.commandDraft = null;
  action();
}

// 展开区：所有改动都写进草稿，点「保存」才提交
function commandEditorBody(section, draft, onChange = () => {}) {
  const spec = COMMAND_SECTIONS[section];
  const item = draft.item;
  const body = document.createElement("div"); body.className = "command-card-body";
  const grid = document.createElement("div"); grid.className = "field-grid";

  const field = (title, hint, control, wide = false) => {
    const wrap = document.createElement("div"); wrap.className = `field ${wide ? "is-wide" : ""}`;
    const head = document.createElement("div"); head.className = "field-title";
    const label = document.createElement("span"); label.textContent = title; head.append(label);
    wrap.append(head, control);
    if (hint) { const p = document.createElement("p"); p.className = "field-hint"; p.textContent = hint; wrap.append(p); }
    return wrap;
  };

  // 触发词：实时校验空值和重名
  const triggerInput = document.createElement("input");
  triggerInput.value = item.trigger || "";
  triggerInput.placeholder = section === "extract" ? "例如 形象" : "例如 扩";
  const triggerHint = document.createElement("p"); triggerHint.className = "field-hint";
  const validateTrigger = () => {
    const value = triggerInput.value.trim();
    const items = state.commandPresets[section] || [];
    const clash = items.some((other, i) => i !== draft.index && String(other.trigger || "").trim() === value);
    triggerInput.classList.toggle("is-invalid", !value || clash);
    triggerHint.textContent = !value ? "触发词不能为空"
      : (clash ? `「${value}」已经被另一条指令用掉了` : spec.triggerHint);
    triggerHint.classList.toggle("is-error", !value || clash);
  };
  triggerInput.addEventListener("input", () => { item.trigger = triggerInput.value; validateTrigger(); });
  const triggerField = field("触发词", "", triggerInput);
  triggerField.append(triggerHint);
  validateTrigger();
  grid.append(triggerField);

  const switcher = document.createElement("button");
  switcher.type = "button";
  switcher.className = `switch ${item.enabled === false ? "" : "is-on"}`;
  switcher.setAttribute("role", "switch");
  switcher.setAttribute("aria-checked", String(item.enabled !== false));
  const switchRow = document.createElement("div"); switchRow.className = "switch-row";
  const switchText = document.createElement("span"); switchText.className = "switch-state";
  switchText.textContent = item.enabled === false ? "已停用" : "启用中";
  switchRow.append(switchText, switcher);
  switcher.addEventListener("click", () => {
    item.enabled = item.enabled === false;
    const on = item.enabled !== false;
    switcher.classList.toggle("is-on", on);
    switcher.setAttribute("aria-checked", String(on));
    switchText.textContent = on ? "启用中" : "已停用";
    onChange();
  });
  grid.append(field("启用", "", switchRow));

  const userTemplate = document.createElement("textarea");
  userTemplate.rows = 3;
  userTemplate.value = item.prompt || "";
  userTemplate.placeholder = section === "extract" ? "留空即只把图片发给模型，用户补充的说明会自动接在后面" : "{{user_text}} 会被替换为用户输入";
  userTemplate.addEventListener("input", () => { item.prompt = userTemplate.value; });
  grid.append(field("用户内容模板", spec.promptHint, userTemplate, true));

  const systemPrompt = document.createElement("textarea");
  systemPrompt.rows = 5;
  systemPrompt.className = "command-system-prompt";
  systemPrompt.value = item.system_prompt || "";
  systemPrompt.placeholder = "写给模型的规则";
  systemPrompt.addEventListener("input", () => { item.system_prompt = systemPrompt.value; });
  grid.append(field("系统提示词", "", systemPrompt, true));

  // 提供商：留空 = 跟随「文字加工配置」
  const globalProvider = String(state.draft[spec.providerKey] || "");
  const providerSelect = document.createElement("select");
  providerSelect.add(new Option(globalProvider ? `跟随全局（${globalProvider}）` : "跟随全局（当前会话默认模型）", ""));
  (state.commandProviders || []).forEach(p => providerSelect.add(new Option(String(p.id), String(p.id))));
  const currentProvider = String(item.provider_id || "");
  if (currentProvider && !(state.commandProviders || []).some(p => String(p.id) === currentProvider)) {
    providerSelect.add(new Option(`${currentProvider}（当前未连接）`, currentProvider));
  }
  providerSelect.value = currentProvider;
  grid.append(field("模型提供商", "", providerSelect));

  // 模型：选了提供商也能选，只是提示会用提供商默认；不再禁用
  const modelSelect = document.createElement("select");
  const syncModels = () => {
    const pid = providerSelect.value;
    const provider = (state.commandProviders || []).find(p => String(p.id) === pid);
    modelSelect.replaceChildren();
    modelSelect.add(new Option(pid ? "提供商默认模型" : "跟随全局模型", ""));
    (provider && Array.isArray(provider.models) ? provider.models : []).forEach(m => modelSelect.add(new Option(m, m)));
    const current = String(item.model || "");
    if (current && ![...modelSelect.options].some(o => o.value === current)) modelSelect.add(new Option(`${current}（当前未列出）`, current));
    modelSelect.value = current;
  };
  providerSelect.addEventListener("change", () => {
    item.provider_id = providerSelect.value;
    item.model = "";
    modelSelect.value = "";
    syncModels();
    onChange();
  });
  modelSelect.addEventListener("change", () => { item.model = modelSelect.value || ""; });
  grid.append(field("指定模型", providerSelect.value ? "留空则使用提供商默认模型" : "留空则跟随全局模型", modelSelect));

  const timeoutInput = document.createElement("input");
  timeoutInput.type = "number"; timeoutInput.min = "0"; timeoutInput.max = "600";
  timeoutInput.value = String(item.timeout || 0);
  timeoutInput.addEventListener("input", () => { item.timeout = Number(timeoutInput.value) || 0; });
  grid.append(field("超时（秒）", "0 = 使用全局超时", timeoutInput));

  const isolationSelect = document.createElement("select");
  const isolationOptions = section === "extract"
    ? [["", "跟随提取全局开关"], ["on", "强制开启"], ["off", "强制关闭"]]
    : [["", "跟随扩写全局开关"], ["on", "强制开启"], ["off", "强制关闭"]];
  isolationOptions.forEach(([value, label]) => isolationSelect.add(new Option(label, value)));
  isolationSelect.value = ["on", "off"].includes(String(item.isolation || "")) ? item.isolation : "";
  isolationSelect.addEventListener("change", () => { item.isolation = isolationSelect.value; onChange(); });
  grid.append(field("独立会话", "跟随分区全局开关，或仅为这条指令强制开启/关闭", isolationSelect));

  const foot = document.createElement("div"); foot.className = "command-card-foot";
  const remove = document.createElement("button");
  remove.type = "button"; remove.className = "danger-button command-delete";
  remove.innerHTML = `${icon("trash", 16)}删除这条`;
  if (draft.index === null) remove.hidden = true;
  remove.addEventListener("click", async () => {
    const name = String(item.trigger || "").trim() || "这条指令";
    if (!(await confirmAction(`删除「${name}」？`, "删除后这条指令不再响应，可以重新添加。"))) return;
    const items = (state.commandPresets[section] || []).filter((_, i) => i !== draft.index);
    remove.disabled = true;
    try {
      if (!stageCommandItems(section, items)) {
        remove.disabled = false;
        return;
      }
      state.commandOpenKey = "";
      state.commandDraft = null;
      toast(`已移除「${name}」，点击右下角保存后生效`);
    } catch (error) {
      toast(`删除失败：${readableError(error)}`, "error");
      remove.disabled = false;
      return;
    }
    renderCommandPage(section);
  });
  const cancel = document.createElement("button");
  cancel.type = "button"; cancel.className = "soft-button"; cancel.textContent = "收起";
  cancel.addEventListener("click", () => guardUnsaved(() => { state.commandOpenKey = ""; state.commandDraft = null; renderCommandPage(section); }));
  const save = document.createElement("button");
  save.type = "button"; save.className = "primary-button";
  save.innerHTML = `${icon("save", 16)}保存`;
  save.addEventListener("click", async () => {
    const next = Object.assign({}, item, normalizeDraft(item));
    if (!next.trigger) { toast("触发词不能为空", "error"); triggerInput.focus(); return; }
    const items = (state.commandPresets[section] || []).slice();
    if (items.some((other, i) => i !== draft.index && String(other.trigger || "").trim() === next.trigger)) {
      toast(`触发词「${next.trigger}」已存在`, "error"); triggerInput.focus(); return;
    }
    if (draft.index === null) items.push(next); else items[draft.index] = next;
    const label = save.innerHTML;
    save.disabled = true;
    save.textContent = "保存中…";
    try {
      if (!stageCommandItems(section, items)) {
        save.innerHTML = label;
        save.disabled = false;
        return;
      }
      state.commandOpenKey = `${section}:${next.trigger}`;
      const saved = (state.commandPresets[section] || []).findIndex(other => String(other.trigger) === String(next.trigger));
      state.commandDraft = saved >= 0 ? { section, index: saved, item: clone(state.commandPresets[section][saved]) } : null;
      toast("修改已加入草稿，点击右下角保存后生效");
    } catch (error) {
      // 草稿保持原样，用户输入不会丢
      save.innerHTML = label;
      save.disabled = false;
      toast(`保存失败：${readableError(error)}`, "error");
      return;
    }
    renderCommandPage(section);
  });
  save.hidden = true;
  foot.append(remove, cancel);
  body.append(grid, foot);
  body.addEventListener("input", () => setDirty(true));
  body.addEventListener("change", () => setDirty(true));
  switcher.addEventListener("click", () => setDirty(true));
  syncModels();
  return body;
}

// 新增：只开一个草稿卡片，保存后才进列表
function openNewCommand(section) {
  section = commandSectionOf(section);
  if (!state.commandLoaded[section]) {
    // 列表还没读到就新增，等于用“一条”覆盖服务器上的全部指令
    toast("指令列表还在读取，请稍候再添加", "error");
    showCommandLoading(section);
    loadCommandPage();
    return;
  }
  const base = section === "extract" ? "提取" : "扩写";
  let trigger = base;
  let n = 2;
  const items = state.commandPresets[section] || [];
  while (items.some(item => String(item.trigger || "").trim() === trigger)) trigger = `${base}${n++}`;
  state.commandDraft = {
    section,
    index: null,
    item: {
      __template_key: `${section}_item`,
      trigger,
      prompt: section === "expand" ? "用户描述：{{user_text}}" : "",
      system_prompt: "",
      provider_id: "",
      model: "",
      timeout: 0,
       enabled: true,
       isolation: "",
    },
  };
  state.commandOpenKey = `${section}:__new__`;
  renderCommandPage(section);
  const panel = commandPanel(section);
  requestAnimationFrame(() => {
    const input = panel && $(".command-card.is-open input", panel);
    if (input) { input.focus(); input.select(); }
  });
}

function commandSectionOf(section) {
  return COMMAND_SECTIONS[section] ? section : "expand";
}

// ---------------- 指令调用历史（扩写 / 提取） ----------------
function llmHistoryParams() {
  const form = $("#llm-filters");
  const values = form ? Object.fromEntries(new FormData(form).entries()) : {};
  const params = { page: state.llmPage || 1, page_size: state.pageSize || 20 };
  if (values.section) params.section = values.section;
  if (values.status) params.status = values.status;
  if (values.source) params.source = values.source;
  if (values.start_date) params.start_time = new Date(`${values.start_date}T00:00:00`).getTime() / 1000;
  if (values.end_date) params.end_time = new Date(`${values.end_date}T23:59:59`).getTime() / 1000;
  return params;
}

async function loadLlmHistory() {
  const body = $("#llm-history-body");
  if (!body) return;
  if (!state.llmPage) state.llmPage = 1;
  body.innerHTML = `<tr><td colspan="5"><div class="loading-card"><span class="spinner"></span>正在读取调用记录…</div></td></tr>`;
  $("#llm-history-empty")?.classList.remove("is-visible");
  try {
    const result = await apiGet("llm-history", llmHistoryParams());
    state.llmHistory = result.items || [];
    state.llmTotal = Number(result.total || 0);
    renderLlmHistory();
  } catch (error) {
    state.llmHistory = []; state.llmTotal = 0; renderLlmHistory();
    toast(`调用历史加载失败：${readableError(error)}`, "error");
  }
}

async function loadLlmStats() {
  try {
    const stats = await apiGet("llm-history/stats");
    const values = {
      total: stats.total || 0,
      success: stats.success || 0,
      failed: stats.failed || 0,
      avg_ms: stats.total ? formatDuration(stats.avg_ms) : "—",
    };
    Object.entries(values).forEach(([key, value]) => {
      const node = $(`[data-llm-stat="${key}"]`); if (node) node.textContent = value;
    });
  } catch (error) { toast(`统计加载失败：${readableError(error)}`, "error"); }
}

function renderLlmHistory() {
  const body = $("#llm-history-body");
  if (!body) return;
  body.replaceChildren();
  const items = state.llmHistory || [];
  const total = state.llmTotal || 0;
  $("#llm-count").textContent = `${total} 条`;
  $("#llm-history-empty").classList.toggle("is-visible", items.length === 0);
  items.forEach(item => {
    const row = document.createElement("tr");
    const timeCell = document.createElement("td");
    timeCell.innerHTML = `<span class="cell-main"></span><small class="cell-sub"></small>`;
    $(".cell-main", timeCell).textContent = formatTime(item.timestamp);
    $(".cell-sub", timeCell).textContent = `#${item.id} · 用户 ${text(item.user_id)}`;

    const inputCell = document.createElement("td");
    inputCell.innerHTML = `<span class="cell-main"></span><small class="cell-sub"></small>`;
    const isExtract = item.section === "extract";
    $(".cell-main", inputCell).textContent = `${isExtract ? "提取" : "扩写"} · ${text(item.input_text || item.trigger_word)}`;
    // 列表里不放提供商 / 模型列，悬停时一起显示
    $(".cell-main", inputCell).title = `${esc(text(item.input_text))}（${esc(text(item.model || "提供商默认模型"))} · ${esc(text(item.provider || "会话默认"))}）`;
    const sourceLabel = item.source === "bot" ? "生图预处理" : "用户指令";
    const sub = item.status === "failed" && item.error_message
      ? `失败原因 · ${item.error_message}`
      : `${sourceLabel} · 触发词 ${esc(text(item.trigger_word))}${item.image_count ? ` · ${item.image_count} 张图` : ""}`;
    $(".cell-sub", inputCell).textContent = sub;
    $(".cell-sub", inputCell).title = sub;

    const durationCell = document.createElement("td");
    durationCell.textContent = formatDuration(item.duration_ms);

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    const ok = item.status === "success";
    badge.className = `tag ${ok ? "success" : "failed"}`;
    badge.textContent = ok ? "成功" : "失败";
    statusCell.append(badge);

    const actionCell = document.createElement("td");
    actionCell.className = "right";
    actionCell.innerHTML = `<div class="row-actions"><button class="copy-row" title="复制结果" aria-label="复制结果">${icon("copy")}</button><button class="view-row" title="查看详情" aria-label="查看详情">${icon("eye")}</button><button class="delete-row" title="删除记录" aria-label="删除记录">${icon("trash")}</button></div>`;
    $(".copy-row", actionCell).addEventListener("click", async event => {
      // 列表接口不返回完整输出，点复制时才按 id 取详情，省一次全量请求
      const button = event.currentTarget;
      button.disabled = true;
      try {
        let detail = item;
        try { detail = await apiGet(`llm-history/${item.id}`); } catch { /* 退回列表里的摘要 */ }
        const content = detail.output_text || item.output_text || "";
        if (!String(content).trim()) { toast("这条记录没有可复制的结果", "error"); return; }
        if (await copyText(content)) toast("已复制结果");
        else toast("当前环境不允许自动复制，请打开详情手动选择", "error");
      } finally {
        button.disabled = false;
      }
    });
    $(".view-row", actionCell).addEventListener("click", () => openLlmCallDetail(item));
    $(".delete-row", actionCell).addEventListener("click", async () => {
      if (!(await confirmAction("删除这条调用记录？", "删除后无法恢复。"))) return;
      try { await apiPost("llm-history/delete", { id: item.id }); toast("记录已删除"); await loadLlmHistory(); await loadLlmStats(); }
      catch (error) { toast(`删除失败：${readableError(error)}`, "error"); }
    });

    row.append(timeCell, inputCell, durationCell, statusCell, actionCell);
    body.append(row);
  });
  const pages = Math.max(1, Math.ceil(total / (state.pageSize || 20)));
  const page = state.llmPage || 1;
  $("#llm-page-summary").textContent = `第 ${page} / ${pages} 页`;
  $("#llm-prev-page").disabled = page <= 1;
  $("#llm-next-page").disabled = page >= pages;
}

async function openLlmCallDetail(item) {
  const dialog = makeDialog("modal");
  dialog.innerHTML = `<button class="modal-close" aria-label="关闭">${icon("close")}</button><div class="detail-layout"><h2>调用记录 #${esc(item.id)}</h2><div class="detail-grid"></div></div>`;
  $(".modal-close", dialog).addEventListener("click", () => dialog.close());
  const grid = $(".detail-grid", dialog);
  let detail = item;
  try { detail = await apiGet(`llm-history/${esc(item.id)}`); } catch { /* 列表数据已够用 */ }
  const imageSourceNames = { message: "本条消息图", quoted: "引用消息图", forward: "合并转发图", avatar: "被 @ 的人的头像" };
  const entries = [
    ["指令类型", detail.section === "extract" ? "提取图片形象" : "扩写提示词"],
    ["来源", detail.source === "bot" ? "生图预处理（出图前自动调用）" : "用户指令"],
    ["状态", detail.status === "success" ? "成功" : "失败"],
    ["触发词", detail.trigger_word],
    ["提供商", detail.provider || "当前会话默认"],
    ["模型", detail.model || "提供商默认模型"],
    ["耗时", formatDuration(detail.duration_ms)],
    ["超时上限", `${detail.timeout_total || 0} 秒`],
    ["图片来源", String(detail.image_source || "").split(",").filter(Boolean).map(k => imageSourceNames[k] || k).join("、") || `${detail.image_count || 0} 张`],
    ["用户 / 群", `${detail.user_id || "—"}${detail.group_id ? ` / ${detail.group_id}` : ""}`],
    ["时间", formatTime(detail.timestamp)],
  ];
  if (detail.error_message) entries.push(["失败原因", detail.error_message]);
  entries.forEach(([title, value]) => {
    const cell = document.createElement("div"); cell.className = "detail-item";
    cell.innerHTML = `<span></span><strong></strong>`;
    $("span", cell).textContent = title;
    $("strong", cell).textContent = text(value);
    grid.append(cell);
  });
  [["输入", detail.input_text], ["系统提示词", detail.system_prompt], ["实际发送的提示词", detail.prompt], ["输出", detail.output_text]].forEach(([title, value]) => {
    if (!String(value || "").trim()) return;
    const block = document.createElement("div"); block.className = "detail-prompt";
    const label = document.createElement("span"); label.textContent = title;
    const pre = document.createElement("pre"); pre.textContent = String(value);
    block.append(label, pre);
    grid.append(block);
  });
  // 生成结果单独给一个复制按钮，复制到的是纯文本，方便直接拿去再出图
  const outputText = String(detail.output_text || "").trim();
  if (outputText) {
    const bar = document.createElement("div"); bar.className = "detail-prompt-actions";
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "primary-button";
    copyBtn.innerHTML = `${icon("copy", 16)}<span>复制生成结果</span>`;
    copyBtn.addEventListener("click", async () => {
      if (await copyText(outputText)) toast("已复制生成结果");
      else toast("当前环境不允许自动复制，请手动选择文本", "error");
    });
    bar.append(copyBtn);
    grid.append(bar);
  }
  dialog.showModal();
}

function initLlmHistory() {
  const form = $("#llm-filters");
  if (!form) return;
  state.llmPage = 1;
  form.addEventListener("submit", event => { event.preventDefault(); state.llmPage = 1; loadLlmHistory(); });
  $("#reset-llm-filters")?.addEventListener("click", () => { form.reset(); state.llmPage = 1; loadLlmHistory(); });
  $("#refresh-llm-history")?.addEventListener("click", () => { loadLlmHistory(); loadLlmStats(); });
  $("#llm-prev-page")?.addEventListener("click", () => { if ((state.llmPage || 1) > 1) { state.llmPage -= 1; loadLlmHistory(); } });
  $("#llm-next-page")?.addEventListener("click", () => {
    const page = state.llmPage || 1;
    if (page * (state.pageSize || 20) < (state.llmTotal || 0)) { state.llmPage = page + 1; loadLlmHistory(); }
  });
  $("#clear-llm-history")?.addEventListener("click", async () => {
    if (!(await confirmAction("清空全部调用记录？", "只删除这里的历史，不影响已有图片和配置。"))) return;
    try { const result = await apiPost("llm-history/clear", {}); state.llmPage = 1; toast(`已清空 ${result.removed || 0} 条记录`); await loadLlmHistory(); await loadLlmStats(); }
    catch (error) { toast(`清空失败：${readableError(error)}`, "error"); }
  });
}

async function loadProvidersPage() {
  const page = $("#page-providers");
  page.querySelectorAll(":scope > .providers-ui").forEach(node => node.remove());
  const loading = document.createElement("div"); loading.className = "providers-ui loading-card"; loading.innerHTML = `<span class="spinner"></span>正在读取模型提供商…`; page.append(loading);
  try { await fetchConfigData(); state.dirty = false; renderProvidersPage(); }
  catch (error) {
    loading.replaceChildren();
    const label = document.createElement("span"); label.textContent = `模型提供商加载失败：${readableError(error)}`;
    const retry = document.createElement("button"); retry.type = "button"; retry.className = "soft-button"; retry.textContent = "重试";
    retry.addEventListener("click", () => loadProvidersPage());
    loading.append(label, retry);
  }
}

function renderConfig() {
  const page = $("#page-config");
  page.querySelectorAll(":scope > .config-ui, :scope > .config-toolbar, :scope > .config-layout").forEach(node => node.remove());
  const ui = document.createElement("div"); ui.className = "config-ui";
  ui.innerHTML = `<div class="config-layout"><div class="config-content"></div></div><button class="category-launcher" type="button" aria-haspopup="true" aria-expanded="false"><svg class="icon icon-settings" width="17" height="17" aria-hidden="true"><use href="#i-settings"></use></svg><span class="category-launcher-label">配置分类</span><svg class="icon icon-chevron" width="15" height="15" aria-hidden="true"><use href="#i-chevron"></use></svg></button><nav class="config-nav" aria-label="配置分类"></nav>`;
  page.append(ui);
  const nav = $(".config-nav", ui); const content = $(".config-content", ui);
  const launcher = $(".category-launcher", ui);
  const launcherLabel = $(".category-launcher-label", ui);
  const setNavOpen = open => { nav.classList.toggle("is-open", open); launcher.setAttribute("aria-expanded", String(open)); };
  launcher.addEventListener("click", event => { event.stopPropagation(); setNavOpen(!nav.classList.contains("is-open")); });
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
      available.forEach(key => grid.append(createField(key, state.schema[key], state.draft[key], next => {
        state.draft[key] = next;
        setDirty(true);
        if (["model_templates", "image_providers", "llm_tag_provider_id", "expand_provider_id", "extract_provider_id"].includes(key)) requestAnimationFrame(() => keepViewPosition(renderConfig));
      })));
      group.append(heading, grid); section.append(group);
    });
    content.append(section);
    button.addEventListener("click", () => {
      state.configCategory = category.key;
      launcherLabel.textContent = category.title;
      $$("button", nav).forEach(node => node.classList.toggle("is-active", node === button));
      $$(".config-section", content).forEach(node => node.classList.toggle("is-active", node.dataset.category === category.key));
      setNavOpen(false);
    });
  });
  const initialActive = $(".config-nav button.is-active", ui) || $(".config-nav button", ui);
  if (initialActive) launcherLabel.textContent = initialActive.textContent;
  setDirty(state.dirty);
}

async function fetchConfigData() {
  const result = await apiGet("config");
  state.schema = result.schema || {};
  state.draft = clone(result.config || {});
  showVersion(result.version);
  try {
    const providers = await apiGet("astrbot-providers");
    state.astrbotProviders = Array.isArray(providers.items) ? providers.items : [];
  } catch {
    state.astrbotProviders = [];
  }
  try {
    const meta = await apiGet("preset/param-meta");
    state.presetParamRoles = (meta && typeof meta.roles === "object" && meta.roles) ? meta.roles : {};
  } catch {
    state.presetParamRoles = {};
  }
  return result;
}

async function loadConfig() {
  const page = $("#page-config");
  page.querySelectorAll(":scope > .config-ui, :scope > .config-toolbar, :scope > .config-layout").forEach(node => node.remove());
  const loading = document.createElement("div"); loading.className = "config-ui loading-card"; loading.innerHTML = `<span class="spinner"></span>正在读取插件配置…`; page.append(loading);
  try {
    await fetchConfigData();
    state.dirty = false;
    renderConfig();
  } catch (error) {
    loading.replaceChildren();
    const label = document.createElement("span"); label.textContent = `配置加载失败：${readableError(error)}`;
    const retry = document.createElement("button"); retry.type = "button"; retry.className = "soft-button"; retry.textContent = "重试";
    retry.addEventListener("click", () => loadConfig());
    loading.append(label, retry);
    toast(`配置加载失败：${readableError(error)}`, "error");
  }
}

async function saveConfig(button = $("#global-save")) {
  if (state.saving) return;
  // 配置尚未加载完成时禁止保存：否则会把空列表当成用户意图提交上去，
  // 覆盖服务器上已有的指令 / 预设（表现为“保存后全都不见了”）。
  if (!Object.keys(state.schema || {}).length) {
    toast("配置还没读取完成，请稍候再保存", "error");
    return;
  }
  const draft = state.commandDraft;
  if (draftDirty(draft)) {
    if (!state.commandLoaded[draft.section]) {
      toast("指令列表还没读取完成，请稍候再保存", "error");
      return;
    }
    const next = Object.assign({}, draft.item, normalizeDraft(draft.item));
    if (!next.trigger) { toast("触发词不能为空", "error"); return; }
    const items = [...(state.commandPresets[draft.section] || [])];
    if (items.some((item, i) => i !== draft.index && item.trigger === next.trigger)) {
      toast("触发词已存在，请使用唯一名称", "error"); return;
    }
    if (draft.index === null) items.push(next); else items[draft.index] = next;
    if (!stageCommandItems(draft.section, items)) return;
  }
  const uniquenessError = validateUniqueCollections(state.draft);
  if (uniquenessError) {
    toast(uniquenessError, "error");
    return;
  }
  if (!button) return;
  const availableDefaults = { text: new Set([""]), edit: new Set([""]) };
  (state.draft.image_providers || []).forEach(provider => {
    if (!provider || provider.enabled === false) return;
    (provider.models || []).forEach(model => {
      if (model && model.enabled !== false && model.enabled_as_default !== false) availableDefaults[String(model.mode || "text")]?.add(String(model.name || model.model || "").trim());
    });
  });
  (state.astrbotProviders || []).forEach(provider => {
    const models = provider.models?.length ? provider.models : (provider.model ? [provider.model] : []);
    models.forEach(model => { availableDefaults.text.add(`@astrbot:${provider.id}:${model}`); availableDefaults.edit.add(`@astrbot:${provider.id}:${model}`); });
  });
  (state.draft.comfyui_workflows || []).forEach(item => { if (!item || item.enabled === false || item.enabled_as_default === false || !String(item.name || "").trim()) return; const mode = String(item.refer_field || "").trim() ? "edit" : "text"; availableDefaults[mode].add(String(item.name).trim()); });
  // 保留用户已有选择：列表为空或缓存未加载不能成为静默清空默认模型的理由。
  // 后端负责校验真正失效的配置，界面只显示不可用状态。
  const idleContent = button.innerHTML;
  state.saving = true;
  button.disabled = true; button.innerHTML = `<span class="spinner"></span>`; button.setAttribute("aria-label", "保存并重载中");
  try {
    // 声明哪些列表是“已加载且被用户清空”：后端据此放行；未声明的空列表一律视为误提交
    const allowEmpty = Object.entries(state.schema || {})
      .filter(([key, spec]) => ["template_list", "list"].includes(spec?.type) && Array.isArray(state.draft[key]) && state.draft[key].length === 0)
      .map(([key]) => key)
      .filter(key => !key.endsWith("_presets") || state.commandLoaded[key.startsWith("extract") ? "extract" : "expand"]);
    const savedConfig = await apiPost("config", {
      config: state.draft,
      allowEmpty,
      // 基线：本页加载时看到的触发词。后端据此判断“哪些是用户真删的”，
      // 过期草稿没见过的新指令会被原样保留。
      basePresets: state.commandLoaded.expand || state.commandLoaded.extract ? clone(state.commandBase) : null,
    });
    if (savedConfig && typeof savedConfig === "object") {
      state.draft = clone(savedConfig);
      // 服务端返回的是权威结果，同步回指令列表，避免本地与服务器再次错位
      ["expand", "extract"].forEach(section => {
        const key = `${section}_presets`;
        if (Array.isArray(state.draft[key])) {
          state.commandPresets[section] = clone(state.draft[key]);
          state.commandLoaded[section] = true;
          // 保存成功后页面即代表最新状态，基线跟着更新
          state.commandBase[section] = state.commandPresets[section]
            .map(item => String((item && item.trigger) || "").trim())
            .filter(Boolean);
        }
      });
    }
    // 保存后保持当前展开的指令卡片与位置：收起再重绘会让页面剧烈跳动
    const openSection = state.commandDraft && state.commandDraft.section;
    const openTrigger = state.commandDraft && state.commandDraft.item
      ? String(state.commandDraft.item.trigger || "").trim() : "";
    state.commandDraft = null;
    state.commandOpenKey = "";
    setDirty(false);
    if (["expand", "extract"].includes(openSection) && openTrigger) {
      const index = (state.commandPresets[openSection] || [])
        .findIndex(item => String(item.trigger || "").trim() === openTrigger);
      if (index >= 0) {
        state.commandOpenKey = `${openSection}:${openTrigger}`;
        state.commandDraft = { section: openSection, index, item: clone(state.commandPresets[openSection][index]) };
      }
    }
    if ($("#page-textlab")?.classList.contains("is-active")) {
      if (["expand", "extract"].includes(state.labSection)) keepViewPosition(() => renderCommandPage(state.labSection));
    }
    toast("配置已保存，运行配置已重载");
  }
  catch (error) { setDirty(true); toast(`保存失败：${readableError(error)}`, "error"); }
  finally {
    state.saving = false;
    button.innerHTML = idleContent;
    button.setAttribute("aria-label", "保存并重载");
    button.disabled = !state.dirty;
  }
}

function stageCommandItems(section, items) {
  // 指令列表尚未从服务器读取完成时，绝不能用本地（可能是空的）列表覆盖草稿：
  // 那会把服务器上已有的指令整表清掉，表现为“保存后全都不见了”。
  if (!state.commandLoaded[section]) {
    toast("指令列表还在读取，请稍候再保存", "error");
    return false;
  }
  state.commandPresets[section] = clone(items);
  state.draft[section === "extract" ? "extract_presets" : "expand_presets"] = clone(items);
  setDirty(true);
  return true;
}

// 全局唯一的隐藏文件选择器（上传组件共用）
function ensureFilePicker() {
  let picker = $("#file-upload-picker");
  if (!picker) {
    picker = document.createElement("input");
    picker.id = "file-upload-picker";
    picker.type = "file";
    picker.hidden = true;
    document.body.append(picker);
  }
  return picker;
}

async function boot() {
  const bridgeState = $("#bridge-state");
  // 先保证基础 UI 可用；任何一块初始化失败都不能把整站拖死
  try {
    ensureFilePicker();
    initChrome(); initHistory(); initApngHistory(); initLlmHistory(); initLabChrome();
  } catch (error) {
    console.error("[neko_draw] 界面初始化异常", error);
    toast(`界面初始化异常：${readableError(error)}`, "error");
  }
  try {
    const context = await connectBridge();
    try {
      await fetchConfigData();
      state.dirty = false;
    } catch (error) {
      toast(`配置读取失败：${readableError(error)}`, "error");
    }
    bridgeState.className = "bridge-state is-ready"; $("span", bridgeState).textContent = "已连接";
    renderProvidersPage();
    await Promise.all([loadHistory(), loadStats()]);
  } catch (error) {
    bridgeState.className = "bridge-state is-error"; $("span", bridgeState).textContent = "连接失败";
    toast(readableError(error), "error"); state.history = []; state.total = 0; renderHistory(); renderStats();
  }
}

window.addEventListener("beforeunload", event => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
boot();
