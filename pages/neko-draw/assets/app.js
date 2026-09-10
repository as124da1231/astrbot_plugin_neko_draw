import { inferredProtocol, recommendedModelConfig } from "./model-profiles.js?v=2.2.4";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const icon = (name, size = 18) => `<svg class="icon icon-${name}" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

const state = {
  bridge: null,
  bridgePromise: null,
  history: [],
  stats: null,
  page: 1,
  pageSize: 10,
  total: 0,
  apngHistory: [],
  apngPage: 1,
  apngTotal: 0,
  astrbotProviders: [],
  providerModels: {},
  selectedProvider: 0,
  schema: {},
  draft: {},
  dirty: false,
  configCategory: "model",
};

const CATEGORIES = [
  { key: "model", title: "默认模型选择", description: "文生图与图片编辑分别选择默认模型；既可使用插件内启用的模型，也可直接选择 AstrBot 已有模型。", groups: [
    ["默认模型", ["default_text_model_v2", "default_edit_model_v2"]],
  ]},
  { key: "prompt", title: "预设提示词", description: "每个预设对应一个触发词；模板中的 {{user_text}} 会被替换为用户输入，也可以附带风格参考图。", groups: [
    ["触发词预设", ["prompt"]],
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
    ["网络", ["proxy"]], ["并发与重试", ["max_concurrency", "max_429_retries", "retry_429_delay"]], ["超时与轮询", ["timeout", "poll_interval"]],
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
  $$(".page").forEach(node => node.classList.toggle("is-active", node.id === `page-${page}`));
  $$(".nav-item").forEach(node => node.classList.toggle("is-active", node.dataset.page === page));
  $("#crumb").textContent = { providers: "模型提供商", config: "插件配置", history: "生成历史", apng: "APNG 作品" }[page] || "工作空间";
  $("#sidebar").classList.remove("is-open");
  $("#sidebar-backdrop")?.classList.remove("is-visible");
  if (page === "config" && !$("#page-config .config-ui")) {
    if (Object.keys(state.schema).length) renderConfig(); else loadConfig();
  }
  if (page === "providers" && !$("#page-providers .providers-ui")) {
    if (Object.keys(state.schema).length) renderProvidersPage(); else loadProvidersPage();
  }
  if (page === "apng") loadApngHistory();
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
  $$(".nav-item").forEach(node => node.addEventListener("click", () => showPage(node.dataset.page)));
  $("#mobile-menu").addEventListener("click", () => {
    const open = $("#sidebar").classList.toggle("is-open");
    $("#sidebar-backdrop")?.classList.toggle("is-visible", open);
  });
  $("#sidebar-backdrop")?.addEventListener("click", () => {
    $("#sidebar").classList.remove("is-open");
    $("#sidebar-backdrop").classList.remove("is-visible");
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
    if (state.dirty && !await confirmAction("放弃未保存的修改？", "页面将重新读取当前配置。")) return;
    try {
      await fetchConfigData();
      state.dirty = false;
      const active = $(".nav-item.is-active")?.dataset.page;
      if (active === "providers") renderProvidersPage();
      if (active === "config") renderConfig();
      setDirty(false);
      toast("配置已重新加载");
    } catch (error) { toast(`重新加载失败：${error.message}`, "error"); }
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
  const rate = stats.total ? `${((stats.success / stats.total) * 100).toFixed(1)}%` : "—";
  const values = { total: stats.total || 0, success: stats.success || 0, failed: stats.failed || 0, rate };
  Object.entries(values).forEach(([key, value]) => { const node = $(`[data-stat="${key}"]`); if (node) node.textContent = value; });
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
    toast(`下载失败：${error.message}`, "error");
  }
}

async function downloadHistoryImage(item, index = 0) {
  return downloadAsset(`history/image/${item.id}/${index}`, `neko-draw-${item.id}-output-${index + 1}`);
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
  } catch (error) { $(".detail-image", dialog).textContent = `原图读取失败：${error.message}`; }
}

function renderAssetGallery(root, item, kind, count, routeBase, thumbBase, label, allowDownload = true) {
  if (!count) return;
  const section = document.createElement("section"); section.className = "asset-section";
  section.innerHTML = `<h3>${label} <small>${count} 张</small></h3><div class="asset-gallery"></div>`; root.append(section);
  const gallery = $(".asset-gallery", section);
  for (let index = 0; index < count; index += 1) {
    const button = document.createElement("button"); button.className = "asset-thumb-button"; button.innerHTML = `<span class="spinner"></span><span>${index + 1}</span>`;
    button.addEventListener("click", () => openFullAsset(`${routeBase}/${item.id}/${index}`, `${label} ${index + 1}`, `neko-draw-${item.id}-${kind}-${index + 1}`, allowDownload)); gallery.append(button);
    const thumbEndpoint = thumbBase === "history/thumbnail" ? `${thumbBase}/${item.id}/${kind}/${index}` : `${thumbBase}/${item.id}/${index}`;
    apiGet(thumbEndpoint).then(result => { const image = new Image(); image.className="asset-thumb"; image.src=result.data_url; image.alt=`${label}缩略图 ${index+1}`; button.replaceChildren(image); }).catch(() => { button.classList.add("no-thumb"); button.textContent=`${index+1} · 点击读取`; });
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
    timeCell.innerHTML = `<div class="history-record-cell"><span class="history-cover">${icon("image",16)}</span><span><span class="cell-main"></span><small class="cell-sub"></small></span></div>`;
    $(".cell-main", timeCell).textContent = formatTime(item.timestamp);
    $(".cell-sub", timeCell).textContent = `#${item.id} · 用户 ${text(item.user_id)}`;
    const outputCover = (item.image_thumbnail_paths || []).findIndex(Boolean); const inputCover = (item.source_thumbnail_paths || []).findIndex(Boolean);
    const coverKind = outputCover >= 0 ? "output" : (inputCover >= 0 ? "input" : ""); const coverIndex = outputCover >= 0 ? outputCover : inputCover;
    if (coverKind) apiGet(`history/thumbnail/${item.id}/${coverKind}/${coverIndex}`).then(result => { const img=new Image(); img.src=result.data_url; img.alt="记录缩略图"; $(".history-cover",timeCell).replaceChildren(img); }).catch(()=>{});

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
  dialog.innerHTML = `<button class="modal-close" aria-label="关闭">${icon("close")}</button><div class="detail-layout"><h2>生成记录 #${item.id}</h2><div class="detail-grid"></div></div>`;
  $(".modal-close", dialog).addEventListener("click", () => dialog.close());
  const grid = $(".detail-grid", dialog);
  const details = [
    ["生成时间", formatTime(item.timestamp)], ["状态", item.status === "success" ? "成功" : "失败"],
    ["用户 / 群组", `${text(item.user_id)} / ${text(item.group_id)}`], ["耗时", formatDuration(item.generation_time_ms)],
    ["模型模板", text(item.model_template)], ["模型提供商", text(item.provider)],
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
  const root = $(".detail-layout", dialog);
  renderAssetGallery(root, item, "input", (item.source_image_paths || []).length, "history/source", "history/thumbnail", "输入原图");
  renderAssetGallery(root, item, "output", (item.image_paths || []).length, "history/image", "history/thumbnail", "生成图片");
  if (!(item.image_paths || []).length && !(item.source_image_paths || []).length) { const note=document.createElement("p"); note.className="empty-note"; note.textContent=item.error_message||"此记录没有图片"; root.append(note); }
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

async function viewApng(item) {
  const dialog = makeDialog("modal");
  dialog.innerHTML = `<button class="modal-close">${icon("close")}</button><div class="detail-layout"><h2>APNG 记录 #${item.id}</h2><p class="cleanup-note">APNG 成品发送后已自动清理；这里只保留制作时的输入原图。</p><div class="detail-grid"><div class="detail-item"><span>帧数</span><strong>${item.frame_count}</strong></div><div class="detail-item"><span>间隔</span><strong>${formatDuration(item.duration_ms)}</strong></div><div class="detail-item"><span>循环</span><strong>${Number(item.loop) === 0 ? "无限" : item.loop + " 次"}</strong></div><div class="detail-item"><span>制作时间</span><strong>${formatTime(item.timestamp)}</strong></div></div></div>`;
  $(".modal-close", dialog).addEventListener("click", () => dialog.close()); dialog.showModal();
  renderAssetGallery($(".detail-layout",dialog), item, "input", Number(item.source_count||0), "apng-history/source", "apng-history/thumbnail", "输入原图", false);
}

function renderApngHistory() {
  const body = $("#apng-body"); body.replaceChildren();
  $("#apng-count").textContent = `${state.apngTotal} 条`;
  $("#apng-empty").classList.toggle("is-visible", state.apngHistory.length === 0);
  state.apngHistory.forEach(item => {
    const row = document.createElement("tr");
    row.innerHTML = `<td><div class="history-record-cell"><span class="history-cover">${icon("image",16)}</span><span><span class="cell-main"></span><small class="cell-sub"></small></span></div></td><td>${item.frame_count} 帧</td><td>${formatDuration(item.duration_ms)}</td><td>${Number(item.loop) === 0 ? "无限" : item.loop + " 次"}</td><td>${Number(item.source_count||0)} 张</td><td class="right"><div class="row-actions"><button class="view-row" title="查看原图">${icon("eye")}</button><button class="delete-row" title="删除">${icon("trash")}</button></div></td>`;
    $(".cell-main", row).textContent = formatTime(item.timestamp); $(".cell-sub", row).textContent = `#${item.id} · 用户 ${text(item.user_id)}`;
    const coverIndex = (item.source_thumbnail_paths || []).findIndex(Boolean);
    if (coverIndex >= 0) apiGet(`apng-history/thumbnail/${item.id}/${coverIndex}`).then(result => { const img=new Image(); img.src=result.data_url; img.alt="原图缩略图"; $(".history-cover",row).replaceChildren(img); }).catch(()=>{});
    $(".view-row", row).addEventListener("click", () => viewApng(item));
    $(".delete-row", row).addEventListener("click", async () => {
      if (!await confirmAction("删除这条 APNG 记录？", "记录、全部输入原图副本和缩略图都会彻底删除，已发送到 QQ 的消息不受影响。")) return;
      try { await apiPost("apng-history/delete", { id: item.id }); toast("APNG 已删除"); loadApngHistory(); }
      catch (error) { toast(`删除失败：${error.message}`, "error"); }
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
  } catch (error) { state.apngHistory = []; state.apngTotal = 0; renderApngHistory(); toast(`APNG 记录加载失败：${error.message}`, "error"); }
}

function initApngHistory() {
  if (!$("#refresh-apng")) return;
  $("#refresh-apng").addEventListener("click", loadApngHistory);
  $("#apng-prev").addEventListener("click", () => { if (state.apngPage > 1) { state.apngPage -= 1; loadApngHistory(); } });
  $("#apng-next").addEventListener("click", () => { if (state.apngPage * state.pageSize < state.apngTotal) { state.apngPage += 1; loadApngHistory(); } });
  $("#clear-apng").addEventListener("click", async () => {
    if (!await confirmAction("清空全部 APNG？", "所有独立 APNG 记录及服务器文件都会被删除。")) return;
    try { const result = await apiPost("apng-history/clear", {}); state.apngPage = 1; toast(`已清理 ${result.deleted || 0} 个作品`); loadApngHistory(); }
    catch (error) { toast(`清理失败：${error.message}`, "error"); }
  });
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
  if (!$("#history-filters")) return;
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
    select.add(new Option(detail ? `${item.id}（${detail}）` : item.id, item.id));
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

function integratedDefaultSelectField(key, value, onChange) {
  const select = document.createElement("select");
  select.add(new Option("无（不设置默认模型）", ""));
  const mode = key === "default_edit_model_v2" ? "edit" : "text";
  const choices = [];
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
  if (!choices.length) select.options[0].textContent = "无（暂无可用模型）";
  ["猫娘画图", "AstrBot 已有模型"].forEach(groupName => {
    const rows = choices.filter(item => item.group === groupName); if (!rows.length) return;
    const group = document.createElement("optgroup"); group.label = groupName;
    rows.forEach(item => group.append(new Option(item.label, item.value))); select.append(group);
  });
  const current = String(value || "");
  if (current && !choices.some(item => item.value === current)) select.add(new Option(`${current}（当前不可用）`, current));
  select.value = current; select.disabled = !choices.length && !current;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function modelEditor(provider, initial, editing, onDone) {
  const dialog = makeDialog("modal template-editor-modal");
  const candidate = clone(initial || { name: "", model: "", mode: "text", enabled: true, enabled_as_default: true, fallback_order: 20, refer_field: "", max_refer_images: 0, min_prompt_length: 0, params: {}, custom_model: true });
  let recommendation = recommendedModelConfig(provider.base_url, candidate.model, candidate.mode);
  dialog.innerHTML = `<div class="template-modal-head"><div><span class="eyebrow">IMAGE LAB / MODEL</span><h2>${editing ? "修改模型" : "添加模型"}</h2><p>${provider.name || "模型提供商"}</p></div><button class="modal-close" type="button">${icon("close")}</button></div><div class="template-modal-body"><div class="template-modal-error"></div><div class="model-recommendation"><span>可添加参数：<b></b><small>不会自动修改；点击后只补充尚未设置的推荐项</small></span><button class="soft-button restore-params" type="button">${icon("plus",15)}添加推荐参数</button></div><div class="field-grid model-editor-grid"></div></div><div class="template-modal-foot"><button class="soft-button model-test" type="button">${icon("refresh",16)}测试模型连接</button><button class="soft-button cancel" type="button">取消</button><button class="primary-button confirm" type="button">${icon("save",16)}保存模型</button></div>`;
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
      field.innerHTML = `<div class="field-title"><span>已选模型</span><span class="field-key">model</span></div><div class="fixed-model-id">${text(candidate.model)}</div><p class="field-hint">模型已从提供商列表选定，无需再次选择</p>`;
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
    catch (error) { status.textContent = `✕ 模型测试失败：${error.message}`; status.className = "model-dialog-status is-error"; } finally { button.disabled = false; }
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
  const candidate = clone(initial || { __template_key: "image_provider", name: "", source: "custom", api_key: "", base_url: "https://api.openai.com/v1", astrbot_provider_id: "", enabled: true, models: [] });
  dialog.innerHTML = `<div class="template-modal-head"><div><span class="eyebrow">IMAGE LAB / PROVIDER</span><h2>${editing ? "修改模型提供商" : "新建模型提供商"}</h2><p>连接与模型集中配置</p></div><button class="modal-close" type="button">${icon("close")}</button></div><div class="template-modal-body"><div class="template-modal-error"></div><div class="field-grid provider-fields"></div><div class="provider-model-section"><div class="provider-model-toolbar"><div><h3>模型</h3><small class="provider-status">连接后获取可使用的生图模型</small></div><button class="soft-button add-model" type="button">${icon("plus",16)}自定义模型</button></div><div class="integrated-model-list"></div></div></div><div class="template-modal-foot"><button class="soft-button provider-fetch" type="button">${icon("refresh",16)}测试连接并获取模型</button><button class="soft-button cancel" type="button">取消</button><button class="primary-button confirm" type="button">${icon("save",16)}${editing ? "保存修改" : "添加提供商"}</button></div>`;
  const fields = $(".provider-fields", dialog); const modelList = $(".integrated-model-list", dialog);
  const renderModels = () => {
    modelList.replaceChildren();
    (candidate.models || []).forEach((model, index) => {
      const row = document.createElement("div"); row.className = "integrated-model-row";
      row.innerHTML = `<div><strong>${text(model.name || model.model)}</strong><small>${text(model.model)} · ${model.mode === "edit" ? "图片编辑" : "文生图"}</small></div><span class="provider-chip">${model.enabled === false ? "已停用" : "已启用"}</span><button class="icon-button edit-model" title="设置">${icon("settings",16)}</button><button class="icon-button delete-model" title="删除">${icon("trash",16)}</button>`;
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
      api_key: { type: "string", secret: true, description: "API Key" }, base_url: { type: "string", description: "API Base URL", hint: "只填根地址；例如 https://api.siliconflow.cn/v1" },
      astrbot_provider_id: { type: "string", description: "AstrBot 提供商" }, enabled: { type: "bool", description: "启用提供商" },
    };
    Object.entries(specs).filter(([key]) => candidate.source === "astrbot" ? !["api_key", "base_url"].includes(key) : key !== "astrbot_provider_id").forEach(([key, spec]) => fields.append(createField(key, spec, candidate[key], next => { candidate[key] = next; if (key === "source") renderFields(); }, true)));
  };
  renderFields(); renderModels();
  $(".add-model", dialog).onclick = () => modelEditor(candidate, null, false, model => { candidate.models ||= []; candidate.models.push(model); renderModels(); });
  $(".provider-fetch", dialog).onclick = async event => {
    const button = event.currentTarget; button.disabled = true; button.innerHTML = `<span class="spinner"></span>正在连接`;
    try {
      const result = await apiPost("providers/models", { provider: candidate }); const models = result.models || []; state.providerModels[candidate.name] = models;
      $(".provider-status", dialog).textContent = `${result.message}；点击模型即可加入`; $(".provider-status", dialog).classList.remove("is-error");
      candidate.available_models = models; modelList.replaceChildren(); models.forEach(id => { const row = document.createElement("button"); row.type = "button"; row.className = "discovered-model"; row.innerHTML = `<span>${id}</span><b>＋ 启用</b>`; row.onclick = () => modelEditor(candidate, { model: id, name: id, mode: "text", enabled: true, enabled_as_default: true, fallback_order: 20, refer_field: "", max_refer_images: 0, min_prompt_length: 0, params: {}, custom_model: false }, false, model => { candidate.models ||= []; candidate.models.push(model); renderModels(); }); modelList.append(row); });
      if (!models.length) renderModels();
    } catch (error) { $(".provider-status", dialog).textContent = `连接失败：${error.message}`; $(".provider-status", dialog).classList.add("is-error"); } finally { button.disabled = false; button.innerHTML = `${icon("refresh",16)}测试连接并获取模型`; }
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
  const setting = (label, hint, control) => { const row = document.createElement("div"); row.className = "provider-setting-row"; const meta = document.createElement("div"); meta.innerHTML = `<strong>${label}</strong>${hint ? `<small>${hint}</small>` : ""}`; row.append(meta, control); return row; };
  const input = (value, secret, changed) => { const wrap = document.createElement("div"); wrap.className = secret ? "input-wrap" : ""; const control = document.createElement("input"); control.type = secret ? "password" : "text"; control.value = value ?? ""; control.onchange = () => changed(control.value); wrap.append(control); if (secret) { const reveal = document.createElement("button"); reveal.type = "button"; reveal.className = "input-action"; reveal.innerHTML = icon("eye", 17); reveal.onclick = () => { control.type = control.type === "password" ? "text" : "password"; }; wrap.append(reveal); } return wrap; };
  const render = () => {
    root.replaceChildren();
    const sidebar = document.createElement("aside"); sidebar.className = "provider-console-sidebar";
    const sideHead = document.createElement("div"); sideHead.className = "provider-console-head"; sideHead.innerHTML = `<h3>提供商源</h3><button type="button">＋ 新增</button>`;
    $("button", sideHead).onclick = () => { items.push({ __template_key: "image_provider", name: "", source: "custom", api_key: "", base_url: "https://api.openai.com/v1", enabled: true, timeout: 300, proxy: "", custom_headers: {}, models: [] }); selected = items.length - 1; emit(); };
    sidebar.append(sideHead);
    const sourceList = document.createElement("div"); sourceList.className = "provider-source-list";
    items.forEach((provider, index) => { const card = document.createElement("button"); card.type = "button"; card.className = `provider-source-card ${index === selected ? "is-active" : ""}`; card.innerHTML = `<span class="provider-source-logo">${String(provider.name || "?").slice(0, 1).toUpperCase()}</span><span><strong>${text(provider.name)}</strong><small>${text(provider.base_url)}</small></span><i>${icon("trash", 16)}</i>`; card.onclick = event => { if (event.target.closest("i")) { items.splice(index, 1); selected = Math.min(selected, Math.max(0, items.length - 1)); emit(); } else { selected = index; state.selectedProvider = index; render(); } }; sourceList.append(card); });
    sidebar.append(sourceList); root.append(sidebar);
    const detail = document.createElement("section"); detail.className = "provider-console-detail"; root.append(detail);
    if (!items.length) { detail.innerHTML = `<div class="provider-console-empty"><b>尚未添加模型提供商</b><span>点击左侧“新增”，填写 API Base URL 和 Key。</span></div>`; return; }
    const provider = items[selected]; provider.models ||= []; provider.source = "custom";
    const detected = inferredProtocol(provider.base_url);
    detail.innerHTML = `<div class="provider-detail-title"><div><h2>${text(provider.name)}</h2><p>${text(provider.base_url)}</p><small class="detected-protocol">已自动识别：${detected === "OpenAI" ? "OpenAI 兼容" : detected}</small></div></div><div class="provider-settings"><h3>设置</h3><div class="provider-setting-list"></div><details class="provider-advanced"><summary>高级配置…</summary><div class="provider-setting-list advanced-list"></div></details></div><div class="provider-models"><div class="provider-model-header"><div><h3>模型</h3><small>已配置 ${(provider.models || []).length} 个</small></div><div class="provider-model-actions"><input class="model-search" type="search" placeholder="搜索已配置模型"><button class="soft-button fetch-models" type="button">${icon("refresh", 16)}${detected === "WaveSpeed" ? "测试连接" : "获取模型列表"}</button><button class="text-button custom-model" type="button">＋ 自定义模型</button></div></div><div class="provider-inline-status" role="status">${detected === "WaveSpeed" ? "WaveSpeed 模型需要自定义添加，模型 ID 会自动拼接到请求路径。" : ""}</div><div class="provider-configured-models"></div><div class="provider-discovered-models"></div><div class="model-search-empty" hidden>没有匹配的模型</div></div>`;
    const list = $(".provider-setting-list", detail);
    list.append(setting("ID", "提供商唯一 ID（不是模型 ID）", input(provider.name, false, value => { provider.name = value.trim(); emit(); })));
    list.append(setting("API Key", "API 密钥", input(provider.api_key, true, value => { provider.api_key = value; emit(false); })));
    list.append(setting("API Base URL", "填写后自动识别接口协议", input(provider.base_url, false, value => { provider.base_url = value.trim().replace(/\/$/, ""); delete provider.protocol; emit(); })));
    const advanced = $(".advanced-list", detail);
    advanced.append(setting("超时时间", "单位为秒", input(provider.timeout ?? 300, false, value => { provider.timeout = Number(value) || 300; emit(false); })));
    advanced.append(setting("代理地址", "仅对该提供商的 API 请求生效", input(provider.proxy || "", false, value => { provider.proxy = value.trim(); emit(false); })));
    advanced.append(createField("custom_headers", { type: "object", description: "自定义请求头" }, provider.custom_headers || {}, value => { provider.custom_headers = value; emit(false); }, true));
    const rows = $(".provider-configured-models", detail);
    const drawRows = filter => { rows.replaceChildren(); (provider.models || []).forEach((model, index) => { if (filter && !`${model.name} ${model.model}`.toLowerCase().includes(filter)) return; const row = document.createElement("div"); row.className = "provider-model-row"; row.innerHTML = `<div><strong>${text(model.name || model.model)}</strong><small>${text(model.model)}</small><span>${model.mode === "edit" ? "图片编辑" : "文生图"}</span><b class="model-test-badge" hidden>✓ 测试通过</b></div><button class="switch ${model.enabled === false ? "" : "is-on"}" role="switch" aria-label="启用模型"></button><button class="icon-button test-one" title="测试连接">${icon("refresh", 15)}</button><button class="icon-button configure-one" title="模型设置">${icon("settings", 16)}</button><button class="icon-button remove-one" title="删除">${icon("trash", 16)}</button>`; $(".switch", row).onclick = () => { model.enabled = model.enabled === false; emit(); }; $(".test-one", row).onclick = async event => { const button = event.currentTarget; const status = $(".provider-inline-status", detail); const badge = $(".model-test-badge", row); button.disabled = true; row.classList.remove("is-tested"); badge.hidden = true; status.textContent = "正在测试模型连接…"; status.className = "provider-inline-status is-testing"; try { const result = await apiPost("models/test", { provider, model }); status.textContent = `✓ 测试成功 · ${result.message || "模型可用"}`; status.className = "provider-inline-status is-success"; row.classList.add("is-tested"); badge.hidden = false; } catch (error) { status.textContent = `✕ 模型测试失败 · ${error.message}`; status.className = "provider-inline-status is-error"; } finally { button.disabled = false; } }; $(".configure-one", row).onclick = () => modelEditor(provider, model, true, next => { provider.models[index] = next; emit(); }); $(".remove-one", row).onclick = () => { provider.models.splice(index, 1); emit(); }; rows.append(row); }); };
    const applyModelFilter = () => { const filter = $(".model-search", detail).value.trim().toLowerCase(); drawRows(filter); let visible = rows.children.length; $$(".discovered-model", detail).forEach(row => { row.hidden = Boolean(filter) && !row.textContent.toLowerCase().includes(filter); if (!row.hidden) visible += 1; }); $(".model-search-empty", detail).hidden = visible > 0; };
    applyModelFilter(); $(".model-search", detail).oninput = applyModelFilter;
    $(".custom-model", detail).onclick = () => modelEditor(provider, null, false, model => { provider.models.push(model); emit(); });
    $(".fetch-models", detail).onclick = async event => { const button = event.currentTarget; button.disabled = true; const status = $(".provider-inline-status", detail); const waveSpeed = inferredProtocol(provider.base_url) === "WaveSpeed"; status.textContent = waveSpeed ? "正在测试连接…" : "正在连接并读取模型…"; status.className = "provider-inline-status is-testing"; try { const result = waveSpeed ? await apiPost("providers/test", { provider }) : await apiPost("providers/models", { provider }); const models = result.models || []; state.providerModels[provider.name] = models; provider.available_models = models; status.textContent = waveSpeed ? `✓ 连接成功 · ${result.message || "请使用自定义模型"}` : `✓ 连接成功 · ${result.message || `发现 ${models.length} 个模型`}`; status.className = "provider-inline-status is-success"; const discovered = $(".provider-discovered-models", detail); discovered.replaceChildren(); const configured = new Set(provider.models.map(model => model.model)); models.filter(id => !configured.has(id)).forEach(id => { const row = document.createElement("button"); row.type = "button"; row.className = "discovered-model"; row.innerHTML = `<span>${id}</span><b>＋ 启用</b>`; row.onclick = () => modelEditor(provider, { model: id, name: id, mode: "text", enabled: true, enabled_as_default: true, fallback_order: 20, refer_field: "", max_refer_images: 0, min_prompt_length: 0, params: {}, custom_model: false }, false, model => { provider.models.push(model); emit(); }); discovered.append(row); }); applyModelFilter(); emit(false); } catch (error) { status.textContent = `✕ 连接失败 · ${error.message}`; status.className = "provider-inline-status is-error"; } finally { button.disabled = false; } };
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
  const modelNames = (config.model_templates || []).map(item => item && typeof item === "object" ? String(item.name || "").trim() : "").filter(Boolean);
  const duplicateModel = modelNames.find((name, index) => modelNames.indexOf(name) !== index);
  if (duplicateModel) return `模型模板名称「${duplicateModel}」已存在，请使用唯一名称`;
  const triggers = (config.prompt || []).map(promptTrigger).filter(Boolean);
  const duplicateTrigger = triggers.find((trigger, index) => triggers.indexOf(trigger) !== index);
  if (duplicateTrigger) return `预设触发词「${duplicateTrigger}」已存在，请使用唯一触发词`;
  const missingProvider = (config.model_templates || []).find(item => item && typeof item === "object" && !providerNames.includes(String(item.provider || "").trim()));
  if (missingProvider) return `模型模板「${missingProvider.name || "未命名"}」选择的模型提供商不存在`;
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
      toast(`连接失败：${error.message}`, "error");
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
      const provider = document.createElement("span"); provider.className = "provider-chip"; provider.textContent = configKey === "model_providers" ? (item.type || "未选择类型") : (item.provider || "未选择提供商");
      summary.append(label); if (["model_templates", "model_providers"].includes(configKey)) summary.append(provider);
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
      const add = document.createElement("button"); add.type = "button"; add.className = "soft-button"; add.innerHTML = `${icon("plus", 15)}${addLabel}`;
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

async function loadProvidersPage() {
  const page = $("#page-providers");
  page.querySelectorAll(":scope > .providers-ui").forEach(node => node.remove());
  const loading = document.createElement("div"); loading.className = "providers-ui loading-card"; loading.innerHTML = `<span class="spinner"></span>正在读取模型提供商…`; page.append(loading);
  try { await fetchConfigData(); state.dirty = false; renderProvidersPage(); }
  catch (error) { loading.textContent = `模型提供商加载失败：${error.message}`; }
}

function renderConfig() {
  const page = $("#page-config");
  page.querySelectorAll(":scope > .config-ui, :scope > .config-toolbar, :scope > .config-layout").forEach(node => node.remove());
  const ui = document.createElement("div"); ui.className = "config-ui";
  ui.innerHTML = `<div class="config-layout"><nav class="config-nav" aria-label="配置分类"></nav><div class="config-content"></div></div>`;
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
      available.forEach(key => grid.append(createField(key, state.schema[key], state.draft[key], next => {
        state.draft[key] = next;
        setDirty(true);
        if (["model_templates", "image_providers"].includes(key)) requestAnimationFrame(renderConfig);
      })));
      group.append(heading, grid); section.append(group);
    });
    content.append(section);
    button.addEventListener("click", () => {
      state.configCategory = category.key;
      $$("button", nav).forEach(node => node.classList.toggle("is-active", node === button));
      $$(".config-section", content).forEach(node => node.classList.toggle("is-active", node.dataset.category === category.key));
    });
  });
  setDirty(state.dirty);
}

async function fetchConfigData() {
  const result = await apiGet("config");
  state.schema = result.schema || {};
  state.draft = clone(result.config || {});
  try {
    const providers = await apiGet("astrbot-providers");
    state.astrbotProviders = Array.isArray(providers.items) ? providers.items : [];
  } catch {
    state.astrbotProviders = [];
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
  } catch (error) { loading.textContent = `配置加载失败：${error.message}`; toast(`配置加载失败：${error.message}`, "error"); }
}

async function saveConfig(button = $("#global-save")) {
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
  for (const [key, mode] of [["default_text_model_v2", "text"], ["default_edit_model_v2", "edit"]]) {
    if (!availableDefaults[mode].has(String(state.draft[key] || "").trim())) state.draft[key] = "";
  }
  const idleContent = button.innerHTML;
  button.disabled = true; button.innerHTML = `<span class="spinner"></span>`; button.setAttribute("aria-label", "保存并重载中");
  try {
    await apiPost("config", { config: state.draft });
    setDirty(false); toast("配置已保存，运行配置已重载");
  }
  catch (error) { setDirty(true); toast(`保存失败：${error.message}`, "error"); }
  finally { button.innerHTML = idleContent; button.setAttribute("aria-label", "保存并重载"); button.disabled = !state.dirty; }
}

function addConfigHero() {
  const page = $("#page-config");
  const hero = page.querySelector(".hero");
  hero.insertAdjacentHTML("afterend", "");
  const filePicker = document.createElement("input"); filePicker.id = "file-upload-picker"; filePicker.type = "file"; filePicker.hidden = true; document.body.append(filePicker);
}

async function boot() {
  const bridgeState = $("#bridge-state");
  try {
    initChrome(); initHistory(); initApngHistory(); addConfigHero();
    const context = await connectBridge();
    try {
      await fetchConfigData();
      state.dirty = false;
    } catch (error) {
      toast(`配置读取失败：${error.message}`, "error");
    }
    bridgeState.className = "bridge-state is-ready"; $("span", bridgeState).textContent = "已连接";
    renderProvidersPage();
    await Promise.all([loadHistory(), loadStats()]);
  } catch (error) {
    bridgeState.className = "bridge-state is-error"; $("span", bridgeState).textContent = "连接失败";
    toast(error.message, "error"); state.history = []; state.total = 0; renderHistory(); renderStats();
  }
}

window.addEventListener("beforeunload", event => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
boot();
