const itemId = document.body.dataset.itemId;
let currentItem = null;
let nextReviewItem = null;

const reasonLabels = {
  blurred_object: "Размытый объект",
  uncertain_tray_state: "Неоднозначное состояние лотка",
  uncertain_object_boundary: "Неуверенная граница объекта",
  heavy_occlusion: "Сильное перекрытие",
  camera_or_line_shift: "Смещение камеры или линии",
  ambiguous_qr_code: "Неоднозначный QR-код",
  agent_output_invalid: "Некорректный ответ агента",
  agent_timeout: "Превышено время обработки",
  agent_failure: "Ошибка запуска агента",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = response.headers.get("content-type")?.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) {
    const details = Array.isArray(body?.detail)
      ? body.detail.map((error) => `${error.loc?.join(".")}: ${error.msg}`).join("; ")
      : body?.detail;
    throw new Error(details || `Ошибка HTTP ${response.status}`);
  }
  return body;
}

function notice(message, error = false) {
  const node = document.querySelector("#notice");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = false;
}

function statusLabel(status) {
  return {
    pending: "В очереди", processing: "Обработка", review: "На проверке",
    approved: "Подтверждено", rejected: "Отклонено", failed: "Ошибка",
  }[status] || status;
}

function render(item) {
  currentItem = item;
  document.querySelector("#item-name").textContent = item.original_name;
  document.querySelector("#item-size").textContent = `${item.width} × ${item.height}`;
  const badge = document.querySelector("#item-status");
  badge.textContent = statusLabel(item.status);
  badge.className = `status status-${item.status}`;
  document.querySelector("#source-image").src = item.source_url;
  const preview = document.querySelector("#preview-image");
  const previewZoomButton = document.querySelector("#preview-zoom-button");
  if (item.preview_url) {
    preview.src = `${item.preview_url}?v=${encodeURIComponent(item.updated_at)}`;
    preview.alt = "Кадр с наложенной разметкой";
    previewZoomButton.disabled = false;
  } else {
    preview.removeAttribute("src");
    preview.alt = "Предпросмотр не создан из-за ошибки";
    previewZoomButton.disabled = true;
  }
  document.querySelector("#validation-link").href = `/api/items/${item.id}/validation`;

  const objects = item.annotation?.objects || [];
  document.querySelector("#objects-count").textContent = objects.length;
  const list = document.querySelector("#objects-list");
  list.replaceChildren();
  if (!objects.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = item.annotation ? "Целевые объекты не найдены" : "Структурированный ответ недоступен";
    list.append(empty);
  } else {
    objects.forEach((object, index) => {
      const row = document.createElement("div");
      row.className = "object-row";
      const number = document.createElement("span");
      number.className = "object-index";
      number.textContent = index + 1;
      const name = document.createElement("span");
      name.className = "object-name";
      name.textContent = `${object.class_id} · ${object.class_name}`;
      const geometry = document.createElement("span");
      geometry.className = "object-detail";
      geometry.textContent = `${object.polygon.length} точки · видно ${Math.round(object.visible_fraction * 100)}%`;
      const occlusion = document.createElement("span");
      occlusion.className = "object-detail";
      occlusion.textContent = object.occluded ? "Есть перекрытие" : "Без перекрытия";
      row.append(number, name, geometry, occlusion);
      list.append(row);
    });
  }

  const reasons = [...(item.review_reasons || []), ...(item.validation_errors || [])];
  const reasonsList = document.querySelector("#reasons-list");
  reasonsList.replaceChildren();
  if (!reasons.length) {
    const normal = document.createElement("li");
    normal.textContent = item.status === "review"
      ? "Обязательная ручная проверка MVP"
      : "Дополнительных причин нет";
    reasonsList.append(normal);
  } else {
    for (const reason of [...new Set(reasons)]) {
      const node = document.createElement("li");
      node.textContent = reasonLabels[reason] || reason;
      reasonsList.append(node);
    }
  }

  const canDecide = item.status === "review";
  document.querySelector("#approve-button").disabled = !canDecide || item.validation_errors.length > 0;
  document.querySelector("#reject-button").disabled = !canDecide;
  document.querySelector("#retry-button").disabled = !["review", "failed", "rejected"].includes(item.status);
  document.querySelector("#edit-button").disabled = !canDecide || !item.annotation;
  document.querySelector("#annotation-json").value = item.annotation
    ? JSON.stringify(item.annotation, null, 2)
    : "";
}

async function updateNextButton() {
  const button = document.querySelector("#next-button");
  nextReviewItem = null;
  button.disabled = true;
  button.textContent = "Ищу следующий…";
  button.title = "";
  try {
    const response = await api(`/api/items/${itemId}/next-review`);
    nextReviewItem = response.item;
    if (!nextReviewItem) {
      button.textContent = "Кадров больше нет";
      button.title = "Все доступные кадры уже проверены";
      return;
    }
    button.textContent = "Следующий кадр →";
    button.disabled = false;
    button.title = `Открыть ${nextReviewItem.original_name}`;
  } catch (error) {
    button.textContent = "Следующий кадр недоступен";
    button.title = error.message;
  }
}

async function load() {
  try {
    const item = await api(`/api/items/${itemId}`);
    render(item);
    await updateNextButton();
  } catch (error) {
    notice(error.message, true);
  }
}

async function action(path, success) {
  try {
    await api(path, { method: "POST" });
    notice(success);
    await load();
    const nextButton = document.querySelector("#next-button");
    if (!nextButton.disabled) nextButton.focus();
  } catch (error) {
    notice(error.message, true);
  }
}

document.querySelector("#approve-button").addEventListener("click", () =>
  action(`/api/items/${itemId}/approve`, "Результат подтверждён и доступен для экспорта.")
);
document.querySelector("#reject-button").addEventListener("click", () =>
  action(`/api/items/${itemId}/reject`, "Результат отклонён и не попадёт в датасет.")
);
document.querySelector("#retry-button").addEventListener("click", () =>
  action(`/api/items/${itemId}/retry`, "Повторная обработка поставлена в очередь.")
);
document.querySelector("#next-button").addEventListener("click", () => {
  if (nextReviewItem) window.location.assign(nextReviewItem.review_url);
});

const lightbox = document.querySelector("#image-lightbox");
const lightboxImage = document.querySelector("#lightbox-image");
const lightboxTitle = document.querySelector("#lightbox-title");
const lightboxClose = document.querySelector("#lightbox-close");
let lightboxTrigger = null;

function openLightbox(image, title) {
  const source = image.currentSrc || image.src;
  if (!source) return;
  lightboxTrigger = document.activeElement;
  lightboxImage.src = source;
  lightboxImage.alt = image.alt;
  lightboxTitle.textContent = title;
  lightbox.showModal();
  lightboxClose.focus();
}

function closeLightbox() {
  if (lightbox.open) lightbox.close();
}

document.querySelector("#source-zoom-button").addEventListener("click", () => {
  openLightbox(document.querySelector("#source-image"), "Оригинал");
});
document.querySelector("#preview-zoom-button").addEventListener("click", () => {
  openLightbox(document.querySelector("#preview-image"), "Предпросмотр разметки");
});
lightboxClose.addEventListener("click", closeLightbox);
lightbox.addEventListener("click", (event) => {
  if (event.target === lightbox) closeLightbox();
});
lightbox.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeLightbox();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && lightbox.open) {
    event.preventDefault();
    closeLightbox();
  }
});
lightbox.addEventListener("close", () => {
  lightboxImage.removeAttribute("src");
  if (lightboxTrigger instanceof HTMLElement) lightboxTrigger.focus();
  lightboxTrigger = null;
});

document.querySelector("#edit-button").addEventListener("click", () => {
  const editor = document.querySelector("#editor");
  editor.hidden = !editor.hidden;
  if (!editor.hidden) document.querySelector("#annotation-json").focus();
});
document.querySelector("#save-button").addEventListener("click", async () => {
  try {
    const annotation = JSON.parse(document.querySelector("#annotation-json").value);
    const updated = await api(`/api/items/${itemId}/annotation`, {
      method: "POST",
      body: JSON.stringify(annotation),
    });
    render(updated);
    notice(updated.validation_errors.length
      ? "Изменения сохранены, но остаются ошибки валидации."
      : "Изменения проверены; YOLO и preview пересозданы.");
  } catch (error) {
    notice(`Исправление не сохранено: ${error.message}`, true);
  }
});

load();
