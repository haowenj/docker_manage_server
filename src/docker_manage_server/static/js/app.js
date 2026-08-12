document.querySelectorAll("form[data-confirm]").forEach((element) => {
  element.addEventListener("submit", (event) => {
    if (!window.confirm(element.dataset.confirm)) event.preventDefault();
  });
});

document.querySelectorAll("[data-dialog-open]").forEach((button) => {
  button.addEventListener("click", () => {
    const dialog = document.getElementById(button.dataset.dialogOpen);
    if (dialog) dialog.showModal();
  });
});

document.querySelectorAll("[data-container-dialog]").forEach((dialog) => {
  dialog.querySelector("[data-dialog-close]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

const renderTextList = (list, values, emptyText = "无") => {
  const items = values.length ? values : [emptyText];
  list.replaceChildren(...items.map((value) => {
    const row = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = value;
    row.appendChild(code);
    return row;
  }));
};

const renderImageNames = (item) => {
  const title = document.querySelector("[data-image-title]");
  const list = document.querySelector("[data-image-name-list]");
  if (!title || !list) return;

  const names = item.tags.length ? item.tags : [item.short_id];
  title.textContent = names[0];
  list.replaceChildren(...names.map((name) => {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = name;
    return tag;
  }));
  if (!item.tags.length) {
    const marker = document.createElement("span");
    marker.className = "muted";
    marker.textContent = "未标记";
    list.appendChild(marker);
  }
};

document.querySelectorAll("[data-image-delete-dialog]").forEach((dialog) => {
  const form = dialog.querySelector("[data-image-delete-form]");
  const submit = dialog.querySelector("[data-image-delete-submit]");
  const preview = dialog.querySelector("[data-image-delete-preview]");
  const resultPanel = dialog.querySelector("[data-image-delete-result]");
  const errorPanel = dialog.querySelector("[data-image-delete-error]");
  const imageStatus = dialog.querySelector("[data-image-exists-status]");
  const listLink = dialog.querySelector("[data-image-list-link]");

  dialog.querySelector("[data-dialog-close]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    errorPanel.hidden = true;
    errorPanel.textContent = "";

    try {
      const response = await fetch(dialog.dataset.deleteUrl, {
        method: "DELETE",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);

      renderTextList(dialog.querySelector("[data-deleted-tags]"), payload.deleted_tags);
      renderTextList(dialog.querySelector("[data-retained-tags]"), payload.retained_tags);
      renderTextList(dialog.querySelector("[data-skipped-tags]"), payload.skipped_tags);
      preview.hidden = true;
      form.hidden = true;
      resultPanel.hidden = false;
      document.querySelector("[data-image-delete-open]")?.setAttribute("hidden", "");

      if (!payload.image_exists) {
        imageStatus.textContent = "镜像已不存在。";
        listLink.hidden = false;
        return;
      }

      imageStatus.textContent = "镜像仍存在，名称已同步。";
      try {
        const detailResponse = await fetch(dialog.dataset.detailUrl, {
          headers: { Accept: "application/json" },
        });
        const detail = await detailResponse.json().catch(() => ({}));
        if (!detailResponse.ok) {
          throw new Error(detail.detail || `HTTP ${detailResponse.status}`);
        }
        renderImageNames(detail.item);
      } catch (_error) {
        errorPanel.textContent = "删除已完成，但详情同步失败，请手动刷新。";
        errorPanel.hidden = false;
      }
    } catch (error) {
      errorPanel.textContent = `删除失败：${error.message}`;
      errorPanel.hidden = false;
      submit.disabled = false;
    }
  });
});

const autoOpenContainer = document.querySelector("[data-auto-open-dialog]");
const autoOpenDialogId = autoOpenContainer?.dataset.autoOpenDialog;
if (autoOpenDialogId) {
  const autoOpenDialog = document.getElementById(autoOpenDialogId);
  if (autoOpenDialog instanceof HTMLDialogElement && !autoOpenDialog.open) {
    autoOpenDialog.showModal();
  }
}

document.querySelectorAll("[data-directory-editor]").forEach((editor) => {
  const list = editor.querySelector("[data-directory-list]");
  const template = editor.querySelector("[data-directory-row-template]");
  const initialNode = editor.querySelector("[data-directory-initial]");
  const serialized = editor.querySelector("[data-directory-json]");
  const addButton = editor.querySelector("[data-directory-add]");

  const readMode = (row) => {
    let value = 0;
    row.querySelectorAll("[data-permission-bit]").forEach((checkbox) => {
      if (checkbox.checked) {
        value |= Number.parseInt(checkbox.dataset.permissionBit, 8);
      }
    });
    return value.toString(8).padStart(4, "0");
  };

  const renderMode = (row, mode) => {
    const numeric = Number.parseInt(mode, 8);
    row.querySelectorAll("[data-permission-bit]").forEach((checkbox) => {
      const bit = Number.parseInt(checkbox.dataset.permissionBit, 8);
      checkbox.checked = (numeric & bit) === bit;
    });
    const preset = row.querySelector("[data-directory-mode]");
    preset.value = Array.from(preset.options).some((option) => option.value === mode)
      ? mode
      : "";
    row.querySelector("[data-directory-mode-value]").textContent = mode;
    row.querySelector("[data-permission-warning]").hidden = mode !== "0777";
  };

  const addRule = (rule = { path: "", mode: "0755" }) => {
    const fragment = template.content.cloneNode(true);
    const row = fragment.querySelector("[data-directory-rule]");
    row.querySelector("[data-directory-path]").value = rule.path || "";
    renderMode(row, rule.mode || "0755");
    row.querySelector("[data-directory-mode]").addEventListener("change", (event) => {
      if (event.target.value) renderMode(row, event.target.value);
    });
    row.querySelectorAll("[data-permission-bit]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => renderMode(row, readMode(row)));
    });
    row.querySelector("[data-directory-remove]").addEventListener("click", () => row.remove());
    list.appendChild(fragment);
  };

  let initial = [];
  try {
    initial = JSON.parse(initialNode.textContent || "[]");
  } catch (_error) {
    initial = [];
  }
  if (initial.length) initial.forEach(addRule);
  else addRule();

  addButton.addEventListener("click", () => addRule());
  editor.addEventListener("submit", () => {
    const rules = Array.from(list.querySelectorAll("[data-directory-rule]"))
      .map((row) => ({
        path: row.querySelector("[data-directory-path]").value.trim(),
        mode: readMode(row),
      }))
      .filter((rule) => rule.path);
    serialized.value = JSON.stringify(rules);
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
