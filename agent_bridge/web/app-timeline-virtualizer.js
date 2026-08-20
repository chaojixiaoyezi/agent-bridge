"use strict";

const TIMELINE_VIRTUAL_THRESHOLD = 120;
const TIMELINE_VIRTUAL_WINDOW_SIZE = 96;
const TIMELINE_VIRTUAL_EDGE_BUFFER = 20;
const TIMELINE_VIRTUAL_MIN_HEIGHT = 48;
const TIMELINE_VIRTUAL_MAX_AVERAGE_HEIGHT = 520;

function timelineVirtualDefaultHeight() {
  if (state.workspaceDensity === "simple") return 92;
  if (state.workspaceDensity === "standard") return 126;
  return 178;
}

function createTimelineVirtualState(roomId) {
  return {
    roomId,
    enabled: false,
    start: 0,
    end: 0,
    averageHeight: timelineVirtualDefaultHeight(),
    heights: new Map(),
    measuredTotal: 0,
    measuredCount: 0,
    heightRevision: 0,
    measurementContext: "",
    offsetSource: null,
    offsetRevision: -1,
    offsets: null,
    observer: null,
    observerFrame: null,
    layoutTimer: null,
  };
}

function disconnectTimelineVirtualObserver(virtual = state.timelineVirtual) {
  if (!virtual) return;
  virtual.observer?.disconnect();
  virtual.observer = null;
  if (virtual.observerFrame) {
    window.cancelAnimationFrame(virtual.observerFrame);
    virtual.observerFrame = null;
  }
  if (virtual.layoutTimer) {
    window.clearTimeout(virtual.layoutTimer);
    virtual.layoutTimer = null;
  }
}

function resetTimelineVirtualState(roomId = null) {
  disconnectTimelineVirtualObserver();
  state.timelineVirtual = roomId ? createTimelineVirtualState(roomId) : null;
  if (elements?.timeline) {
    delete elements.timeline.dataset.virtualized;
    delete elements.timeline.dataset.virtualStart;
    delete elements.timeline.dataset.virtualEnd;
    delete elements.timeline.dataset.virtualTotal;
  }
}

function timelineMeasurementContext() {
  const width = Math.max(0, Math.round(elements.timeline.clientWidth || 0));
  return `${state.workspaceDensity || "detailed"}:${width}`;
}

function ensureTimelineVirtualState(messages) {
  const roomId = state.selectedRoom || state.loadedRoom || "";
  let virtual = state.timelineVirtual;
  if (!virtual || virtual.roomId !== roomId) {
    resetTimelineVirtualState(roomId);
    virtual = state.timelineVirtual;
  }
  const context = timelineMeasurementContext();
  if (
    virtual.measurementContext
    && context !== virtual.measurementContext
    && !context.endsWith(":0")
  ) {
    virtual.heights.clear();
    virtual.measuredTotal = 0;
    virtual.measuredCount = 0;
    virtual.averageHeight = timelineVirtualDefaultHeight();
    virtual.heightRevision += 1;
    virtual.offsetSource = null;
    virtual.offsets = null;
    state.messageRenderSignature = "";
  }
  if (!context.endsWith(":0")) virtual.measurementContext = context;
  virtual.enabled = messages.length > TIMELINE_VIRTUAL_THRESHOLD;
  if (!virtual.enabled) {
    virtual.start = 0;
    virtual.end = messages.length;
  }
  return virtual;
}

function prepareTimelineMessageIndexes(messages) {
  if (state.timelineMessageIndexSource === messages) return;
  const byId = new Map();
  const positions = new Map();
  const replyCounts = new Map();
  messages.forEach((message, index) => {
    byId.set(message.message_id, message);
    positions.set(message.message_id, index);
    if (message.reply_to) {
      replyCounts.set(
        message.reply_to,
        Number(replyCounts.get(message.reply_to) || 0) + 1,
      );
    }
  });
  state.timelineMessageIndexSource = messages;
  state.timelineMessageById = byId;
  state.timelineMessagePositionById = positions;
  state.timelineReplyCounts = replyCounts;
}

function timelineMessageAt(messageId) {
  return state.timelineMessageById.get(messageId) || null;
}

function timelineMessagePosition(messageId) {
  const position = state.timelineMessagePositionById.get(messageId);
  return Number.isInteger(position) ? position : -1;
}

function timelineLoadedReplyCount(messageId) {
  return Number(state.timelineReplyCounts.get(messageId) || 0);
}

function timelineVirtualOffsets(messages, virtual = state.timelineVirtual) {
  if (
    virtual.offsets
    && virtual.offsetSource === messages
    && virtual.offsetRevision === virtual.heightRevision
  ) {
    return virtual.offsets;
  }
  const offsets = new Float64Array(messages.length + 1);
  for (let index = 0; index < messages.length; index += 1) {
    const measured = Number(virtual.heights.get(messages[index].message_id));
    const height = Number.isFinite(measured) && measured > 0
      ? measured
      : virtual.averageHeight;
    offsets[index + 1] = offsets[index] + height;
  }
  virtual.offsetSource = messages;
  virtual.offsetRevision = virtual.heightRevision;
  virtual.offsets = offsets;
  return offsets;
}

function resolveTimelineVirtualRange(
  messages,
  {
    forceBottom = false,
    wasNearBottom = false,
    targetMessageId = null,
    anchor = null,
    virtualIndex = null,
  } = {},
) {
  prepareTimelineMessageIndexes(messages);
  const virtual = ensureTimelineVirtualState(messages);
  if (!virtual.enabled) return { start: 0, end: messages.length, virtualized: false };

  let focusIndex = targetMessageId ? timelineMessagePosition(targetMessageId) : -1;
  if (focusIndex < 0 && Number.isInteger(virtualIndex)) {
    focusIndex = Math.max(0, Math.min(messages.length - 1, virtualIndex));
  }
  if (focusIndex < 0 && anchor?.messageId) {
    focusIndex = timelineMessagePosition(anchor.messageId);
  }
  if (forceBottom || (focusIndex < 0 && wasNearBottom)) {
    focusIndex = messages.length - 1;
  }
  if (focusIndex < 0 && virtual.end > virtual.start) {
    focusIndex = Math.floor((virtual.start + virtual.end - 1) / 2);
  }
  if (focusIndex < 0) focusIndex = messages.length - 1;

  let start = forceBottom || (wasNearBottom && !targetMessageId && virtualIndex === null)
    ? messages.length - TIMELINE_VIRTUAL_WINDOW_SIZE
    : focusIndex - Math.floor(TIMELINE_VIRTUAL_WINDOW_SIZE / 2);
  start = Math.max(
    0,
    Math.min(messages.length - TIMELINE_VIRTUAL_WINDOW_SIZE, start),
  );
  const end = Math.min(messages.length, start + TIMELINE_VIRTUAL_WINDOW_SIZE);
  virtual.start = start;
  virtual.end = end;
  return { start, end, virtualized: true };
}

function renderedTimelineRange(messages) {
  const virtual = state.timelineVirtual;
  if (
    virtual?.enabled
    && virtual.roomId === (state.selectedRoom || state.loadedRoom || "")
  ) {
    return {
      start: Math.max(0, Math.min(messages.length, virtual.start)),
      end: Math.max(0, Math.min(messages.length, virtual.end)),
      virtualized: true,
    };
  }
  return { start: 0, end: messages.length, virtualized: false };
}

function createTimelineVirtualSpacer(kind) {
  const spacer = makeElement("div", `timeline-virtual-spacer ${kind}`);
  spacer.dataset.virtualSpacer = kind;
  spacer.setAttribute("aria-hidden", "true");
  return spacer;
}

function createTimelineVirtualRow(message, index, messages) {
  const row = makeElement("div", "timeline-virtual-row");
  row.dataset.virtualIndex = String(index);
  row.dataset.virtualMessageId = message.message_id;
  if (index === messages.length - 1) row.classList.add("is-final-message");
  const previousDay = index > 0 ? dayLabel(messages[index - 1].created_at) : "";
  const currentDay = dayLabel(message.created_at);
  if (currentDay !== previousDay) {
    row.append(makeElement("div", "day-divider", currentDay));
  }
  row.append(createMessageElement(message));
  return row;
}

function applyTimelineVirtualSpacerHeights(messages) {
  const virtual = state.timelineVirtual;
  if (!virtual?.enabled) return;
  const offsets = timelineVirtualOffsets(messages, virtual);
  const topSpacer = elements.timeline.querySelector('[data-virtual-spacer="top"]');
  const bottomSpacer = elements.timeline.querySelector('[data-virtual-spacer="bottom"]');
  if (topSpacer) topSpacer.style.height = `${Math.max(0, offsets[virtual.start])}px`;
  if (bottomSpacer) {
    bottomSpacer.style.height = `${Math.max(
      0,
      offsets[messages.length] - offsets[virtual.end],
    )}px`;
  }
  elements.timeline.dataset.virtualized = "true";
  elements.timeline.dataset.virtualStart = String(virtual.start);
  elements.timeline.dataset.virtualEnd = String(virtual.end);
  elements.timeline.dataset.virtualTotal = String(messages.length);
}

function recordTimelineVirtualMeasurements(messages) {
  const virtual = state.timelineVirtual;
  if (!virtual?.enabled) return false;
  let changed = false;
  for (const row of elements.timeline.querySelectorAll(".timeline-virtual-row")) {
    const messageId = row.dataset.virtualMessageId;
    const height = row.getBoundingClientRect().height;
    if (!messageId || !Number.isFinite(height) || height < TIMELINE_VIRTUAL_MIN_HEIGHT) {
      continue;
    }
    const previous = virtual.heights.get(messageId);
    if (Number.isFinite(previous) && Math.abs(previous - height) < 0.5) continue;
    if (Number.isFinite(previous)) {
      virtual.measuredTotal += height - previous;
    } else {
      virtual.measuredTotal += height;
      virtual.measuredCount += 1;
    }
    virtual.heights.set(messageId, height);
    changed = true;
  }
  if (!changed) return false;
  if (virtual.measuredCount > 0) {
    virtual.averageHeight = Math.max(
      TIMELINE_VIRTUAL_MIN_HEIGHT,
      Math.min(
        TIMELINE_VIRTUAL_MAX_AVERAGE_HEIGHT,
        virtual.measuredTotal / virtual.measuredCount,
      ),
    );
  }
  virtual.heightRevision += 1;
  virtual.offsets = null;
  applyTimelineVirtualSpacerHeights(messages);
  return true;
}

function restoreCapturedTimelineAnchor(anchor) {
  if (!anchor?.messageId) return false;
  const anchored = [...elements.timeline.querySelectorAll("article[data-message-id]")]
    .find((article) => article.dataset.messageId === anchor.messageId);
  if (!anchored) return false;
  const timelineTop = elements.timeline.getBoundingClientRect().top;
  elements.timeline.scrollTop += (
    anchored.getBoundingClientRect().top
    - timelineTop
    - anchor.offset
  );
  return true;
}

function observeTimelineVirtualRows(messages) {
  const virtual = state.timelineVirtual;
  if (!virtual?.enabled || typeof window.ResizeObserver !== "function") return;
  disconnectTimelineVirtualObserver(virtual);
  const roomId = virtual.roomId;
  virtual.observer = new ResizeObserver(() => {
    if (virtual.observerFrame) return;
    virtual.observerFrame = window.requestAnimationFrame(() => {
      virtual.observerFrame = null;
      if (state.timelineVirtual !== virtual || virtual.roomId !== roomId) return;
      const wasNearBottom = isNearTimelineBottom();
      const anchor = wasNearBottom ? null : captureTimelineAnchor();
      if (!recordTimelineVirtualMeasurements(messages)) return;
      if (wasNearBottom) {
        elements.timeline.scrollTop = elements.timeline.scrollHeight;
      } else {
        restoreCapturedTimelineAnchor(anchor);
      }
      updateNewMessageIndicator();
    });
  });
  for (const row of elements.timeline.querySelectorAll(".timeline-virtual-row")) {
    virtual.observer.observe(row);
  }
}

function syncTimelineVirtualDom(messages) {
  const virtual = state.timelineVirtual;
  if (!virtual?.enabled) {
    disconnectTimelineVirtualObserver(virtual);
    delete elements.timeline.dataset.virtualized;
    delete elements.timeline.dataset.virtualStart;
    delete elements.timeline.dataset.virtualEnd;
    delete elements.timeline.dataset.virtualTotal;
    return;
  }
  applyTimelineVirtualSpacerHeights(messages);
  recordTimelineVirtualMeasurements(messages);
  observeTimelineVirtualRows(messages);
}

function scheduleTimelineVirtualLayoutSync() {
  const virtual = state.timelineVirtual;
  if (!virtual?.enabled) return;
  if (virtual.layoutTimer) window.clearTimeout(virtual.layoutTimer);
  virtual.layoutTimer = window.setTimeout(() => {
    virtual.layoutTimer = null;
    if (state.timelineVirtual !== virtual || virtual.roomId !== state.selectedRoom) return;
    const context = timelineMeasurementContext();
    if (!context || context.endsWith(":0") || context === virtual.measurementContext) return;
    const wasNearBottom = isNearTimelineBottom();
    const anchor = wasNearBottom ? null : captureTimelineAnchor();
    virtual.heights.clear();
    virtual.measuredTotal = 0;
    virtual.measuredCount = 0;
    virtual.averageHeight = timelineVirtualDefaultHeight();
    virtual.heightRevision += 1;
    virtual.measurementContext = context;
    virtual.offsetSource = null;
    virtual.offsets = null;
    applyTimelineVirtualSpacerHeights(state.messages);
    recordTimelineVirtualMeasurements(state.messages);
    if (wasNearBottom) {
      elements.timeline.scrollTop = elements.timeline.scrollHeight;
    } else {
      restoreCapturedTimelineAnchor(anchor);
    }
    updateNewMessageIndicator();
  }, 90);
}

function timelineVirtualIndexAtScroll(messages) {
  const virtual = state.timelineVirtual;
  if (!virtual?.enabled || !messages.length) return -1;
  const offsets = timelineVirtualOffsets(messages, virtual);
  const topSpacer = elements.timeline.querySelector('[data-virtual-spacer="top"]');
  const logicalOffset = Math.max(
    0,
    elements.timeline.scrollTop - Number(topSpacer?.offsetTop || 0),
  );
  let low = 0;
  let high = messages.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (offsets[middle + 1] <= logicalOffset) low = middle + 1;
    else high = middle;
  }
  return Math.max(0, Math.min(messages.length - 1, low));
}

function updateTimelineVirtualWindow() {
  const messages = state.messages;
  const virtual = state.timelineVirtual;
  if (!virtual?.enabled || virtual.roomId !== state.selectedRoom) return;
  const index = timelineVirtualIndexAtScroll(messages);
  if (index < 0) return;
  const needsEarlierWindow = virtual.start > 0
    && index < virtual.start + TIMELINE_VIRTUAL_EDGE_BUFFER;
  const needsLaterWindow = virtual.end < messages.length
    && index >= virtual.end - TIMELINE_VIRTUAL_EDGE_BUFFER;
  if (!needsEarlierWindow && !needsLaterWindow) return;
  renderMessages(messages, { virtualIndex: index, forceVirtual: true });
}

function captureTimelineVirtualSnapshot() {
  const virtual = state.timelineVirtual;
  if (!virtual || virtual.roomId !== state.loadedRoom) return null;
  return {
    enabled: virtual.enabled,
    start: virtual.start,
    end: virtual.end,
    averageHeight: virtual.averageHeight,
    heights: [...virtual.heights.entries()],
    measuredTotal: virtual.measuredTotal,
    measuredCount: virtual.measuredCount,
    heightRevision: virtual.heightRevision,
    measurementContext: virtual.measurementContext,
  };
}

function restoreTimelineVirtualSnapshot(snapshot, roomId) {
  disconnectTimelineVirtualObserver();
  const virtual = createTimelineVirtualState(roomId);
  if (snapshot) {
    virtual.enabled = Boolean(snapshot.enabled);
    virtual.start = Math.max(0, Number(snapshot.start || 0));
    virtual.end = Math.max(virtual.start, Number(snapshot.end || 0));
    virtual.averageHeight = Math.max(
      TIMELINE_VIRTUAL_MIN_HEIGHT,
      Number(snapshot.averageHeight || timelineVirtualDefaultHeight()),
    );
    virtual.heights = new Map(snapshot.heights || []);
    virtual.measuredTotal = Number(snapshot.measuredTotal || 0);
    virtual.measuredCount = Number(snapshot.measuredCount || 0);
    virtual.heightRevision = Number(snapshot.heightRevision || 0);
    virtual.measurementContext = String(snapshot.measurementContext || "");
  }
  state.timelineVirtual = virtual;
}
