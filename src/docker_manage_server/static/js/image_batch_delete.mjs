export const selectionState = (checkedItems) => {
  const count = checkedItems.filter(Boolean).length;
  return {
    count,
    canDelete: count > 0,
    all: checkedItems.length > 0 && count === checkedItems.length,
    indeterminate: count > 0 && count < checkedItems.length,
  };
};

export const deletionCandidateIds = (preview) => (
  preview.deletable.map((item) => item.id)
);

export const imageLabel = (item) => (
  item.tags?.[0] || `${item.short_id || item.id}（未标记）`
);

export const imagesListUrl = (query, page) => {
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  params.set("page", String(page));
  return `/images?${params.toString()}`;
};

const appendImage = (list, item, { containers = false, error = false } = {}) => {
  const row = document.createElement("li");
  const code = document.createElement("code");
  code.textContent = imageLabel(item);
  row.appendChild(code);
  if (containers && item.containers?.length) {
    const nested = document.createElement("ul");
    nested.className = "batch-container-list";
    item.containers.forEach((container) => {
      const containerRow = document.createElement("li");
      containerRow.textContent = `${container.name}（${container.status}）`;
      nested.appendChild(containerRow);
    });
    row.appendChild(nested);
  }
  if (error && item.error) {
    const message = document.createElement("span");
    message.className = "batch-item-error";
    message.textContent = item.error;
    row.appendChild(message);
  }
  list.appendChild(row);
};

const renderImages = (list, items, options) => {
  list.replaceChildren();
  items.forEach((item) => appendImage(list, item, options));
  list.closest("section").hidden = items.length === 0;
};

const renderMissing = (list, ids) => {
  list.replaceChildren();
  ids.forEach((id) => {
    const row = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = id;
    row.appendChild(code);
    list.appendChild(row);
  });
  list.closest("section").hidden = ids.length === 0;
};

const responseError = (payload, status) => {
  if (typeof payload.detail === "string") return payload.detail;
  return `HTTP ${status}`;
};

const root = typeof document === "undefined"
  ? null
  : document.querySelector("[data-image-batch]");

if (root) {
  const enter = root.querySelector("[data-image-batch-enter]");
  const actions = root.querySelector("[data-image-batch-actions]");
  const cancel = root.querySelector("[data-image-batch-cancel]");
  const submit = root.querySelector("[data-image-batch-submit]");
  const count = root.querySelector("[data-image-selected-count]");
  const cells = Array.from(root.querySelectorAll("[data-image-select-cell]"));
  const items = Array.from(root.querySelectorAll("[data-image-select-item]"));
  const selectAll = root.querySelector("[data-image-select-all]");
  const dialog = root.querySelector("[data-image-batch-dialog]");
  const close = dialog.querySelector("[data-image-batch-close]");
  const confirm = dialog.querySelector("[data-image-batch-confirm]");
  const returnButton = dialog.querySelector("[data-image-batch-return]");
  const previewPanel = dialog.querySelector("[data-image-batch-preview]");
  const resultPanel = dialog.querySelector("[data-image-batch-result]");
  const errorPanel = dialog.querySelector("[data-image-batch-error]");
  let completed = false;
  let candidateIds = [];

  const selectedIds = () => (
    items.filter((item) => item.checked).map((item) => item.value)
  );

  const syncSelection = () => {
    const state = selectionState(items.map((item) => item.checked));
    count.textContent = String(state.count);
    submit.disabled = !state.canDelete;
    selectAll.checked = state.all;
    selectAll.indeterminate = state.indeterminate;
  };

  const setBatchMode = (enabled) => {
    enter.hidden = enabled;
    actions.hidden = !enabled;
    cells.forEach((cell) => { cell.hidden = !enabled; });
    if (!enabled) items.forEach((item) => { item.checked = false; });
    syncSelection();
  };

  const showError = (message) => {
    errorPanel.textContent = message;
    errorPanel.hidden = false;
  };

  const clearError = () => {
    errorPanel.textContent = "";
    errorPanel.hidden = true;
  };

  const returnToList = () => {
    const page = Number.parseInt(
      dialog.dataset.suggestedPage || root.dataset.page,
      10,
    );
    window.location.assign(imagesListUrl(root.dataset.query, page));
  };

  enter.addEventListener("click", () => setBatchMode(true));
  cancel.addEventListener("click", () => setBatchMode(false));
  items.forEach((item) => item.addEventListener("change", syncSelection));
  selectAll.addEventListener("change", () => {
    items.forEach((item) => { item.checked = selectAll.checked; });
    syncSelection();
  });

  submit.addEventListener("click", async () => {
    const imageIds = selectedIds();
    if (!imageIds.length) return;
    submit.disabled = true;
    confirm.disabled = true;
    candidateIds = [];
    clearError();
    try {
      const response = await fetch(root.dataset.previewUrl, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ image_ids: imageIds }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(responseError(payload, response.status));
      }
      renderImages(
        dialog.querySelector("[data-batch-deletable]"),
        payload.deletable,
      );
      renderImages(
        dialog.querySelector("[data-batch-in-use]"),
        payload.in_use,
        { containers: true },
      );
      renderMissing(
        dialog.querySelector("[data-batch-missing]"),
        payload.missing,
      );
      candidateIds = deletionCandidateIds(payload);
      confirm.disabled = candidateIds.length === 0;
      previewPanel.hidden = false;
      resultPanel.hidden = true;
      completed = false;
      dialog.showModal();
    } catch (error) {
      showError(`预检失败：${error.message}`);
      if (!dialog.open) dialog.showModal();
    } finally {
      syncSelection();
    }
  });

  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    clearError();
    try {
      const response = await fetch(root.dataset.deleteUrl, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          image_ids: candidateIds,
          query: root.dataset.query,
          page: Number.parseInt(root.dataset.page, 10),
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(responseError(payload, response.status));
      }
      renderImages(
        dialog.querySelector("[data-batch-deleted]"),
        payload.deleted,
      );
      renderImages(
        dialog.querySelector("[data-batch-result-in-use]"),
        payload.in_use,
        { containers: true },
      );
      renderMissing(
        dialog.querySelector("[data-batch-result-missing]"),
        payload.missing,
      );
      renderImages(
        dialog.querySelector("[data-batch-failed]"),
        payload.failed,
        { error: true },
      );
      dialog.querySelector("[data-image-batch-summary]").textContent = (
        `已删除 ${payload.deleted.length} 个，跳过 ${payload.in_use.length + payload.missing.length} 个，失败 ${payload.failed.length} 个。`
      );
      dialog.dataset.suggestedPage = String(payload.suggested_page);
      previewPanel.hidden = true;
      resultPanel.hidden = false;
      completed = true;
    } catch (error) {
      showError(`删除失败：${error.message}`);
      confirm.disabled = false;
    }
  });

  close.addEventListener("click", () => {
    if (completed) returnToList();
    else dialog.close();
  });
  returnButton.addEventListener("click", returnToList);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog && !completed) dialog.close();
  });
  dialog.addEventListener("cancel", (event) => {
    if (completed) {
      event.preventDefault();
      returnToList();
    }
  });
  syncSelection();
}
