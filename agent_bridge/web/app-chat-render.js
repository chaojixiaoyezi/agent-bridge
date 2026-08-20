"use strict";


function setConnection(online, text) {
  elements.statusDot.classList.toggle("online", online);
  elements.statusDot.classList.toggle("offline", !online);
  elements.connectionLabel.textContent = text;
}

function roomErrorMessage(message) {
  const text = String(message || "创建失败");
  if (text.includes("conversation_id must be")) {
    return "聊天室名称必须为 1–128 个字符。";
  }
  if (text.includes("conversation_id cannot contain") || text.includes("cannot be . or ..")) {
    return "聊天室名称不能包含 /、\\、?、#、%、控制字符，也不能只写一个或两个点。";
  }
  if (text.includes("already exists")) {
    return "这个聊天室名称已经存在，废弃聊天室的名称也不能重复使用。";
  }
  return text;
}

function syncRoomSelection() {
  for (const card of elements.roomList.querySelectorAll(".room-card[data-room]")) {
    const selected = card.dataset.room === state.selectedRoom;
    card.classList.toggle("active", selected);
    if (selected) card.setAttribute("aria-current", "true");
    else card.removeAttribute("aria-current");
  }
}

function renderRooms() {
  const signature = JSON.stringify([
    state.currentUser?.user_id || "",
    state.filter.trim().toLocaleLowerCase("zh-CN"),
    state.rooms.map((room) => [
      room.conversation_id,
      room.status,
      room.latest_created_at || room.last_activity_at,
      room.latest_sender_client_type || room.latest_sender_alias,
      room.latest_body,
      room.online_count,
      room.current_participant_count ?? room.participant_count ?? 0,
      room.message_count,
    ]),
  ]);
  if (signature === state.roomRenderSignature) {
    syncRoomSelection();
    return;
  }
  state.roomRenderSignature = signature;
  elements.roomList.replaceChildren();
  const normalizedFilter = state.filter.trim().toLowerCase();
  const visibleRooms = state.rooms.filter((room) => room.conversation_id.toLowerCase().includes(normalizedFilter));
  elements.roomCount.textContent = String(state.rooms.length);

  if (!visibleRooms.length) {
    const empty = makeElement(
      "p",
      "muted-copy",
      normalizedFilter
        ? "没有匹配的聊天室。"
        : isAdmin() ? "还没有聊天室，点右上角 ＋ 创建一个。" : "还没有聊天室，请等待管理员创建。",
    );
    empty.style.padding = "12px";
    elements.roomList.append(empty);
    return;
  }

  const groups = [
    { status: "active", label: "使用中" },
    { status: "abandoned", label: "废弃聊天室" },
  ];
  for (const group of groups) {
    const rooms = visibleRooms.filter((room) => room.status === group.status);
    if (!rooms.length) continue;
    const section = makeElement("section", "room-group");
    const groupHeading = makeElement("div", "room-group-heading");
    groupHeading.append(makeElement("span", "", group.label));
    groupHeading.append(makeElement("span", "", rooms.length));
    section.append(groupHeading);

    for (const room of rooms) {
      const abandoned = room.status === "abandoned";
      const button = makeElement("button", `room-card${room.conversation_id === state.selectedRoom ? " active" : ""}${abandoned ? " abandoned" : ""}`);
      button.type = "button";
      button.dataset.room = room.conversation_id;

      const titleLine = makeElement("div", "room-card-title");
      titleLine.append(makeElement("strong", "", room.conversation_id));
      if (abandoned) {
        titleLine.append(makeElement("span", "abandoned-badge", "已废弃"));
      } else {
        titleLine.append(makeElement("span", "room-card-time", shortTime(room.latest_created_at || room.last_activity_at)));
      }
      button.append(titleLine);

      const latestSender = room.latest_sender_client_type || room.latest_sender_alias;
      const preview = room.latest_body
        ? `${latestSender ? `${latestSender}：` : ""}${room.latest_body}`
        : abandoned ? "没有消息，房间因长期未使用而废弃。" : "房间已建立，等待第一条消息。";
      button.append(makeElement("p", "room-preview", preview));

      const meta = makeElement("div", "room-meta");
      if (!abandoned) meta.append(makeElement("span", "online-count", `${room.online_count} 在线`));
      const participantCount = abandoned
        ? Number(room.participant_count || 0)
        : Number(room.current_participant_count ?? room.participant_count ?? 0);
      meta.append(makeElement("span", "", `${participantCount} 会话`));
      meta.append(makeElement("span", "", `${room.message_count} 消息`));
      button.append(meta);
      button.addEventListener("click", () => selectRoom(room.conversation_id));
      section.append(button);
    }
    elements.roomList.append(section);
  }
  syncRoomSelection();
}

function isNearTimelineBottom() {
  return elements.timeline.scrollHeight - elements.timeline.scrollTop - elements.timeline.clientHeight < 80;
}

function captureTimelineAnchor() {
  const timelineTop = elements.timeline.getBoundingClientRect().top;
  const timelineBottom = timelineTop + elements.timeline.clientHeight;
  const articles = elements.timeline.querySelectorAll("article[data-message-id]");
  for (const article of articles) {
    const rect = article.getBoundingClientRect();
    if (rect.bottom > timelineTop && rect.top < timelineBottom) {
      return { messageId: article.dataset.messageId, offset: rect.top - timelineTop };
    }
  }
  return null;
}

function routeLabel(message) {
  if (message.task) return "结构化任务";
  if (message.wake_all_agents) return "@全员 · 唤醒 Agent";
  if (message.audience_kind === "participant") return "@成员";
  if (message.audience_kind === "role") return `@角色 · ${message.audience_value}`;
  if (message.audience_kind === "broadcast") return "房间广播";
  return message.mentions?.length ? "群聊 · 含 @" : "群聊";
}

function participantName(participantId) {
  const participant = state.participantById.get(participantId);
  return participant?.display_name || participant?.client_type || participantId;
}

function senderSeatLabel(message) {
  if (message.sender_client_type === "web-user" || message.sender_seat === "web") return null;
  return {
    main: { label: "本体", className: "main" },
    shadow: { label: "值守影子", className: "shadow" },
    executor: { label: "本体执行", className: "executor" },
    a2a: { label: "A2A 接入", className: "a2a" },
    unknown: { label: "历史来源未标记", className: "unknown" },
  }[message.sender_seat || "unknown"];
}

const DELIVERY_STATUS_LABELS = {
  replied: "已回复",
  read: "本体已读取",
  injected: "已注入本体 TUI",
  acknowledged: "已确认",
  notified: "已通知",
  queued: "等待接收",
  offline: "Bridge 会话离线",
  unavailable: "已离开聊天室",
  cancelled: "已取消",
};

function deliverySummaryText(message) {
  const summary = message.agent_delivery_summary;
  if (!summary || Number(summary.total || 0) === 0) {
    return `#${roomSequence(message)} · ${message.ack_count || 0}/${message.receipt_count || 0} 已确认/已通知`;
  }
  const segments = [];
  const replied = Number(summary.replied || 0);
  const read = Number(summary.read || 0);
  const receivedWaiting = ["injected", "acknowledged", "notified"]
    .reduce((total, key) => total + Number(summary[key] || 0), 0);
  const queued = Number(summary.queued || 0);
  const offline = Number(summary.offline || 0);
  const unavailable = Number(summary.unavailable || 0);
  const cancelled = Number(summary.cancelled || 0);
  if (replied) segments.push(`${replied} 已回复`);
  if (read) segments.push(`${read} 本体已读`);
  if (receivedWaiting) segments.push(`${receivedWaiting} 已收到未回复`);
  if (queued) segments.push(`${queued} 排队未收到`);
  if (offline) segments.push(`${offline} 离线未收到`);
  if (unavailable) segments.push(`${unavailable} 已离群`);
  if (cancelled) segments.push(`${cancelled} 已取消`);
  if (Number(summary.dnd || 0)) segments.push(`${summary.dnd} 免打扰`);
  return `#${roomSequence(message)} · ${segments.join(" · ") || "无 Agent 接收目标"}`;
}

function deliveryMoment(delivery) {
  return delivery.status_at;
}

function populateDeliveryDetails(container, message) {
  const deliveries = Array.isArray(message.agent_deliveries)
    ? message.agent_deliveries
    : [];
  const list = makeElement("div", "message-delivery-list");
  if (!deliveries.length) {
    list.append(makeElement("p", "message-delivery-empty", "这条消息没有 Agent 接收目标。"));
  }
  for (const delivery of deliveries) {
    const row = makeElement("div", `message-delivery-row status-${delivery.status || "queued"}`);
    row.append(createAvatarElement({
      avatarKey: delivery.avatar_key,
      clientType: delivery.client_type,
      label: delivery.display_name,
      className: "message-delivery-avatar",
    }));
    const identity = makeElement("span", "message-delivery-identity");
    identity.append(makeElement("strong", "", delivery.display_name || delivery.client_type));
    identity.append(makeElement("small", "", delivery.client_type || delivery.participant_id));
    row.append(identity);
    const stateBox = makeElement("span", "message-delivery-state");
    stateBox.append(makeElement(
      "span",
      `message-delivery-chip status-${delivery.status || "queued"}`,
      DELIVERY_STATUS_LABELS[delivery.status] || delivery.status || "等待接收",
    ));
    if (delivery.dnd_active) {
      stateBox.append(makeElement("span", "message-delivery-chip dnd", "免打扰至 0 点"));
    }
    if (delivery.actionable) {
      stateBox.append(makeElement("span", "message-delivery-chip required", "要求回复"));
    }
    if (!delivery.active_endpoint && !["offline", "unavailable", "cancelled"].includes(delivery.status)) {
      stateBox.append(makeElement("span", "message-delivery-chip offline", "当前离线"));
    }
    const moment = deliveryMoment(delivery);
    if (moment) stateBox.append(makeElement("time", "", fullTime(moment)));
    row.append(stateBox);
    list.append(row);
  }
  container.replaceChildren(list);
}

function createDeliveryDetails(message) {
  const details = makeElement("details", "message-delivery-details");
  details.append(makeElement("summary", "receipt-label", deliverySummaryText(message)));
  const body = makeElement("div", "message-delivery-body");
  details.append(body);
  details.addEventListener("toggle", () => {
    if (!details.open) {
      state.openDeliveryDetails.delete(message.message_id);
      return;
    }
    state.openDeliveryDetails.add(message.message_id);
    const latest = timelineMessageAt(message.message_id) || message;
    populateDeliveryDetails(body, latest);
  });
  if (state.openDeliveryDetails.has(message.message_id)) {
    details.open = true;
    populateDeliveryDetails(body, timelineMessageAt(message.message_id) || message);
  }
  return details;
}

function createMessageElement(message) {
  const article = makeElement("article", "message");
  article.dataset.messageId = message.message_id;
  if (message.message_id === state.roomSearchTargetMessageId) {
    article.classList.add("search-target");
  }
  const messageMarkers = roomMarkersForMessage(message.message_id);
  const head = makeElement("div", "message-head");
  const senderLine = makeElement("div", "sender-line");
  senderLine.append(createAvatarElement({
    avatarKey: message.sender_avatar_key,
    clientType: message.sender_client_type,
    label: message.sender_display_name,
    className: "message-avatar",
  }));
  senderLine.append(makeElement("strong", "", message.sender_display_name || message.sender_client_type));
  const seat = senderSeatLabel(message);
  if (seat) {
    const seatBadge = makeElement("span", `sender-seat-badge ${seat.className}`, seat.label);
    seatBadge.title = seat.className === "shadow"
      ? "同一公开身份的聊天室值守影子：参与讨论和转达，不代表本体任务进度"
      : seat.label;
    senderLine.append(seatBadge);
  }
  const signature = message.sender_signature || message.sender_alias || "未填写签名";
  senderLine.append(makeElement("span", "client-label", `${signature} · ${message.sender_client_type}`));
  senderLine.append(makeElement("span", "route-badge", routeLabel(message)));
  if (message.visibility?.kind === "restricted") {
    senderLine.append(makeElement("span", "restricted-message-badge", "定向内容"));
  }
  for (const marker of messageMarkers) {
    const markerLabel = marker.marker_kind === "decision" ? "决策" : "置顶";
    const markerBadge = makeElement(
      "span",
      `message-marker-badge ${marker.marker_kind}`,
      markerLabel,
    );
    if (marker.note) markerBadge.title = marker.note;
    senderLine.append(markerBadge);
  }
  if (message.task) {
    const statusLabel = {
      queued: "等待领取",
      claimed: "已领取",
      running: "执行中",
      needs_input: "等待补充",
      completed: "已完成",
      failed: "失败",
      cancelled: "已取消",
    }[message.task.status] || message.task.status;
    senderLine.append(makeElement("span", "task-badge", `任务 · ${statusLabel}`));
  }
  if (message.authorization) {
    senderLine.append(makeElement("span", "authorization-badge revoked", "授权待提交"));
  }
  head.append(senderLine);
  head.append(makeElement("time", "message-time", fullTime(message.created_at)));
  article.append(head);
  if (message.body) article.append(makeElement("p", "message-body", message.body));

  if (message.visibility?.kind === "restricted") {
    const recipients = message.visibility.recipients || [];
    const targetLabel = message.visibility.target_kind === "room_agents"
      ? "发送时本聊天室的全部 Agent"
      : recipients.map((item) => item.display_name || item.client_type || item.participant_id).join("、");
    article.append(makeElement(
      "p",
      "message-visibility-label",
      `仅 ${targetLabel || "指定 Agent"} 可读取此消息的文字、链接和附件`,
    ));
  }

  if (message.attachments?.length) {
    const assets = makeElement("div", "message-assets");
    for (const attachment of message.attachments) {
      const url = `/api/rooms/${encodeURIComponent(message.conversation_id)}/attachments/${encodeURIComponent(attachment.attachment_id)}`;
      if (attachment.kind === "image") {
        const link = makeElement("a", "message-image-card");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = `${attachment.filename} · ${formatBytes(attachment.size_bytes)}`;
        const image = document.createElement("img");
        image.src = url;
        image.alt = attachment.filename;
        image.loading = "lazy";
        image.decoding = "async";
        link.append(image);
        assets.append(link);
      } else {
        const link = makeElement("a", "message-file-card");
        link.href = url;
        link.download = attachment.filename;
        link.append(makeElement("span", "message-file-mark", "FILE"));
        const copy = makeElement("span", "message-file-copy");
        copy.append(makeElement("strong", "", attachment.filename));
        copy.append(makeElement("small", "", `${formatBytes(attachment.size_bytes)} · 下载并由本地权限决定如何使用`));
        link.append(copy);
        assets.append(link);
      }
    }
    article.append(assets);
  }

  if (message.links?.length) {
    const links = makeElement("div", "message-links");
    for (const item of message.links) {
      const link = makeElement("a", "message-link-card");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.append(makeElement("span", "message-link-mark", "↗"));
      const copy = makeElement("span", "message-link-copy");
      copy.append(makeElement("strong", "", item.host));
      copy.append(makeElement("small", "", item.display));
      link.append(copy);
      links.append(link);
    }
    article.append(links);
  }

  if (message.task) {
    const task = message.task;
    const labels = {
      queued: "等待领取",
      claimed: "已领取",
      running: "执行中",
      needs_input: "等待补充",
      completed: "已完成",
      failed: "失败",
      cancelled: "已取消",
    };
    const taskCard = makeElement("div", "task-card");
    const taskHead = makeElement("div", "task-card-head");
    const targetText = task.target_kind === "room_agents"
      ? "由聊天室 Agent 先领取，再按需分工"
      : `候选：${task.target_participant_ids.map(participantName).join("、")}`;
    taskHead.append(makeElement("span", "", targetText));
    taskHead.append(makeElement(
      "span",
      `task-status ${task.status}`,
      labels[task.status] || task.status,
    ));
    taskCard.append(taskHead);
    if (task.result_summary) {
      taskCard.append(makeElement("p", "task-summary", task.result_summary));
    }
    const room = state.rooms.find((item) => item.conversation_id === message.conversation_id);
    if (room?.can_cancel_tasks && !["completed", "failed", "cancelled"].includes(task.status)) {
      const cancelTask = makeElement("button", "message-reply-button task-cancel", "取消任务");
      cancelTask.type = "button";
      cancelTask.addEventListener("click", async () => {
        if (!window.confirm("取消后，尚未领取的任务不会再执行；已领取的本机回合可能继续到当前回合结束，但其结果不会被记为任务完成。确定取消？")) return;
        cancelTask.disabled = true;
        try {
          await fetchJson(`/api/tasks/${encodeURIComponent(task.task_id)}/cancel`, {
            method: "POST",
            headers: { "X-Agent-Bridge-Intent": "cancel-task" },
          });
          await refresh({ fullRoom: true });
        } catch (error) {
          window.alert(error.message);
        } finally {
          cancelTask.disabled = false;
        }
      });
      taskCard.append(cancelTask);
    }
    article.append(taskCard);
  }

  if (message.body_delivery) {
    const delivery = message.body_delivery;
    const applied = Number(delivery.applied_count || 0);
    const delivered = Number(delivery.delivered_count || 0);
    const total = Number(delivery.count || 0);
    const label = applied >= total
      ? `本体已接收并纳入当前任务 · ${applied}/${total}`
      : delivered > 0
        ? `已送达本体执行席，等待本轮落实 · ${delivered}/${total}`
        : `正在投递本体执行席 · 0/${total}`;
    article.append(makeElement(
      "p",
      `body-delivery-label ${applied >= total ? "applied" : "pending"}`,
      label,
    ));
  }

  if (message.forwarded_from) {
    const source = message.forwarded_from;
    const sourceSender = source.sender_display_name || source.sender_client_type;
    article.append(makeElement(
      "p",
      "forward-label",
      `显式转发自「${source.conversation_id}」#${roomSequence(source)} · ${sourceSender}`,
    ));
  }

  if (message.mentions?.length) {
    article.append(makeElement("p", "mention-label", `特别通知：${message.mentions.map(participantName).join("、")}`));
  }
  if (message.reply_to) {
    const original = timelineMessageAt(message.reply_to);
    const replyLabel = original
      ? `回复 ${original.sender_display_name || original.sender_client_type}：${original.body.slice(0, 90)}`
      : `回复消息 ${message.reply_to}`;
    article.append(makeElement("p", "reply-label", replyLabel));
  }
  if (message.claimant_display_name || message.claimant_alias) {
    article.append(makeElement("p", "claim-label", `由 ${message.claimant_display_name || message.claimant_alias} 领取`));
  }
  if (message.authorization) {
    article.append(makeElement(
      "p",
      "authorization-label revoked",
      "当前按普通聊天处理；授权功能暂未开放。",
    ));
    if (isAdmin()) {
      const submitButton = makeElement("button", "message-reply-button authorization-submit", "提交授权");
      submitButton.type = "button";
      submitButton.title = "授权功能暂未开放，当前只保留入口";
      submitButton.addEventListener("click", () => {
        window.alert("授权功能暂未开放；当前聊天室消息仍按普通讨论处理。这里只预留提交入口。");
      });
      article.append(submitButton);
    }
  }
  article.append(createDeliveryDetails(message));

  if (message.refs?.length) {
    const refs = makeElement("div", "ref-list");
    for (const ref of message.refs) {
      const label = ref.label ? `${ref.label} · ` : "";
      refs.append(makeElement("div", "ref-item", `${label}${ref.path}${ref.sha256 ? ` · sha256:${ref.sha256}` : ""}`));
    }
    article.append(refs);
  }
  const selectedRoom = state.rooms.find((room) => room.conversation_id === state.selectedRoom);
  if (
    selectedRoom?.status === "active"
    && selectedRoom?.can_assign_tasks
    && message.sender_participant_id === state.currentUser?.participant_id
    && message.message_kind === "message"
    && !message.task
  ) {
    const promoteButton = makeElement("button", "message-reply-button task-promote", "转为任务");
    promoteButton.type = "button";
    promoteButton.title = "保留这条消息的原文、序号和附近上下文，交给任务执行席";
    promoteButton.addEventListener("click", async () => {
      if (!window.confirm("把这条普通聊天转为结构化任务？原文和序号保持不变，之前的聊天通知会停止，改由任务执行席领取。")) return;
      promoteButton.disabled = true;
      try {
        await fetchJson(`/api/messages/${encodeURIComponent(message.message_id)}/convert-to-task`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Agent-Bridge-Intent": "convert-message-to-task",
          },
          body: JSON.stringify({}),
        });
        await refresh({ fullRoom: true });
      } catch (error) {
        window.alert(`转为任务失败：${error.message}`);
        promoteButton.disabled = false;
      }
    });
    article.append(promoteButton);
  }
  if (!message.reply_to && selectedRoom?.status === "active") {
    const replyButton = makeElement("button", "message-reply-button message-direct-reply", "回复");
    replyButton.type = "button";
    replyButton.addEventListener("click", () => startComposerReply(message));
    article.append(replyButton);
  }
  const rootMessage = message.reply_to
    ? timelineMessageAt(message.reply_to)
    : message;
  const loadedReplyCount = timelineLoadedReplyCount(
    rootMessage?.message_id || message.message_id,
  );
  const replyCount = Math.max(
    loadedReplyCount,
    Number(rootMessage?.reply_count || message.reply_count || 0),
  );
  if (message.reply_to || replyCount > 0) {
    const threadButton = makeElement(
      "button",
      "message-reply-button message-thread-button",
      replyCount > 0 ? `话题串 · ${replyCount}` : "查看话题串",
    );
    threadButton.type = "button";
    threadButton.addEventListener("click", () => openMessageThread(message));
    article.append(threadButton);
  }
  if (selectedRoom?.can_manage_highlights) {
    const knowledgeActions = makeElement("span", "message-knowledge-actions");
    for (const markerKind of ["pin", "decision"]) {
      const active = messageMarkers.some((item) => item.marker_kind === markerKind);
      const label = markerKind === "decision"
        ? (active ? "取消决策" : "记为决策")
        : (active ? "取消置顶" : "置顶");
      const markerButton = makeElement(
        "button",
        `message-reply-button message-marker-button ${markerKind}${active ? " active" : ""}`,
        label,
      );
      markerButton.type = "button";
      markerButton.addEventListener("click", async () => {
        markerButton.disabled = true;
        try {
          await toggleRoomMarker(message, markerKind);
        } catch (error) {
          window.alert(`${markerKind === "decision" ? "决策" : "置顶"}操作失败：${error.message}`);
          markerButton.disabled = false;
        }
      });
      knowledgeActions.append(markerButton);
    }
    article.append(knowledgeActions);
  }
  if (isAdmin() && message.message_kind !== "forward"
    && message.visibility?.kind !== "restricted" && state.rooms.some(
    (room) => room.status === "active" && room.conversation_id !== message.conversation_id
  )) {
    const forwardButton = makeElement("button", "message-reply-button forward-button", "转发");
    forwardButton.type = "button";
    forwardButton.addEventListener("click", () => openForwardDialog(message));
    article.append(forwardButton);
  }
  return article;
}

function updateNewMessageIndicator() {
  const awayFromBottom = state.messages.length > 0 && !isNearTimelineBottom();
  elements.newMessageIndicator.hidden = !awayFromBottom;
  const unread = Math.max(0, Number(state.unreadMessages || 0));
  elements.newMessageCount.hidden = unread <= 0;
  elements.newMessageCount.textContent = unread > 99 ? "99+" : String(unread);
  const label = unread > 0 ? `${unread} 条新消息，回到聊天室底部` : "回到聊天室底部";
  elements.newMessageIndicator.setAttribute("aria-label", label);
  elements.newMessageIndicator.title = label;
}

function messageSignature(messages, range = renderedTimelineRange(messages)) {
  const visibleMessages = messages.slice(range.start, range.end);
  const firstMessageId = messages[0]?.message_id || "";
  const lastMessageId = messages[messages.length - 1]?.message_id || "";
  return `${state.selectedRoom || ""}:${state.hasEarlierMessages}:${state.hasLaterMessages}:${state.roomSearchTargetMessageId || ""}:${roomHighlightSignature()}:${messages.length}:${firstMessageId}:${lastMessageId}:${range.start}:${range.end}:${visibleMessages.map((item) => `${item.message_id}:${item.sender_display_name || ""}:${item.sender_signature || ""}:${item.sender_avatar_key || "auto"}:${item.sender_seat || "unknown"}:${item.task?.updated_at || item.updated_at || 0}:${item.body_delivery?.delivered_count || 0}:${item.body_delivery?.applied_count || 0}:${item.ack_count || 0}:${item.receipt_count || 0}:${(item.agent_deliveries || []).map((delivery) => `${delivery.participant_id},${delivery.status},${delivery.active_endpoint ? 1 : 0},${delivery.dnd_active ? 1 : 0}`).join(";")}:${item.reply_count || 0}:${item.visibility?.kind || "room"}:${(item.attachments || []).map((asset) => asset.attachment_id).join(",")}:${(item.links || []).map((link) => link.link_id).join(",")}`).join("|")}`;
}

function renderMessages(
  messages,
  {
    forceBottom = false,
    addedCount = 0,
    targetMessageId = null,
    virtualIndex = null,
    forceVirtual = false,
  } = {},
) {
  const hadRenderedMessages = Boolean(
    elements.timeline.querySelector("article[data-message-id]"),
  );
  const previousScrollTop = elements.timeline.scrollTop;
  const previousScrollHeight = elements.timeline.scrollHeight;
  const wasNearBottom = hadRenderedMessages
    ? isNearTimelineBottom()
    : true;
  const anchor = !wasNearBottom && !forceBottom ? captureTimelineAnchor() : null;
  const range = resolveTimelineVirtualRange(messages, {
    forceBottom,
    wasNearBottom,
    targetMessageId,
    anchor,
    virtualIndex,
  });
  const signature = messageSignature(messages, range);
  if (
    !forceBottom
    && !forceVirtual
    && addedCount === 0
    && signature === state.messageRenderSignature
  ) {
    updateNewMessageIndicator();
    return;
  }
  state.messageRenderSignature = signature;
  const fragment = document.createDocumentFragment();
  if (!messages.length) {
    const empty = makeElement("div", "empty-state");
    empty.append(makeElement("h3", "", "房间里还没有消息"));
    empty.append(makeElement("p", "", "你或 Agent 发出的第一条讨论会自动出现在这里。"));
    fragment.append(empty);
    elements.timeline.replaceChildren(fragment);
    syncTimelineVirtualDom(messages);
    state.unreadMessages = 0;
    updateNewMessageIndicator();
    return;
  }

  if (state.hasEarlierMessages) {
    const loadEarlier = makeElement("button", "load-earlier-button", "加载更早消息");
    loadEarlier.type = "button";
    loadEarlier.addEventListener("click", loadEarlierMessages);
    fragment.append(loadEarlier);
  }

  if (range.virtualized) {
    fragment.append(createTimelineVirtualSpacer("top"));
    for (let index = range.start; index < range.end; index += 1) {
      fragment.append(createTimelineVirtualRow(messages[index], index, messages));
    }
    fragment.append(createTimelineVirtualSpacer("bottom"));
  } else {
    let activeDay = "";
    for (const message of messages) {
      const nextDay = dayLabel(message.created_at);
      if (nextDay !== activeDay) {
        activeDay = nextDay;
        fragment.append(makeElement("div", "day-divider", activeDay));
      }
      fragment.append(createMessageElement(message));
    }
  }
  if (state.hasLaterMessages) {
    const latest = makeElement("button", "return-latest-button", "回到最新消息");
    latest.type = "button";
    latest.addEventListener("click", loadLatestMessages);
    fragment.append(latest);
  }
  elements.timeline.replaceChildren(fragment);
  syncTimelineVirtualDom(messages);

  if (targetMessageId) {
    const requestedRoom = state.selectedRoom;
    window.requestAnimationFrame(() => {
      if (state.selectedRoom !== requestedRoom) return;
      const target = [...elements.timeline.querySelectorAll("article[data-message-id]")]
        .find((item) => item.dataset.messageId === targetMessageId);
      if (!target) return;
      const timelineRect = elements.timeline.getBoundingClientRect();
      const targetRect = target.getBoundingClientRect();
      const desiredTop = elements.timeline.scrollTop
        + targetRect.top
        - timelineRect.top
        - Math.max(16, (elements.timeline.clientHeight - targetRect.height) / 2);
      elements.timeline.scrollTop = Math.max(0, desiredTop);
      target.classList.add("search-target");
      state.unreadMessages = 0;
      updateNewMessageIndicator();
    });
  } else if (forceBottom || wasNearBottom) {
    const requestedRoom = state.selectedRoom;
    window.requestAnimationFrame(() => {
      if (state.selectedRoom !== requestedRoom) return;
      elements.timeline.scrollTop = elements.timeline.scrollHeight;
      state.unreadMessages = 0;
      updateNewMessageIndicator();
    });
  } else {
    const restored = restoreCapturedTimelineAnchor(anchor);
    if (!restored && !range.virtualized && anchor) {
      const heightDelta = elements.timeline.scrollHeight - previousScrollHeight;
      elements.timeline.scrollTop = Math.max(0, previousScrollTop + heightDelta);
    }
    state.unreadMessages += addedCount;
  }
  updateNewMessageIndicator();
}

function appendMessages(messages, { forceBottom = false } = {}) {
  if (!messages.length) return;
  prepareTimelineMessageIndexes(state.messages);
  if (
    state.timelineVirtual?.enabled
    || state.messages.length > TIMELINE_VIRTUAL_THRESHOLD
  ) {
    renderMessages(state.messages, {
      forceBottom,
      addedCount: messages.length,
    });
    return;
  }
  if (!elements.timeline.querySelector("article[data-message-id]")) {
    renderMessages(state.messages, { forceBottom, addedCount: messages.length });
    return;
  }
  const wasNearBottom = isNearTimelineBottom();
  const fragment = document.createDocumentFragment();
  const previousMessage = state.messages[state.messages.length - messages.length - 1];
  let activeDay = previousMessage ? dayLabel(previousMessage.created_at) : "";
  for (const message of messages) {
    const nextDay = dayLabel(message.created_at);
    if (nextDay !== activeDay) {
      activeDay = nextDay;
      fragment.append(makeElement("div", "day-divider", activeDay));
    }
    fragment.append(createMessageElement(message));
  }
  elements.timeline.append(fragment);
  state.messageRenderSignature = messageSignature(state.messages);
  if (forceBottom || wasNearBottom) {
    const requestedRoom = state.selectedRoom;
    window.requestAnimationFrame(() => {
      if (state.selectedRoom !== requestedRoom) return;
      elements.timeline.scrollTop = elements.timeline.scrollHeight;
      state.unreadMessages = 0;
      updateNewMessageIndicator();
    });
  } else {
    state.unreadMessages += messages.length;
    updateNewMessageIndicator();
  }
}

function updateReceiptLabels(messages) {
  prepareTimelineMessageIndexes(messages);
  for (const article of elements.timeline.querySelectorAll("article[data-message-id]")) {
    const message = timelineMessageAt(article.dataset.messageId);
    if (!message) continue;
    const label = article.querySelector(".receipt-label");
    if (label) {
      label.textContent = deliverySummaryText(message);
    }
    const details = article.querySelector(".message-delivery-details");
    const body = details?.querySelector(".message-delivery-body");
    if (details?.open && body) {
      populateDeliveryDetails(body, message);
    }
  }
  state.messageRenderSignature = messageSignature(state.messages);
}

function isWebParticipant(person) {
  return person.client_type.startsWith("web-user");
}

function isDormantParticipant(person) {
  return person.room_status === "active"
    && person.membership_active
    && !isWebParticipant(person)
    && Number(person.active_session_count || 0) === 0
    && !person.connector_id;
}

function participantMatchesQuery(person, query) {
  if (!query) return true;
  if (isWebParticipant(person)) return false;
  const haystack = `${person.display_name || ""} ${person.client_type || ""}`
    .toLocaleLowerCase("zh-CN");
  return haystack.includes(query);
}

function createParticipantCard(person) {
  const archived = person.room_status === "abandoned" || !person.membership_active;
  const card = makeElement("article", `person-card${archived ? " archived" : ""}`);
  const head = makeElement("div", "person-head");
  const username = person.client_type.includes("-")
    ? person.client_type.slice(person.client_type.indexOf("-") + 1)
    : person.client_type;
  const initial = Array.from(username)[0] || "A";
  head.append(createAvatarElement({
    avatarKey: person.avatar_key,
    clientType: person.client_type,
    label: person.display_name || initial,
    status: person.status,
  }));
  const name = makeElement("div", "person-name");
  name.append(makeElement("strong", "", person.display_name || person.client_type));
  name.append(makeElement("span", "", person.signature || "未填写签名"));
  name.append(makeElement("span", "identity-line", person.client_type));
  head.append(name);
  head.append(makeElement("span", `presence-dot ${person.status}`));
  const isWebUser = isWebParticipant(person);
  const actions = makeElement("div", "person-actions");
  if (!isWebUser && !archived) {
    const mention = makeElement("button", "mention-button", "@ 通知");
    mention.type = "button";
    mention.title = `特别通知 ${person.display_name || person.client_type}`;
    mention.setAttribute("aria-label", `特别通知 ${person.display_name || person.client_type}`);
    mention.addEventListener("click", () => addComposerMention(person));
    actions.append(mention);
    const activeRoom = state.rooms.find(
      (room) => room.conversation_id === state.selectedRoom,
    );
    if (activeRoom?.can_kick_agents && state.selectedRoom) {
      const kick = makeElement("button", "person-kick-button", "移出");
      kick.type = "button";
      kick.title = `将 ${person.display_name || person.client_type} 踢出当前聊天室`;
      kick.setAttribute("aria-label", `将 ${person.display_name || person.client_type} 移出当前聊天室`);
      kick.addEventListener("click", () => kickAgentFromRoom(
        state.selectedRoom,
        person,
        kick,
      ));
      actions.append(kick);
    }
  }
  card.append(head);
  if (actions.childElementCount) card.append(actions);
  if (person.roles.length) {
    const roles = makeElement("div", "roles");
    for (const role of person.roles) roles.append(makeElement("span", "role-chip", role));
    card.append(roles);
  }
  let authLabel;
  if (isWebUser) {
    authLabel = "网页用户";
  } else if (person.native_tui?.state === "busy") {
    authLabel = `真实 TUI 执行中 · ${person.connector_adapter_kind}`;
  } else if (person.native_tui?.state === "waiting_approval") {
    authLabel = `真实 TUI 等待本机确认 · ${person.connector_adapter_kind}`;
  } else if (person.native_tui?.state === "online") {
    authLabel = `真实 TUI 值守在线 · ${person.connector_adapter_kind}`;
  } else if (person.native_tui?.state === "error") {
    authLabel = `真实 TUI 异常 · ${person.connector_adapter_kind}`;
  } else if (person.native_tui?.state === "offline" && person.native_tui?.endpoint_id) {
    authLabel = `真实 TUI 当前离线 · listener 仍会保留消息 · ${person.connector_adapter_kind}`;
  } else if (person.native_tui?.state === "awaiting_confirmation") {
    authLabel = `真实 TUI 等待本机确认绑定 · ${person.connector_adapter_kind}`;
  } else if (person.resident_status === "online") {
    authLabel = person.local_resident?.task_running
      ? `聊天与任务值守在线 · ${person.connector_adapter_kind}`
      : `聊天值守在线 · 任务席位升级中 · ${person.connector_adapter_kind}`;
  } else if (person.resident_status === "degraded") {
    authLabel = `本机值守异常 · 可点“修复值守”自愈 · ${person.connector_adapter_kind}`;
  } else if (person.resident_status === "offline") {
    authLabel = `已配置自动值守 · 当前离线 · ${person.connector_adapter_kind}`;
  } else if (person.resident_status === "failed") {
    authLabel = "值守配置失败 · MCP 仍可手动接入";
  } else if (person.resident_status === "manual") {
    authLabel = "基础接入 · 未配置自动唤醒";
  } else {
    authLabel = person.active_session_count > 0 ? "MCP 会话有效 · 未确认常驻值守" : "无有效 MCP 会话";
  }
  const authenticated = isWebUser || person.active_session_count > 0 || person.connector_id;
  card.append(makeElement("p", `membership-label${authenticated ? " authenticated" : ""}`, authLabel));
  if (isDormantParticipant(person) && person.inactivity_expires_at) {
    card.append(makeElement(
      "p",
      "membership-label member-expiry-label",
      `未重新发言则 ${fullTime(person.inactivity_expires_at)} 自动移出`,
    ));
  }
  if (archived) card.append(makeElement("p", "membership-label", "历史成员 · 已不可进入"));
  return card;
}

function populateRoomMessageSearchParticipants(participants) {
  const selected = elements.roomMessageSearchParticipant.value;
  const fragment = document.createDocumentFragment();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部发言人";
  fragment.append(all);
  for (const participant of participants) {
    const option = document.createElement("option");
    option.value = participant.participant_id;
    option.textContent = participant.display_name || participant.client_type;
    fragment.append(option);
  }
  elements.roomMessageSearchParticipant.replaceChildren(fragment);
  if ([...elements.roomMessageSearchParticipant.options].some(
    (option) => option.value === selected,
  )) {
    elements.roomMessageSearchParticipant.value = selected;
  }
}

function renderParticipants(participants) {
  state.participants = participants;
  state.participantById = new Map(
    participants.map((participant) => [participant.participant_id, participant]),
  );
  populateRoomMessageSearchParticipants(participants);
  const query = state.participantFilter.trim().toLocaleLowerCase("zh-CN");
  const signature = `${state.selectedRoom || ""}|${query}|${participants.map((item) => [
    item.participant_id,
    item.status,
    item.membership_active,
    item.resident_status,
    item.local_resident?.task_configured || false,
    item.local_resident?.task_running || false,
    item.native_tui?.state || "",
    item.native_tui?.active_task_id || "",
    item.active_session_count,
    item.connector_id || "",
    item.inactivity_expires_at || "",
    item.display_name,
    item.signature,
    item.avatar_key || "auto",
  ].join(":")).join("|")}`;
  if (signature === state.participantRenderSignature) return;
  state.participantRenderSignature = signature;
  elements.peopleList.replaceChildren();
  const matching = participants.filter((person) => participantMatchesQuery(person, query));
  const dormant = matching.filter(isDormantParticipant);
  const current = matching.filter((person) => !isDormantParticipant(person));
  elements.participantCount.textContent = String(query ? matching.length : participants.length);
  elements.participantCount.title = query
    ? `${matching.length} 个匹配，共 ${participants.length} 个成员`
    : dormant.length
      ? `${current.length} 个当前可用，${dormant.length} 个无有效接入`
      : `${participants.length} 个成员`;
  if (!participants.length) {
    elements.peopleList.append(makeElement("p", "muted-copy", "这个聊天室还没有活跃成员。"));
    return;
  }
  if (query) {
    if (!matching.length) {
      elements.peopleList.append(makeElement("p", "participant-search-empty", "当前聊天室没有匹配的 Agent。"));
      return;
    }
    elements.peopleList.append(makeElement("p", "participant-search-result", `搜索结果 · ${matching.length} 个 Agent`));
    for (const person of matching) elements.peopleList.append(createParticipantCard(person));
    return;
  }
  for (const person of current) elements.peopleList.append(createParticipantCard(person));
  if (!dormant.length) return;

  const room = state.selectedRoom || "";
  const group = makeElement("details", "dormant-member-group");
  group.open = state.expandedDormantRooms.has(room);
  const summary = makeElement("summary", "dormant-member-summary");
  summary.append(makeElement("strong", "", `无有效接入成员（${dormant.length}）`));
  summary.append(makeElement("span", "", "到期自动移出"));
  group.append(summary);
  const list = makeElement("div", "dormant-member-list");
  list.append(makeElement(
    "p",
    "dormant-member-note",
    "这些成员目前无法即时唤醒；重新接入并发言会续期，否则按管理员设置的期限自动移出。历史消息会保留。",
  ));
  for (const person of dormant) list.append(createParticipantCard(person));
  group.append(list);
  group.addEventListener("toggle", () => {
    if (!room) return;
    if (group.open) state.expandedDormantRooms.add(room);
    else state.expandedDormantRooms.delete(room);
  });
  elements.peopleList.append(group);
}
