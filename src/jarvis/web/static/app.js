/* Jarvis web client: one WebSocket, the same event stream every face consumes.
   The interface is Trillion's - a dark ground, a single living orb, and the
   conversation floating above it. Trillion's orb reacts to your voice; this one
   reacts to what the agent is doing, which is the equivalent signal for a
   typed interface: it brightens while thinking, turns amber while a tool runs,
   red on a fault, and goes cold when the socket drops. */

const thread = document.getElementById("thread");
const input = document.getElementById("input");
const sendButton = document.getElementById("send");
const stopButton = document.getElementById("stop");
const resetButton = document.getElementById("reset");
const holdButton = document.getElementById("hold");
const noticesButton = document.getElementById("notices-button");
const noticesCount = document.getElementById("notices-count");
const inboxCount = document.getElementById("inbox-count");
const inboxEntries = document.getElementById("inbox-entries");
const connPill = document.getElementById("conn");
const wmdot = document.getElementById("wmdot");
const empty = document.getElementById("empty");
const approvalModal = document.getElementById("approval-modal");
const approvalNote = document.getElementById("approval-note");
const toasts = document.getElementById("toasts");
const apanel = document.getElementById("apanel");
const mhint = document.getElementById("mhint");

const tele = {
  uptime: document.getElementById("tele-uptime"),
  in: document.getElementById("tele-in"),
  out: document.getElementById("tele-out"),
  cache: document.getElementById("tele-cache"),
  tools: document.getElementById("tele-tools"),
  state: document.getElementById("tele-state"),
};

let socket = null;
let streaming = false;
let current = null;          // the assistant message being built
let pendingApproval = null;
let retryDelay = 500;
let paused = false;
const counters = { in: 0, out: 0, cache: 0, tools: 0 };
const inbox = new Map();     // id -> {text, detail, level, source}
const startedAt = Date.now();

/* ---------- the orb ---------- */
/* Trillion's renderer: a drifting starfield, then four radial gradients
   composited with 'lighter' so they add into a single bloom. `energy` and the
   colour are eased toward their targets rather than snapped, which is what
   makes a state change read as the orb responding rather than blinking. */

const canvas = document.getElementById("orb");
const ctx = canvas.getContext("2d");
let W = 0, H = 0, cx = 0, cy = 0, dpr = 1;

function resize() {
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  W = window.innerWidth;
  H = window.innerHeight;
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  canvas.style.width = `${W}px`;
  canvas.style.height = `${H}px`;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  // Centred on the conversation column, not the window, so the panel does not
  // push the orb off-axis.
  const panel = window.innerWidth > 860 ? apanel.offsetWidth : 0;
  cx = (W - panel) / 2;
  cy = H / 2;
}
resize();
window.addEventListener("resize", resize);

const STARS = Array.from({ length: 180 }, () => ({
  x: Math.random(), y: Math.random(),
  r: Math.random() * 1.4 + 0.3,
  ph: Math.random() * Math.PI * 2,
  sp: Math.random() * 1.5 + 0.8,
}));

const TEAL = [45, 212, 168];
const AMBER = [255, 176, 70];
const RED = [255, 90, 90];
const GREY = [120, 132, 145];

const STATES = {
  offline: { label: "offline", energy: 0.0, colour: GREY, period: 6 },
  idle: { label: "standby", energy: 0.18, colour: TEAL, period: 4 },
  thinking: { label: "processing", energy: 0.72, colour: TEAL, period: 1.6 },
  tool: { label: "executing", energy: 0.85, colour: AMBER, period: 1.1 },
  speaking: { label: "responding", energy: 0.55, colour: TEAL, period: 2.4 },
  error: { label: "fault", energy: 0.9, colour: RED, period: 0.9 },
};

let target = STATES.offline;
let energy = 0;
let colour = GREY.slice();
let period = 6;

function setState(name) {
  target = STATES[name] || STATES.idle;
  tele.state.textContent = target.label;
}

const lerp = (a, b, t) => a + (b - a) * t;

function drawFrame(t) {
  energy = lerp(energy, target.energy, 0.055);
  period = lerp(period, target.period, 0.04);
  for (let i = 0; i < 3; i += 1) colour[i] = lerp(colour[i], target.colour[i], 0.05);

  const [r0, g0, b0] = colour.map(Math.round);
  const rgb = `${r0},${g0},${b0}`;
  const p = Math.sin((t * Math.PI * 2) / period) * 0.5 + 0.5;
  const br = 0.55 + p * 0.45 + energy * 0.7;

  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#0E0F13";
  ctx.fillRect(0, 0, W, H);

  STARS.forEach((s) => {
    const tw = Math.sin(t * s.sp + s.ph) * 0.5 + 0.5;
    ctx.beginPath();
    ctx.arc(s.x * W, s.y * H, s.r * (0.7 + tw * 0.3), 0, Math.PI * 2);
    ctx.fillStyle = `rgba(255,255,255,${0.2 + tw * 0.55})`;
    ctx.fill();
  });

  ctx.save();
  ctx.globalCompositeOperation = "lighter";

  let g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 220);
  g.addColorStop(0, `rgba(${rgb},${0.07 * br})`);
  g.addColorStop(0.5, `rgba(${rgb},${0.025 * br})`);
  g.addColorStop(1, `rgba(${rgb},0)`);
  ctx.beginPath(); ctx.arc(cx, cy, 220, 0, Math.PI * 2); ctx.fillStyle = g; ctx.fill();

  g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 155);
  g.addColorStop(0, `rgba(80,60,180,${0.045 * br})`);
  g.addColorStop(1, "rgba(80,60,180,0)");
  ctx.beginPath(); ctx.arc(cx, cy, 155, 0, Math.PI * 2); ctx.fillStyle = g; ctx.fill();

  g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 100);
  g.addColorStop(0, `rgba(${rgb},${0.22 * br})`);
  g.addColorStop(0.6, `rgba(${rgb},${0.07 * br})`);
  g.addColorStop(1, `rgba(${rgb},0)`);
  ctx.beginPath(); ctx.arc(cx, cy, 100, 0, Math.PI * 2); ctx.fillStyle = g; ctx.fill();

  const r = 42 + p * 5 + energy * 8;
  g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
  g.addColorStop(0, `rgba(${Math.min(255, r0 + 165)},255,245,${0.95 * br})`);
  g.addColorStop(0.25, `rgba(${rgb},${0.85 * br})`);
  g.addColorStop(0.7, `rgba(${Math.round(r0 * 0.45)},${Math.round(g0 * 0.75)},${Math.round(b0 * 0.72)},${0.3 * br})`);
  g.addColorStop(1, `rgba(${Math.round(r0 * 0.22)},${Math.round(g0 * 0.47)},${Math.round(b0 * 0.48)},0)`);
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fillStyle = g; ctx.fill();

  g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 16 + energy * 4);
  g.addColorStop(0, `rgba(255,255,255,${0.65 * br})`);
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.beginPath(); ctx.arc(cx, cy, 16, 0, Math.PI * 2); ctx.fillStyle = g; ctx.fill();

  ctx.restore();
}

let started;
function loop(ts) {
  if (!started) started = ts;
  drawFrame((ts - started) / 1000);
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);

/* ---------- telemetry ---------- */

function bumpCounters({ input_tokens = 0, output_tokens = 0, cache_read_tokens = 0 }) {
  counters.in += input_tokens;
  counters.out += output_tokens;
  counters.cache += cache_read_tokens;
  tele.in.textContent = counters.in.toLocaleString();
  tele.out.textContent = counters.out.toLocaleString();
  tele.cache.textContent = counters.cache.toLocaleString();
}

setInterval(() => {
  const elapsed = Math.floor((Date.now() - startedAt) / 1000);
  const minutes = String(Math.floor(elapsed / 60)).padStart(2, "0");
  const seconds = String(elapsed % 60).padStart(2, "0");
  tele.uptime.textContent = `${minutes}:${seconds}`;
}, 1000);

/* ---------- rendering helpers ---------- */

const escapeHtml = (text) =>
  text.replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

/* Just enough markdown for chat: fenced code, inline code, bold, italic, links.
   Everything is escaped first, so model and tool output cannot inject markup. */
function renderMarkdown(text) {
  const blocks = [];
  const mark = (index) => `␂CODE${index}␃`;

  let html = escapeHtml(text).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code data-lang="${lang}">${code.replace(/\n$/, "")}</code></pre>`);
    return mark(blocks.length - 1);
  });

  html = html
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  html = html
    .split(/\n{2,}/)
    .map((para) => (para.trim() ? `<p>${para.replace(/\n/g, "<br>")}</p>` : ""))
    .join("");

  return html.replace(/␂CODE(\d+)␃/g, (_, index) => blocks[Number(index)]);
}

function atBottom() {
  return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
}

function scroll(force = false) {
  if (force || atBottom()) thread.scrollTop = thread.scrollHeight;
}

function addBubble(role, text = "") {
  empty.classList.add("hidden");
  const node = document.createElement("div");
  node.className = `bubble ${role}`;
  if (role === "user") node.textContent = text;
  thread.append(node);
  scroll(true);
  return node;
}

function addBlock(className, text = "") {
  empty.classList.add("hidden");
  const node = document.createElement("div");
  node.className = className;
  if (text) node.textContent = text;
  thread.append(node);
  scroll();
  return node;
}

/* ---------- assistant stream ---------- */

function assistantBubble() {
  if (!current) current = { node: addBubble("assistant"), text: "", thinking: null };
  return current;
}

function appendText(delta) {
  const message = assistantBubble();
  message.text += delta;
  message.node.innerHTML = renderMarkdown(message.text) + '<span class="cursor"></span>';
  if (message.thinking) message.node.prepend(message.thinking);
  scroll();
}

function appendThinking(delta) {
  const message = assistantBubble();
  if (!message.thinking) {
    message.thinking = document.createElement("div");
    message.thinking.className = "thinking";
    message.node.prepend(message.thinking);
  }
  message.thinking.textContent += delta;
  scroll();
}

function finishMessage() {
  if (!current) return;
  current.node.innerHTML = renderMarkdown(current.text);
  if (current.thinking) current.node.prepend(current.thinking);
  if (!current.text.trim() && !current.thinking) current.node.remove();
  current = null;
}

/* ---------- tools ---------- */

const openTools = new Map();

function describeArgs(args) {
  if (!args) return "";
  for (const key of ["path", "command", "query", "pattern", "task", "key", "text", "id"]) {
    if (key in args) return String(args[key]);
  }
  const first = Object.values(args)[0];
  return first === undefined ? "" : String(first);
}

function toolStarted(event) {
  finishMessage();
  const node = document.createElement("details");
  node.className = "tool running";
  node.innerHTML = `
    <summary>
      <span class="icon">&#9673;</span>
      <span class="name"></span>
      <span class="args"></span>
      <span class="ms"></span>
    </summary>`;
  node.querySelector(".name").textContent = event.name;
  node.querySelector(".args").textContent = describeArgs(event.input);
  thread.append(node);
  openTools.set(event.tool_use_id || event.name, node);
  counters.tools += 1;
  tele.tools.textContent = counters.tools;
  scroll();
}

function toolFinished(event) {
  const node = openTools.get(event.tool_use_id || event.name);
  if (!node) return;
  openTools.delete(event.tool_use_id || event.name);
  node.classList.remove("running");
  if (event.is_error) node.classList.add("error");
  node.querySelector(".icon").textContent = event.is_error ? "✕" : "✓";
  node.querySelector(".ms").textContent = event.duration_ms ? `${event.duration_ms}ms` : "";
  const output = document.createElement("pre");
  output.textContent = event.result || "(no output)";
  node.append(output);
  if (event.is_error) node.open = true;
  scroll();
}

/* ---------- the inbox ---------- */

function renderInbox() {
  const items = [...inbox.values()];
  inboxCount.textContent = items.length;
  noticesCount.textContent = items.length;
  noticesCount.classList.toggle("hidden", items.length === 0);

  inboxEntries.replaceChildren();
  if (!items.length) {
    const blank = document.createElement("p");
    blank.className = "pempty";
    blank.textContent = "nothing waiting";
    inboxEntries.append(blank);
    return;
  }
  items.forEach((notice) => {
    const entry = document.createElement("div");
    entry.className = `pentry ${notice.level}`;
    const label = document.createElement("div");
    label.className = "pe-l";
    label.textContent = notice.text;
    const meta = document.createElement("div");
    meta.className = "pe-m";
    meta.textContent = notice.source;
    const clear = document.createElement("button");
    clear.className = "pe-x";
    clear.textContent = "×";
    clear.title = "Dismiss";
    clear.onclick = () => {
      entry.classList.add("gone");
      send({ type: "dismiss", id: notice.id });
    };
    entry.append(label, meta, clear);
    inboxEntries.append(entry);
  });
}

/* A notice the heartbeat surfaced. It lands in the panel either way; it only
   enters the conversation when it was loud enough to interrupt. */
function surfaced(event, quiet = false) {
  inbox.set(event.notice_id, {
    id: event.notice_id,
    text: event.text,
    detail: event.detail || "",
    level: event.level || "quiet",
    source: event.source || "jarvis",
  });
  renderInbox();
  if (quiet) return;

  finishMessage();
  const block = addBlock(`surfaced ${event.level || "quiet"}`);
  const label = document.createElement("span");
  label.className = "label";
  label.textContent = event.source || "jarvis";
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = event.text;
  const clear = document.createElement("button");
  clear.className = "dismiss";
  clear.textContent = "dismiss";
  clear.onclick = () => {
    block.classList.add("dismissed");
    clear.remove();
    send({ type: "dismiss", id: event.notice_id });
  };
  block.append(label, body, clear);
  if (event.detail) {
    const detail = document.createElement("pre");
    detail.className = "detail";
    detail.textContent = event.detail;
    block.append(detail);
  }
  // A notice that interrupted deserves a toast. A batch of them arriving
  // because you just opened the tab does not - you are already looking at it.
  if (event.level !== "quiet" && !event.caught_up) {
    toast(event.source || "jarvis", event.text, event.level);
  }
  scroll();
}

function setPaused(state) {
  paused = state;
  holdButton.classList.toggle("active", paused);
  holdButton.title = paused
    ? "Held - proactive behaviour is stopped. Click to resume."
    : "Hold - stop all proactive behaviour";
  wmdot.classList.toggle("held", paused);
}

/* ---------- approval ---------- */

function askApproval(event) {
  pendingApproval = event.id;
  document.getElementById("approval-action").textContent = event.action;
  document.getElementById("approval-detail").textContent = event.detail;
  approvalNote.textContent =
    "Approving this authorises this action only — the next one asks again.";
  approvalModal.classList.remove("hidden");
  document.getElementById("allow").focus();
}

function answerApproval(allow) {
  if (pendingApproval) send({ type: "approval", id: pendingApproval, allow });
  pendingApproval = null;
  approvalModal.classList.add("hidden");
  input.focus();
}

function toast(label, text, level = "quiet", ms = 20000) {
  const node = document.createElement("div");
  node.className = `toast ${level}`;
  const title = document.createElement("span");
  title.className = "label";
  title.textContent = label;
  const body = document.createElement("div");
  body.textContent = text;
  node.append(title, body);
  toasts.append(node);
  setTimeout(() => node.remove(), ms);
}

/* ---------- socket ---------- */

function setStreaming(active) {
  streaming = active;
  stopButton.classList.toggle("hidden", !active);
  sendButton.classList.toggle("hidden", active);
  mhint.textContent = active
    ? "working — press stop to interrupt"
    : "enter to send · shift+enter for a new line";
}

function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
}

function handle(event) {
  switch (event.kind) {
    case "ready":
      document.getElementById("model").textContent = event.model;
      document.getElementById("workspace").textContent = event.workspace;
      document.getElementById("readout-approval").textContent = event.approval;
      setPaused(Boolean(event.paused));
      send({ type: "notices" });   // fill the panel without waiting to be asked
      break;
    case "text":
      setState("speaking");
      appendText(event.text);
      break;
    case "thinking":
      setState("thinking");
      appendThinking(event.text);
      break;
    case "tool_started":
      setState("tool");
      toolStarted(event);
      break;
    case "tool_finished":
      toolFinished(event);
      break;
    case "notice":
      finishMessage();
      addBlock(`notice ${event.level || "info"}`, event.message);
      scroll();
      break;
    case "reminder":
      finishMessage();
      addBlock("surfaced notify").append(
        Object.assign(document.createElement("span"), { className: "label", textContent: "reminder" }),
        Object.assign(document.createElement("div"), { className: "body", textContent: event.text }),
      );
      toast("reminder", event.text, "notify");
      scroll();
      break;
    case "surfaced":
      surfaced(event);
      break;
    case "paused":
      setPaused(Boolean(event.paused));
      addBlock("notice warn", event.paused
        ? "held — checks, reminders and background work are stopped"
        : "resumed — proactive behaviour is back on");
      scroll();
      break;
    case "dismissed":
      if (event.id === "all") inbox.clear();
      else inbox.delete(event.id);
      renderInbox();
      break;
    case "notices":
      inbox.clear();
      event.notices.forEach((notice) =>
        inbox.set(notice.id, { ...notice, level: notice.level || "quiet" }));
      renderInbox();
      break;
    case "error":
      finishMessage();
      setState("error");
      addBlock("error-line", event.message);
      scroll();
      break;
    case "approval_request":
      askApproval(event);
      break;
    case "turn_finished":
      finishMessage();
      setStreaming(false);
      setState(socket && socket.readyState === WebSocket.OPEN ? "idle" : "offline");
      bumpCounters(event);
      if (event.output_tokens) {
        const cached = event.cache_read_tokens ? ` · ${event.cache_read_tokens} cached` : "";
        addBlock("usage", `${event.input_tokens} in · ${event.output_tokens} out${cached}`);
      }
      break;
  }
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/ws`);

  socket.onopen = () => {
    retryDelay = 500;
    connPill.textContent = "online";
    connPill.className = "conn-pill ok";
    wmdot.classList.remove("off");
    setState("idle");
  };
  socket.onmessage = (message) => handle(JSON.parse(message.data));
  socket.onclose = () => {
    connPill.textContent = "reconnecting…";
    connPill.className = "conn-pill err";
    wmdot.classList.add("off");
    setState("offline");
    setStreaming(false);
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 10000);
  };
  socket.onerror = () => socket.close();
}

/* ---------- input ---------- */

function submit() {
  const text = input.value.trim();
  if (!text || streaming) return;
  addBubble("user", text);
  finishMessage();
  send({ type: "message", text });
  input.value = "";
  input.style.height = "auto";
  setStreaming(true);
  setState("thinking");
  scroll(true);
}

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 168)}px`;
});

sendButton.addEventListener("click", submit);
stopButton.addEventListener("click", () => send({ type: "interrupt" }));
holdButton.addEventListener("click", () => send({ type: "pause", paused: !paused }));
noticesButton.addEventListener("click", () => {
  apanel.classList.remove("col");
  send({ type: "notices" });
});

resetButton.addEventListener("click", () => {
  send({ type: "reset" });
  thread.querySelectorAll(".bubble, .tool, .notice, .usage, .error-line, .surfaced")
    .forEach((node) => node.remove());
  empty.classList.remove("hidden");
  current = null;
  input.focus();
});

document.getElementById("ptgl").addEventListener("click", () => {
  apanel.classList.toggle("col");
  // The orb is centred on the conversation column, so recentre it once the
  // panel has finished sliding.
  setTimeout(resize, 340);
});

document.getElementById("allow").addEventListener("click", () => answerApproval(true));
document.getElementById("deny").addEventListener("click", () => answerApproval(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && pendingApproval) answerApproval(false);
});

renderInbox();
setState("offline");
connect();
