/* J.A.R.V.I.S. web client: one WebSocket, the same event stream every face consumes. */

const thread = document.getElementById("thread");
const input = document.getElementById("input");
const sendButton = document.getElementById("send");
const stopButton = document.getElementById("stop");
const resetButton = document.getElementById("reset");
const statusDot = document.getElementById("status");
const linkText = document.getElementById("link-text");
const empty = document.getElementById("empty");
const approvalModal = document.getElementById("approval-modal");
const toasts = document.getElementById("toasts");
const reactor = document.getElementById("reactor");
const holdButton = document.getElementById("hold");
const noticesButton = document.getElementById("notices-button");
const noticesCount = document.getElementById("notices-count");

const tele = {
  uptime: document.getElementById("tele-uptime"),
  in: document.getElementById("tele-in"),
  out: document.getElementById("tele-out"),
  cache: document.getElementById("tele-cache"),
  tools: document.getElementById("tele-tools"),
  notices: document.getElementById("tele-notices"),
  state: document.getElementById("tele-state"),
};

let socket = null;
let streaming = false;
let current = null;      // the assistant message being built
let pendingApproval = null;
let retryDelay = 500;
const counters = { in: 0, out: 0, cache: 0, tools: 0 };
let waiting = 0;   // open notices
let paused = false;
const startedAt = Date.now();

/* ---------- reactor + telemetry ---------- */

const STATE_LABELS = {
  offline: "offline",
  idle: "standby",
  thinking: "processing",
  tool: "executing",
  speaking: "responding",
  error: "fault",
};

function setState(state) {
  reactor.dataset.state = state;
  tele.state.textContent = STATE_LABELS[state] || state;
}

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

/* ---------- boot ---------- */

function boot() {
  const bootEl = document.getElementById("boot");
  const log = document.getElementById("boot-log");
  const lines = [
    "J.A.R.V.I.S.  interface mark I",
    "initialising display ......... ok",
    "opening channel .............. ok",
    "agent core ................... online",
  ];
  let index = 0;

  const timer = setInterval(() => {
    log.textContent += (index ? "\n" : "") + lines[index];
    index += 1;
    if (index >= lines.length) {
      clearInterval(timer);
      setTimeout(() => bootEl.classList.add("done"), 380);
    }
  }, 190);

  // Never let the flourish trap anyone in it.
  setTimeout(() => bootEl.classList.add("done"), 3000);
}

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

function addMessage(role, text = "") {
  empty.classList.add("hidden");
  const wrapper = document.createElement("div");
  wrapper.className = `msg ${role}`;
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = role === "user" ? "sir" : "jarvis";
  const body = document.createElement("div");
  body.className = "body";
  if (role === "user") body.textContent = text;
  wrapper.append(who, body);
  thread.append(wrapper);
  scroll(true);
  return body;
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

function assistantBody() {
  if (!current) {
    current = { body: addMessage("assistant"), text: "", thinking: null };
  }
  return current;
}

function appendText(delta) {
  const message = assistantBody();
  message.text += delta;
  message.body.innerHTML = renderMarkdown(message.text) + '<span class="cursor"></span>';
  scroll();
}

function appendThinking(delta) {
  const message = assistantBody();
  if (!message.thinking) {
    message.thinking = document.createElement("div");
    message.thinking.className = "thinking";
    message.body.before(message.thinking);
  }
  message.thinking.textContent += delta;
  scroll();
}

function finishMessage() {
  if (current) {
    current.body.innerHTML = renderMarkdown(current.text);
    current = null;
  }
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

/* ---------- approvals & toasts ---------- */

function askApproval(event) {
  pendingApproval = event.id;
  document.getElementById("approval-action").textContent = event.action;
  document.getElementById("approval-detail").textContent = event.detail;
  approvalModal.classList.remove("hidden");
  document.getElementById("allow").focus();
}

function answerApproval(allow) {
  if (pendingApproval) send({ type: "approval", id: pendingApproval, allow });
  pendingApproval = null;
  approvalModal.classList.add("hidden");
}

function setWaiting(count) {
  waiting = Math.max(0, count);
  tele.notices.textContent = waiting;
  noticesCount.textContent = waiting;
  noticesCount.classList.toggle("hidden", waiting === 0);
}

function setPaused(state) {
  paused = state;
  holdButton.textContent = paused ? "held" : "hold";
  holdButton.classList.toggle("active", paused);
  holdButton.title = paused
    ? "Proactive behaviour is held. Click to resume."
    : "Kill switch: hold all proactive behaviour";
}

/* Anything the heartbeat surfaces. Dismissible, because an inbox you cannot
   empty is clutter you learn to ignore. */
function surfaced(event) {
  finishMessage();
  const block = addBlock(`notice surfaced ${event.level}`, "");
  const title = document.createElement("span");
  title.className = "label";
  title.textContent = event.source;
  const body = document.createElement("div");
  body.textContent = event.text;
  const clear = document.createElement("button");
  clear.className = "dismiss";
  clear.textContent = "dismiss";
  clear.onclick = () => {
    send({ type: "dismiss", id: event.notice_id });
    block.classList.add("dismissed");
    clear.remove();
  };
  block.append(title, body, clear);
  if (event.detail) {
    const detail = document.createElement("pre");
    detail.className = "detail";
    detail.textContent = event.detail;
    block.append(detail);
  }
  setWaiting(waiting + 1);
  if (event.level !== "quiet") toast(event.source, event.text);
}

function toast(label, text, ms = 20000) {
  const node = document.createElement("div");
  node.className = "toast";
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
      setWaiting(event.waiting || 0);
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
      break;
    case "reminder":
      toast("reminder", event.text);
      finishMessage();
      addBlock("notice warn", `${event.text}`);
      break;
    case "error":
      finishMessage();
      setState("error");
      addBlock("error-line", event.message);
      break;
    case "approval_request":
      askApproval(event);
      break;
    case "surfaced":
      surfaced(event);
      break;
    case "paused":
      setPaused(Boolean(event.paused));
      addBlock("notice warn", event.paused
        ? "proactive behaviour held — checks and reminders will not fire"
        : "proactive behaviour resumed");
      break;
    case "dismissed":
      setWaiting(event.id === "all" ? 0 : waiting - (event.count || 0));
      break;
    case "notices":
      finishMessage();
      setWaiting(event.notices.length);
      if (!event.notices.length) {
        addBlock("notice", "inbox empty");
      } else {
        event.notices.forEach((notice) => surfaced({ ...notice, notice_id: notice.id }));
        setWaiting(event.notices.length);
      }
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
    statusDot.className = "dot online";
    linkText.textContent = "online";
    setState("idle");
  };
  socket.onmessage = (message) => handle(JSON.parse(message.data));
  socket.onclose = () => {
    statusDot.className = "dot offline";
    linkText.textContent = "reconnecting";
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
  addMessage("user", text);
  finishMessage();
  send({ type: "message", text });
  input.value = "";
  input.style.height = "auto";
  setStreaming(true);
  setState("thinking");
}

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 190)}px`;
});

sendButton.addEventListener("click", submit);
stopButton.addEventListener("click", () => send({ type: "interrupt" }));
holdButton.addEventListener("click", () => send({ type: "pause", paused: !paused }));
noticesButton.addEventListener("click", () => {
  thread.querySelectorAll(".notice.surfaced").forEach((n) => n.remove());
  send({ type: "notices" });
});

resetButton.addEventListener("click", () => {
  send({ type: "reset" });
  thread.querySelectorAll(".msg, .tool, .notice, .usage, .error-line").forEach((n) => n.remove());
  empty.classList.remove("hidden");
  current = null;
});
document.getElementById("allow").addEventListener("click", () => answerApproval(true));
document.getElementById("deny").addEventListener("click", () => answerApproval(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && pendingApproval) answerApproval(false);
});

boot();
connect();
