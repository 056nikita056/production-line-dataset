const manualItemId = document.body.dataset.itemId;
const manualDialog = document.querySelector("#annotation-editor");
const manualStage = document.querySelector("#annotation-stage");
const manualMedia = document.querySelector("#annotation-media");
const manualImage = document.querySelector("#annotation-image");
const manualOverlay = document.querySelector("#annotation-overlay");

const manualClasses = {
  tray_filled: { id: 0, label: "С курицей", css: "tray-filled" },
  qr_code: { id: 2, label: "QR-код", css: "qr-code" },
  tray_empty: { id: 3, label: "Пустой лоток", css: "tray-empty" },
};

const manualState = {
  revisionId: null,
  item: null,
  objects: [],
  activeClass: "tray_filled",
  tool: "select",
  selectedObject: null,
  selectedVertex: null,
  building: [],
  undo: [],
  redo: [],
  drag: null,
  zoom: 1,
  panX: 0,
  panY: 0,
  labels: true,
  fill: true,
  dirty: false,
  spacePressed: false,
  serverErrors: [],
};

async function manualApi(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = response.headers.get("content-type")?.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) {
    const detail = Array.isArray(body?.detail)
      ? body.detail.map((entry) => entry.msg).join("; ")
      : body?.detail;
    throw new Error(detail || `Ошибка HTTP ${response.status}`);
  }
  return body;
}

function manualClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function manualSvg(name, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

function manualPoint(event) {
  const matrix = manualOverlay.getScreenCTM();
  if (!matrix) return null;
  const point = manualOverlay.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const transformed = point.matrixTransform(matrix.inverse());
  return {
    x: Math.max(0, Math.min(1000, Math.round(transformed.x))),
    y: Math.max(0, Math.min(1000, Math.round(transformed.y))),
  };
}

function manualArea(points) {
  return Math.abs(points.reduce((sum, point, index) => {
    const next = points[(index + 1) % points.length];
    return sum + point.x * next.y - next.x * point.y;
  }, 0) / 2);
}

function manualOrientation(a, b, c) {
  const value = (b.y - a.y) * (c.x - b.x) - (b.x - a.x) * (c.y - b.y);
  if (value === 0) return 0;
  return value > 0 ? 1 : 2;
}

function manualOnSegment(a, b, c) {
  return b.x >= Math.min(a.x, c.x) && b.x <= Math.max(a.x, c.x)
    && b.y >= Math.min(a.y, c.y) && b.y <= Math.max(a.y, c.y);
}

function manualSegmentsIntersect(a, b, c, d) {
  const first = manualOrientation(a, b, c);
  const second = manualOrientation(a, b, d);
  const third = manualOrientation(c, d, a);
  const fourth = manualOrientation(c, d, b);
  if (first !== second && third !== fourth) return true;
  return (first === 0 && manualOnSegment(a, c, b))
    || (second === 0 && manualOnSegment(a, d, b))
    || (third === 0 && manualOnSegment(c, a, d))
    || (fourth === 0 && manualOnSegment(c, b, d));
}

function manualSelfIntersects(points) {
  for (let first = 0; first < points.length; first += 1) {
    const firstNext = (first + 1) % points.length;
    for (let second = first + 1; second < points.length; second += 1) {
      const secondNext = (second + 1) % points.length;
      if (firstNext === second || secondNext === first) continue;
      if (manualSegmentsIntersect(
        points[first], points[firstNext], points[second], points[secondNext]
      )) return true;
    }
  }
  return false;
}

function manualGeometryErrors() {
  const errors = [];
  const invalid = new Set();
  manualState.objects.forEach((object, index) => {
    if (object.polygon.length < 4 || object.polygon.length > 20) {
      errors.push(`Объект ${index + 1}: требуется от 4 до 20 вершин`);
      invalid.add(index);
    }
    if (manualArea(object.polygon) <= 1) {
      errors.push(`Объект ${index + 1}: нулевая площадь`);
      invalid.add(index);
    }
    const duplicate = object.polygon.some((point, pointIndex) => {
      const next = object.polygon[(pointIndex + 1) % object.polygon.length];
      return point.x === next.x && point.y === next.y;
    });
    if (duplicate) {
      errors.push(`Объект ${index + 1}: соседние вершины совпадают`);
      invalid.add(index);
    }
    if (manualSelfIntersects(object.polygon)) {
      errors.push(`Объект ${index + 1}: контур пересекает сам себя`);
      invalid.add(index);
    }
  });
  return { errors, invalid };
}

function manualPushHistory() {
  manualState.undo.push(manualClone(manualState.objects));
  if (manualState.undo.length > 100) manualState.undo.shift();
  manualState.redo = [];
}

function manualMarkDirty() {
  manualState.dirty = true;
  const state = document.querySelector("#editor-save-state");
  state.textContent = "Есть несохранённые изменения";
  state.className = "editor-save-state dirty";
}

function manualMarkSaved(message = "Черновик сохранён") {
  manualState.dirty = false;
  const state = document.querySelector("#editor-save-state");
  state.textContent = message;
  state.className = "editor-save-state saved";
}

function manualSetHint(message) {
  document.querySelector("#editor-hint").textContent = message;
}

function manualRenderToolbar() {
  document.querySelectorAll("[data-editor-class]").forEach((button) => {
    button.classList.toggle(
      "active",
      button.dataset.editorClass === manualState.activeClass,
    );
  });
  document.querySelectorAll("[data-editor-tool]").forEach((button) => {
    button.classList.toggle(
      "active",
      button.dataset.editorTool === manualState.tool,
    );
  });
  manualOverlay.classList.toggle("adding", ["four", "polygon"].includes(manualState.tool));
  manualOverlay.classList.toggle("panning", manualState.tool === "pan");
  manualOverlay.classList.toggle("dragging", Boolean(manualState.drag));
  manualOverlay.classList.toggle("annotation-overlay-no-fill", !manualState.fill);
  manualOverlay.classList.toggle("annotation-overlay-no-labels", !manualState.labels);
  document.querySelector("#editor-toggle-fill").classList.toggle("active", manualState.fill);
  document.querySelector("#editor-toggle-labels").classList.toggle("active", manualState.labels);
  document.querySelector("#editor-undo").disabled = !manualState.undo.length;
  document.querySelector("#editor-redo").disabled = !manualState.redo.length;
  const selected = manualState.selectedObject !== null;
  const selectedObject = selected ? manualState.objects[manualState.selectedObject] : null;
  document.querySelector("#editor-delete-object").disabled = !selected;
  document.querySelector("#editor-delete-vertex").disabled = !(
    selectedObject
    && manualState.selectedVertex !== null
    && selectedObject.polygon.length > 4
  );
}

function manualRenderSidebar(errors) {
  const counts = { tray_filled: 0, qr_code: 0, tray_empty: 0 };
  manualState.objects.forEach((object) => { counts[object.class_name] += 1; });
  document.querySelector("#editor-object-count").textContent = manualState.objects.length;
  const countNode = document.querySelector("#editor-counts");
  countNode.replaceChildren();
  Object.entries(counts).forEach(([name, count]) => {
    const badge = document.createElement("span");
    badge.textContent = `${manualClasses[name].label}: ${count}`;
    countNode.append(badge);
  });
  const list = document.querySelector("#editor-object-list");
  list.replaceChildren();
  manualState.objects.forEach((object, index) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `editor-object-row${manualState.selectedObject === index ? " active" : ""}`;
    const number = document.createElement("span");
    number.textContent = index + 1;
    const name = document.createElement("span");
    name.textContent = manualClasses[object.class_name].label;
    const points = document.createElement("small");
    points.textContent = `${object.polygon.length} вершин`;
    row.append(number, name, points);
    row.addEventListener("click", () => {
      manualState.selectedObject = index;
      manualState.selectedVertex = null;
      manualState.tool = "select";
      manualRender();
    });
    list.append(row);
  });
  const errorsNode = document.querySelector("#editor-errors");
  errorsNode.replaceChildren();
  const allErrors = [...errors, ...manualState.serverErrors];
  errorsNode.classList.toggle("has-errors", allErrors.length > 0);
  (allErrors.length ? allErrors : ["Ошибок нет"]).forEach((error) => {
    const item = document.createElement("li");
    item.textContent = error;
    errorsNode.append(item);
  });
}

function manualRender() {
  const { errors, invalid } = manualGeometryErrors();
  manualOverlay.replaceChildren();
  manualState.objects.forEach((object, index) => {
    const polygon = manualSvg("polygon", {
      points: object.polygon.map((point) => `${point.x},${point.y}`).join(" "),
      class: [
        "annotation-object",
        manualClasses[object.class_name].css,
        manualState.selectedObject === index ? "selected" : "",
        invalid.has(index) ? "invalid" : "",
      ].filter(Boolean).join(" "),
      "data-object-index": index,
    });
    manualOverlay.append(polygon);
    const first = object.polygon[0];
    const labelAtRight = first.x > 800;
    const label = manualSvg("text", {
      x: labelAtRight ? first.x - 8 : first.x + 8,
      y: Math.max(24, first.y - 10),
      class: "annotation-label",
      "text-anchor": labelAtRight ? "end" : "start",
    });
    label.textContent = `${index + 1}. ${manualClasses[object.class_name].label}`;
    manualOverlay.append(label);
    if (manualState.selectedObject === index) {
      object.polygon.forEach((point, pointIndex) => {
        manualOverlay.append(manualSvg("circle", {
          cx: point.x,
          cy: point.y,
          r: manualState.selectedVertex === pointIndex ? 8 : 6,
          class: `annotation-vertex${manualState.selectedVertex === pointIndex ? " active" : ""}`,
          "data-object-index": index,
          "data-vertex-index": pointIndex,
        }));
      });
    }
  });
  if (manualState.building.length) {
    manualOverlay.append(manualSvg("polyline", {
      points: manualState.building.map((point) => `${point.x},${point.y}`).join(" "),
      class: "building-line",
    }));
    manualState.building.forEach((point) => {
      manualOverlay.append(manualSvg("circle", {
        cx: point.x, cy: point.y, r: 6, class: "building-point",
      }));
    });
  }
  manualRenderToolbar();
  manualRenderSidebar(errors);
}

function manualSetTool(tool) {
  if (manualState.building.length && !window.confirm("Отменить незавершённый контур?")) return;
  manualState.building = [];
  manualState.tool = tool;
  manualState.selectedVertex = null;
  const hints = {
    select: "Перетащите вершину или контур. Колесо увеличивает область под курсором.",
    four: "Поставьте четыре точки по внешнему контуру объекта.",
    polygon: "Ставьте 4–20 точек. Двойной клик или Enter завершает контур.",
    pan: "Перетаскивайте фотографию; колесо увеличивает область под курсором до 800%.",
  };
  manualSetHint(hints[tool]);
  manualRender();
}

function manualFinishBuilding() {
  const unique = manualState.building.filter((point, index, points) => (
    index === 0 || point.x !== points[index - 1].x || point.y !== points[index - 1].y
  ));
  manualState.building = [];
  if (unique.length < 4) {
    manualSetHint("Для контура нужно минимум четыре разные точки.");
    manualRender();
    return;
  }
  if (unique.length > 20) unique.length = 20;
  manualPushHistory();
  manualState.objects.push({
    class_id: manualClasses[manualState.activeClass].id,
    class_name: manualState.activeClass,
    polygon: unique,
    occluded: false,
    visible_fraction: 1,
  });
  manualState.selectedObject = manualState.objects.length - 1;
  manualState.selectedVertex = null;
  manualState.serverErrors = [];
  manualMarkDirty();
  manualSetHint("Контур добавлен. Можно продолжить или выбрать инструмент редактирования.");
  manualRender();
}

function manualDeleteSelection() {
  const object = manualState.objects[manualState.selectedObject];
  if (!object) return;
  if (manualState.selectedVertex !== null && object.polygon.length > 4) {
    manualPushHistory();
    object.polygon.splice(manualState.selectedVertex, 1);
    manualState.selectedVertex = null;
  } else {
    manualPushHistory();
    manualState.objects.splice(manualState.selectedObject, 1);
    manualState.selectedObject = null;
    manualState.selectedVertex = null;
  }
  manualState.serverErrors = [];
  manualMarkDirty();
  manualRender();
}

function manualUndo() {
  if (!manualState.undo.length) return;
  manualState.redo.push(manualClone(manualState.objects));
  manualState.objects = manualState.undo.pop();
  manualState.selectedObject = null;
  manualState.selectedVertex = null;
  manualMarkDirty();
  manualRender();
}

function manualRedo() {
  if (!manualState.redo.length) return;
  manualState.undo.push(manualClone(manualState.objects));
  manualState.objects = manualState.redo.pop();
  manualState.selectedObject = null;
  manualState.selectedVertex = null;
  manualMarkDirty();
  manualRender();
}

function manualApplyTransform() {
  manualMedia.style.transform = `translate(${manualState.panX}px, ${manualState.panY}px) scale(${manualState.zoom})`;
  document.querySelector("#editor-zoom-level").textContent = `${Math.round(manualState.zoom * 100)}%`;
  document.querySelector("#editor-zoom-out").disabled = manualState.zoom <= 0.5;
  document.querySelector("#editor-zoom-in").disabled = manualState.zoom >= 8;
}

function manualFit() {
  if (!manualState.item) return;
  const availableWidth = Math.max(100, manualStage.clientWidth - 36);
  const availableHeight = Math.max(100, manualStage.clientHeight - 36);
  const aspect = manualState.item.width / manualState.item.height;
  let width = availableWidth;
  let height = width / aspect;
  if (height > availableHeight) {
    height = availableHeight;
    width = height * aspect;
  }
  manualMedia.style.width = `${width}px`;
  manualMedia.style.height = `${height}px`;
  manualState.zoom = 1;
  manualState.panX = 0;
  manualState.panY = 0;
  manualApplyTransform();
}

function manualZoom(multiplier, clientX = null, clientY = null) {
  const previousZoom = manualState.zoom;
  const nextZoom = Math.max(0.5, Math.min(8, previousZoom * multiplier));
  if (nextZoom === previousZoom) return;
  const stageRect = manualStage.getBoundingClientRect();
  const anchorX = (clientX ?? (stageRect.left + stageRect.width / 2))
    - stageRect.left - stageRect.width / 2;
  const anchorY = (clientY ?? (stageRect.top + stageRect.height / 2))
    - stageRect.top - stageRect.height / 2;
  const imageX = (anchorX - manualState.panX) / previousZoom;
  const imageY = (anchorY - manualState.panY) / previousZoom;
  manualState.panX = anchorX - imageX * nextZoom;
  manualState.panY = anchorY - imageY * nextZoom;
  manualState.zoom = nextZoom;
  manualApplyTransform();
}

function manualPayload() {
  return {
    image_width: manualState.item.width,
    image_height: manualState.item.height,
    objects: manualClone(manualState.objects),
    needs_review: false,
    review_reasons: [],
  };
}

async function manualEnsureDraft() {
  if (manualState.revisionId) return manualState.revisionId;
  const draft = await manualApi(`/api/items/${manualItemId}/revisions/manual`, {
    method: "POST",
    body: JSON.stringify({ start_empty: false }),
  });
  manualState.revisionId = draft.id;
  return draft.id;
}

async function manualSaveDraft() {
  const button = document.querySelector("#editor-save-draft");
  button.disabled = true;
  try {
    await manualEnsureDraft();
    await manualApi(
      `/api/items/${manualItemId}/revisions/${manualState.revisionId}/draft`,
      { method: "PUT", body: JSON.stringify(manualPayload()) },
    );
    manualState.serverErrors = [];
    manualMarkSaved();
    manualRender();
  } catch (error) {
    manualSetHint(`Черновик не сохранён: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function manualValidateAndSave() {
  const local = manualGeometryErrors();
  if (local.errors.length) {
    manualSetHint("Исправьте геометрию перед сохранением.");
    manualRender();
    return;
  }
  const button = document.querySelector("#editor-validate-save");
  button.disabled = true;
  try {
    await manualEnsureDraft();
    const result = await manualApi(
      `/api/items/${manualItemId}/revisions/${manualState.revisionId}/validate`,
      { method: "POST", body: JSON.stringify(manualPayload()) },
    );
    manualState.serverErrors = result.validation.errors || [];
    manualMarkSaved(result.validation.valid ? "Ручная ревизия сохранена" : "Черновик сохранён с ошибками");
    manualRender();
    if (result.validation.valid) {
      manualDialog.close();
      window.dispatchEvent(new CustomEvent("manual-annotation-saved"));
    } else {
      manualSetHint("Сервер нашёл ошибки. Они показаны справа; исправьте контуры и повторите.");
    }
  } catch (error) {
    manualSetHint(`Результат не сохранён: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function manualOpenEditor() {
  const trigger = document.querySelector("#manual-editor-button");
  trigger.disabled = true;
  try {
    const [item, revisions] = await Promise.all([
      manualApi(`/api/items/${manualItemId}`),
      manualApi(`/api/items/${manualItemId}/revisions`),
    ]);
    const draft = revisions.find((revision) => revision.is_draft) || null;
    manualState.item = item;
    manualState.revisionId = draft?.id || null;
    manualState.objects = manualClone(
      draft?.annotation?.objects || item.annotation?.objects || []
    ).filter(
      (object) => object.class_name !== "line"
    );
    manualState.activeClass = "tray_filled";
    manualState.tool = "select";
    manualState.selectedObject = null;
    manualState.selectedVertex = null;
    manualState.building = [];
    manualState.undo = [];
    manualState.redo = [];
    manualState.serverErrors = draft?.validation_errors || [];
    manualState.dirty = false;
    document.querySelector("#editor-file-name").textContent = item.original_name;
    manualImage.src = item.source_url;
    manualDialog.showModal();
    manualMarkSaved(draft?.updated_at ? "Черновик загружен" : "Редактор готов");
    manualFit();
    manualRender();
  } catch (error) {
    if (typeof notice === "function") notice(`Редактор не открыт: ${error.message}`, true);
  } finally {
    trigger.disabled = false;
  }
}

function manualCloseEditor() {
  if (manualState.dirty && !window.confirm("Закрыть редактор без сохранения последних изменений?")) return;
  manualDialog.close();
}

function manualNearestEdge(points, point) {
  let best = { index: 0, distance: Infinity };
  points.forEach((start, index) => {
    const end = points[(index + 1) % points.length];
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const lengthSquared = dx * dx + dy * dy || 1;
    const t = Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared));
    const projected = { x: start.x + t * dx, y: start.y + t * dy };
    const distance = Math.hypot(point.x - projected.x, point.y - projected.y);
    if (distance < best.distance) best = { index, distance };
  });
  return best;
}

manualOverlay.addEventListener("pointerdown", (event) => {
  const point = manualPoint(event);
  if (!point) return;
  if (["four", "polygon"].includes(manualState.tool)) {
    manualState.building.push(point);
    if (manualState.tool === "four" && manualState.building.length === 4) {
      manualFinishBuilding();
    } else if (manualState.building.length >= 20) {
      manualFinishBuilding();
    } else {
      manualRender();
    }
    return;
  }
  const shouldPan = manualState.tool === "pan" || manualState.spacePressed || event.button === 1;
  if (shouldPan) {
    event.preventDefault();
    manualState.drag = {
      kind: "pan",
      clientX: event.clientX,
      clientY: event.clientY,
      panX: manualState.panX,
      panY: manualState.panY,
    };
    manualOverlay.setPointerCapture(event.pointerId);
    manualRenderToolbar();
    return;
  }
  const vertex = event.target.closest?.(".annotation-vertex");
  const polygon = event.target.closest?.(".annotation-object");
  if (vertex) {
    const objectIndex = Number(vertex.dataset.objectIndex);
    const vertexIndex = Number(vertex.dataset.vertexIndex);
    manualState.selectedObject = objectIndex;
    manualState.selectedVertex = vertexIndex;
    manualPushHistory();
    manualState.drag = { kind: "vertex", objectIndex, vertexIndex, moved: false };
    manualOverlay.setPointerCapture(event.pointerId);
    manualRender();
    return;
  }
  if (polygon) {
    const objectIndex = Number(polygon.dataset.objectIndex);
    manualState.selectedObject = objectIndex;
    manualState.selectedVertex = null;
    manualPushHistory();
    manualState.drag = {
      kind: "object",
      objectIndex,
      start: point,
      polygon: manualClone(manualState.objects[objectIndex].polygon),
      moved: false,
    };
    manualOverlay.setPointerCapture(event.pointerId);
    manualRender();
    return;
  }
  manualState.selectedObject = null;
  manualState.selectedVertex = null;
  manualRender();
});

manualOverlay.addEventListener("pointermove", (event) => {
  const drag = manualState.drag;
  if (!drag) return;
  if (drag.kind === "pan") {
    manualState.panX = drag.panX + event.clientX - drag.clientX;
    manualState.panY = drag.panY + event.clientY - drag.clientY;
    manualApplyTransform();
    return;
  }
  const point = manualPoint(event);
  if (!point) return;
  drag.moved = true;
  if (drag.kind === "vertex") {
    manualState.objects[drag.objectIndex].polygon[drag.vertexIndex] = point;
  } else if (drag.kind === "object") {
    const xs = drag.polygon.map((entry) => entry.x);
    const ys = drag.polygon.map((entry) => entry.y);
    const rawX = point.x - drag.start.x;
    const rawY = point.y - drag.start.y;
    const dx = Math.max(-Math.min(...xs), Math.min(1000 - Math.max(...xs), rawX));
    const dy = Math.max(-Math.min(...ys), Math.min(1000 - Math.max(...ys), rawY));
    manualState.objects[drag.objectIndex].polygon = drag.polygon.map((entry) => ({
      x: Math.round(entry.x + dx), y: Math.round(entry.y + dy),
    }));
  }
  manualMarkDirty();
  manualRender();
});

manualOverlay.addEventListener("pointerup", () => {
  if (manualState.drag && manualState.drag.kind !== "pan") {
    if (manualState.drag.moved) manualMarkDirty();
    else manualState.undo.pop();
  }
  manualState.drag = null;
  manualRenderToolbar();
});

manualOverlay.addEventListener("dblclick", (event) => {
  event.preventDefault();
  if (manualState.tool === "polygon") {
    manualFinishBuilding();
    return;
  }
  if (manualState.tool !== "select" || manualState.selectedObject === null) return;
  const point = manualPoint(event);
  if (!point) return;
  const object = manualState.objects[manualState.selectedObject];
  if (object.polygon.length >= 20) {
    manualSetHint("У объекта уже максимальные 20 вершин.");
    return;
  }
  const edge = manualNearestEdge(object.polygon, point);
  manualPushHistory();
  object.polygon.splice(edge.index + 1, 0, point);
  manualState.selectedVertex = edge.index + 1;
  manualMarkDirty();
  manualRender();
});

document.querySelectorAll("[data-editor-tool]").forEach((button) => {
  button.addEventListener("click", () => manualSetTool(button.dataset.editorTool));
});

document.querySelectorAll("[data-editor-class]").forEach((button) => {
  button.addEventListener("click", () => {
    const className = button.dataset.editorClass;
    manualState.activeClass = className;
    if (manualState.tool === "select" && manualState.selectedObject !== null) {
      manualPushHistory();
      const object = manualState.objects[manualState.selectedObject];
      object.class_name = className;
      object.class_id = manualClasses[className].id;
      manualMarkDirty();
    }
    manualRender();
  });
});

document.querySelector("#editor-delete-object").addEventListener("click", () => {
  manualState.selectedVertex = null;
  manualDeleteSelection();
});
document.querySelector("#editor-delete-vertex").addEventListener("click", manualDeleteSelection);
document.querySelector("#editor-undo").addEventListener("click", manualUndo);
document.querySelector("#editor-redo").addEventListener("click", manualRedo);
document.querySelector("#editor-zoom-in").addEventListener("click", () => manualZoom(1.25));
document.querySelector("#editor-zoom-out").addEventListener("click", () => manualZoom(0.8));
document.querySelector("#editor-fit").addEventListener("click", manualFit);
manualStage.addEventListener("wheel", (event) => {
  if (!manualDialog.open) return;
  event.preventDefault();
  manualZoom(event.deltaY < 0 ? 1.16 : 1 / 1.16, event.clientX, event.clientY);
}, { passive: false });
document.querySelector("#editor-toggle-labels").addEventListener("click", () => {
  manualState.labels = !manualState.labels;
  manualRender();
});
document.querySelector("#editor-toggle-fill").addEventListener("click", () => {
  manualState.fill = !manualState.fill;
  manualRender();
});
document.querySelector("#editor-clear").addEventListener("click", () => {
  if (!manualState.objects.length || !window.confirm("Удалить все объекты текущего слоя?")) return;
  manualPushHistory();
  manualState.objects = [];
  manualState.selectedObject = null;
  manualState.selectedVertex = null;
  manualMarkDirty();
  manualRender();
});
document.querySelector("#editor-start-empty").addEventListener("click", () => {
  if (manualState.objects.length && !window.confirm("Начать с пустого слоя? Текущие объекты будут удалены.")) return;
  manualPushHistory();
  manualState.objects = [];
  manualState.selectedObject = null;
  manualState.selectedVertex = null;
  manualMarkDirty();
  manualRender();
});
document.querySelector("#editor-save-draft").addEventListener("click", manualSaveDraft);
document.querySelector("#editor-validate-save").addEventListener("click", manualValidateAndSave);
document.querySelector("#manual-editor-button").addEventListener("click", manualOpenEditor);
document.querySelector("#editor-close").addEventListener("click", manualCloseEditor);
manualDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  manualCloseEditor();
});
manualImage.addEventListener("load", manualFit);
window.addEventListener("resize", () => {
  if (manualDialog.open && manualState.zoom === 1 && manualState.panX === 0 && manualState.panY === 0) manualFit();
});
window.addEventListener("beforeunload", (event) => {
  if (!manualState.dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

document.addEventListener("keydown", (event) => {
  if (!manualDialog.open) return;
  const modifier = event.ctrlKey || event.metaKey;
  if (event.code === "Space") manualState.spacePressed = true;
  if (modifier && event.key.toLowerCase() === "z") {
    event.preventDefault();
    if (event.shiftKey) manualRedo(); else manualUndo();
    return;
  }
  if (modifier && event.key.toLowerCase() === "s") {
    event.preventDefault();
    manualSaveDraft();
    return;
  }
  if (!modifier && ["1", "2", "3"].includes(event.key)) {
    event.preventDefault();
    const names = { "1": "tray_filled", "2": "qr_code", "3": "tray_empty" };
    document.querySelector(`[data-editor-class="${names[event.key]}"]`).click();
  } else if (event.key === "Enter" && manualState.building.length) {
    event.preventDefault();
    manualFinishBuilding();
  } else if (event.key === "Escape" && manualState.building.length) {
    event.preventDefault();
    manualState.building = [];
    manualRender();
  } else if (["Delete", "Backspace"].includes(event.key)) {
    event.preventDefault();
    manualDeleteSelection();
  } else if (event.key === "+" || event.key === "=") {
    event.preventDefault();
    manualZoom(1.25);
  } else if (event.key === "-") {
    event.preventDefault();
    manualZoom(0.8);
  } else if (event.key === "0") {
    event.preventDefault();
    manualFit();
  } else if (!modifier && event.key.toLowerCase() === "s") {
    event.preventDefault();
    manualSaveDraft();
  }
});

document.addEventListener("keyup", (event) => {
  if (event.code === "Space") manualState.spacePressed = false;
});
