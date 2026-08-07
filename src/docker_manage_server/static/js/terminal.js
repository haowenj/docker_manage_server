import { Terminal } from "/static/vendor/xterm/xterm.mjs";
import { FitAddon } from "/static/vendor/xterm/addon-fit.mjs";

const encoder = new TextEncoder();

function terminalUrl(root) {
  const url = new URL(root.dataset.terminalUrl, window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("command", root.dataset.terminalCommand || "/bin/sh");
  return url;
}

function connect(root, viewport, status, button) {
  button.disabled = true;
  status.textContent = "正在连接…";
  viewport.replaceChildren();

  const terminal = new Terminal({
    convertEol: true,
    cursorBlink: true,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: 14,
    theme: {
      background: "#ffffff",
      foreground: "#1e293b",
      cursor: "#2563eb",
      selectionBackground: "#bfdbfe",
    },
  });
  const fitAddon = new FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(viewport);
  fitAddon.fit();

  const socket = new WebSocket(terminalUrl(root));
  socket.binaryType = "arraybuffer";

  const sendResize = () => {
    if (socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({
      type: "resize",
      width: terminal.cols,
      height: terminal.rows,
    }));
  };

  const resizeObserver = new ResizeObserver(() => {
    fitAddon.fit();
    sendResize();
  });
  resizeObserver.observe(viewport);

  socket.addEventListener("open", () => {
    status.textContent = "已连接";
    fitAddon.fit();
    sendResize();
    terminal.focus();
  });

  socket.addEventListener("message", async (event) => {
    if (event.data instanceof ArrayBuffer) {
      terminal.write(new Uint8Array(event.data));
      return;
    }
    if (event.data instanceof Blob) {
      terminal.write(new Uint8Array(await event.data.arrayBuffer()));
      return;
    }
    terminal.write(String(event.data));
  });

  terminal.onData((data) => {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(encoder.encode(data));
    }
  });

  socket.addEventListener("error", () => {
    status.textContent = "终端连接错误";
  });

  socket.addEventListener("close", (event) => {
    resizeObserver.disconnect();
    status.textContent = event.code === 1000
      ? "连接已关闭"
      : `连接已关闭（代码 ${event.code}）`;
    button.disabled = false;
  });

  window.addEventListener("beforeunload", () => socket.close(), { once: true });
}

const root = document.querySelector("[data-terminal-url]");
if (root) {
  const button = root.querySelector("[data-terminal-connect]");
  const viewport = root.querySelector("[data-terminal-viewport]");
  const status = root.querySelector("[data-terminal-status]");
  button.addEventListener("click", () => connect(root, viewport, status, button));
}
