document.querySelectorAll("form[data-confirm]").forEach((element) => {
  element.addEventListener("submit", (event) => {
    if (!window.confirm(element.dataset.confirm)) event.preventDefault();
  });
});

const taskRoot = document.querySelector("[data-task-poll-url]");
if (taskRoot && taskRoot.dataset.taskStatus === "deploying") {
  const labels = {
    deploying: "部署中",
    deployed: "已部署",
    failed: "失败",
  };
  const status = taskRoot.querySelector("[data-task-status-label]");
  const output = document.querySelector("[data-task-output]");
  const error = document.querySelector("[data-task-error]");

  const timer = window.setInterval(async () => {
    try {
      const response = await fetch(taskRoot.dataset.taskPollUrl, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const task = await response.json();
      status.textContent = labels[task.status] || task.status;
      taskRoot.dataset.taskStatus = task.status;
      if (output) output.textContent = task.command_output || "暂无命令输出";
      if (error) error.textContent = task.error || "";
      if (task.status === "deployed" || task.status === "failed") {
        window.clearInterval(timer);
        window.location.reload();
      }
    } catch (_error) {
      status.textContent = "状态刷新失败";
    }
  }, 2000);
}

document.querySelectorAll("[data-log-viewer]").forEach((viewer) => {
  const tail = viewer.querySelector("[data-log-tail]");
  const timestamps = viewer.querySelector("[data-log-timestamps]");
  const refresh = viewer.querySelector("[data-log-refresh]");
  const output = viewer.querySelector("[data-log-output]");

  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    output.textContent = "正在读取日志…";
    const query = new URLSearchParams({
      tail: tail.value,
      timestamps: String(timestamps.checked),
    });
    try {
      const response = await fetch(`${viewer.dataset.logUrl}?${query}`, {
        headers: { Accept: "text/plain" },
      });
      const text = await response.text();
      if (!response.ok) throw new Error(text || `HTTP ${response.status}`);
      output.textContent = text || "日志为空。";
    } catch (error) {
      output.textContent = `日志读取失败：${error.message}`;
    } finally {
      refresh.disabled = false;
    }
  });
});
