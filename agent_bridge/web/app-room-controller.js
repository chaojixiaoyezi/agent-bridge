"use strict";


function roomCreator(room) {
  if (room.owner_display_name) return `${room.owner_display_name} 创建`;
  if (room.creator_kind === "user") return "管理员创建";
  if (room.creator_kind === "legacy") return "历史房间";
  return `${room.creator_client_type || room.creator_participant_id || "Agent"} 创建`;
}

function updateComposer(room) {
  const integrationRoom = room?.room_kind === "integration";
  if (integrationRoom) state.composerMode = "task";
  const canSpeak = Boolean(
    room
    && room.status === "active"
    && (!integrationRoom || room.can_assign_tasks),
  );
  if (state.composerMode === "task" && !room?.can_assign_tasks) {
    state.composerMode = "chat";
  }
  const effectiveCooldown = state.messageRateLimits?.current_user_effective_cooldown_seconds ?? 60;
  elements.ownerMessageBody.disabled = !canSpeak;
  elements.sendOwnerMessage.disabled = !canSpeak;
  elements.chooseComposerFiles.disabled = !canSpeak || state.composerMode === "task";
  elements.toggleComposerLink.disabled = !canSpeak || state.composerMode === "task";
  elements.composerChatMode.hidden = integrationRoom;
  elements.composerTaskMode.hidden = !(canSpeak && room?.can_assign_tasks);
  elements.composerTaskMode.textContent = integrationRoom ? "TUI 指令" : "本体任务";
  elements.composerTaskMode.disabled = Boolean(
    state.composerAttachments.length || state.composerLinks.length,
  );
  elements.composerChatMode.classList.toggle("active", state.composerMode === "chat");
  elements.composerTaskMode.classList.toggle("active", state.composerMode === "task");
  elements.sendOwnerMessage.textContent = integrationRoom
    ? "发送到 Agent"
    : state.composerMode === "task" ? "提交任务" : "发送";
  elements.wakeAllAgents.hidden = !(
    canSpeak && room?.can_wake_all && state.composerMode === "chat"
  );
  elements.ownerMessageBody.placeholder = canSpeak
    ? integrationRoom
      ? "像在 Claude Code TUI 中一样布置工作；本机工作目录和权限保持不变…"
      : state.composerMode === "task"
      ? "描述要完成的任务；@ Agent 可指定候选，不 @ 则由群内一个 Agent 先领取并按需分工…"
      : `${state.currentUser?.display_name || "Web 用户"}发言（${isAdmin() ? "不限频" : `每个房间间隔 ${formatCooldown(effectiveCooldown)}`}）；Enter 发送，Shift+Enter 换行…`
    : room ? "废弃聊天室仅保留历史，不能继续发言。" : "先选择一个使用中的聊天室。";
  if (!canSpeak) elements.ownerMessageFeedback.textContent = "";
  renderComposerAssets();
  updateComposerContext();
}

function updateComposerContext() {
  const replied = state.composerReplyTo
    ? state.messages.find((message) => message.message_id === state.composerReplyTo)
    : null;
  if (state.composerAttachments.length) {
    const mentionIds = selectedMentionIds(elements.ownerMessageBody.value);
    elements.composerContext.hidden = false;
    elements.composerContextTitle.textContent = "定向文件/图片 · 整条消息受限";
    if (state.composerWakeAll) {
      elements.composerContextBody.textContent = "文字、链接和附件只发给本聊天室当前全部 Agent。";
    } else if (mentionIds.length) {
      elements.composerContextBody.textContent = `只发给：${mentionIds.map(participantName).join("、")}。其他 Agent 不会收到正文、文件名、缩略图或下载地址。`;
    } else {
      elements.composerContextBody.textContent = "发送前请在正文中 @ 至少一个 Agent，或选择 @全员。";
    }
  } else if (replied) {
    elements.composerContext.hidden = false;
    elements.composerContextTitle.textContent = `回复 ${replied.sender_display_name || replied.sender_client_type}`;
    elements.composerContextBody.textContent = replied.body.slice(0, 140);
  } else if (state.composerWakeAll) {
    elements.composerContext.hidden = false;
    elements.composerContextTitle.textContent = "@全员：唤醒本聊天室所有 Agent";
    elements.composerContextBody.textContent = "Agent 会全部收到唤醒，但可以根据兴趣选择是否回复。";
  } else if (state.composerMode === "task") {
    elements.composerContext.hidden = false;
    elements.composerContextTitle.textContent = "结构化任务 · 可执行";
    elements.composerContextBody.textContent = state.composerMentions.size
      ? "将从你 @ 的 Agent 中原子领取；领取者可继续分配子任务。"
      : "将由聊天室内一个 Agent 先领取；本机权限仍是最终边界。";
  } else {
    elements.composerContext.hidden = true;
    elements.composerContextTitle.textContent = "";
    elements.composerContextBody.textContent = "";
  }
  elements.wakeAllAgents?.classList.toggle("active", state.composerWakeAll);
}

function setComposerMode(mode) {
  const room = state.rooms.find((item) => item.conversation_id === state.selectedRoom);
  if (mode === "task" && (
    !room?.can_assign_tasks
    || state.composerAttachments.length
    || state.composerLinks.length
  )) return;
  ensureComposerPanelExpanded();
  state.composerMode = mode === "task" ? "task" : "chat";
  state.composerWakeAll = false;
  updateComposer(room);
  elements.ownerMessageBody.focus();
}

function clearComposerContext() {
  state.composerReplyTo = null;
  state.composerWakeAll = false;
  updateComposerContext();
}

function startComposerReply(message) {
  if (!message || message.reply_to) return;
  ensureComposerPanelExpanded();
  state.composerReplyTo = message.message_id;
  state.composerWakeAll = false;
  updateComposerContext();
  elements.ownerMessageBody.focus();
}

function hideMentionMenu() {
  elements.mentionMenu.hidden = true;
  elements.mentionMenu.replaceChildren();
}

function mentionQuery() {
  const cursor = elements.ownerMessageBody.selectionStart;
  const prefix = elements.ownerMessageBody.value.slice(0, cursor);
  const atIndex = prefix.lastIndexOf("@");
  if (atIndex < 0) return null;
  const queryText = prefix.slice(atIndex + 1);
  if (queryText.includes("@") || /\s/u.test(queryText) || Array.from(queryText).length > 64) {
    return null;
  }
  return {
    query: queryText.toLocaleLowerCase("zh-CN"),
    start: atIndex,
    end: cursor,
  };
}

function updateMentionMenu() {
  const current = mentionQuery();
  if (!current) {
    hideMentionMenu();
    return;
  }
  const candidates = state.participants
    .filter((person) => !isWebParticipant(person) && person.membership_active)
    .filter((person) => !isDormantParticipant(person))
    .filter((person) => {
      const haystack = `${person.display_name} ${person.client_type}`.toLocaleLowerCase("zh-CN");
      return haystack.includes(current.query);
    })
    .slice(0, 8);
  elements.mentionMenu.replaceChildren();
  for (const person of candidates) {
    const option = makeElement("button", "mention-option");
    option.type = "button";
    option.append(makeElement("strong", "", `@${person.display_name || person.client_type}`));
    option.append(makeElement("span", "", person.signature || person.client_type));
    option.addEventListener("mousedown", (event) => {
      event.preventDefault();
      addComposerMention(person, current);
    });
    elements.mentionMenu.append(option);
  }
  elements.mentionMenu.hidden = candidates.length === 0;
}

function addComposerMention(person, range = null) {
  ensureComposerPanelExpanded();
  const label = person.display_name || person.client_type;
  const token = `@${label}`;
  const textarea = elements.ownerMessageBody;
  const start = range?.start ?? textarea.selectionStart;
  const end = range?.end ?? textarea.selectionEnd;
  const prefix = start > 0 && !/\s/u.test(textarea.value[start - 1]) ? " " : "";
  const insertion = `${prefix}${token} `;
  textarea.setRangeText(insertion, start, end, "end");
  state.composerMentions.set(person.participant_id, token);
  hideMentionMenu();
  textarea.focus();
}

function selectedMentionIds(bodyText) {
  const ids = [];
  for (const [participantId, token] of state.composerMentions.entries()) {
    const escaped = token.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
    const pattern = new RegExp(
      `${escaped}(?=$|[\\s,，。.!！?？:：;；、)）\\]】}>》])`,
      "iu",
    );
    if (pattern.test(bodyText)) ids.push(participantId);
  }
  return ids;
}

const COMPOSER_MAX_ATTACHMENTS = 5;
const COMPOSER_MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024;
const COMPOSER_MAX_TOTAL_BYTES = 25 * 1024 * 1024;
const COMPOSER_MAX_LINKS = 8;
const COMPOSER_INLINE_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

function normalizedComposerLink(rawValue) {
  const value = String(rawValue || "").trim();
  if (!value || value.length > 2048) throw new Error("链接长度应为 1–2048 个字符。");
  let parsed;
  try {
    parsed = new URL(value);
  } catch (error) {
    throw new Error("请输入完整的 http:// 或 https:// 链接。");
  }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error("链接必须是无内嵌账号密码的 HTTP(S) 地址。");
  }
  return parsed.href;
}

function clearComposerAssets() {
  for (const item of state.composerAttachments) {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  }
  state.composerAttachments = [];
  state.composerLinks = [];
  elements.composerFileInput.value = "";
  elements.composerLinkUrl.value = "";
  elements.composerLinkEntry.hidden = true;
  renderComposerAssets();
  updateComposerContext();
}

function addComposerFiles(files) {
  const additions = [...files];
  if (!additions.length) return;
  if (state.composerAttachments.length + additions.length > COMPOSER_MAX_ATTACHMENTS) {
    throw new Error(`一条消息最多添加 ${COMPOSER_MAX_ATTACHMENTS} 个文件或图片。`);
  }
  let total = state.composerAttachments.reduce((sum, item) => sum + item.file.size, 0);
  for (const file of additions) {
    if (file.size <= 0) throw new Error(`「${file.name}」是空文件，不能发送。`);
    if (file.size > COMPOSER_MAX_ATTACHMENT_BYTES) {
      throw new Error(`「${file.name}」超过单个 12 MB 限制。`);
    }
    total += file.size;
    if (total > COMPOSER_MAX_TOTAL_BYTES) throw new Error("附件合计不能超过 25 MB。");
  }
  for (const file of additions) {
    const previewUrl = COMPOSER_INLINE_IMAGE_TYPES.has(file.type)
      ? URL.createObjectURL(file)
      : null;
    state.composerAttachments.push({
      id: window.crypto.randomUUID(),
      file,
      previewUrl,
    });
  }
  state.composerMode = "chat";
  renderComposerAssets();
  updateComposer(state.rooms.find((room) => room.conversation_id === state.selectedRoom));
}

function addComposerLink(rawValue) {
  const url = normalizedComposerLink(rawValue);
  if (state.composerLinks.includes(url)) return;
  if (state.composerLinks.length >= COMPOSER_MAX_LINKS) {
    throw new Error(`一条消息最多添加 ${COMPOSER_MAX_LINKS} 个链接。`);
  }
  state.composerLinks.push(url);
  elements.composerLinkUrl.value = "";
  elements.composerLinkEntry.hidden = true;
  renderComposerAssets();
  updateComposerContext();
}

function composerLinksForMessage(bodyText) {
  const result = [...state.composerLinks];
  const candidates = String(bodyText || "").match(/https?:\/\/[^\s<>"']+/giu) || [];
  for (const candidate of candidates) {
    const trimmed = candidate.replace(/[，。！？；：、）】》,.!?;:)\]]+$/u, "");
    if (!trimmed) continue;
    let normalized;
    try {
      normalized = normalizedComposerLink(trimmed);
    } catch (error) {
      continue;
    }
    if (!result.includes(normalized)) result.push(normalized);
  }
  if (result.length > COMPOSER_MAX_LINKS) {
    throw new Error(`一条消息最多包含 ${COMPOSER_MAX_LINKS} 个不同链接。`);
  }
  return result;
}

function renderComposerAssets() {
  const hasItems = state.composerAttachments.length || state.composerLinks.length;
  elements.composerAssetTray.hidden = !hasItems;
  elements.composerAssetItems.replaceChildren();
  if (!hasItems) return;
  const parts = [];
  if (state.composerAttachments.length) parts.push(`${state.composerAttachments.length} 个文件/图片`);
  if (state.composerLinks.length) parts.push(`${state.composerLinks.length} 个链接`);
  elements.composerAssetTitle.textContent = parts.join(" · ");
  if (state.composerAttachments.length) {
    const mentionIds = selectedMentionIds(elements.ownerMessageBody.value);
    elements.composerAssetRecipient.textContent = state.composerWakeAll
      ? "仅当前全部 Agent 可见"
      : mentionIds.length
        ? `仅 ${mentionIds.map(participantName).join("、")} 可见`
        : "需要先 @ 接收 Agent";
    elements.composerAssetRecipient.classList.toggle("warning", !state.composerWakeAll && !mentionIds.length);
  } else {
    elements.composerAssetRecipient.textContent = "链接按普通聊天室消息发送";
    elements.composerAssetRecipient.classList.remove("warning");
  }
  for (const item of state.composerAttachments) {
    const card = makeElement("div", "composer-asset-item attachment");
    if (item.previewUrl) {
      const image = document.createElement("img");
      image.src = item.previewUrl;
      image.alt = "";
      card.append(image);
    } else {
      card.append(makeElement("span", "composer-file-mark", "FILE"));
    }
    const copy = makeElement("span", "composer-asset-copy");
    copy.append(makeElement("strong", "", item.file.name));
    copy.append(makeElement("small", "", formatBytes(item.file.size)));
    card.append(copy);
    const remove = makeElement("button", "composer-asset-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `移除 ${item.file.name}`);
    remove.addEventListener("click", () => {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
      state.composerAttachments = state.composerAttachments.filter((candidate) => candidate.id !== item.id);
      renderComposerAssets();
      updateComposer(state.rooms.find((room) => room.conversation_id === state.selectedRoom));
    });
    card.append(remove);
    elements.composerAssetItems.append(card);
  }
  for (const url of state.composerLinks) {
    const parsed = new URL(url);
    const card = makeElement("div", "composer-asset-item link");
    card.append(makeElement("span", "composer-link-mark", "↗"));
    const copy = makeElement("span", "composer-asset-copy");
    copy.append(makeElement("strong", "", parsed.hostname));
    copy.append(makeElement("small", "", `${parsed.pathname}${parsed.search}`.slice(0, 100) || "/"));
    card.append(copy);
    const remove = makeElement("button", "composer-asset-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `移除链接 ${parsed.hostname}`);
    remove.addEventListener("click", () => {
      state.composerLinks = state.composerLinks.filter((candidate) => candidate !== url);
      renderComposerAssets();
      updateComposerContext();
    });
    card.append(remove);
    elements.composerAssetItems.append(card);
  }
}

function renderActiveRoomHeader(roomId = state.selectedRoom) {
  const activeRoom = state.rooms.find((room) => room.conversation_id === roomId);
  const searchable = Boolean(activeRoom);
  elements.roomMessageSearchParticipant.disabled = !searchable;
  elements.roomMessageSearchQuery.disabled = !searchable;
  elements.searchRoomMessages.disabled = !searchable;
  for (const control of [
    elements.roomMessageSearchKind,
    elements.roomMessageSearchNotification,
    elements.roomMessageSearchThread,
    elements.roomMessageSearchMarker,
    elements.roomMessageSearchSequence,
    elements.roomMessageSearchFrom,
    elements.roomMessageSearchTo,
  ]) {
    control.disabled = !searchable;
  }
  elements.roomMessageSearchAdvanced.classList.toggle("disabled", !searchable);
  elements.roomTitle.textContent = roomId || "未选择聊天室";
  const abandoned = activeRoom?.status === "abandoned";
  const integrationRoom = activeRoom?.room_kind === "integration";
  elements.workspace.classList.toggle("integration-room", integrationRoom);
  elements.roomRoute.textContent = abandoned
    ? "已废弃 · 仅浏览历史"
    : integrationRoom ? "INTEGRATION ROOM · TUI LIVE VIEW" : "当前聊天室 · 实时消息";
  elements.roomSummary.textContent = activeRoom
    ? abandoned
      ? `已废弃，Agent 不可进入 · ${activeRoom.participant_count} 个历史会话 · ${activeRoom.message_count} 条消息永久保留`
      : integrationRoom
        ? `${roomCreator(activeRoom)} · 单 Agent 本体会话 · Claude Code 可见输出与工具过程持久投影`
        : `${roomCreator(activeRoom)} · ${Number(activeRoom.current_participant_count ?? activeRoom.participant_count ?? 0)} 个会话 · ${activeRoom.message_count} 条持久消息`
    : "本机聊天室";
  updateComposer(activeRoom);
  applyUserPermissions();
  return activeRoom;
}

function cacheActiveRoomSnapshot() {
  const roomId = state.loadedRoom;
  if (!roomId || roomId !== state.selectedRoom) return;
  const snapshot = {
    messages: state.messages,
    participants: state.participants,
    timelineNodes: [...elements.timeline.childNodes],
    hasEarlierMessages: state.hasEarlierMessages,
    hasLaterMessages: state.hasLaterMessages,
    unreadMessages: state.unreadMessages,
    scrollTop: elements.timeline.scrollTop,
    nearBottom: isNearTimelineBottom(),
    timelineVirtual: captureTimelineVirtualSnapshot(),
    searchTargetMessageId: state.roomSearchTargetMessageId,
    roomHighlights: state.highlightsLoadedRoom === roomId
      ? state.roomHighlights
      : { items: [], pins: [], decisions: [], count: 0 },
    highlightsLoaded: state.highlightsLoadedRoom === roomId,
    cachedAt: Date.now(),
  };
  state.roomSnapshots.delete(roomId);
  state.roomSnapshots.set(roomId, snapshot);
  while (state.roomSnapshots.size > ROOM_SNAPSHOT_LIMIT) {
    const oldestRoom = state.roomSnapshots.keys().next().value;
    state.roomSnapshots.delete(oldestRoom);
  }
}

function restoreRoomSnapshot(roomId) {
  const snapshot = state.roomSnapshots.get(roomId);
  if (!snapshot) return false;
  state.roomSnapshots.delete(roomId);
  state.roomSnapshots.set(roomId, snapshot);
  state.loadedRoom = roomId;
  state.messages = snapshot.messages;
  state.timelineMessageIndexSource = null;
  state.participants = snapshot.participants;
  state.hasEarlierMessages = snapshot.hasEarlierMessages;
  state.hasLaterMessages = Boolean(snapshot.hasLaterMessages);
  state.unreadMessages = snapshot.unreadMessages;
  state.roomSearchTargetMessageId = snapshot.searchTargetMessageId || null;
  state.roomHighlights = snapshot.roomHighlights
    || { items: [], pins: [], decisions: [], count: 0 };
  state.highlightsLoadedRoom = snapshot.highlightsLoaded ? roomId : null;
  state.roomSnapshotRestoredAt = Number(snapshot.cachedAt || 0);
  restoreTimelineVirtualSnapshot(snapshot.timelineVirtual, roomId);
  prepareTimelineMessageIndexes(state.messages);
  ensureTimelineVirtualState(state.messages);
  state.participantRenderSignature = "";
  renderParticipants(state.participants);
  if (snapshot.timelineNodes?.length) {
    elements.timeline.replaceChildren(...snapshot.timelineNodes);
    state.messageRenderSignature = messageSignature(state.messages);
    syncTimelineVirtualDom(state.messages);
    updateNewMessageIndicator();
  } else {
    state.messageRenderSignature = "";
    renderMessages(state.messages, { forceBottom: snapshot.nearBottom });
  }
  const requestedRoom = roomId;
  window.requestAnimationFrame(() => {
    if (state.selectedRoom !== requestedRoom) return;
    elements.timeline.scrollTop = snapshot.nearBottom
      ? elements.timeline.scrollHeight
      : Math.min(snapshot.scrollTop, elements.timeline.scrollHeight);
    updateNewMessageIndicator();
  });
  return true;
}

async function selectRoom(roomId) {
  if (roomId === state.selectedRoom && state.loadedRoom === roomId) return;
  cacheActiveRoomSnapshot();
  state.roomRequestController?.abort();
  state.roomSearchRequestController?.abort();
  ++state.requestVersion;
  state.selectedRoom = roomId;
  state.participantFilter = "";
  elements.participantSearch.value = "";
  state.composerMentions.clear();
  clearComposerAssets();
  state.composerMode = "chat";
  state.taskPermissions = null;
  clearComposerContext();
  clearRoomMessageSearch();
  elements.roomSearchMenu.open = false;
  elements.roomToolsMenu.open = false;
  hideMentionMenu();
  updateNewMessageIndicator();
  window.localStorage.setItem("agentBridgeSelectedRoom", roomId);
  renderRooms();
  const restored = restoreRoomSnapshot(roomId);
  state.roomNavigationTarget = restored ? null : roomId;
  if (!restored) {
    state.loadedRoom = null;
    state.messageRenderSignature = "";
    resetTimelineVirtualState(roomId);
    state.participantRenderSignature = "";
    state.messages = [];
    state.participants = [];
    state.participantById = new Map();
    state.hasEarlierMessages = false;
    state.hasLaterMessages = false;
    state.unreadMessages = 0;
    state.roomSnapshotRestoredAt = 0;
    state.roomHighlights = { items: [], pins: [], decisions: [], count: 0 };
    state.highlightsLoadedRoom = null;
    renderParticipants([]);
    renderMessages([]);
  }
  renderActiveRoomHeader(roomId);
  const snapshotFresh = restored
    && Date.now() - state.roomSnapshotRestoredAt < ROOM_SNAPSHOT_FRESH_MS;
  refreshActiveRoom(!restored, !restored, {
    navigation: !restored,
    refreshParticipants: !snapshotFresh,
    refreshReceipts: restored && !snapshotFresh,
    refreshHighlights: !snapshotFresh,
  }).catch((error) => {
    if (error.name !== "AbortError") console.error(error);
  }).finally(() => {
    if (
      state.roomNavigationTarget === roomId
      && state.selectedRoom === roomId
      && state.loadedRoom !== roomId
    ) {
      state.roomNavigationTarget = null;
    }
  });
}

elements.participantSearch.addEventListener("input", (event) => {
  state.participantFilter = event.currentTarget.value;
  state.participantRenderSignature = "";
  renderParticipants(state.participants);
});
elements.participantSearch.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !elements.participantSearch.value) return;
  event.preventDefault();
  elements.participantSearch.value = "";
  state.participantFilter = "";
  state.participantRenderSignature = "";
  renderParticipants(state.participants);
});

function clearRoomMessageSearch({ clearInputs = true } = {}) {
  state.roomSearchRequestController?.abort();
  state.roomSearchRequestController = null;
  state.roomSearchResults = [];
  state.roomSearchHasMore = false;
  state.roomSearchNextBefore = null;
  state.roomSearchFingerprint = "";
  state.roomSearchTargetMessageId = null;
  elements.timeline.querySelector(".message.search-target")?.classList.remove("search-target");
  state.messageRenderSignature = "";
  if (clearInputs) {
    elements.roomMessageSearchQuery.value = "";
    elements.roomMessageSearchParticipant.value = "";
    elements.roomMessageSearchKind.value = "";
    elements.roomMessageSearchNotification.value = "";
    elements.roomMessageSearchThread.value = "";
    elements.roomMessageSearchMarker.value = "";
    elements.roomMessageSearchSequence.value = "";
    elements.roomMessageSearchFrom.value = "";
    elements.roomMessageSearchTo.value = "";
    elements.roomMessageSearchAdvanced.open = false;
  }
  elements.roomMessageSearchResults.replaceChildren();
  elements.roomMessageSearchResults.hidden = true;
  elements.clearRoomMessageSearch.hidden = true;
  elements.roomMessageSearchFeedback.textContent = "";
}

function renderRoomMessageSearchResults() {
  elements.roomMessageSearchResults.replaceChildren();
  elements.roomMessageSearchResults.hidden = false;
  elements.clearRoomMessageSearch.hidden = false;
  if (!state.roomSearchResults.length) {
    elements.roomMessageSearchResults.append(makeElement(
      "p",
      "room-message-search-empty",
      "当前聊天室没有匹配消息。",
    ));
    return;
  }
  const list = makeElement("div", "room-message-search-result-list");
  for (const result of state.roomSearchResults) {
    const button = makeElement("button", "room-message-search-result");
    button.type = "button";
    button.dataset.messageId = result.message_id;
    const heading = makeElement("span", "room-message-search-result-heading");
    heading.append(makeElement(
      "strong",
      "",
      result.sender_display_name || result.sender_client_type,
    ));
    heading.append(makeElement(
      "small",
      "",
      `#${roomSequence(result)} · ${fullTime(result.created_at)}`,
    ));
    button.append(heading);
    const facets = [];
    if (result.message_kind === "task") facets.push("任务");
    if (result.message_kind === "forward") facets.push("转发");
    facets.push(result.notification_mode === "mention" ? "艾特" : "普通");
    if (result.reply_to) facets.push("引用回复");
    if (result.marker_kinds?.includes("decision")) facets.push("决策");
    if (result.marker_kinds?.includes("pin")) facets.push("置顶");
    button.append(makeElement(
      "span",
      "room-message-search-result-facets",
      facets.join(" · "),
    ));
    button.append(makeElement(
      "span",
      "room-message-search-result-body",
      `${result.body_preview}${result.body_truncated ? "…" : ""}`,
    ));
    button.addEventListener("click", () => jumpToRoomSearchResult(result));
    list.append(button);
  }
  elements.roomMessageSearchResults.append(list);
  if (state.roomSearchHasMore) {
    const more = makeElement("button", "load-search-results", "加载更多结果");
    more.type = "button";
    more.addEventListener("click", () => searchRoomMessagesPage({ append: true }));
    elements.roomMessageSearchResults.append(more);
  }
}

function roomSearchDateBoundary(value, { nextDay = false } = {}) {
  if (!value) return null;
  const boundary = new Date(`${value}T00:00:00`);
  if (nextDay) boundary.setDate(boundary.getDate() + 1);
  return Math.floor(boundary.getTime() / 1000);
}

function currentRoomSearchCriteria() {
  const sequenceText = elements.roomMessageSearchSequence.value.trim();
  return {
    q: elements.roomMessageSearchQuery.value.trim(),
    sender_participant_id: elements.roomMessageSearchParticipant.value,
    message_kind: elements.roomMessageSearchKind.value,
    notification_mode: elements.roomMessageSearchNotification.value,
    thread_scope: elements.roomMessageSearchThread.value,
    marker_kind: elements.roomMessageSearchMarker.value,
    room_sequence: sequenceText ? Number.parseInt(sequenceText, 10) : null,
    created_after: roomSearchDateBoundary(elements.roomMessageSearchFrom.value),
    created_before: roomSearchDateBoundary(
      elements.roomMessageSearchTo.value,
      { nextDay: true },
    ),
  };
}

async function searchRoomMessagesPage({ append = false } = {}) {
  const roomId = state.selectedRoom;
  const criteria = currentRoomSearchCriteria();
  if (!roomId) return;
  if (!Object.values(criteria).some((value) => value !== null && value !== "")) {
    elements.roomMessageSearchFeedback.textContent = "请输入关键词或至少选择一个筛选条件。";
    elements.roomMessageSearchQuery.focus();
    return;
  }
  if (
    criteria.created_after !== null
    && criteria.created_before !== null
    && criteria.created_after >= criteria.created_before
  ) {
    elements.roomMessageSearchFeedback.textContent = "起始日期必须早于截止日期。";
    return;
  }
  const fingerprint = JSON.stringify(criteria);
  if (append && fingerprint !== state.roomSearchFingerprint) append = false;
  if (append && !state.roomSearchNextBefore) return;
  state.roomSearchRequestController?.abort();
  const controller = new AbortController();
  state.roomSearchRequestController = controller;
  elements.searchRoomMessages.disabled = true;
  elements.roomMessageSearchFeedback.textContent = append ? "正在加载更多…" : "正在搜索…";
  const parameters = new URLSearchParams({ limit: "25" });
  for (const [key, value] of Object.entries(criteria)) {
    if (value !== null && value !== "") parameters.set(key, String(value));
  }
  if (append) parameters.set("before_sequence", String(state.roomSearchNextBefore));
  try {
    const payload = await fetchJson(
      `/api/rooms/${encodeURIComponent(roomId)}/search?${parameters.toString()}`,
      { signal: controller.signal },
    );
    if (state.selectedRoom !== roomId || controller.signal.aborted) return;
    if (append) {
      const known = new Set(state.roomSearchResults.map((item) => item.message_id));
      state.roomSearchResults.push(
        ...payload.results.filter((item) => !known.has(item.message_id)),
      );
    } else {
      state.roomSearchResults = payload.results;
      state.roomSearchFingerprint = fingerprint;
    }
    state.roomSearchHasMore = Boolean(payload.has_more);
    state.roomSearchNextBefore = payload.next_before_sequence;
    elements.roomMessageSearchFeedback.textContent = state.roomSearchResults.length
      ? `${state.roomSearchResults.length} 条匹配 · 仅当前聊天室`
      : "没有匹配消息";
    renderRoomMessageSearchResults();
  } catch (error) {
    if (error.name !== "AbortError") {
      elements.roomMessageSearchFeedback.textContent = `搜索失败：${error.message}`;
    }
  } finally {
    if (state.roomSearchRequestController === controller) {
      state.roomSearchRequestController = null;
      elements.searchRoomMessages.disabled = false;
    }
  }
}

async function jumpToRoomSearchResult(result) {
  const roomId = state.selectedRoom;
  if (!roomId) return;
  state.roomRequestController?.abort();
  const controller = new AbortController();
  state.roomRequestController = controller;
  elements.roomMessageSearchFeedback.textContent = `正在定位 #${roomSequence(result)}…`;
  try {
    const payload = await fetchJson(
      `/api/rooms/${encodeURIComponent(roomId)}/messages?limit=${INITIAL_ROOM_MESSAGE_LIMIT}&around_sequence=${encodeURIComponent(result.sequence)}`,
      { signal: controller.signal },
    );
    if (state.selectedRoom !== roomId || controller.signal.aborted) return;
    state.messages = payload.messages;
    state.hasEarlierMessages = Boolean(payload.has_earlier);
    state.hasLaterMessages = Boolean(payload.has_later);
    state.loadedRoom = roomId;
    state.roomSearchTargetMessageId = result.message_id;
    state.messageRenderSignature = "";
    renderMessages(state.messages, { targetMessageId: result.message_id });
    cacheActiveRoomSnapshot();
    elements.roomMessageSearchResults.hidden = true;
    elements.roomMessageSearchFeedback.textContent = `已定位 #${roomSequence(result)}`;
  } catch (error) {
    if (error.name !== "AbortError") {
      elements.roomMessageSearchFeedback.textContent = `定位失败：${error.message}`;
    }
  } finally {
    if (state.roomRequestController === controller) {
      state.roomRequestController = null;
    }
  }
}

function renderPendingCenter() {
  const payload = state.pendingCenter || {};
  const counts = payload.counts || {};
  const needsMe = Number(counts.needs_me ?? counts.incoming ?? 0);
  const waitingOther = Number(counts.waiting_other ?? counts.outgoing ?? 0);
  const informational = Number(counts.informational ?? counts.oversight ?? 0);
  const total = Number(counts.attention_total ?? (needsMe + waitingOther));
  elements.pendingCenterBadge.hidden = total === 0;
  elements.pendingCenterBadge.textContent = total > 99 ? "99+" : String(total);
  elements.openPendingCenter.classList.toggle("has-pending", total > 0);

  elements.pendingCenterSummary.replaceChildren();
  for (const [label, count, tone] of [
    ["待我处理", needsMe, "urgent"],
    ["等待对方", waitingOther, "waiting"],
    ["仅供关注", informational, "oversight"],
  ]) {
    const card = makeElement("span", `pending-summary-card ${tone}`);
    card.append(
      makeElement("strong", "", String(count)),
      makeElement("small", "", label),
    );
    elements.pendingCenterSummary.append(card);
  }

  elements.pendingCenterList.replaceChildren();
  const responseItems = payload.pending_responses || [];
  const taskItems = payload.active_tasks || [];
  const incomingItems = responseItems.filter(
    (item) => (item.attention_kind || item.direction) === "needs_me"
      || (!item.attention_kind && item.direction === "incoming"),
  );
  elements.acknowledgeAllPending.hidden = incomingItems.length === 0;
  elements.acknowledgeAllPending.textContent = incomingItems.length > 1
    ? `将 ${incomingItems.length} 项全部标为已处理`
    : "标为已处理";
  if (!responseItems.length && !taskItems.length) {
    const empty = makeElement("div", "pending-center-empty");
    empty.append(
      makeElement("strong", "", "当前没有待处理事项"),
      makeElement("p", "", "需要你处理或等待对方的事项已经收口。"),
    );
    elements.pendingCenterList.append(empty);
    return;
  }

  const sections = [
    ["needs_me", "待我处理"],
    ["waiting_other", "等待对方"],
    ["informational", "仅通知，不计入顶部待办"],
  ];
  const attentionKind = (item) => item.attention_kind || ({
    incoming: "needs_me",
    outgoing: "waiting_other",
    oversight: "informational",
  }[item.direction] || "informational");
  const deliveryLabels = {
    queued: "等待送达",
    legacy_delivered: "已通知，等待处理",
    native_injected: "已注入本体 TUI",
    native_applied: "本体已读取，等待回复",
    replied: "已回复",
    legacy_acked: "已确认",
    cancelled: "已取消",
  };
  const taskLabels = {
    queued: "等待领取",
    claimed: "已领取",
    running: "执行中",
    needs_input: "等待补充",
  };

  for (const [kind, sectionLabel] of sections) {
    const messages = responseItems.filter((item) => attentionKind(item) === kind);
    const tasks = taskItems.filter((item) => attentionKind(item) === kind);
    if (!messages.length && !tasks.length) continue;
    elements.pendingCenterList.append(
      makeElement("h3", `pending-section-title ${kind}`, sectionLabel),
    );
    for (const item of messages) {
      const card = makeElement(
        "article",
        `pending-center-item ${item.direction} ${kind}`,
      );
      const locateButton = makeElement("button", "pending-item-main");
      locateButton.type = "button";
      const heading = makeElement("span", "pending-item-heading");
      heading.append(
        makeElement("strong", "", item.conversation_id),
        makeElement("span", `pending-kind ${item.direction}`, sectionLabel),
      );
      const sender = item.sender?.display_name || item.sender?.client_type || "未知成员";
      const target = item.target?.display_name || item.target?.client_type || "未知成员";
      locateButton.append(
        heading,
        makeElement("span", "pending-item-route", `${sender} → ${target}`),
        makeElement(
          "span",
          "pending-item-body",
          `${item.body_preview}${item.body_truncated ? "…" : ""}`,
        ),
        makeElement(
          "small",
          "pending-item-meta",
          `#${roomSequence(item)} · ${formatAge(item.age_seconds)} · ${deliveryLabels[item.delivery_stage] || (item.delivery_state === "delivered" ? "已通知" : "等待处理")}`,
        ),
      );
      locateButton.addEventListener("click", () => locatePendingCenterItem(item));
      card.append(locateButton);
      if (kind === "needs_me") {
        const actions = makeElement("span", "pending-item-actions");
        const acknowledge = makeElement(
          "button",
          "pending-acknowledge-button",
          "标为已处理",
        );
        acknowledge.type = "button";
        acknowledge.addEventListener("click", async () => {
          acknowledge.disabled = true;
          try {
            await acknowledgePendingItems([item]);
          } catch (error) {
            acknowledge.disabled = false;
            elements.pendingCenterFeedback.classList.add("error");
            elements.pendingCenterFeedback.textContent = `处理失败：${error.message}`;
          }
        });
        actions.append(acknowledge);
        card.append(actions);
      }
      elements.pendingCenterList.append(card);
    }
    for (const task of tasks) {
      const button = makeElement("button", `pending-center-item task ${task.status}`);
      button.type = "button";
      const heading = makeElement("span", "pending-item-heading");
      heading.append(
        makeElement("strong", "", task.conversation_id),
        makeElement("span", `pending-kind task ${task.status}`, taskLabels[task.status] || task.status),
      );
      const claimant = task.claimant_display_name || task.claimant_client_type || "尚未领取";
      button.append(
        heading,
        makeElement("span", "pending-item-route", `执行者：${claimant}`),
        makeElement(
          "span",
          "pending-item-body",
          `${task.body_preview}${task.body_truncated ? "…" : ""}`,
        ),
        makeElement(
          "small",
          "pending-item-meta",
          `${task.source_room_sequence ? `#${task.source_room_sequence} · ` : ""}${formatAge(task.age_seconds)}`,
        ),
      );
      button.addEventListener("click", () => locatePendingCenterItem({
        conversation_id: task.conversation_id,
        sequence: task.source_sequence,
        message_id: task.source_message_id,
      }));
      elements.pendingCenterList.append(button);
    }
  }

  if (payload.has_more) {
    elements.pendingCenterList.append(makeElement(
      "p",
      "pending-center-truncated",
      "当前只显示最早的 100 项；完成后列表会自动补入后续事项。",
    ));
  }
}

async function acknowledgePendingItems(items) {
  const byRoom = new Map();
  for (const item of items) {
    const roomItems = byRoom.get(item.conversation_id) || [];
    roomItems.push(item.message_id);
    byRoom.set(item.conversation_id, roomItems);
  }
  elements.pendingCenterFeedback.classList.remove("error", "success");
  elements.pendingCenterFeedback.textContent = "正在标记为已处理…";
  let acknowledged = 0;
  for (const [roomId, messageIds] of byRoom) {
    const payload = await fetchJson(
      `/api/rooms/${encodeURIComponent(roomId)}/pending-responses/acknowledge`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Agent-Bridge-Intent": "acknowledge-pending-responses",
        },
        body: JSON.stringify({ message_ids: messageIds }),
      },
    );
    acknowledged += Number(payload.acknowledgement?.acknowledged_count || 0);
  }
  await loadPendingCenter();
  await refreshActiveRoom(false, false, {
    refreshParticipants: false,
    refreshReceipts: true,
  });
  elements.pendingCenterFeedback.classList.add("success");
  elements.pendingCenterFeedback.textContent = `已标记 ${acknowledged} 项为已处理；没有发送新的聊天消息。`;
}

async function loadPendingCenter() {
  const payload = await fetchJson("/api/pending-responses?limit=100");
  state.pendingCenter = payload;
  renderPendingCenter();
  return payload;
}

async function locatePendingCenterItem(item) {
  if (elements.pendingCenterDialog.open) elements.pendingCenterDialog.close();
  const roomId = item.conversation_id;
  if (!roomId) return;
  if (state.selectedRoom !== roomId) await selectRoom(roomId);
  if (!item.sequence || !item.message_id) return;
  state.roomRequestController?.abort();
  const controller = new AbortController();
  state.roomRequestController = controller;
  try {
    const payload = await fetchJson(
      `/api/rooms/${encodeURIComponent(roomId)}/messages?limit=${INITIAL_ROOM_MESSAGE_LIMIT}&around_sequence=${encodeURIComponent(item.sequence)}`,
      { signal: controller.signal },
    );
    if (state.selectedRoom !== roomId || controller.signal.aborted) return;
    state.messages = payload.messages;
    state.hasEarlierMessages = Boolean(payload.has_earlier);
    state.hasLaterMessages = Boolean(payload.has_later);
    state.loadedRoom = roomId;
    state.roomSearchTargetMessageId = item.message_id;
    state.messageRenderSignature = "";
    renderMessages(state.messages, { targetMessageId: item.message_id });
    cacheActiveRoomSnapshot();
  } catch (error) {
    if (error.name !== "AbortError") console.error(error);
  } finally {
    if (state.roomRequestController === controller) {
      state.roomRequestController = null;
    }
  }
}

elements.acknowledgeAllPending.addEventListener("click", async () => {
  const items = (state.pendingCenter?.pending_responses || []).filter(
    (item) => (item.attention_kind || item.direction) === "needs_me"
      || (!item.attention_kind && item.direction === "incoming"),
  );
  if (!items.length) return;
  if (!window.confirm(
    `确认将当前显示的 ${items.length} 项标为已处理？这不会发送聊天回复。`,
  )) return;
  elements.acknowledgeAllPending.disabled = true;
  try {
    await acknowledgePendingItems(items);
  } catch (error) {
    elements.pendingCenterFeedback.classList.add("error");
    elements.pendingCenterFeedback.textContent = `批量处理失败：${error.message}`;
  } finally {
    elements.acknowledgeAllPending.disabled = false;
  }
});

elements.openPendingCenter.addEventListener("click", async () => {
  elements.pendingCenterFeedback.classList.remove("error", "success");
  elements.pendingCenterFeedback.textContent = "正在核对最新状态…";
  renderPendingCenter();
  if (!elements.pendingCenterDialog.open) elements.pendingCenterDialog.showModal();
  try {
    await loadPendingCenter();
    elements.pendingCenterFeedback.textContent = state.pendingCenter.has_more
      ? "事项较多，当前显示最早的 100 项。"
      : "已同步最新状态。";
  } catch (error) {
    elements.pendingCenterFeedback.classList.add("error");
    elements.pendingCenterFeedback.textContent = `载入失败：${error.message}`;
  }
});
elements.closePendingCenter.addEventListener("click", () => elements.pendingCenterDialog.close());
elements.pendingCenterDialog.addEventListener("click", (event) => {
  if (event.target === elements.pendingCenterDialog) elements.pendingCenterDialog.close();
});

function roomMarkersForMessage(messageId) {
  if (state.highlightsLoadedRoom !== state.selectedRoom) return [];
  return (state.roomHighlights.items || []).filter(
    (item) => item.message_id === messageId,
  );
}

function roomHighlightSignature() {
  if (state.highlightsLoadedRoom !== state.selectedRoom) return "";
  return (state.roomHighlights.items || [])
    .map((item) => `${item.message_id}:${item.marker_kind}:${item.marker_updated_at}:${item.note}`)
    .sort()
    .join("|");
}

async function loadRoomHighlights(roomId = state.selectedRoom) {
  if (!roomId) return null;
  const payload = await fetchJson(
    `/api/rooms/${encodeURIComponent(roomId)}/highlights?limit=200`,
  );
  if (state.selectedRoom !== roomId) return payload;
  state.roomHighlights = payload;
  state.highlightsLoadedRoom = roomId;
  state.messageRenderSignature = "";
  return payload;
}

function renderRoomHighlights() {
  elements.roomHighlightsList.replaceChildren();
  const items = state.roomHighlights.items || [];
  elements.roomHighlightsTitle.textContent = state.selectedRoom
    ? `${state.selectedRoom} · 房间要点`
    : "房间要点";
  if (!items.length) {
    elements.roomHighlightsList.append(makeElement(
      "p",
      "muted-copy",
      "还没有置顶或决策记录。聊天室管理者可从任意消息下方添加。",
    ));
    return;
  }
  for (const kind of ["decision", "pin"]) {
    const group = items.filter((item) => item.marker_kind === kind);
    if (!group.length) continue;
    elements.roomHighlightsList.append(makeElement(
      "h3",
      "room-highlight-section-title",
      kind === "decision" ? `决策 · ${group.length}` : `置顶 · ${group.length}`,
    ));
    for (const item of group) {
      const button = makeElement("button", `room-highlight-item ${kind}`);
      button.type = "button";
      const heading = makeElement("span", "room-highlight-heading");
      heading.append(
        makeElement("strong", "", `#${roomSequence(item)} · ${item.sender_display_name || item.sender_client_type}`),
        makeElement("small", "", fullTime(item.message_created_at)),
      );
      button.append(heading);
      if (item.note) button.append(makeElement("span", "room-highlight-note", item.note));
      button.append(makeElement(
        "span",
        "room-highlight-body",
        `${item.body_preview}${item.body_truncated ? "…" : ""}`,
      ));
      button.addEventListener("click", async () => {
        elements.roomHighlightsDialog.close();
        await locatePendingCenterItem(item);
      });
      elements.roomHighlightsList.append(button);
    }
  }
}

async function toggleRoomMarker(message, markerKind) {
  const room = state.rooms.find(
    (item) => item.conversation_id === message.conversation_id,
  );
  if (!room?.can_manage_highlights) return;
  const existing = roomMarkersForMessage(message.message_id).find(
    (item) => item.marker_kind === markerKind,
  );
  let note = existing?.note || "";
  if (!existing && markerKind === "decision") {
    const proposed = window.prompt(
      "填写决策说明（可留空，原消息仍会完整保留）：",
      "",
    );
    if (proposed === null) return;
    note = proposed;
  }
  const endpoint = `/api/rooms/${encodeURIComponent(message.conversation_id)}/messages/${encodeURIComponent(message.message_id)}/markers/${encodeURIComponent(markerKind)}`;
  await fetchJson(endpoint, {
    method: existing ? "DELETE" : "PUT",
    headers: {
      "Content-Type": "application/json",
      "X-Agent-Bridge-Intent": "manage-room-highlight",
    },
    ...(existing ? {} : { body: JSON.stringify({ note }) }),
  });
  await loadRoomHighlights(message.conversation_id);
  renderMessages(state.messages);
  renderRoomHighlights();
}

async function openMessageThread(message) {
  const roomId = message.conversation_id;
  elements.messageThreadTitle.textContent = `${roomId} · 话题串`;
  elements.messageThreadFeedback.classList.remove("error", "success");
  elements.messageThreadFeedback.textContent = "正在读取话题…";
  elements.messageThreadList.replaceChildren();
  if (!elements.messageThreadDialog.open) elements.messageThreadDialog.showModal();
  try {
    const payload = await fetchJson(
      `/api/rooms/${encodeURIComponent(roomId)}/threads/${encodeURIComponent(message.message_id)}?limit=200`,
    );
    if (state.selectedRoom !== roomId) return;
    for (const item of payload.messages || []) {
      const article = makeElement(
        "article",
        `message-thread-item${item.reply_to ? " reply" : " root"}`,
      );
      const heading = makeElement("div", "message-thread-heading");
      heading.append(
        createAvatarElement({
          avatarKey: item.sender_avatar_key,
          clientType: item.sender_client_type,
          label: item.sender_display_name,
          className: "compact-avatar",
        }),
        makeElement("strong", "", item.sender_display_name || item.sender_client_type),
        makeElement("small", "", `#${roomSequence(item)} · ${fullTime(item.created_at)}`),
      );
      article.append(heading, makeElement("p", "message-thread-body", item.body));
      elements.messageThreadList.append(article);
    }
    elements.messageThreadFeedback.textContent = payload.has_more
      ? "话题较长，当前显示前 200 条回复。"
      : `${payload.reply_count || 0} 条回复。`;
  } catch (error) {
    elements.messageThreadFeedback.classList.add("error");
    elements.messageThreadFeedback.textContent = `话题读取失败：${error.message}`;
  }
}

elements.openRoomHighlights.addEventListener("click", async () => {
  elements.roomHighlightsFeedback.classList.remove("error", "success");
  elements.roomHighlightsFeedback.textContent = "正在读取房间要点…";
  if (!elements.roomHighlightsDialog.open) elements.roomHighlightsDialog.showModal();
  try {
    await loadRoomHighlights();
    renderRoomHighlights();
    elements.roomHighlightsFeedback.textContent = state.roomHighlights.count
      ? `已同步 ${state.roomHighlights.count} 条房间要点。`
      : "当前还没有房间要点。";
  } catch (error) {
    elements.roomHighlightsFeedback.classList.add("error");
    elements.roomHighlightsFeedback.textContent = `载入失败：${error.message}`;
  }
});
elements.closeRoomHighlights.addEventListener("click", () => elements.roomHighlightsDialog.close());
elements.roomHighlightsDialog.addEventListener("click", (event) => {
  if (event.target === elements.roomHighlightsDialog) elements.roomHighlightsDialog.close();
});
elements.closeMessageThread.addEventListener("click", () => elements.messageThreadDialog.close());
elements.messageThreadDialog.addEventListener("click", (event) => {
  if (event.target === elements.messageThreadDialog) elements.messageThreadDialog.close();
});

elements.roomMessageSearchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  searchRoomMessagesPage();
});
elements.clearRoomMessageSearch.addEventListener("click", () => {
  clearRoomMessageSearch();
  if (state.hasLaterMessages) loadLatestMessages();
});
elements.roomMessageSearchQuery.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  event.preventDefault();
  clearRoomMessageSearch();
});

function mergeMessages(existing, incoming) {
  const byId = new Map(existing.map((message) => [message.message_id, message]));
  for (const message of incoming) byId.set(message.message_id, message);
  return [...byId.values()].sort((left, right) => left.sequence - right.sequence);
}

async function loadEarlierMessages(event) {
  const button = event?.currentTarget;
  const firstSequence = state.messages[0]?.sequence;
  const roomId = state.selectedRoom;
  if (!roomId || !firstSequence) return;
  state.roomRequestController?.abort();
  const controller = new AbortController();
  state.roomRequestController = controller;
  if (button) {
    button.disabled = true;
    button.textContent = "正在加载…";
  }
  try {
    const encodedRoom = encodeURIComponent(roomId);
    const payload = await fetchJson(
      `/api/rooms/${encodedRoom}/messages?limit=200&before_sequence=${encodeURIComponent(firstSequence)}`,
      { signal: controller.signal },
    );
    if (state.selectedRoom !== roomId || controller.signal.aborted) return;
    state.messages = mergeMessages(state.messages, payload.messages);
    state.hasEarlierMessages = payload.has_more;
    renderMessages(state.messages);
    cacheActiveRoomSnapshot();
  } catch (error) {
    if (error.name !== "AbortError") {
      console.error(error);
      if (button) button.textContent = "加载失败 · 重试";
    }
  } finally {
    if (state.roomRequestController === controller) {
      state.roomRequestController = null;
    }
    if (button) button.disabled = false;
  }
}

async function loadLatestMessages() {
  const roomId = state.selectedRoom;
  if (!roomId) return;
  state.roomRequestController?.abort();
  const controller = new AbortController();
  state.roomRequestController = controller;
  try {
    const payload = await fetchJson(
      `/api/rooms/${encodeURIComponent(roomId)}/messages?limit=${INITIAL_ROOM_MESSAGE_LIMIT}`,
      { signal: controller.signal },
    );
    if (state.selectedRoom !== roomId || controller.signal.aborted) return;
    state.messages = payload.messages;
    state.hasEarlierMessages = Boolean(payload.has_earlier ?? payload.has_more);
    state.hasLaterMessages = false;
    state.loadedRoom = roomId;
    state.roomSearchTargetMessageId = null;
    state.messageRenderSignature = "";
    renderMessages(state.messages, { forceBottom: true });
    cacheActiveRoomSnapshot();
  } catch (error) {
    if (error.name !== "AbortError") console.error(error);
  } finally {
    if (state.roomRequestController === controller) {
      state.roomRequestController = null;
    }
  }
}

async function refreshActiveRoom(
  forceScroll = false,
  fullRoom = false,
  {
    navigation = false,
    refreshParticipants = true,
    refreshTaskState = false,
    refreshReceipts = false,
    refreshHighlights = false,
  } = {},
) {
  if (!state.selectedRoom) return;
  const selectedRoom = state.selectedRoom;
  if (
    !navigation
    && state.roomNavigationTarget === selectedRoom
    && state.loadedRoom !== selectedRoom
  ) {
    return;
  }
  if (navigation && state.roomNavigationTarget !== selectedRoom) return;
  state.roomRequestController?.abort();
  const controller = new AbortController();
  state.roomRequestController = controller;
  const requestVersion = ++state.requestVersion;
  const encodedRoom = encodeURIComponent(selectedRoom);
  const activeRoom = state.rooms.find((room) => room.conversation_id === selectedRoom);
  const roomChanged = state.loadedRoom !== selectedRoom;
  const browsingHistory = !roomChanged && state.hasLaterMessages;
  const replaceLoadedWindow = roomChanged || (fullRoom && browsingHistory);
  const lastLoadedSequence = state.messages[state.messages.length - 1]?.sequence || 0;
  const hasServerUpdates = Number(activeRoom?.last_sequence || 0) > lastLoadedSequence;
  const messageRequest = roomChanged || fullRoom
    ? fetchJson(
        `/api/rooms/${encodedRoom}/messages?limit=${INITIAL_ROOM_MESSAGE_LIMIT}`,
        { signal: controller.signal },
      )
    : browsingHistory
      ? Promise.resolve(null)
      : refreshTaskState
        ? fetchJson(
            `/api/rooms/${encodedRoom}/messages?limit=${INITIAL_ROOM_MESSAGE_LIMIT}`,
            { signal: controller.signal },
          )
        : hasServerUpdates
          ? fetchJson(
              `/api/rooms/${encodedRoom}/messages?limit=${INCREMENTAL_ROOM_MESSAGE_LIMIT}&after_sequence=${encodeURIComponent(lastLoadedSequence)}`,
              { signal: controller.signal },
            )
      : Promise.resolve(null);
  const receiptRequest = refreshReceipts && state.messages.length > 0
    ? fetchJson(
        `/api/rooms/${encodedRoom}/receipts?limit=${encodeURIComponent(Math.max(INITIAL_ROOM_MESSAGE_LIMIT, state.messages.length))}&after_sequence=${encodeURIComponent(Math.max(0, Number(state.messages[0]?.sequence || 1) - 1))}`,
        { signal: controller.signal },
      )
    : Promise.resolve(null);
  const highlightRequest = roomChanged || fullRoom || refreshHighlights
    ? fetchJson(
        `/api/rooms/${encodedRoom}/highlights?limit=200`,
        { signal: controller.signal },
      )
    : Promise.resolve(null);
  let responses;
  try {
    responses = await Promise.all([
      messageRequest,
      refreshParticipants
        ? fetchJson(
            `/api/rooms/${encodedRoom}/participants`,
            { signal: controller.signal },
          )
        : Promise.resolve(null),
      receiptRequest,
      highlightRequest,
    ]);
  } catch (error) {
    if (error.name === "AbortError") return;
    throw error;
  } finally {
    if (state.roomRequestController === controller) {
      state.roomRequestController = null;
    }
  }
  const [messagePayload, participantPayload, receiptPayload, highlightPayload] = responses;
  if (requestVersion !== state.requestVersion) return;
  renderActiveRoomHeader(selectedRoom);
  if (participantPayload) renderParticipants(participantPayload.participants);
  if (highlightPayload) {
    state.roomHighlights = highlightPayload;
    state.highlightsLoadedRoom = selectedRoom;
    state.messageRenderSignature = "";
  }
  if (messagePayload) {
    let addedCount = 0;
    let appendedMessages = [];
    if (replaceLoadedWindow) {
      state.messages = messagePayload.messages;
      state.hasEarlierMessages = Boolean(
        messagePayload.has_earlier ?? messagePayload.has_more,
      );
      state.hasLaterMessages = Boolean(messagePayload.has_later);
      state.loadedRoom = selectedRoom;
      if (state.roomNavigationTarget === selectedRoom) {
        state.roomNavigationTarget = null;
      }
    } else {
      const knownIds = new Set(state.messages.map((message) => message.message_id));
      appendedMessages = messagePayload.messages.filter(
        (message) => !knownIds.has(message.message_id),
      );
      addedCount = appendedMessages.length;
      state.messages = mergeMessages(state.messages, messagePayload.messages);
      if (fullRoom) {
        state.hasEarlierMessages = Boolean(
          state.hasEarlierMessages
          || messagePayload.has_earlier
          || messagePayload.has_more,
        );
        state.hasLaterMessages = false;
      }
    }
    if (receiptPayload) {
      const receiptById = new Map(
        receiptPayload.receipts.map((receipt) => [receipt.message_id, receipt]),
      );
      state.messages = state.messages.map((message) => {
        const receipt = receiptById.get(message.message_id);
        return receipt ? { ...message, ...receipt } : message;
      });
    }
    const appendOnly = !replaceLoadedWindow
      && !fullRoom
      && !refreshTaskState
      && !highlightPayload
      && appendedMessages.length > 0
      && appendedMessages.every((message) => Number(message.sequence) > lastLoadedSequence);
    if (appendOnly) {
      appendMessages(appendedMessages, { forceBottom: forceScroll });
    } else {
      renderMessages(state.messages, { forceBottom: forceScroll, addedCount });
    }
    if (receiptPayload) updateReceiptLabels(state.messages);
    if (
      !roomChanged
      && !fullRoom
      && !refreshTaskState
      && messagePayload.has_more
    ) {
      window.setTimeout(() => refresh({ mode: "room" }), 0);
    }
  } else if (receiptPayload) {
    const receiptById = new Map(
      receiptPayload.receipts.map((receipt) => [receipt.message_id, receipt]),
    );
    state.messages = state.messages.map((message) => {
      const receipt = receiptById.get(message.message_id);
      return receipt ? { ...message, ...receipt } : message;
    });
    updateReceiptLabels(state.messages);
    if (highlightPayload) renderMessages(state.messages);
  } else if (highlightPayload) {
    renderMessages(state.messages);
  } else if (forceScroll) {
    window.requestAnimationFrame(() => {
      elements.timeline.scrollTop = elements.timeline.scrollHeight;
    });
  }
  cacheActiveRoomSnapshot();
}

const REFRESH_MODE_PRIORITY = { receipt: 0, room: 1, task: 2, presence: 3, full: 4 };

function mergeRefreshOptions(current, incoming) {
  if (!current) return { ...incoming };
  const currentMode = current.mode || "full";
  const incomingMode = incoming.mode || "full";
  return {
    mode: REFRESH_MODE_PRIORITY[incomingMode] > REFRESH_MODE_PRIORITY[currentMode]
      ? incomingMode
      : currentMode,
    fullRoom: Boolean(current.fullRoom || incoming.fullRoom),
    forceRoomBottom: Boolean(current.forceRoomBottom || incoming.forceRoomBottom),
    refreshTaskState: Boolean(
      current.refreshTaskState
      || incoming.refreshTaskState
      || currentMode === "task"
      || incomingMode === "task"
    ),
    refreshReceipts: Boolean(current.refreshReceipts || incoming.refreshReceipts),
    refreshHighlights: Boolean(
      current.refreshHighlights || incoming.refreshHighlights,
    ),
    forceDiagnostics: Boolean(
      current.forceDiagnostics || incoming.forceDiagnostics,
    ),
    forceMonitoring: Boolean(
      current.forceMonitoring || incoming.forceMonitoring,
    ),
  };
}

async function refresh(options = {}) {
  if (!state.currentUser) return;
  const mode = options.mode || "full";
  if (state.refreshing) {
    state.queuedRefresh = mergeRefreshOptions(state.queuedRefresh, { ...options, mode });
    return;
  }
  state.refreshing = true;
  if (mode === "full") elements.refreshButton.classList.add("spinning");
  try {
    if (mode === "receipt") {
      await refreshActiveRoom(false, false, {
        refreshParticipants: false,
        refreshReceipts: true,
      });
      const currentLabel = elements.connectionLabel.textContent || "";
      setConnection(
        true,
        currentLabel.includes("使用中") ? currentLabel : "事件连接正常",
      );
      elements.lastSync.textContent = DATE_TIME_FORMATTERS.syncTime.format(new Date());
      return;
    }
    const refreshPresence = mode === "full" || mode === "presence";
    const sessionRequest = isAdmin() && refreshPresence
      ? fetchJson("/api/sessions")
      : Promise.resolve(null);
    const invitationRequest = isAdmin() && refreshPresence
      ? fetchAgentInvitations()
      : Promise.resolve(null);
    const connectorHealthRequest = isAdmin() && refreshPresence && (
      options.forceDiagnostics === true
      || !state.connectorHealth
      || Date.now() - state.connectorHealthLoadedAt >= CONNECTOR_HEALTH_CACHE_MS
    )
      ? fetchJson("/api/admin/connectors/health")
      : Promise.resolve(null);
    const monitoringRequest = isAdmin() && refreshPresence && (
      options.forceMonitoring === true
      || !state.monitoring
      || Date.now() - state.monitoringLoadedAt >= MONITORING_CACHE_MS
    )
      ? fetchJson(`/api/admin/monitoring?hours=${encodeURIComponent(Number(elements.monitoringWindow.value || 24))}`)
      : Promise.resolve(null);
    const healthRequest = mode === "full" ? fetchJson("/api/health") : Promise.resolve(null);
    const nicknameRequest = mode === "full" ? fetchNicknameRequests() : Promise.resolve(null);
    const pendingCenterRequest = fetchJson("/api/pending-responses?limit=100");
    const [
      healthPayload,
      roomPayload,
      sessionPayload,
      nicknamePayload,
      invitationPayload,
      connectorHealthPayload,
      monitoringPayload,
      pendingCenterPayload,
    ] = await Promise.all([
      healthRequest,
      fetchJson("/api/rooms?limit=200"),
      sessionRequest,
      nicknameRequest,
      invitationRequest,
      connectorHealthRequest,
      monitoringRequest,
      pendingCenterRequest,
    ]);
    if (healthPayload) {
      state.messageRateLimits = healthPayload.message_rate_limits || null;
      state.emailDeliveryEnabled = Boolean(healthPayload.email_delivery_enabled);
      if (healthPayload.current_user) state.currentUser = healthPayload.current_user;
    }
    state.rooms = roomPayload.rooms;
    if (sessionPayload) {
      state.sessions = sessionPayload.sessions;
      state.sessionStats = sessionPayload.stats || { active_count: 0, clearable_count: 0 };
    }
    if (nicknamePayload) state.nicknameRequests = nicknamePayload.requests;
    if (invitationPayload) state.agentInvitations = invitationPayload.invitations;
    if (connectorHealthPayload) {
      state.connectorHealth = connectorHealthPayload;
      state.connectorHealthLoadedAt = Date.now();
      state.connectorHealthRenderSignature = "";
    }
    if (monitoringPayload) {
      state.monitoring = monitoringPayload;
      state.monitoringLoadedAt = Date.now();
      state.monitoringRenderSignature = "";
    }
    state.pendingCenter = pendingCenterPayload;
    if (!state.selectedRoom || !state.rooms.some((room) => room.conversation_id === state.selectedRoom)) {
      state.selectedRoom = state.rooms[0]?.conversation_id || null;
      if (state.selectedRoom) window.localStorage.setItem("agentBridgeSelectedRoom", state.selectedRoom);
    }
    renderRooms();
    populateAccessRooms();
    renderSessions();
    renderAgentInvitations();
    renderConnectorHealth();
    renderMonitoring();
    renderNicknameRequests();
    renderPendingCenter();
    applyUserPermissions();
    if (state.selectedRoom) {
      await refreshActiveRoom(
        options.forceRoomBottom === true,
        options.fullRoom === true,
        {
          refreshParticipants: refreshPresence,
          refreshTaskState: mode === "task" || options.refreshTaskState === true,
          refreshReceipts: options.refreshReceipts === true,
          refreshHighlights: options.refreshHighlights === true,
        },
      );
    }
    if (healthPayload) {
      setConnection(true, `${healthPayload.room_states.active} 使用中 · ${healthPayload.room_states.abandoned} 已废弃`);
    } else {
      const currentLabel = elements.connectionLabel.textContent || "";
      setConnection(
        true,
        currentLabel.includes("使用中") ? currentLabel : "事件连接正常",
      );
    }
    elements.lastSync.textContent = DATE_TIME_FORMATTERS.syncTime.format(new Date());
  } catch (error) {
    setConnection(false, "连接中断");
    elements.lastSync.textContent = "重试中";
    console.error(error);
  } finally {
    state.refreshing = false;
    window.setTimeout(() => elements.refreshButton.classList.remove("spinning"), 300);
    if (state.queuedRefresh) {
      const queued = state.queuedRefresh;
      state.queuedRefresh = null;
      // Drain the merged refresh before this refresh promise settles. A zero-delay
      // timer left an unobservable gap where `refreshing` and `queuedRefresh` were
      // both clear even though a full room refresh was still scheduled.
      await refresh(queued);
    }
  }
}
