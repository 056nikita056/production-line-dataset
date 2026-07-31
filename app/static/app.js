const state = { dashboard: null, filter: "all", polling: null };

const statusLabels = {
  pending: "В очереди",
  processing: "Обработка",
  review: "На проверке",
  approved: "Подтверждено",
  rejected: "Отклонено",
  failed: "Ошибка",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = response.headers.get("content-type")?.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) throw new Error(body?.detail || `Ошибка HTTP ${response.status}`);
  return body;
}

function notice(message, error = false) {
  const node = document.querySelector("#notice");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = false;
  window.clearTimeout(node._timer);
  node._timer = window.setTimeout(() => { node.hidden = true; }, 6500);
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  if (busy) {
    button.dataset.original = button.textContent;
    button.textContent = label;
  } else if (button.dataset.original) {
    button.textContent = button.dataset.original;
  }
}

function renderItems() {
  const body = document.querySelector("#items-body");
  const items = (state.dashboard?.items || []).filter(
    (item) => state.filter === "all"
      || (state.filter === "active"
        ? ["pending", "processing"].includes(item.status)
        : item.status === state.filter)
  );
  body.replaceChildren();
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "empty";
    cell.textContent = state.filter === "all"
      ? "Очередь пуста. Положите изображения в data/incoming."
      : "Кадров с таким статусом нет.";
    row.append(cell);
    body.append(row);
    return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    const fileCell = document.createElement("td");
    const fileWrap = document.createElement("div");
    fileWrap.className = "file-cell";
    const image = document.createElement("img");
    image.className = "thumb";
    image.src = item.source_url;
    image.alt = "";
    image.loading = "lazy";
    const fileMeta = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = item.original_name;
    const dimensions = document.createElement("span");
    dimensions.textContent = `${item.width} × ${item.height}`;
    fileMeta.append(name, dimensions);
    fileWrap.append(image, fileMeta);
    fileCell.append(fileWrap);

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `status status-${item.status}`;
    badge.textContent = statusLabels[item.status] || item.status;
    statusCell.append(badge);

    const resultCell = document.createElement("td");
    resultCell.textContent = item.has_label ? "YOLO + preview" : (item.error_message || "—");
    const actionCell = document.createElement("td");
    if (["review", "failed", "rejected", "approved"].includes(item.status)) {
      const link = document.createElement("a");
      link.className = "text-link";
      link.href = item.review_url;
      link.textContent = item.status === "review" ? "Проверить →" : "Открыть →";
      actionCell.append(link);
    }
    row.append(fileCell, statusCell, resultCell, actionCell);
    body.append(row);
  }
}

function renderDashboard(data) {
  state.dashboard = data;
  document.querySelector("#incoming-count").textContent = data.incoming;
  document.querySelector("#pending-count").textContent = data.queued_count;
  document.querySelector("#review-count").textContent = data.counts.review;
  document.querySelector("#approved-count").textContent = data.counts.approved;
  document.querySelector("#failed-count").textContent = data.counts.failed;
  const system = document.querySelector("#system-state");
  system.querySelector("span:last-child").textContent = data.worker_busy
    ? "Идёт обработка"
    : "Система готова";
  system.querySelector(".pulse").classList.toggle("busy", data.worker_busy);
  document.querySelector("#run-button").disabled = data.worker_busy;
  const errors = document.querySelector("#errors-list");
  errors.replaceChildren();
  if (!data.errors.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "Ошибок пока нет";
    errors.append(empty);
  } else {
    for (const error of data.errors) {
      const item = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = error.code;
      const description = document.createElement("span");
      description.textContent = error.message;
      item.append(title, description);
      errors.append(item);
    }
  }
  renderItems();
  if (data.worker_busy && !state.polling) {
    state.polling = window.setInterval(loadDashboard, 1600);
  }
  if (!data.worker_busy && state.polling) {
    window.clearInterval(state.polling);
    state.polling = null;
  }
}

async function loadDashboard() {
  try {
    renderDashboard(await api("/api/dashboard"));
  } catch (error) {
    notice(error.message, true);
  }
}

document.querySelector("#scan-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "Сканирование…");
  try {
    const result = await api("/api/scan", { method: "POST" });
    notice(`Добавлено: ${result.added}, дубликатов: ${result.duplicates}, отклонено: ${result.rejected}.`);
    await loadDashboard();
  } catch (error) {
    notice(error.message, true);
  } finally {
    setBusy(button, false);
  }
});

document.querySelector("#run-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "Запуск…");
  try {
    const run = await api("/api/runs", { method: "POST" });
    notice(`Запуск создан: ${run.total_items} кадр(ов). Обработка идёт последовательно.`);
    await loadDashboard();
  } catch (error) {
    notice(error.message, true);
    setBusy(button, false);
  }
});

document.querySelector("#export-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "Сборка ZIP…");
  const output = document.querySelector("#export-result");
  try {
    const result = await api("/api/exports", { method: "POST" });
    output.replaceChildren();
    const link = document.createElement("a");
    link.href = result.download_url;
    link.textContent = `Скачать ZIP · ${result.item_count} кадр(ов)`;
    output.append(link);
    notice("Экспорт сформирован только из подтверждённых результатов.");
  } catch (error) {
    notice(error.message, true);
  } finally {
    setBusy(button, false);
  }
});

for (const filter of document.querySelectorAll(".filter")) {
  filter.addEventListener("click", () => {
    document.querySelector(".filter.active")?.classList.remove("active");
    filter.classList.add("active");
    state.filter = filter.dataset.filter;
    renderItems();
  });
}

loadDashboard();
