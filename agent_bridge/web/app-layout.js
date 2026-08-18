"use strict";

const WORKSPACE_LAYOUT_DEFAULTS = Object.freeze({
  roomsWidth: 236,
  peopleWidth: 260,
  composerHeight: 58,
  density: "detailed",
});
const WORKSPACE_LAYOUT_LIMITS = Object.freeze({
  roomsMin: 176,
  peopleMin: 200,
  panelMax: 420,
  composerMin: 58,
  composerMax: 300,
  timelineMin: 420,
});
const WORKSPACE_LAYOUT_STORAGE = Object.freeze({
  roomsCollapsed: "agentBridgeRoomsPanel",
  peopleCollapsed: "agentBridgePeoplePanel",
  roomsWidth: "agentBridgeRoomsWidth",
  peopleWidth: "agentBridgePeopleWidth",
  composerHeight: "agentBridgeComposerHeight",
  composerCollapsed: "agentBridgeComposerPanel",
  density: "agentBridgeWorkspaceDensity",
  focusMode: "agentBridgeWorkspaceFocus",
});

let activeWorkspaceResize = null;
let workspaceResizeFrame = null;
let workspaceScrollFrame = null;
let pendingWorkspaceScrollState = null;

function clampLayoutValue(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, Number(value) || minimum));
}

function readLayoutValue(key, fallback) {
  try {
    const stored = window.localStorage.getItem(key);
    return stored === null ? fallback : stored;
  } catch (error) {
    return fallback;
  }
}

function readLayoutPreference(key, fallback) {
  const stored = readLayoutValue(key, null);
  if (stored === null) return fallback;
  return stored === "collapsed" || stored === "enabled";
}

function readLayoutNumber(key, fallback, minimum, maximum) {
  const stored = Number(readLayoutValue(key, fallback));
  return Number.isFinite(stored)
    ? clampLayoutValue(stored, minimum, maximum)
    : fallback;
}

function composerHeightMaximum() {
  const panelHeight = document.querySelector(".timeline-panel")?.clientHeight || 720;
  return Math.max(
    120,
    Math.min(WORKSPACE_LAYOUT_LIMITS.composerMax, Math.floor(panelHeight * 0.42)),
  );
}

function panelWidthMaximum(kind) {
  const minimum = kind === "rooms"
    ? WORKSPACE_LAYOUT_LIMITS.roomsMin
    : WORKSPACE_LAYOUT_LIMITS.peopleMin;
  const workspaceWidth = elements.workspace.clientWidth || window.innerWidth;
  const overlayPeople = window.matchMedia("(max-width: 1100px)").matches;
  const opposite = kind === "rooms"
    ? (overlayPeople || state.peoplePanelCollapsed ? 0 : state.peoplePanelWidth)
    : (state.roomsPanelCollapsed ? 0 : state.roomsPanelWidth);
  const gaps = kind === "rooms" && overlayPeople ? 12 : 24;
  return Math.max(
    minimum,
    Math.min(
      WORKSPACE_LAYOUT_LIMITS.panelMax,
      workspaceWidth - opposite - gaps - WORKSPACE_LAYOUT_LIMITS.timelineMin,
    ),
  );
}

function persistWorkspaceLayout() {
  try {
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE.roomsCollapsed,
      state.roomsPanelCollapsed ? "collapsed" : "expanded",
    );
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE.peopleCollapsed,
      state.peoplePanelCollapsed ? "collapsed" : "expanded",
    );
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE.roomsWidth,
      String(Math.round(state.roomsPanelWidth)),
    );
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE.peopleWidth,
      String(Math.round(state.peoplePanelWidth)),
    );
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE.composerHeight,
      String(Math.round(state.composerInputHeight)),
    );
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE.composerCollapsed,
      state.composerPanelCollapsed ? "collapsed" : "expanded",
    );
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE.density,
      state.workspaceDensity,
    );
    window.localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE.focusMode,
      state.workspaceFocusMode ? "enabled" : "disabled",
    );
  } catch (error) {
    // Private browsing or a hardened WebView may disable persistent storage.
  }
}

function captureWorkspaceScrollState() {
  if (typeof isNearTimelineBottom !== "function") return null;
  const hasMessages = Boolean(
    elements.timeline.querySelector("article[data-message-id]"),
  );
  if (!hasMessages) return null;
  const nearBottom = isNearTimelineBottom();
  return {
    nearBottom,
    anchor: nearBottom || typeof captureTimelineAnchor !== "function"
      ? null
      : captureTimelineAnchor(),
  };
}

function scheduleWorkspaceScrollRestore(scrollState) {
  if (!scrollState) return;
  pendingWorkspaceScrollState = pendingWorkspaceScrollState?.nearBottom
    ? pendingWorkspaceScrollState
    : scrollState;
  if (workspaceScrollFrame) window.cancelAnimationFrame(workspaceScrollFrame);
  workspaceScrollFrame = window.requestAnimationFrame(() => {
    workspaceScrollFrame = null;
    const pending = pendingWorkspaceScrollState;
    pendingWorkspaceScrollState = null;
    if (!pending) return;
    if (pending.nearBottom) {
      elements.timeline.scrollTop = elements.timeline.scrollHeight;
      return;
    }
    if (!pending.anchor?.messageId) return;
    const anchored = [...elements.timeline.querySelectorAll("article[data-message-id]")]
      .find((article) => article.dataset.messageId === pending.anchor.messageId);
    if (!anchored) return;
    const timelineTop = elements.timeline.getBoundingClientRect().top;
    elements.timeline.scrollTop += (
      anchored.getBoundingClientRect().top
      - timelineTop
      - pending.anchor.offset
    );
  });
}

function applyWorkspaceLayout({ persist = false } = {}) {
  const scrollState = captureWorkspaceScrollState();
  const composerMaximum = composerHeightMaximum();
  state.composerInputHeight = clampLayoutValue(
    state.composerInputHeight,
    WORKSPACE_LAYOUT_LIMITS.composerMin,
    composerMaximum,
  );
  elements.workspace.style.setProperty(
    "--rooms-expanded-column",
    `${Math.round(state.roomsPanelWidth)}px`,
  );
  elements.workspace.style.setProperty(
    "--people-expanded-column",
    `${Math.round(state.peoplePanelWidth)}px`,
  );
  elements.workspace.style.setProperty(
    "--composer-input-height",
    `${Math.round(state.composerInputHeight)}px`,
  );
  elements.workspace.classList.toggle("rooms-collapsed", state.roomsPanelCollapsed);
  elements.workspace.classList.toggle("people-collapsed", state.peoplePanelCollapsed);
  elements.workspace.classList.toggle("focus-mode", state.workspaceFocusMode);
  elements.workspace.classList.toggle(
    "compact-view",
    state.workspaceDensity === "compact",
  );
  elements.workspace.classList.toggle(
    "composer-collapsed",
    state.composerPanelCollapsed,
  );

  elements.toggleRoomsPanel.setAttribute(
    "aria-expanded",
    String(!state.roomsPanelCollapsed),
  );
  elements.togglePeoplePanel.setAttribute(
    "aria-expanded",
    String(!state.peoplePanelCollapsed),
  );
  elements.toggleRoomsPanel.title = state.roomsPanelCollapsed
    ? "展开聊天室列表"
    : "收起聊天室列表";
  elements.togglePeoplePanel.title = state.peoplePanelCollapsed
    ? "展开成员面板"
    : "收起成员面板";

  elements.toggleFocusMode.classList.toggle("active", state.workspaceFocusMode);
  elements.toggleFocusMode.setAttribute("aria-pressed", String(state.workspaceFocusMode));
  elements.layoutFocusStatus.textContent = state.workspaceFocusMode ? "退出" : "开启";

  for (const choice of [elements.layoutDensityCompact, elements.layoutDensityDetailed]) {
    const active = choice.dataset.density === state.workspaceDensity;
    choice.classList.toggle("active", active);
    choice.setAttribute("aria-checked", String(active));
    choice.tabIndex = active ? 0 : -1;
  }

  elements.ownerMessageForm.hidden = state.composerPanelCollapsed;
  elements.toggleComposerPanel.setAttribute(
    "aria-expanded",
    String(!state.composerPanelCollapsed),
  );
  elements.toggleComposerPanel.title = state.composerPanelCollapsed
    ? "展开输入区"
    : "收起输入区";
  elements.layoutToggleComposer.classList.toggle(
    "active",
    state.composerPanelCollapsed,
  );
  elements.layoutToggleComposer.setAttribute(
    "aria-pressed",
    String(state.composerPanelCollapsed),
  );
  elements.layoutComposerStatus.textContent = state.composerPanelCollapsed
    ? "已收起"
    : "展开";

  elements.roomsResizer.setAttribute("aria-valuenow", String(Math.round(state.roomsPanelWidth)));
  elements.roomsResizer.setAttribute("aria-valuemax", String(WORKSPACE_LAYOUT_LIMITS.panelMax));
  elements.roomsResizer.setAttribute("aria-valuetext", `${Math.round(state.roomsPanelWidth)} 像素`);
  elements.peopleResizer.setAttribute("aria-valuenow", String(Math.round(state.peoplePanelWidth)));
  elements.peopleResizer.setAttribute("aria-valuemax", String(WORKSPACE_LAYOUT_LIMITS.panelMax));
  elements.peopleResizer.setAttribute("aria-valuetext", `${Math.round(state.peoplePanelWidth)} 像素`);
  elements.composerResizer.setAttribute("aria-valuenow", String(Math.round(state.composerInputHeight)));
  elements.composerResizer.setAttribute("aria-valuemax", String(composerMaximum));
  elements.composerResizer.setAttribute("aria-valuetext", `${Math.round(state.composerInputHeight)} 像素`);

  scheduleWorkspaceScrollRestore(scrollState);
  if (persist) persistWorkspaceLayout();
}

function setPanelWidth(kind, width, { persist = false } = {}) {
  const minimum = kind === "rooms"
    ? WORKSPACE_LAYOUT_LIMITS.roomsMin
    : WORKSPACE_LAYOUT_LIMITS.peopleMin;
  const next = clampLayoutValue(width, minimum, panelWidthMaximum(kind));
  if (kind === "rooms") state.roomsPanelWidth = next;
  else state.peoplePanelWidth = next;
  applyWorkspaceLayout({ persist });
}

function setComposerHeight(height, { persist = false } = {}) {
  state.composerPanelCollapsed = false;
  state.composerInputHeight = clampLayoutValue(
    height,
    WORKSPACE_LAYOUT_LIMITS.composerMin,
    composerHeightMaximum(),
  );
  applyWorkspaceLayout({ persist });
}

function beginWorkspaceResize(kind, event) {
  if (event.button !== 0 || state.workspaceFocusMode) return;
  event.preventDefault();
  const target = event.currentTarget;
  const startValue = kind === "rooms"
    ? state.roomsPanelWidth
    : kind === "people"
      ? state.peoplePanelWidth
      : state.composerInputHeight;
  activeWorkspaceResize = {
    kind,
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    startValue,
    target,
  };
  target.setPointerCapture(event.pointerId);
  document.body.classList.add(
    "layout-resizing",
    kind === "composer" ? "layout-resizing-rows" : "layout-resizing-columns",
  );
}

function moveWorkspaceResize(event) {
  if (!activeWorkspaceResize || event.pointerId !== activeWorkspaceResize.pointerId) return;
  event.preventDefault();
  const { kind, startX, startY, startValue } = activeWorkspaceResize;
  if (kind === "composer") {
    setComposerHeight(startValue + startY - event.clientY);
    return;
  }
  const horizontalDelta = event.clientX - startX;
  setPanelWidth(
    kind,
    startValue + (kind === "rooms" ? horizontalDelta : -horizontalDelta),
  );
}

function finishWorkspaceResize(event) {
  if (!activeWorkspaceResize || event.pointerId !== activeWorkspaceResize.pointerId) return;
  const { target, pointerId } = activeWorkspaceResize;
  if (target.hasPointerCapture(pointerId)) target.releasePointerCapture(pointerId);
  activeWorkspaceResize = null;
  document.body.classList.remove(
    "layout-resizing",
    "layout-resizing-columns",
    "layout-resizing-rows",
  );
  applyWorkspaceLayout({ persist: true });
}

function bindWorkspaceResizer(element, kind, defaultValue) {
  element.addEventListener("pointerdown", (event) => beginWorkspaceResize(kind, event));
  element.addEventListener("pointermove", moveWorkspaceResize);
  element.addEventListener("pointerup", finishWorkspaceResize);
  element.addEventListener("pointercancel", finishWorkspaceResize);
  element.addEventListener("dblclick", () => {
    if (kind === "composer") setComposerHeight(defaultValue, { persist: true });
    else setPanelWidth(kind, defaultValue, { persist: true });
  });
  element.addEventListener("keydown", (event) => {
    const current = kind === "rooms"
      ? state.roomsPanelWidth
      : kind === "people"
        ? state.peoplePanelWidth
        : state.composerInputHeight;
    const minimum = kind === "rooms"
      ? WORKSPACE_LAYOUT_LIMITS.roomsMin
      : kind === "people"
        ? WORKSPACE_LAYOUT_LIMITS.peopleMin
        : WORKSPACE_LAYOUT_LIMITS.composerMin;
    const maximum = kind === "composer"
      ? composerHeightMaximum()
      : panelWidthMaximum(kind);
    let next = null;
    if (["ArrowRight", "ArrowUp"].includes(event.key)) next = current + 12;
    if (["ArrowLeft", "ArrowDown"].includes(event.key)) next = current - 12;
    if (event.key === "Home") next = minimum;
    if (event.key === "End") next = maximum;
    if (next === null) return;
    event.preventDefault();
    if (kind === "composer") setComposerHeight(next, { persist: true });
    else setPanelWidth(kind, next, { persist: true });
  });
}

function setWorkspaceDensity(density, { persist = true } = {}) {
  state.workspaceDensity = density === "compact" ? "compact" : "detailed";
  applyWorkspaceLayout({ persist });
}

function toggleComposerPanel() {
  state.composerPanelCollapsed = !state.composerPanelCollapsed;
  if (state.composerPanelCollapsed) elements.mentionMenu.hidden = true;
  applyWorkspaceLayout({ persist: true });
}

function ensureComposerPanelExpanded() {
  if (!state.composerPanelCollapsed) return;
  state.composerPanelCollapsed = false;
  applyWorkspaceLayout({ persist: true });
}

function resetWorkspaceLayout() {
  state.roomsPanelCollapsed = false;
  state.peoplePanelCollapsed = true;
  state.workspaceDensity = WORKSPACE_LAYOUT_DEFAULTS.density;
  state.workspaceFocusMode = false;
  state.roomsPanelWidth = WORKSPACE_LAYOUT_DEFAULTS.roomsWidth;
  state.peoplePanelWidth = WORKSPACE_LAYOUT_DEFAULTS.peopleWidth;
  state.composerInputHeight = WORKSPACE_LAYOUT_DEFAULTS.composerHeight;
  state.composerPanelCollapsed = false;
  applyWorkspaceLayout({ persist: true });
}

state.roomsPanelCollapsed = readLayoutPreference(
  WORKSPACE_LAYOUT_STORAGE.roomsCollapsed,
  window.matchMedia("(max-width: 760px)").matches,
);
state.peoplePanelCollapsed = readLayoutPreference(
  WORKSPACE_LAYOUT_STORAGE.peopleCollapsed,
  true,
);
state.roomsPanelWidth = readLayoutNumber(
  WORKSPACE_LAYOUT_STORAGE.roomsWidth,
  WORKSPACE_LAYOUT_DEFAULTS.roomsWidth,
  WORKSPACE_LAYOUT_LIMITS.roomsMin,
  WORKSPACE_LAYOUT_LIMITS.panelMax,
);
state.peoplePanelWidth = readLayoutNumber(
  WORKSPACE_LAYOUT_STORAGE.peopleWidth,
  WORKSPACE_LAYOUT_DEFAULTS.peopleWidth,
  WORKSPACE_LAYOUT_LIMITS.peopleMin,
  WORKSPACE_LAYOUT_LIMITS.panelMax,
);
state.composerInputHeight = readLayoutNumber(
  WORKSPACE_LAYOUT_STORAGE.composerHeight,
  WORKSPACE_LAYOUT_DEFAULTS.composerHeight,
  WORKSPACE_LAYOUT_LIMITS.composerMin,
  WORKSPACE_LAYOUT_LIMITS.composerMax,
);
state.composerPanelCollapsed = readLayoutPreference(
  WORKSPACE_LAYOUT_STORAGE.composerCollapsed,
  false,
);
state.workspaceFocusMode = readLayoutPreference(
  WORKSPACE_LAYOUT_STORAGE.focusMode,
  false,
);
state.workspaceDensity = readLayoutValue(
  WORKSPACE_LAYOUT_STORAGE.density,
  WORKSPACE_LAYOUT_DEFAULTS.density,
) === "compact" ? "compact" : "detailed";

bindWorkspaceResizer(elements.roomsResizer, "rooms", WORKSPACE_LAYOUT_DEFAULTS.roomsWidth);
bindWorkspaceResizer(elements.peopleResizer, "people", WORKSPACE_LAYOUT_DEFAULTS.peopleWidth);
bindWorkspaceResizer(elements.composerResizer, "composer", WORKSPACE_LAYOUT_DEFAULTS.composerHeight);

elements.toggleRoomsPanel.addEventListener("click", () => {
  state.roomsPanelCollapsed = !state.roomsPanelCollapsed;
  applyWorkspaceLayout({ persist: true });
});
elements.togglePeoplePanel.addEventListener("click", () => {
  state.peoplePanelCollapsed = !state.peoplePanelCollapsed;
  applyWorkspaceLayout({ persist: true });
});
elements.toggleFocusMode.addEventListener("click", () => {
  state.workspaceFocusMode = !state.workspaceFocusMode;
  applyWorkspaceLayout({ persist: true });
});
elements.layoutDensityCompact.addEventListener("click", () => setWorkspaceDensity("compact"));
elements.layoutDensityDetailed.addEventListener("click", () => setWorkspaceDensity("detailed"));
for (const [index, choice] of [
  elements.layoutDensityCompact,
  elements.layoutDensityDetailed,
].entries()) {
  choice.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const choices = [elements.layoutDensityCompact, elements.layoutDensityDetailed];
    const step = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1;
    const target = choices[(index + step + choices.length) % choices.length];
    setWorkspaceDensity(target.dataset.density);
    target.focus();
  });
}
elements.toggleComposerPanel.addEventListener("click", toggleComposerPanel);
elements.layoutToggleComposer.addEventListener("click", toggleComposerPanel);
elements.resetWorkspaceLayout.addEventListener("click", resetWorkspaceLayout);

window.addEventListener("resize", () => {
  if (workspaceResizeFrame) window.cancelAnimationFrame(workspaceResizeFrame);
  workspaceResizeFrame = window.requestAnimationFrame(() => {
    workspaceResizeFrame = null;
    if (window.innerWidth > 760) {
      state.roomsPanelWidth = Math.min(state.roomsPanelWidth, panelWidthMaximum("rooms"));
      if (window.innerWidth > 1100) {
        state.peoplePanelWidth = Math.min(state.peoplePanelWidth, panelWidthMaximum("people"));
      }
    }
    state.composerInputHeight = Math.min(
      state.composerInputHeight,
      composerHeightMaximum(),
    );
    applyWorkspaceLayout();
  });
}, { passive: true });

applyWorkspaceLayout();
