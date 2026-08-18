"use strict";


function applyMessageRateConfiguration() {
  const globals = state.rateConfiguration?.globals || {};
  elements.agentGlobalRate.value = String(globals.agent?.cooldown_seconds ?? 15);
  elements.webUserGlobalRate.value = String(globals.web_user?.cooldown_seconds ?? 60);
  const maximum = Number(state.rateConfiguration?.maximum_cooldown_seconds || 86400);
  elements.agentGlobalRate.max = String(maximum);
  elements.webUserGlobalRate.max = String(maximum);
}

function renderMessageRateParticipants() {
  elements.messageRateResults.replaceChildren();
  if (!state.rateParticipants.length) {
    elements.messageRateResults.append(makeElement("p", "muted-copy", "没有匹配的 Agent 或普通用户。"));
    return;
  }

  const maximum = Number(state.rateConfiguration?.maximum_cooldown_seconds || 86400);
  for (const participant of state.rateParticipants) {
    const card = makeElement("article", "rate-result-card");
    const heading = makeElement("div", "rate-result-heading");
    const identity = makeElement("div", "rate-result-identity");
    identity.append(makeElement("strong", "", participant.display_name));
    identity.append(makeElement(
      "span",
      "",
      participant.actor_kind === "agent"
        ? `Agent · ${participant.client_type}`
        : `普通用户 · ${participant.username}`,
    ));
    if (participant.signature) identity.append(makeElement("small", "", participant.signature));
    heading.append(identity);
    heading.append(makeElement(
      "span",
      "rate-effective-badge",
      `当前 ${formatCooldown(participant.effective_cooldown_seconds)}`,
    ));
    card.append(heading);

    const individualLabel = participant.individual_cooldown_seconds === null
      ? "未设置"
      : formatCooldown(participant.individual_cooldown_seconds);
    card.append(makeElement(
      "p",
      "rate-result-meta",
      `整体 ${formatCooldown(participant.global_cooldown_seconds)} · 单独 ${individualLabel} · 取较短值`,
    ));

    const controls = makeElement("div", "rate-result-controls");
    const inputWrap = makeElement("label", "rate-number-wrap rate-override-wrap");
    const input = makeElement("input", "rate-override-input");
    input.type = "number";
    input.min = "0";
    input.max = String(maximum);
    input.step = "0.001";
    input.placeholder = `整体 ${participant.global_cooldown_seconds}`;
    input.setAttribute("aria-label", `${participant.display_name}的单独发言间隔秒数`);
    if (participant.individual_cooldown_seconds !== null) {
      input.value = String(participant.individual_cooldown_seconds);
    }
    inputWrap.append(input, makeElement("small", "", "秒"));

    const clear = makeElement("button", "secondary-button compact-button", "恢复整体");
    clear.type = "button";
    clear.hidden = participant.individual_cooldown_seconds === null;
    const save = makeElement("button", "primary-button compact-button", "保存单独值");
    save.type = "button";

    const setBusy = (busy) => {
      input.disabled = busy;
      clear.disabled = busy;
      save.disabled = busy;
    };
    save.addEventListener("click", async () => {
      if (!input.value || !input.reportValidity()) return;
      setBusy(true);
      elements.messageRateSearchFeedback.classList.remove("error", "success");
      elements.messageRateSearchFeedback.textContent = `正在保存 ${participant.display_name} 的单独设置…`;
      try {
        await fetchJson(`/api/message-rates/participants/${encodeURIComponent(participant.participant_id)}`, {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-Agent-Bridge-Intent": "set-participant-message-rate",
          },
          body: JSON.stringify({ cooldown_seconds: Number(input.value) }),
        });
        await searchMessageRateParticipants();
        elements.messageRateSearchFeedback.classList.add("success");
        elements.messageRateSearchFeedback.textContent = `${participant.display_name} 的单独设置已保存。`;
      } catch (error) {
        elements.messageRateSearchFeedback.classList.add("error");
        elements.messageRateSearchFeedback.textContent = error.message;
        setBusy(false);
      }
    });
    clear.addEventListener("click", async () => {
      setBusy(true);
      elements.messageRateSearchFeedback.classList.remove("error", "success");
      elements.messageRateSearchFeedback.textContent = `正在恢复 ${participant.display_name} 的整体设置…`;
      try {
        await fetchJson(`/api/message-rates/participants/${encodeURIComponent(participant.participant_id)}`, {
          method: "DELETE",
          headers: { "X-Agent-Bridge-Intent": "clear-participant-message-rate" },
        });
        await searchMessageRateParticipants();
        elements.messageRateSearchFeedback.classList.add("success");
        elements.messageRateSearchFeedback.textContent = `${participant.display_name} 已恢复使用整体设置。`;
      } catch (error) {
        elements.messageRateSearchFeedback.classList.add("error");
        elements.messageRateSearchFeedback.textContent = error.message;
        setBusy(false);
      }
    });
    controls.append(inputWrap, clear, save);
    card.append(controls);
    elements.messageRateResults.append(card);
  }
}

async function loadMessageRateConfiguration() {
  const payload = await fetchJson("/api/message-rates");
  state.rateConfiguration = payload;
  applyMessageRateConfiguration();
}

async function searchMessageRateParticipants() {
  const params = new URLSearchParams({
    query: elements.messageRateSearch.value.trim(),
    actor_kind: elements.messageRateKind.value,
    limit: "50",
  });
  const payload = await fetchJson(`/api/message-rates/participants/search?${params.toString()}`);
  state.rateParticipants = payload.participants;
  renderMessageRateParticipants();
}

async function openMessageRateDialog() {
  if (!isAdmin()) return;
  elements.messageRateGlobalFeedback.textContent = "";
  elements.messageRateGlobalFeedback.classList.remove("error", "success");
  elements.messageRateSearchFeedback.textContent = "正在载入设置…";
  elements.messageRateSearchFeedback.classList.remove("error", "success");
  state.rateParticipants = [];
  elements.messageRateResults.replaceChildren();
  if (!elements.messageRateDialog.open) elements.messageRateDialog.showModal();
  try {
    await Promise.all([
      loadMessageRateConfiguration(),
      searchMessageRateParticipants(),
    ]);
    elements.messageRateSearchFeedback.textContent = "";
    window.setTimeout(() => elements.messageRateSearch.focus(), 0);
  } catch (error) {
    elements.messageRateSearchFeedback.classList.add("error");
    elements.messageRateSearchFeedback.textContent = error.message;
  }
}

function renderRoomPermissionUsers() {
  elements.roomPermissionResults.replaceChildren();
  if (!state.roomPermissionUsers.length) {
    elements.roomPermissionResults.append(makeElement("p", "muted-copy", "没有匹配的普通用户。"));
    return;
  }
  for (const user of state.roomPermissionUsers) {
    const card = makeElement("article", "rate-result-card");
    const heading = makeElement("div", "rate-result-heading");
    const identity = makeElement("div", "rate-result-identity");
    identity.append(makeElement("strong", "", user.display_name));
    identity.append(makeElement("span", "", `@${user.username} · 使用中 ${user.owned_active_room_count}/${user.room_limit} 个`));
    if (user.signature) identity.append(makeElement("small", "", user.signature));
    heading.append(identity);
    heading.append(makeElement(
      "span",
      "rate-effective-badge",
      user.can_create_rooms ? "已授权" : "未授权",
    ));
    card.append(heading);

    const controls = makeElement("div", "rate-result-controls");
    const permissionLabel = makeElement("label", "room-permission-toggle");
    const permission = makeElement("input", "");
    permission.type = "checkbox";
    permission.checked = user.can_create_rooms;
    permissionLabel.append(permission, makeElement("span", "", "允许创建聊天室"));
    const limitWrap = makeElement("label", "rate-number-wrap rate-override-wrap");
    const limit = makeElement("input", "rate-override-input");
    limit.type = "number";
    limit.min = "1";
    limit.max = "100";
    limit.step = "1";
    limit.value = String(user.room_limit);
    limit.setAttribute("aria-label", `${user.display_name}的聊天室上限`);
    limitWrap.append(limit, makeElement("small", "", "个"));
    const save = makeElement("button", "primary-button compact-button", "保存");
    save.type = "button";
    save.addEventListener("click", async () => {
      if (!limit.reportValidity()) return;
      permission.disabled = true;
      limit.disabled = true;
      save.disabled = true;
      elements.roomPermissionFeedback.classList.remove("error", "success");
      elements.roomPermissionFeedback.textContent = `正在保存 ${user.display_name} 的权限…`;
      try {
        await fetchJson(`/api/admin/web-users/${encodeURIComponent(user.user_id)}/room-permission`, {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            "X-Agent-Bridge-Intent": "manage-room-permission",
          },
          body: JSON.stringify({
            can_create_rooms: permission.checked,
            room_limit: Number(limit.value),
          }),
        });
        await searchRoomPermissionUsers();
        elements.roomPermissionFeedback.classList.add("success");
        elements.roomPermissionFeedback.textContent = `${user.display_name} 的建房权限已保存。`;
      } catch (error) {
        elements.roomPermissionFeedback.classList.add("error");
        elements.roomPermissionFeedback.textContent = error.message;
        permission.disabled = false;
        limit.disabled = false;
        save.disabled = false;
      }
    });
    controls.append(permissionLabel, limitWrap, save);
    card.append(controls);
    elements.roomPermissionResults.append(card);
  }
}

async function searchRoomPermissionUsers() {
  const params = new URLSearchParams({
    query: elements.roomPermissionSearch.value.trim(),
    limit: "50",
  });
  const payload = await fetchJson(`/api/admin/web-users/room-permissions?${params.toString()}`);
  state.roomPermissionUsers = payload.users;
  renderRoomPermissionUsers();
}

async function openRoomPermissionDialog() {
  if (!isAdmin()) return;
  state.roomPermissionUsers = [];
  elements.roomPermissionResults.replaceChildren();
  elements.roomPermissionFeedback.classList.remove("error", "success");
  elements.roomPermissionFeedback.textContent = "正在载入普通用户…";
  elements.roomPermissionDialog.showModal();
  try {
    await searchRoomPermissionUsers();
    elements.roomPermissionFeedback.textContent = "";
    window.setTimeout(() => elements.roomPermissionSearch.focus(), 0);
  } catch (error) {
    elements.roomPermissionFeedback.classList.add("error");
    elements.roomPermissionFeedback.textContent = error.message;
  }
}

function renderRegistrationCodes() {
  elements.registrationCodeList.replaceChildren();
  if (!state.registrationCodes.length) {
    elements.registrationCodeList.append(
      makeElement("p", "muted-copy", "还没有生成过注册码。"),
    );
    return;
  }
  const statusLabels = {
    active: "可用",
    exhausted: "已用完",
    expired: "已过期",
    revoked: "已撤销",
  };
  for (const code of state.registrationCodes) {
    const card = makeElement("article", "rate-result-card registration-code-card");
    const heading = makeElement("div", "rate-result-heading");
    const identity = makeElement("div", "rate-result-identity");
    identity.append(makeElement("strong", "", code.label || "未备注注册码"));
    identity.append(makeElement(
      "span",
      "",
      `已使用 ${code.use_count}/${code.max_uses} 次 · ${fullTime(code.created_at)} 创建`,
    ));
    identity.append(makeElement("small", "", `有效期至 ${fullTime(code.expires_at)}`));
    heading.append(identity);
    heading.append(makeElement(
      "span",
      `rate-effective-badge registration-code-status ${code.status}`,
      statusLabels[code.status] || code.status,
    ));
    card.append(heading);
    if (code.status === "active") {
      const controls = makeElement("div", "rate-result-controls");
      const revoke = makeElement("button", "revoke-button", "撤销");
      revoke.type = "button";
      revoke.addEventListener("click", async () => {
        revoke.disabled = true;
        elements.registrationCodeFeedback.classList.remove("error", "success");
        elements.registrationCodeFeedback.textContent = "正在撤销注册码…";
        try {
          await fetchJson(
            `/api/admin/web-registration-codes/${encodeURIComponent(code.code_id)}/revoke`,
            {
              method: "POST",
              headers: { "X-Agent-Bridge-Intent": "revoke-registration-code" },
            },
          );
          await loadRegistrationCodes();
          elements.registrationCodeFeedback.classList.add("success");
          elements.registrationCodeFeedback.textContent = "注册码已撤销，之后不能再使用。";
        } catch (error) {
          elements.registrationCodeFeedback.classList.add("error");
          elements.registrationCodeFeedback.textContent = error.message;
          revoke.disabled = false;
        }
      });
      controls.append(revoke);
      card.append(controls);
    }
    elements.registrationCodeList.append(card);
  }
}

async function loadRegistrationCodes() {
  const payload = await fetchJson("/api/admin/web-registration-codes?limit=100");
  state.registrationCodes = payload.codes || [];
  renderRegistrationCodes();
}

async function openRegistrationCodeDialog() {
  if (!isAdmin()) return;
  state.generatedRegistrationCode = "";
  elements.generatedRegistrationCode.textContent = "";
  elements.registrationCodeOutput.hidden = true;
  elements.registrationCodeFeedback.classList.remove("error", "success");
  elements.registrationCodeFeedback.textContent = "正在载入注册码…";
  elements.registrationCodeList.replaceChildren();
  if (!elements.registrationCodeDialog.open) {
    elements.registrationCodeDialog.showModal();
  }
  try {
    await loadRegistrationCodes();
    elements.registrationCodeFeedback.textContent = "";
    window.setTimeout(() => elements.registrationCodeLabel.focus(), 0);
  } catch (error) {
    elements.registrationCodeFeedback.classList.add("error");
    elements.registrationCodeFeedback.textContent = error.message;
  }
}

const ADMIN_AUDIT_CATEGORY_LABELS = {
  access: "接入与凭证",
  authorization: "授权",
  connector: "连接与值守",
  identity: "身份与昵称",
  history: "历史治理",
  knowledge: "知识与转发",
  lifecycle: "生命周期",
  membership: "成员",
  monitoring: "监控",
  permission: "权限",
  policy: "策略",
  rate_limit: "发言频率",
  room: "聊天室",
  session: "会话",
  task: "任务",
};

const ADMIN_AUDIT_ACTION_LABELS = {
  "a2a_grant.create": "创建 A2A 接入授权",
  "a2a_grant.revoke": "撤销 A2A 接入授权",
  "registration_code.create": "生成注册码",
  "registration_code.revoke": "撤销注册码",
  "room.create": "创建聊天室",
  "room.rename": "更改聊天室名称",
  "room_creation_permission.update": "调整普通用户建房权限",
  "web_room_member.upsert": "加入或调整 Web 成员",
  "web_room_member.remove": "移出 Web 成员",
  "agent_lifecycle.update": "调整 Agent 有效期",
  "monitoring_alert.acknowledge": "确认运行告警",
  "connector.rotation_request": "请求轮换连接凭证",
  "connector.revoke": "撤销连接设备",
  "agent_room_member.kick": "将 Agent 踢出聊天室",
  "agent_room_member.copy": "复制 Agent 到其他聊天室",
  "message_rate.global_update": "调整总体发言频率",
  "message_rate.override_set": "设置单个对象发言频率",
  "message_rate.override_clear": "清除单个对象发言频率",
  "agent_invitation.create": "创建 Agent 邀请",
  "agent_invitation.revoke": "撤销 Agent 邀请",
  "session.cleanup": "清理失效会话",
  "session.revoke": "撤销 Agent 会话",
  "message_marker.set": "设置消息标记",
  "message_marker.remove": "移除消息标记",
  "task.create": "创建聊天室任务",
  "task.convert_from_message": "把消息转为任务",
  "wake_policy.update": "调整聊天室唤醒策略",
  "task_policy.update": "调整聊天室任务策略",
  "task_grant.update": "调整用户任务权限",
  "task.cancel": "取消聊天室任务",
  "chat_authorization.revoke": "撤销聊天授权",
  "message.forward": "跨聊天室转发消息",
  "room_residents.repair": "修复聊天室值守",
  "nickname_request.review": "审批 Agent 昵称",
  "history.export": "导出聊天室历史",
  "history.retention_policy.update": "调整历史保留策略",
  "history.redaction.preview": "预览旧正文清除范围",
  "history.redaction.execute": "清除旧消息正文",
};

function populateAdminAuditFacets(payload) {
  const selectedCategory = elements.adminAuditCategory.value;
  const selectedActor = elements.adminAuditActor.value;
  elements.adminAuditCategory.replaceChildren(new Option("全部类别", ""));
  for (const item of payload.facets?.categories || []) {
    elements.adminAuditCategory.append(new Option(
      `${ADMIN_AUDIT_CATEGORY_LABELS[item.category] || item.category} · ${item.count}`,
      item.category,
    ));
  }
  elements.adminAuditCategory.value = selectedCategory;
  elements.adminAuditActor.replaceChildren(new Option("全部人员", ""));
  for (const actor of payload.facets?.actors || []) {
    elements.adminAuditActor.append(new Option(
      `${actor.display_name} (${actor.username}) · ${actor.count}`,
      actor.user_id,
    ));
  }
  elements.adminAuditActor.value = selectedActor;
}

function renderAdminAudit() {
  const payload = state.adminAudit;
  elements.adminAuditSummary.replaceChildren();
  elements.adminAuditList.replaceChildren();
  const summaryItems = [
    [payload.summary?.total_count || 0, "累计记录"],
    [payload.summary?.success_count || 0, "成功"],
    [payload.summary?.denied_count || 0, "被拒绝"],
    [payload.summary?.failed_count || 0, "失败"],
  ];
  for (const [value, label] of summaryItems) {
    const card = makeElement(
      "span",
      `connector-health-summary-card ${label === "失败" && Number(value) ? "failed" : ""}`,
    );
    card.append(
      makeElement("strong", "", String(value)),
      makeElement("small", "", label),
    );
    elements.adminAuditSummary.append(card);
  }
  if (!payload.events.length) {
    elements.adminAuditList.append(makeElement("p", "muted-copy", "当前筛选范围没有审计记录。"));
  }
  const outcomeLabels = { success: "成功", denied: "被拒绝", failed: "失败" };
  for (const event of payload.events) {
    const card = makeElement("article", `audit-event-card ${event.outcome}`);
    const heading = makeElement("div", "audit-event-heading");
    heading.append(
      makeElement(
        "strong",
        "",
        ADMIN_AUDIT_ACTION_LABELS[event.action] || event.action,
      ),
      makeElement(
        "span",
        `audit-event-outcome ${event.outcome}`,
        `${outcomeLabels[event.outcome] || event.outcome} · HTTP ${event.status_code}`,
      ),
    );
    const actor = `${event.actor_display_name} (${event.actor_username})`;
    const category = ADMIN_AUDIT_CATEGORY_LABELS[event.category] || event.category;
    const targetParts = [];
    if (event.conversation_id) targetParts.push(`聊天室：${event.conversation_id}`);
    if (event.target_id) targetParts.push(`${event.target_kind}：${event.target_id}`);
    const meta = makeElement("div", "audit-event-meta");
    meta.append(
      makeElement("span", "", `#${event.sequence} · ${category} · ${actor} · ${fullTime(event.occurred_at)}`),
      makeElement("span", "audit-event-target", targetParts.join(" · ") || "全局操作"),
    );
    card.append(
      heading,
      meta,
      makeElement("small", "audit-event-request", `${event.http_method} ${event.route} · ${event.request_id}`),
    );
    elements.adminAuditList.append(card);
  }
  elements.loadMoreAdminAudit.hidden = !payload.has_more;
  elements.adminAuditFeedback.classList.remove("error");
  elements.adminAuditFeedback.textContent = `已显示 ${payload.events.length} 条 · 日志只追加，敏感正文和凭证不入库`;
}

async function loadAdminAudit({ append = false } = {}) {
  if (!isAdmin()) return;
  const parameters = new URLSearchParams({
    limit: "100",
    query: elements.adminAuditQuery.value.trim(),
    category: elements.adminAuditCategory.value,
    outcome: elements.adminAuditOutcome.value,
    actor_web_user_id: elements.adminAuditActor.value,
    conversation_id: elements.adminAuditRoom.value.trim(),
    hours: elements.adminAuditHours.value,
  });
  if (append && state.adminAudit.next_before_sequence) {
    parameters.set("before_sequence", String(state.adminAudit.next_before_sequence));
  }
  elements.refreshAdminAudit.disabled = true;
  elements.loadMoreAdminAudit.disabled = true;
  elements.adminAuditFeedback.classList.remove("error");
  elements.adminAuditFeedback.textContent = append ? "正在加载更早记录…" : "正在读取审计记录…";
  try {
    const payload = await fetchJson(`/api/admin/audit?${parameters.toString()}`);
    const events = append
      ? [...state.adminAudit.events, ...(payload.events || [])]
      : (payload.events || []);
    state.adminAudit = { ...payload, events };
    populateAdminAuditFacets(payload);
    renderAdminAudit();
  } catch (error) {
    elements.adminAuditFeedback.classList.add("error");
    elements.adminAuditFeedback.textContent = error.message;
  } finally {
    elements.refreshAdminAudit.disabled = false;
    elements.loadMoreAdminAudit.disabled = false;
  }
}

async function openAdminAuditDialog() {
  if (!isAdmin()) return;
  elements.globalToolsMenu.open = false;
  if (!elements.adminAuditDialog.open) elements.adminAuditDialog.showModal();
  await loadAdminAudit();
}

function populateHistoryRoomOptions() {
  const previousSearch = elements.historySearchRoom.value;
  const previousExport = elements.historyExportRoom.value || state.selectedRoom || "";
  const previousRedaction = elements.historyRedactionRoom.value;
  const rooms = [...state.rooms].sort((left, right) => (
    left.conversation_id.localeCompare(right.conversation_id, "zh-CN")
  ));
  elements.historySearchRoom.replaceChildren(new Option("全部聊天室", ""));
  elements.historyExportRoom.replaceChildren();
  elements.historyRedactionRoom.replaceChildren(new Option("全部已废弃聊天室", ""));
  for (const room of rooms) {
    const status = room.status === "abandoned" ? "已废弃" : "使用中";
    const label = `${room.conversation_id} · ${status}`;
    elements.historySearchRoom.append(new Option(label, room.conversation_id));
    elements.historyExportRoom.append(new Option(label, room.conversation_id));
    if (room.status === "abandoned") {
      elements.historyRedactionRoom.append(new Option(label, room.conversation_id));
    }
  }
  if ([...elements.historySearchRoom.options].some((option) => option.value === previousSearch)) {
    elements.historySearchRoom.value = previousSearch;
  }
  if ([...elements.historyExportRoom.options].some((option) => option.value === previousExport)) {
    elements.historyExportRoom.value = previousExport;
  }
  if ([...elements.historyRedactionRoom.options].some((option) => option.value === previousRedaction)) {
    elements.historyRedactionRoom.value = previousRedaction;
  }
  elements.exportRoomHistory.disabled = rooms.length === 0;
}

function optionalHistoryTimestamp(input) {
  if (!input.value) return null;
  const milliseconds = new Date(input.value).getTime();
  if (!Number.isFinite(milliseconds)) throw new Error("搜索时间格式无效");
  return milliseconds / 1000;
}

function renderHistorySearchResults() {
  const history = state.historyGovernance;
  elements.historySearchResults.replaceChildren();
  if (!history.searchResults.length) {
    elements.historySearchResults.append(
      makeElement("p", "muted-copy", "当前条件没有匹配消息。"),
    );
  }
  const kindLabels = { message: "消息", task: "任务", forward: "转发" };
  for (const result of history.searchResults) {
    const card = makeElement(
      "button",
      `history-result-card ${result.content_redacted ? "redacted" : ""}`,
    );
    card.type = "button";
    const heading = makeElement("span", "history-result-heading");
    heading.append(
      makeElement(
        "strong",
        "",
        `${result.conversation_id} · #${roomSequence(result)} · ${result.sender_display_name}`,
      ),
      makeElement(
        "span",
        "",
        `${kindLabels[result.message_kind] || result.message_kind} · ${fullTime(result.created_at)}`,
      ),
    );
    const body = makeElement("span", "history-result-body", result.body_preview || "（空正文）");
    const markers = (result.marker_kinds || []).join("、");
    const meta = makeElement("span", "history-result-meta");
    meta.append(
      makeElement(
        "span",
        "",
        result.content_redacted ? "正文已清除，消息记录保留" : (markers ? `标记：${markers}` : result.sender_client_type),
      ),
      makeElement("span", "", result.room_status === "abandoned" ? "已废弃聊天室" : "使用中"),
    );
    card.append(heading, body, meta);
    card.addEventListener("click", async () => {
      elements.historyGovernanceDialog.close();
      await locatePendingCenterItem({
        conversation_id: result.conversation_id,
        sequence: result.sequence,
        message_id: result.message_id,
      });
    });
    elements.historySearchResults.append(card);
  }
  elements.historySearchMore.hidden = !history.searchHasMore;
}

async function loadHistorySearch({ append = false } = {}) {
  if (!isAdmin()) return;
  let createdAfter;
  let createdBefore;
  try {
    createdAfter = optionalHistoryTimestamp(elements.historySearchFrom);
    createdBefore = optionalHistoryTimestamp(elements.historySearchTo);
    if (createdAfter && createdBefore && createdAfter >= createdBefore) {
      throw new Error("开始时间必须早于结束时间");
    }
  } catch (error) {
    elements.historySearchFeedback.classList.add("error");
    elements.historySearchFeedback.textContent = error.message;
    return;
  }
  const parameters = new URLSearchParams({
    limit: "50",
    q: elements.historySearchQuery.value.trim(),
    conversation_id: elements.historySearchRoom.value,
    sender: elements.historySearchSender.value.trim(),
    message_kind: elements.historySearchKind.value,
  });
  if (createdAfter !== null) parameters.set("created_after", String(createdAfter));
  if (createdBefore !== null) parameters.set("created_before", String(createdBefore));
  if (append && state.historyGovernance.searchNextBefore) {
    parameters.set("before_sequence", String(state.historyGovernance.searchNextBefore));
  }
  elements.historySearchMore.disabled = true;
  elements.historySearchFeedback.classList.remove("error", "success");
  elements.historySearchFeedback.textContent = append ? "正在加载更早结果…" : "正在跨聊天室检索…";
  try {
    const payload = await fetchJson(`/api/admin/history/search?${parameters.toString()}`);
    const nextResults = payload.results || [];
    state.historyGovernance.searchResults = append
      ? [...state.historyGovernance.searchResults, ...nextResults]
      : nextResults;
    state.historyGovernance.searchHasMore = Boolean(payload.has_more);
    state.historyGovernance.searchNextBefore = payload.next_before_sequence;
    renderHistorySearchResults();
    elements.historySearchFeedback.textContent = `已显示 ${state.historyGovernance.searchResults.length} 条跨聊天室结果`;
  } catch (error) {
    elements.historySearchFeedback.classList.add("error");
    elements.historySearchFeedback.textContent = error.message;
  } finally {
    elements.historySearchMore.disabled = false;
  }
}

async function loadHistoryRetention() {
  const payload = await fetchJson("/api/admin/history/retention");
  state.historyGovernance.retention = payload;
  elements.historyRetentionMode.value = payload.policy.mode;
  elements.historyRetentionDays.value = String(payload.policy.retention_days);
  const modeCopy = payload.policy.mode === "forever"
    ? "当前永久保留；没有任何自动清除。"
    : `当前允许手动清除 ${payload.policy.retention_days} 天前、已废弃聊天室的正文；共有 ${payload.eligible_message_count} 条当前符合条件。`;
  elements.historyRetentionFeedback.classList.remove("error", "success");
  elements.historyRetentionFeedback.textContent = modeCopy;
  return payload;
}

function resetHistoryRedactionPreview() {
  state.historyGovernance.redactionPreview = null;
  elements.historyRedactionConfirm.hidden = true;
  elements.historyRedactionSummary.textContent = "";
  elements.historyRedactionPhrase.textContent = "";
  elements.historyRedactionConfirmation.value = "";
}

async function openHistoryGovernanceDialog() {
  if (!isAdmin()) return;
  elements.globalToolsMenu.open = false;
  populateHistoryRoomOptions();
  resetHistoryRedactionPreview();
  elements.historySearchFeedback.textContent = "";
  elements.historyExportFeedback.textContent = "";
  elements.historyRedactionFeedback.textContent = "";
  if (!elements.historyGovernanceDialog.open) {
    elements.historyGovernanceDialog.showModal();
  }
  const results = await Promise.allSettled([
    loadHistoryRetention(),
    loadHistorySearch(),
  ]);
  if (results[0].status === "rejected") {
    elements.historyRetentionFeedback.classList.add("error");
    elements.historyRetentionFeedback.textContent = results[0].reason.message;
  }
  window.setTimeout(() => elements.historySearchQuery.focus(), 0);
}

async function downloadRoomHistory() {
  const roomId = elements.historyExportRoom.value;
  if (!roomId) return;
  elements.exportRoomHistory.disabled = true;
  elements.historyExportFeedback.classList.remove("error", "success");
  elements.historyExportFeedback.textContent = "正在生成完整历史文件…";
  try {
    const response = await fetch(
      `/api/admin/rooms/${encodeURIComponent(roomId)}/history-export`,
      {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "X-Agent-Bridge-Intent": "export-room-history",
        },
      },
    );
    if (!response.ok) {
      let errorPayload = {};
      try { errorPayload = await response.json(); } catch (_error) { /* no-op */ }
      if (response.status === 401) window.setTimeout(() => handleAuthenticationLost(), 0);
      throw new Error(errorPayload.error || `HTTP ${response.status}`);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const encodedFilename = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    let filename = `${roomId}-history.json`;
    if (encodedFilename) {
      try { filename = decodeURIComponent(encodedFilename); } catch (_error) { /* no-op */ }
    }
    const blob = await response.blob();
    const downloadUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = downloadUrl;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(downloadUrl);
    elements.historyExportFeedback.classList.add("success");
    elements.historyExportFeedback.textContent = `${roomId} 的完整历史已下载。`;
  } catch (error) {
    elements.historyExportFeedback.classList.add("error");
    elements.historyExportFeedback.textContent = error.message;
  } finally {
    elements.exportRoomHistory.disabled = false;
  }
}

async function saveHistoryRetentionPolicy(event) {
  event.preventDefault();
  if (!elements.historyRetentionForm.reportValidity()) return;
  resetHistoryRedactionPreview();
  elements.saveHistoryRetention.disabled = true;
  elements.historyRetentionFeedback.classList.remove("error", "success");
  elements.historyRetentionFeedback.textContent = "正在保存；本操作不会清除任何消息…";
  try {
    await fetchJson("/api/admin/history/retention", {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "update-history-retention",
      },
      body: JSON.stringify({
        mode: elements.historyRetentionMode.value,
        retention_days: Number(elements.historyRetentionDays.value),
      }),
    });
    await loadHistoryRetention();
    elements.historyRetentionFeedback.classList.add("success");
    elements.historyRetentionFeedback.textContent += " 策略已保存，未自动改动历史。";
  } catch (error) {
    elements.historyRetentionFeedback.classList.add("error");
    elements.historyRetentionFeedback.textContent = error.message;
  } finally {
    elements.saveHistoryRetention.disabled = false;
  }
}

async function previewHistoryRedaction(event) {
  event.preventDefault();
  if (!elements.historyRedactionPreviewForm.reportValidity()) return;
  resetHistoryRedactionPreview();
  elements.previewHistoryRedaction.disabled = true;
  elements.historyRedactionFeedback.classList.remove("error", "success");
  elements.historyRedactionFeedback.textContent = "正在计算快照；不会修改数据…";
  try {
    const payload = await fetchJson("/api/admin/history/redaction-preview", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "preview-history-redaction",
      },
      body: JSON.stringify({
        conversation_id: elements.historyRedactionRoom.value,
        reason: elements.historyRedactionReason.value.trim(),
      }),
    });
    const preview = payload.preview;
    state.historyGovernance.redactionPreview = preview;
    const roomCopy = (preview.room_counts || [])
      .map((item) => `${item.conversation_id} ${item.message_count} 条`)
      .join("、");
    const remainder = preview.more_eligible_messages_may_remain
      ? `；本批之后仍有 ${preview.total_eligible_message_count - preview.eligible_message_count} 条可另行预览`
      : "";
    elements.historyRedactionSummary.textContent = `本次只会清除 ${preview.eligible_message_count} 条旧正文（${roomCopy}）${remainder}；消息行、序号、路由、回执和审计不删除。`;
    elements.historyRedactionPhrase.textContent = preview.confirmation_phrase;
    elements.historyRedactionConfirm.hidden = false;
    elements.historyRedactionFeedback.classList.add("success");
    elements.historyRedactionFeedback.textContent = "预览已生成，数据尚未改变；确认短语 10 分钟内有效。";
    window.setTimeout(() => elements.historyRedactionConfirmation.focus(), 0);
  } catch (error) {
    elements.historyRedactionFeedback.classList.add("error");
    elements.historyRedactionFeedback.textContent = error.message;
  } finally {
    elements.previewHistoryRedaction.disabled = false;
  }
}

async function executeHistoryRedaction() {
  const preview = state.historyGovernance.redactionPreview;
  if (!preview) return;
  const phrase = elements.historyRedactionConfirmation.value.trim();
  if (phrase !== preview.confirmation_phrase) {
    elements.historyRedactionFeedback.classList.add("error");
    elements.historyRedactionFeedback.textContent = "确认短语不完全一致，未执行。";
    return;
  }
  if (!window.confirm(`确认清除 ${preview.eligible_message_count} 条旧消息正文？消息记录本身仍会保留。`)) return;
  elements.executeHistoryRedaction.disabled = true;
  elements.historyRedactionFeedback.classList.remove("error", "success");
  elements.historyRedactionFeedback.textContent = "正在清除旧正文…";
  try {
    const payload = await fetchJson("/api/admin/history/redaction-execute", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "execute-history-redaction",
      },
      body: JSON.stringify({
        preview_id: preview.preview_id,
        confirmation_phrase: phrase,
      }),
    });
    resetHistoryRedactionPreview();
    state.roomSnapshots.clear();
    await Promise.all([loadHistoryRetention(), loadHistorySearch()]);
    elements.historyRedactionFeedback.classList.add("success");
    elements.historyRedactionFeedback.textContent = `已清除 ${payload.result.redacted_message_count} 条旧正文；没有删除消息行、序号、回执或审计。`;
    if (state.selectedRoom) refreshActiveRoom(true, true).catch(console.error);
  } catch (error) {
    elements.historyRedactionFeedback.classList.add("error");
    elements.historyRedactionFeedback.textContent = error.message;
  } finally {
    elements.executeHistoryRedaction.disabled = false;
  }
}

function renderTaskPermissionMembers() {
  elements.taskPermissionResults.replaceChildren();
  const members = state.taskPermissions?.members || [];
  for (const member of members) {
    const card = makeElement("article", "rate-result-card");
    const identity = makeElement("div", "rate-result-identity");
    identity.append(makeElement("strong", "", member.display_name));
    identity.append(makeElement("span", "", `${member.username} · ${member.role === "admin" ? "全局管理员" : "普通用户"}`));
    card.append(identity);
    if (member.is_room_owner) {
      card.append(makeElement("p", "rate-result-meta", "聊天室创建者 · 始终拥有完整任务权限"));
      elements.taskPermissionResults.append(card);
      continue;
    }
    const controls = makeElement("div", "task-grant-controls");
    const assignLabel = makeElement("label", "task-grant-toggle");
    const assign = makeElement("input", "");
    assign.type = "checkbox";
    assign.checked = member.can_assign_tasks;
    assignLabel.append(assign, makeElement("span", "", "可布置任务"));
    const cancelLabel = makeElement("label", "task-grant-toggle");
    const cancel = makeElement("input", "");
    cancel.type = "checkbox";
    cancel.checked = member.can_cancel_tasks;
    cancelLabel.append(cancel, makeElement("span", "", "可取消任务"));
    const save = makeElement("button", "primary-button compact-button", "保存");
    save.type = "button";
    save.addEventListener("click", async () => {
      assign.disabled = true;
      cancel.disabled = true;
      save.disabled = true;
      try {
        const room = state.selectedRoom;
        state.taskPermissions = await fetchJson(
          `/api/rooms/${encodeURIComponent(room)}/task-grants/${encodeURIComponent(member.user_id)}`,
          {
            method: "PUT",
            headers: {
              "Content-Type": "application/json",
              "X-Agent-Bridge-Intent": "manage-task-permissions",
            },
            body: JSON.stringify({
              can_assign_tasks: assign.checked,
              can_cancel_tasks: cancel.checked,
            }),
          },
        );
        renderTaskPermissionMembers();
        elements.taskPermissionFeedback.classList.add("success");
        elements.taskPermissionFeedback.textContent = `${member.display_name} 的任务权限已保存。`;
        await refresh({});
      } catch (error) {
        elements.taskPermissionFeedback.classList.add("error");
        elements.taskPermissionFeedback.textContent = error.message;
        assign.disabled = false;
        cancel.disabled = false;
        save.disabled = false;
      }
    });
    controls.append(assignLabel, cancelLabel, save);
    card.append(controls);
    elements.taskPermissionResults.append(card);
  }
}

async function openTaskPermissionDialog() {
  const room = state.rooms.find((item) => item.conversation_id === state.selectedRoom);
  if (!room?.can_manage_task_permissions) return;
  elements.taskPermissionRoom.textContent = `当前聊天室：${room.conversation_id}`;
  elements.taskPermissionFeedback.classList.remove("error", "success");
  elements.taskPermissionFeedback.textContent = "正在载入任务权限…";
  elements.taskPermissionResults.replaceChildren();
  elements.taskPermissionDialog.showModal();
  try {
    state.taskPermissions = await fetchJson(
      `/api/rooms/${encodeURIComponent(room.conversation_id)}/task-permissions`,
    );
    elements.allowGlobalAdminTasks.checked = state.taskPermissions.allow_global_admin;
    renderTaskPermissionMembers();
    elements.taskPermissionFeedback.textContent = "";
  } catch (error) {
    elements.taskPermissionFeedback.classList.add("error");
    elements.taskPermissionFeedback.textContent = error.message;
  }
}

function updateWakePolicyFields() {
  elements.wakePolicyDigestFields.hidden = elements.wakePolicyMode.value !== "digest";
}

async function openWakePolicyDialog() {
  const room = state.rooms.find((item) => item.conversation_id === state.selectedRoom);
  if (!room?.can_manage_wake_policy) return;
  elements.wakePolicyRoom.textContent = `当前聊天室：${room.conversation_id}`;
  elements.wakePolicyFeedback.classList.remove("error", "success");
  elements.wakePolicyFeedback.textContent = "正在载入策略…";
  if (!elements.wakePolicyDialog.open) elements.wakePolicyDialog.showModal();
  try {
    const policy = await fetchJson(
      `/api/rooms/${encodeURIComponent(room.conversation_id)}/wake-policy`,
    );
    elements.wakePolicyMode.value = policy.mode || "mention";
    elements.wakeDigestMinMessages.value = String(policy.digest_min_messages || 10);
    elements.wakeDigestAfterMinutes.value = String(
      Math.max(0.5, Number(policy.digest_after_seconds || 7200) / 60),
    );
    updateWakePolicyFields();
    elements.wakePolicyFeedback.textContent = "";
  } catch (error) {
    elements.wakePolicyFeedback.classList.add("error");
    elements.wakePolicyFeedback.textContent = error.message;
  }
}

function memberSelectionTotal() {
  let count = 0;
  for (const selected of state.memberSelections.values()) count += selected.size;
  return count;
}

function updateMemberSelectionCount() {
  const count = memberSelectionTotal();
  elements.memberSelectionCount.textContent = `已选 ${count} 个成员`;
  elements.migrateMembers.disabled = count === 0 || !elements.memberTargetRoom.value;
}

function renderMemberRooms() {
  elements.memberRoomList.replaceChildren();
  const target = elements.memberTargetRoom.value;
  const query = elements.memberSearch.value.trim().toLocaleLowerCase("zh-CN");
  const selectedInTarget = state.memberSelections.get(target);
  if (selectedInTarget) selectedInTarget.clear();

  for (const room of state.memberRooms) {
    const isTarget = room.conversation_id === target;
    const agents = room.agents.filter((agent) => {
      if (!query) return true;
      const haystack = `${agent.display_name} ${agent.client_type} ${agent.signature}`
        .toLocaleLowerCase("zh-CN");
      return haystack.includes(query);
    });
    if (query && !agents.length) continue;

    const card = makeElement("section", `member-room-card${isTarget ? " target-room" : ""}`);
    const heading = makeElement("div", "member-room-heading");
    heading.append(makeElement("strong", "", room.conversation_id));
    heading.append(makeElement("span", "", isTarget ? "目标房间" : `${room.agents.length} 个 Agent`));
    card.append(heading);
    const list = makeElement("div", "member-agent-list");
    if (!agents.length) {
      list.append(makeElement("p", "member-empty", "没有可复制加入的 Agent。"));
    }
    for (const agent of agents) {
      const row = makeElement("div", "member-agent-row");
      const checkbox = makeElement("input");
      checkbox.type = "checkbox";
      checkbox.disabled = isTarget;
      checkbox.checked = Boolean(state.memberSelections.get(room.conversation_id)?.has(agent.participant_id));
      checkbox.setAttribute("aria-label", `从 ${room.conversation_id} 选择 ${agent.display_name}`);
      checkbox.addEventListener("change", () => {
        const selected = state.memberSelections.get(room.conversation_id) || new Set();
        if (checkbox.checked) selected.add(agent.participant_id);
        else selected.delete(agent.participant_id);
        state.memberSelections.set(room.conversation_id, selected);
        updateMemberSelectionCount();
      });
      const identity = makeElement("div", "member-agent-identity");
      identity.append(makeElement("strong", "", agent.display_name || agent.client_type));
      identity.append(makeElement("span", "", `${agent.signature || "未填写签名"} · ${agent.client_type}`));
      identity.append(makeElement(
        "span",
        "member-agent-expiry",
        `不发言到期：${fullTime(agent.inactivity_expires_at)}`,
      ));
      const kick = makeElement("button", "secondary-button member-agent-kick", "踢出");
      kick.type = "button";
      kick.addEventListener("click", () => kickAgentFromRoom(
        room.conversation_id,
        agent,
        kick,
      ));
      row.append(checkbox, identity, kick);
      list.append(row);
    }
    card.append(list);
    elements.memberRoomList.append(card);
  }
  if (!elements.memberRoomList.children.length) {
    elements.memberRoomList.append(makeElement("p", "muted-copy", "没有匹配的 Agent。"));
  }
  updateMemberSelectionCount();
}

async function loadMemberManagementData({ preserveTarget = true } = {}) {
  const previousTarget = preserveTarget
    ? (elements.webMemberRoom.value || elements.memberTargetRoom.value || state.selectedRoom || "")
    : (state.selectedRoom || "");
  const manageableRooms = state.rooms.filter(
    (room) => room.status === "active" && room.can_manage_web_members,
  );
  elements.agentLifecycleSection.hidden = !isAdmin();
  elements.memberMigrationSection.hidden = !isAdmin();
  elements.roomWebMembersSection.hidden = manageableRooms.length === 0;
  elements.webMemberRoom.replaceChildren();
  for (const room of manageableRooms) {
    const option = makeElement("option", "", room.conversation_id);
    option.value = room.conversation_id;
    elements.webMemberRoom.append(option);
  }
  if ([...elements.webMemberRoom.options].some((option) => option.value === previousTarget)) {
    elements.webMemberRoom.value = previousTarget;
  }
  if (!isAdmin()) {
    state.memberRooms = [];
    state.memberSelections = new Map();
    elements.memberRoomList.replaceChildren();
    await loadRoomWebUsers();
    return;
  }
  const [lifecycle, members] = await Promise.all([
    fetchJson("/api/agent-lifecycle"),
    fetchJson("/api/admin/room-members"),
  ]);
  state.agentLifecycle = lifecycle;
  state.memberRooms = members.rooms || [];
  elements.agentInactivityDays.min = String(lifecycle.minimum_days || 1);
  elements.agentInactivityDays.max = String(lifecycle.maximum_days || 3650);
  elements.agentInactivityDays.value = String(lifecycle.inactivity_days || members.inactivity_days || 10);
  elements.unactivatedAgentInactivityDays.min = String(lifecycle.minimum_days || 1);
  elements.unactivatedAgentInactivityDays.max = String(lifecycle.maximum_days || 3650);
  elements.unactivatedAgentInactivityDays.value = String(
    lifecycle.unactivated_inactivity_days || members.unactivated_inactivity_days || 3,
  );
  elements.memberTargetRoom.replaceChildren();
  for (const room of state.memberRooms) {
    const option = makeElement("option", "", room.conversation_id);
    option.value = room.conversation_id;
    elements.memberTargetRoom.append(option);
  }
  if ([...elements.memberTargetRoom.options].some((option) => option.value === previousTarget)) {
    elements.memberTargetRoom.value = previousTarget;
  }
  renderMemberRooms();
  await loadRoomWebUsers();
}

async function updateRoomWebUserAccess(user, { active, accessRole = "member" }) {
  const room = elements.webMemberRoom.value;
  const roleLabel = accessRole === "moderator" ? "聊天室管理员" : "普通成员";
  if (!active && !window.confirm(`确认将 ${user.display_name} 移出聊天室“${room}”？`)) return;
  if (
    active
    && accessRole === "moderator"
    && !window.confirm(`确认将 ${user.display_name} 委派为“${room}”的聊天室管理员？`)
  ) return;
  for (const button of elements.webMemberResults.querySelectorAll("button, select")) {
    button.disabled = true;
  }
  elements.webMemberFeedback.classList.remove("error", "success");
  elements.webMemberFeedback.textContent = active ? `正在设置为${roleLabel}…` : "正在移出…";
  try {
    await fetchJson(
      `/api/rooms/${encodeURIComponent(room)}/web-users/${encodeURIComponent(user.user_id)}`,
      {
        method: active ? "PUT" : "DELETE",
        headers: {
          ...(active ? { "Content-Type": "application/json" } : {}),
          "X-Agent-Bridge-Intent": active
            ? "invite-room-web-user"
            : "remove-room-web-user",
        },
        ...(active ? { body: JSON.stringify({ access_role: accessRole }) } : {}),
      },
    );
    await Promise.all([loadRoomWebUsers(), refresh({})]);
    elements.webMemberFeedback.classList.add("success");
    elements.webMemberFeedback.textContent = active
      ? `${user.display_name} 已设置为 ${roleLabel}。`
      : `${user.display_name} 已移出 ${room}，Agent 成员和历史消息未改变。`;
  } catch (error) {
    elements.webMemberFeedback.classList.add("error");
    elements.webMemberFeedback.textContent = error.message;
    renderRoomWebUsers();
  }
}

function renderRoomWebUsers() {
  elements.webMemberResults.replaceChildren();
  if (!state.roomWebUsers.length) {
    elements.webMemberResults.append(makeElement("p", "muted-copy", "没有匹配的普通用户。"));
    return;
  }
  for (const user of state.roomWebUsers) {
    const card = makeElement("article", "rate-result-card web-member-card");
    const identity = makeElement("div", "rate-result-identity");
    identity.append(makeElement("strong", "", user.display_name));
    identity.append(makeElement(
      "span",
      "",
      `${user.username} · ${user.signature || "未填写签名"}`,
    ));
    card.append(identity);

    const roleLabel = user.is_room_owner
      ? "创建者"
      : user.access_role === "moderator" && user.has_room_access
      ? "聊天室管理员"
      : user.has_room_access
      ? "普通成员"
      : "未加入";
    const status = makeElement(
      "span",
      `web-member-status ${user.has_room_access ? "active" : "inactive"}`,
      roleLabel,
    );
    card.append(status);

    if (user.is_room_owner) {
      elements.webMemberResults.append(card);
      continue;
    }
    const managerIsModerator = state.roomWebPermissions?.room_role === "moderator";
    const protectedModerator = managerIsModerator
      && user.has_room_access
      && user.access_role === "moderator";
    if (protectedModerator) {
      card.append(makeElement("span", "web-member-protected", "同级管理员"));
      elements.webMemberResults.append(card);
      continue;
    }
    const actions = makeElement("div", "web-member-actions");
    if (!user.has_room_access) {
      const add = makeElement("button", "primary-button compact-button", "加入");
      add.type = "button";
      add.addEventListener("click", () => updateRoomWebUserAccess(
        user,
        { active: true, accessRole: "member" },
      ));
      actions.append(add);
      if (state.roomWebPermissions?.can_delegate_room_moderators) {
        const promote = makeElement("button", "secondary-button compact-button", "设为管理员");
        promote.type = "button";
        promote.addEventListener("click", () => updateRoomWebUserAccess(
          user,
          { active: true, accessRole: "moderator" },
        ));
        actions.append(promote);
      }
    } else {
      if (state.roomWebPermissions?.can_delegate_room_moderators) {
        const role = makeElement("select", "room-id-input compact-role-select");
        for (const [value, label] of [["member", "普通成员"], ["moderator", "聊天室管理员"]]) {
          const option = makeElement("option", "", label);
          option.value = value;
          role.append(option);
        }
        role.value = user.access_role === "moderator" ? "moderator" : "member";
        role.addEventListener("change", () => updateRoomWebUserAccess(
          user,
          { active: true, accessRole: role.value },
        ));
        actions.append(role);
      }
      const remove = makeElement("button", "secondary-button compact-button danger-button", "移出");
      remove.type = "button";
      remove.addEventListener("click", () => updateRoomWebUserAccess(
        user,
        { active: false },
      ));
      actions.append(remove);
    }
    card.append(actions);
    elements.webMemberResults.append(card);
  }
}

async function loadRoomWebUsers() {
  const room = elements.webMemberRoom.value;
  if (!room) {
    state.roomWebUsers = [];
    state.roomWebPermissions = null;
    renderRoomWebUsers();
    return;
  }
  const query = elements.webMemberSearch.value.trim();
  elements.searchWebMembers.disabled = true;
  elements.webMemberFeedback.classList.remove("error", "success");
  elements.webMemberFeedback.textContent = "正在载入用户…";
  try {
    const payload = await fetchJson(
      `/api/rooms/${encodeURIComponent(room)}/web-users?query=${encodeURIComponent(query)}&limit=100`,
    );
    state.roomWebUsers = payload.users || [];
    state.roomWebPermissions = payload.permissions || null;
    renderRoomWebUsers();
    elements.webMemberFeedback.textContent = "";
  } catch (error) {
    state.roomWebUsers = [];
    state.roomWebPermissions = null;
    renderRoomWebUsers();
    elements.webMemberFeedback.classList.add("error");
    elements.webMemberFeedback.textContent = error.message;
  } finally {
    elements.searchWebMembers.disabled = false;
  }
}

async function openMemberManagementDialog() {
  const room = state.rooms.find((item) => item.conversation_id === state.selectedRoom);
  if (!room?.can_manage_web_members) return;
  state.memberSelections = new Map();
  elements.memberSearch.value = "";
  elements.webMemberSearch.value = "";
  elements.agentLifecycleFeedback.textContent = isAdmin() ? "正在载入设置…" : "";
  elements.agentLifecycleFeedback.classList.remove("error", "success");
  elements.memberManagementFeedback.textContent = "";
  elements.memberManagementFeedback.classList.remove("error", "success");
  elements.webMemberFeedback.textContent = "";
  elements.webMemberFeedback.classList.remove("error", "success");
  elements.memberRoomList.replaceChildren();
  elements.webMemberResults.replaceChildren();
  if (!elements.memberManagementDialog.open) elements.memberManagementDialog.showModal();
  try {
    await loadMemberManagementData({ preserveTarget: false });
    elements.agentLifecycleFeedback.textContent = "";
  } catch (error) {
    elements.agentLifecycleFeedback.classList.add("error");
    elements.agentLifecycleFeedback.textContent = error.message;
  }
}

async function kickAgentFromRoom(conversationId, agent, button) {
  const room = state.rooms.find((item) => item.conversation_id === conversationId);
  if (!room?.can_kick_agents) return;
  const name = agent.display_name || agent.client_type;
  if (!window.confirm(`确认将 ${name} 踢出聊天室“${conversationId}”？之后必须重新邀请才能返回该聊天室。`)) return;
  button.disabled = true;
  elements.memberManagementFeedback.classList.remove("error", "success");
  elements.memberManagementFeedback.textContent = `正在将 ${name} 踢出 ${conversationId}…`;
  try {
    await fetchJson(
      `/api/rooms/${encodeURIComponent(conversationId)}/participants/${encodeURIComponent(agent.participant_id)}/kick`,
      {
        method: "POST",
        headers: { "X-Agent-Bridge-Intent": "kick-agent" },
      },
    );
    state.memberSelections.get(conversationId)?.delete(agent.participant_id);
    await refresh({ fullRoom: true });
    if (elements.memberManagementDialog.open) {
      await loadMemberManagementData();
      elements.memberManagementFeedback.classList.add("success");
      elements.memberManagementFeedback.textContent = `${name} 已踢出；历史消息仍保留。`;
    }
  } catch (error) {
    elements.memberManagementFeedback.classList.add("error");
    elements.memberManagementFeedback.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}
