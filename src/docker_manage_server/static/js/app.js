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
