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
  attempts_disagree: "Два корректных результата различаются",
  detail_class_conflict: "На одном месте определены разные классы",
};

const attemptKindLabels = {
  initial: "Первый вызов",
  manual_retry: "Ручной повтор",
  auto_retry: "Автоматическая перепроверка",
  technical_retry: "Технический повтор",
  detail: "Детальный проход",
};

const selectionReasonLabels = {
  only_available_result: "единственный пригодный результат",
  first_is_valid: "первая попытка прошла проверку",
  second_is_valid: "вторая попытка прошла проверку",
  first_has_fewer_validation_errors: "в первой попытке меньше ошибок",
  second_has_fewer_validation_errors: "во второй попытке меньше ошибок",
  valid_results_disagree: "два корректных результата различаются — оставлен первый",
  second_has_fewer_review_reasons: "во второй попытке меньше причин проверки",
  first_kept_by_deterministic_tie_break: "равные результаты — оставлен первый",
  manual_selection: "выбрано оператором",
  detail_refinement_applied: "границы уточнены по детальным фрагментам",
  detail_class_conflict: "детализация нашла другой класс — оставлен исходный вариант",
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
  document.querySelector("#detail-button").disabled = !["review", "failed", "rejected"].includes(item.status);
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
    const [item, attempts] = await Promise.all([
      api(`/api/items/${itemId}`),
      api(`/api/items/${itemId}/attempts`),
    ]);
    render(item);
    renderAttempts(attempts);
    await updateNextButton();
  } catch (error) {
    notice(error.message, true);
  }
}

function renderAttempts(attempts) {
  const list = document.querySelector("#attempts-list");
  list.replaceChildren();
  if (!attempts.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "Вызовов ещё не было";
    list.append(empty);
    return;
  }
  for (const attempt of attempts) {
    const row = document.createElement("article");
    row.className = `attempt-row${attempt.selected ? " selected" : ""}`;
    const heading = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = `${attemptKindLabels[attempt.attempt_kind] || attempt.attempt_kind} · цикл ${attempt.cycle_no}`;
    const duration = document.createElement("span");
    duration.textContent = `${(attempt.duration_ms / 1000).toFixed(1)} с`;
    heading.append(title, duration);
    const detail = document.createElement("p");
    const reason = attempt.trigger_reason === "initial"
      ? "первичный анализ"
      : (reasonLabels[attempt.trigger_reason] || attempt.trigger_reason);
    detail.textContent = `Причина: ${reason}. Изображений: ${attempt.image_count}.`;
    row.append(heading, detail);
    if (attempt.selected) {
      const selected = document.createElement("small");
      const selection = selectionReasonLabels[attempt.selection_reason]
        || attempt.selection_reason
        || "результат цикла";
      selected.textContent = `Выбран: ${selection}`;
      row.append(selected);
    }
    if (attempt.revision_id && !attempt.selected && currentItem?.status === "review") {
      const selectButton = document.createElement("button");
      selectButton.type = "button";
      selectButton.className = "attempt-select";
      selectButton.textContent = "Показать и выбрать этот вариант";
      selectButton.addEventListener("click", async () => {
        selectButton.disabled = true;
        try {
          await api(
            `/api/items/${itemId}/revisions/${attempt.revision_id}/select`,
            { method: "POST" },
          );
          notice("Выбран другой вариант разметки. Проверьте изображение перед принятием.");
          await load();
        } catch (error) {
          notice(error.message, true);
          selectButton.disabled = false;
        }
      });
      row.append(selectButton);
    }
    list.append(row);
  }
}

async function action(path, success, body = null) {
  try {
    await api(path, {
      method: "POST",
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
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
  action(
    `/api/items/${itemId}/retry`,
    "Повторная обработка поставлена в очередь.",
    { recognition_mode: document.querySelector("#retry-mode").value },
  )
);

const detailDialog = document.querySelector("#detail-dialog");
const detailWorkspace = document.querySelector("#detail-workspace");
const detailImage = document.querySelector("#detail-image");
const detailOverlay = document.querySelector("#detail-overlay");
let detailRegions = [];
let detailDraft = null;

function syncDetailOverlay() {
  detailOverlay.style.left = `${detailImage.offsetLeft}px`;
  detailOverlay.style.top = `${detailImage.offsetTop}px`;
  detailOverlay.style.width = `${detailImage.clientWidth}px`;
  detailOverlay.style.height = `${detailImage.clientHeight}px`;
}

function detailPoint(event) {
  const point = detailOverlay.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const matrix = detailOverlay.getScreenCTM();
  if (!matrix) return null;
  const transformed = point.matrixTransform(matrix.inverse());
  return {
    x: Math.max(0, Math.min(currentItem.width, transformed.x)),
    y: Math.max(0, Math.min(currentItem.height, transformed.y)),
  };
}

function svgNode(name, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, value);
  }
  return node;
}

function drawDetailRegions() {
  detailOverlay.replaceChildren();
  detailOverlay.setAttribute("viewBox", `0 0 ${currentItem.width} ${currentItem.height}`);
  detailRegions.forEach((region, index) => {
    const group = svgNode("g", { class: "saved-region", tabindex: "0", role: "button" });
    const rect = svgNode("rect", {
      x: region.left_px,
      y: region.top_px,
      width: region.right_px - region.left_px,
      height: region.bottom_px - region.top_px,
    });
    const label = svgNode("text", {
      x: region.left_px + 8,
      y: region.top_px + 24,
    });
    label.textContent = `${index + 1} · удалить ×`;
    group.append(rect, label);
    const remove = async () => {
      try {
        await api(`/api/items/${itemId}/detail-regions/${region.id}`, { method: "DELETE" });
        detailRegions = detailRegions.filter((entry) => entry.id !== region.id);
        drawDetailRegions();
      } catch (error) {
        notice(error.message, true);
      }
    };
    group.addEventListener("click", remove);
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        remove();
      }
    });
    detailOverlay.append(group);
  });
  if (detailDraft) {
    detailOverlay.append(svgNode("rect", {
      class: "draft-region",
      x: Math.min(detailDraft.start.x, detailDraft.end.x),
      y: Math.min(detailDraft.start.y, detailDraft.end.y),
      width: Math.abs(detailDraft.end.x - detailDraft.start.x),
      height: Math.abs(detailDraft.end.y - detailDraft.start.y),
    }));
  }
  document.querySelector("#detail-count").textContent = `${detailRegions.length} из 4 областей`;
}

async function openDetailDialog() {
  try {
    detailRegions = await api(`/api/items/${itemId}/detail-regions`);
    detailImage.src = currentItem.source_url;
    drawDetailRegions();
    detailDialog.showModal();
    requestAnimationFrame(syncDetailOverlay);
  } catch (error) {
    notice(error.message, true);
  }
}

detailImage.addEventListener("load", syncDetailOverlay);
window.addEventListener("resize", syncDetailOverlay);
if ("ResizeObserver" in window) {
  new ResizeObserver(syncDetailOverlay).observe(detailImage);
}

detailOverlay.addEventListener("pointerdown", (event) => {
  if (event.target.closest(".saved-region") || detailRegions.length >= 4) return;
  const start = detailPoint(event);
  if (!start) return;
  detailOverlay.setPointerCapture(event.pointerId);
  detailDraft = { start, end: start, pointerId: event.pointerId };
  drawDetailRegions();
});
detailOverlay.addEventListener("pointermove", (event) => {
  if (!detailDraft || detailDraft.pointerId !== event.pointerId) return;
  const end = detailPoint(event);
  if (!end) return;
  detailDraft.end = end;
  drawDetailRegions();
});
detailOverlay.addEventListener("pointerup", async (event) => {
  if (!detailDraft || detailDraft.pointerId !== event.pointerId) return;
  const draft = detailDraft;
  detailDraft = null;
  const rectangle = {
    left: Math.round(Math.min(draft.start.x, draft.end.x)),
    top: Math.round(Math.min(draft.start.y, draft.end.y)),
    right: Math.round(Math.max(draft.start.x, draft.end.x)),
    bottom: Math.round(Math.max(draft.start.y, draft.end.y)),
  };
  if (rectangle.right - rectangle.left < 8 || rectangle.bottom - rectangle.top < 8) {
    drawDetailRegions();
    notice("Область слишком мала — протяните рамку не меньше 8 × 8 пикселей.", true);
    return;
  }
  try {
    const saved = await api(`/api/items/${itemId}/detail-regions`, {
      method: "POST",
      body: JSON.stringify(rectangle),
    });
    detailRegions.push(saved);
    drawDetailRegions();
  } catch (error) {
    drawDetailRegions();
    notice(error.message, true);
  }
});

document.querySelector("#detail-button").addEventListener("click", openDetailDialog);
document.querySelector("#detail-close").addEventListener("click", () => detailDialog.close());
document.querySelector("#detail-clear").addEventListener("click", async () => {
  try {
    await Promise.all(detailRegions.map((region) =>
      api(`/api/items/${itemId}/detail-regions/${region.id}`, { method: "DELETE" })
    ));
    detailRegions = [];
    drawDetailRegions();
  } catch (error) {
    notice(error.message, true);
  }
});
document.querySelector("#detail-run").addEventListener("click", async () => {
  const button = document.querySelector("#detail-run");
  button.disabled = true;
  try {
    await api(`/api/items/${itemId}/retry-detail`, {
      method: "POST",
      body: JSON.stringify({
        recognition_mode: document.querySelector("#retry-mode").value,
      }),
    });
    detailDialog.close();
    notice("Детальный проход запущен. Оригинал и выбранные фрагменты отправлены одним вызовом.");
    await load();
  } catch (error) {
    notice(error.message, true);
    button.disabled = false;
  }
});
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
