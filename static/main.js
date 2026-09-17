// ===== 元素 =====
const body = document.body;
const drop = document.getElementById("drop");
const fileInput = document.getElementById("file");
const dropText = document.getElementById("drop-text");
const startBtn = document.getElementById("start");
const errorEl = document.getElementById("error");

const burnModes = document.getElementById("burn-modes");
const burnWarn = document.getElementById("burn-warn");
const progressBox = document.getElementById("progress");
const barFill = document.getElementById("bar-fill");
const statusEl = document.getElementById("status");
const resultBox = document.getElementById("result");

const playerBox = document.getElementById("player-box");
const player = document.getElementById("player");
const subEn = document.getElementById("sub-en");
const subZh = document.getElementById("sub-zh");
const subLoading = document.getElementById("sub-loading");
const rtSrt = document.getElementById("rt-srt");
const rtStatus = document.getElementById("rt-status");

const apiKeyInput = document.getElementById("api-key");
const engineStatus = document.getElementById("engine-status");

const BURN_OK = body.dataset.burnOk === "yes";
const HAS_ENV_KEY = body.dataset.hasKey === "yes";

let currentTab = "realtime";
let selectedFile = null;

// ===== API key =====
apiKeyInput.value = localStorage.getItem("deepseek_key") || "";
apiKeyInput.addEventListener("input", () =>
  localStorage.setItem("deepseek_key", apiKeyInput.value.trim()));

function refreshEngineUI() {
  const hasKey = apiKeyInput.value.trim() || HAS_ENV_KEY;
  engineStatus.textContent = hasKey
    ? "DeepSeek API（已就绪）"
    : "DeepSeek API（请填写 API key）";
}
apiKeyInput.addEventListener("input", refreshEngineUI);

// ===== 标签切换 =====
document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    currentTab = tab.dataset.tab;
    applyTab();
  });
});
function applyTab() {
  hideError();
  resetViews();
  burnModes.hidden = currentTab !== "burn";
  if (burnWarn) burnWarn.hidden = !(currentTab === "burn" && !BURN_OK);
  startBtn.textContent = currentTab === "burn" ? "开始烧录" : "开始播放并翻译";
}

// ===== 文件选择 =====
drop.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
["dragover", "dragenter"].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => setFile(e.dataTransfer.files[0]));

function setFile(file) {
  if (!file) return;
  selectedFile = file;
  drop.classList.add("has-file");
  const mb = (file.size / 1048576).toFixed(1);
  dropText.innerHTML = `📹 ${file.name}<br><span class="hint">${mb} MB · 点击可更换</span>`;
  startBtn.disabled = false;
  hideError();
}

// ===== 开始 =====
startBtn.addEventListener("click", () => {
  if (!selectedFile) return;
  if (!apiKeyInput.value.trim() && !HAS_ENV_KEY) {
    return fail("未填写 DeepSeek API key，请先填写。");
  }
  if (currentTab === "burn") startBurn();
  else startRealtime();
});

function buildForm() {
  const form = new FormData();
  form.append("video", selectedFile);
  form.append("api_key", apiKeyInput.value.trim());
  return form;
}

// ===== 烧录模式 =====
async function startBurn() {
  const form = buildForm();
  form.append("mode", document.querySelector('input[name="mode"]:checked').value);
  startBtn.disabled = true;
  hideError();
  progressBox.hidden = false;
  resultBox.hidden = true;
  setProgress(0, "正在上传视频…");

  let resp;
  try {
    resp = await fetch("/upload", { method: "POST", body: form });
  } catch {
    return fail("上传失败，请检查服务是否还在运行。");
  }
  const data = await safeJson(resp);
  if (!data) return fail(`服务返回异常（HTTP ${resp.status}），文件可能过大或服务出错。`);
  if (!resp.ok) return fail(data.error || "上传失败");
  pollBurn(data.task_id);
}

const STAGE_RANGE = {
  queued: [0, 2], extract: [2, 6], transcribe: [6, 55],
  translate: [55, 75], subtitle: [75, 77], burn: [77, 99],
};
function pollBurn(taskId) {
  const timer = setInterval(async () => {
    let data;
    try {
      const r = await fetch(`/progress/${taskId}`);
      data = await r.json();
      if (!r.ok) throw new Error(data.error);
    } catch (e) {
      clearInterval(timer);
      return fail("获取进度失败：" + e.message);
    }
    if (data.status === "error") { clearInterval(timer); return fail(data.error); }
    if (data.status === "done") {
      clearInterval(timer);
      setProgress(100, data.message);
      document.getElementById("dl-video").href = data.files.video;
      document.getElementById("dl-srt").href = data.files.srt;
      resultBox.hidden = false;
      return;
    }
    const [lo, hi] = STAGE_RANGE[data.stage] || [0, 100];
    setProgress(lo + (hi - lo) * (data.percent / 100), data.message);
  }, 1000);
}

// ===== 边看边译模式 =====
let segments = [];        // {index,start,end,en,zh}
let dispMode = "both";
let evtSource = null;
let processingDone = false;

document.querySelectorAll('input[name="dmode"]').forEach(r =>
  r.addEventListener("change", () => { dispMode = r.value; renderSub(); rebuildCues(); }));

// ===== 全屏 =====
// 浏览器原生全屏按钮只会全屏 <video> 元素本身，DOM 字幕层会被留在外面。
// 方案一（主）：自定义 ⛶ 按钮把整个 .video-wrap 容器全屏，字幕层一起进入全屏。
// 方案二（兜底）：给视频挂一条原生字幕轨（TextTrack），若用户仍以某种方式
// 只把 <video> 元素全屏（如 Safari 原生控件），浏览器会自行渲染字幕轨。
const videoWrap = document.getElementById("video-wrap");
const fsBtn = document.getElementById("fs-btn");

fsBtn.addEventListener("click", toggleFullscreen);
player.addEventListener("dblclick", toggleFullscreen);

function fsElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || null;
}
function toggleFullscreen() {
  if (fsElement()) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  } else {
    (videoWrap.requestFullscreen || videoWrap.webkitRequestFullscreen).call(videoWrap);
  }
}

let nativeTrack = null;   // 原生字幕轨（兜底）
function ensureTrack() {
  if (!nativeTrack) {
    nativeTrack = player.addTextTrack("subtitles", "字幕", "zh");
    nativeTrack.mode = "hidden";
  }
  return nativeTrack;
}
function cueText(seg) {
  if (dispMode === "zh") return seg.zh || "";
  if (dispMode === "en") return seg.en || "";
  return `${seg.en}\n${seg.zh || ""}`;
}
function addCue(seg) {
  if (!window.VTTCue) return;
  try { ensureTrack().addCue(new VTTCue(seg.start, seg.end, cueText(seg))); } catch {}
}
function rebuildCues() {
  if (!nativeTrack || !window.VTTCue) return;
  const old = Array.from(nativeTrack.cues || []);
  old.forEach(c => nativeTrack.removeCue(c));
  segments.forEach(addCue);
}
// 只有 <video> 元素本身被全屏（原生控件路径）时，切到原生字幕轨渲染
["fullscreenchange", "webkitfullscreenchange"].forEach(ev =>
  document.addEventListener(ev, () => {
    const videoOnlyFs = fsElement() === player;
    if (nativeTrack) nativeTrack.mode = videoOnlyFs ? "showing" : "hidden";
    fsBtn.textContent = fsElement() ? "✕" : "⛶";
  }));

async function startRealtime() {
  startBtn.disabled = true;
  hideError();
  segments = [];
  processingDone = false;
  rebuildCues();  // 清掉上一个视频遗留的原生字幕轨内容

  let resp;
  try {
    resp = await fetch("/upload_realtime", { method: "POST", body: buildForm() });
  } catch {
    return fail("上传失败，请检查服务是否还在运行。");
  }
  const data = await safeJson(resp);
  if (!data) return fail(`服务返回异常（HTTP ${resp.status}），文件可能过大或服务出错。`);
  if (!resp.ok) return fail(data.error || "上传失败");

  resetViews();
  playerBox.hidden = false;
  player.src = data.video_url;
  rtStatus.textContent = "字幕正在后台生成，可以直接开始播放。";
  subLoading.hidden = true;
  player.play().catch(() => {});
  connectSSE(data.task_id);
}

function connectSSE(taskId) {
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/stream/${taskId}`);
  evtSource.onmessage = e => {
    // 服务端带 id: 并支持 Last-Event-ID 续传；这里再按 index 去重兜底，
    // 防止任何情况下的重连重发导致 segments 成倍重复
    try {
      const seg = JSON.parse(e.data);
      if (!segments.some(s => s.index === seg.index)) {
        segments.push(seg);
        addCue(seg);  // 同步写入原生字幕轨（全屏兜底用）
      }
    } catch {}
  };
  evtSource.addEventListener("done", e => {
    processingDone = true;
    evtSource.close();
    let info = {};
    try { info = JSON.parse(e.data); } catch {}
    rtStatus.textContent = `字幕全部生成完成，共 ${info.count || segments.length} 段。`;
    subLoading.hidden = true;
    if (info.srt) { rtSrt.href = info.srt; rtSrt.hidden = false; }
  });
  evtSource.addEventListener("error", e => {
    // EventSource 断线也会触发 error（无 data）；有 data 才是后端主动报错
    if (e.data) {
      try { fail(JSON.parse(e.data).error || "字幕生成出错"); } catch { fail("字幕生成出错"); }
      evtSource.close();
    }
  });
}

player.addEventListener("timeupdate", renderSub);
function renderSub() {
  const t = player.currentTime;
  const seg = segments.find(s => t >= s.start && t <= s.end);
  const showEn = dispMode === "both" || dispMode === "en";
  const showZh = dispMode === "both" || dispMode === "zh";
  subEn.textContent = seg && showEn ? seg.en : "";
  subZh.textContent = seg && showZh ? (seg.zh || "") : "";

  // 播放进度超过已生成字幕范围时，提示「字幕生成中…」
  const lastEnd = segments.length ? segments[segments.length - 1].end : 0;
  const behind = !processingDone && t > lastEnd + 0.3 && !seg;
  subLoading.hidden = !behind;
}

// ===== 工具 =====
async function safeJson(resp) {
  // 响应不是 JSON（如超限时 Werkzeug 的 413 HTML 页）时返回 null，避免未捕获异常卡住 UI
  try { return await resp.json(); } catch { return null; }
}
function setProgress(pct, msg) { barFill.style.width = pct + "%"; statusEl.textContent = msg; }
function fail(msg) {
  progressBox.hidden = true;
  errorEl.textContent = "❌ " + msg;
  errorEl.hidden = false;
  startBtn.disabled = false;
}
function hideError() { errorEl.hidden = true; }
function resetViews() {
  progressBox.hidden = true;
  resultBox.hidden = true;
  playerBox.hidden = true;
  rtSrt.hidden = true;
  if (evtSource) { evtSource.close(); evtSource = null; }
  if (player.src) { player.pause(); player.removeAttribute("src"); player.load(); }
}

document.getElementById("again").addEventListener("click", () => location.reload());
document.getElementById("rt-again").addEventListener("click", () => location.reload());

// 初始化
refreshEngineUI();
applyTab();
