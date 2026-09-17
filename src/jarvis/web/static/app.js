/* Jarvis web client: one WebSocket, the same event stream every face consumes. */

const thread = document.getElementById("thread");
const input = document.getElementById("input");
const sendButton = document.getElementById("send");
const stopButton = document.getElementById("stop");
const resetButton = document.getElementById("reset");
const statusDot = document.getElementById("status");
const empty = document.getElementById("empty");
const approvalModal = document.getElementById("approval");
const toasts = document.getElementById("toasts");

let socket = null;
let streaming = false;
let current = null;      // the assistant message being built
let pendingApproval = null;
let retryDelay = 500;

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
  who.textContent = role === "user" ? "you" : "jarvis";
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
      <span class="icon">&#9672;</span>
      <span class="name"></span>
      <span class="args"></span>
      <span class="ms"></span>
    </summary>`;
  node.querySelector(".name").textContent = event.name;
  node.querySelector(".args").textContent = describeArgs(event.input);
  thread.append(node);
  openTools.set(event.tool_use_id || event.name, node);
  scroll();
}

function toolFinished(event) {
  const node = openTools.get(event.tool_use_id || event.name);
  if (!node) return;
  openTools.delete(event.tool_use_id || event.name);
  node.classList.remove("running");
  if (event.is_error) node.classList.add("error");
  node.querySelector(".icon").textContent = event.is_error ? "✗" : "✓";
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
}

function answerApproval(allow) {
  if (pendingApproval) send({ type: "approval", id: pendingApproval, allow });
  pendingApproval = null;
  approvalModal.classList.add("hidden");
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
      document.getElementById("approval").textContent = `approval: ${event.approval}`;
      break;
    case "text": appendText(event.text); break;
    case "thinking": appendThinking(event.text); break;
    case "tool_started": toolStarted(event); break;
    case "tool_finished": toolFinished(event); break;
    case "notice":
      finishMessage();
      addBlock(`notice ${event.level || "info"}`, event.message);
      break;
    case "reminder":
      toast("reminder", event.text);
      finishMessage();
      addBlock("notice warn", `⏰ ${event.text}`);
      break;
    case "error":
      finishMessage();
      addBlock("error-line", event.message);
      break;
    case "approval_request": askApproval(event); break;
    case "turn_finished":
      finishMessage();
      setStreaming(false);
      if (event.output_tokens) {
        const cached = event.cache_read_tokens ? `, ${event.cache_read_tokens} cached` : "";
        addBlock("usage", `${event.input_tokens} in / ${event.output_tokens} out${cached}`);
      }
      break;
  }
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/ws`);

  socket.onopen = () => {
    retryDelay = 500;
    statusDot.className = "status online";
    statusDot.title = "connected";
  };
  socket.onmessage = (message) => handle(JSON.parse(message.data));
  socket.onclose = () => {
    statusDot.className = "status offline";
    statusDot.title = "reconnecting";
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
}

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
});

sendButton.addEventListener("click", submit);
stopButton.addEventListener("click", () => send({ type: "interrupt" }));
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

connect();
