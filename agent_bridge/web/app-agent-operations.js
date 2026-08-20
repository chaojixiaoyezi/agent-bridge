"use strict";


function populateAccessRooms() {
  const activeRooms = state.rooms
    .filter((item) => item.status === "active" && item.can_invite_agents)
    .map((item) => item.conversation_id);
  const signature = JSON.stringify(activeRooms);
  if (signature === state.accessRoomSignature) return;
  state.accessRoomSignature = signature;
  const previous = elements.accessRoom.value || state.selectedRoom || "";
  elements.accessRoom.replaceChildren();
  for (const room of activeRooms) {
    const option = makeElement("option", "", room);
    option.value = room;
    elements.accessRoom.append(option);
  }
  if ([...elements.accessRoom.options].some((option) => option.value === previous)) {
    elements.accessRoom.value = previous;
  }
}

function renderSessions() {
  elements.agentSessionSection.hidden = !isAdmin();
  if (!isAdmin()) return;
  const signature = JSON.stringify([
    state.currentUser?.user_id || "",
    state.sessionStats,
    state.sessions.slice(0, 20),
  ]);
  if (signature === state.sessionRenderSignature) return;
  state.sessionRenderSignature = signature;
  elements.sessionList.replaceChildren();
  const activeCount = Number(state.sessionStats.active_count || 0);
  const clearableCount = Number(state.sessionStats.clearable_count || 0);
  elements.activeSessionCount.textContent = `${activeCount} 个有效凭证`;
  elements.clearInactiveSessions.hidden = clearableCount === 0;
  elements.clearInactiveSessions.textContent = `清理失效（${clearableCount}）`;
  if (!state.sessions.length) {
    elements.sessionList.append(makeElement("p", "muted-copy", "还没有登记的 Agent 会话。"));
    return;
  }
  for (const session of state.sessions.slice(0, 20)) {
    const card = makeElement("article", `session-card ${session.status}`);
    const main = makeElement("div", "session-main");
    main.append(makeElement("strong", "", session.display_name || session.client_type));
    main.append(makeElement("span", "", `${session.signature || "未填写签名"} · ${session.client_type}`));
    main.append(makeElement("span", "", session.conversation_id));
    main.append(makeElement("small", "", session.status === "active"
      ? `保持连接会自动续期 · 当前有效至 ${fullTime(session.expires_at)}`
      : `${session.status}${session.revoked_reason ? ` · ${session.revoked_reason}` : ""}`));
    if (session.connector_id) {
      const listenerOnline = session.connector_last_seen_at
        && (Date.now() / 1000 - Number(session.connector_last_seen_at)) <= 75;
      main.append(makeElement(
        "small",
        "",
        listenerOnline
          ? `常驻 listener 在线 · ${session.connector_adapter_kind}`
          : `${session.connector_setup_status || "值守状态未知"} · ${session.connector_adapter_kind || "适配器未知"}`,
      ));
    }
    card.append(main);
    if (session.status === "active") {
      const revoke = makeElement("button", "revoke-button", "踢出");
      revoke.type = "button";
      revoke.dataset.sessionId = session.session_id;
      revoke.addEventListener("click", () => revokeSession(session.session_id, revoke));
      card.append(revoke);
    }
    elements.sessionList.append(card);
  }
}

function invitationStatusLabel(invitation) {
  const countLabel = `已接入 ${invitation.connector_count || 0} 个 · ${invitation.online_connector_count || 0} 在线`;
  if (invitation.status === "active") {
    return invitation.reusable
      ? `可多人复用 · ${countLabel} · ${fullTime(invitation.expires_at)} 失效`
      : `等待一个 Agent 接受 · ${fullTime(invitation.expires_at)} 失效`;
  }
  if (invitation.status === "expired") return `邀请已过期 · ${countLabel}`;
  if (invitation.status === "revoked") return `邀请与全部连接已撤销 · 共接入 ${invitation.connector_count || 0} 个`;
  if (invitation.status === "exhausted") return `单次邀请已使用 · ${countLabel}`;
  const labels = {
    online: "自动值守在线",
    offline: "已配置值守 · 当前离线",
    awaiting_setup: "已接受 · 正在配置值守",
    manual: "基础接入 · 未配置自动唤醒",
    failed: "值守配置失败",
  };
  return labels[invitation.resident_status] || `已接受 · ${invitation.setup_status}`;
}

function renderAgentInvitations() {
  const roomId = elements.accessRoom.value || state.selectedRoom || "";
  const room = state.rooms.find((item) => item.conversation_id === roomId);
  const canView = isAdmin() || Boolean(room?.can_invite_agents);
  elements.agentInvitationSection.hidden = !canView;
  if (!canView) return;
  const invitations = isAdmin()
    ? state.agentInvitations
    : state.agentInvitations.filter(
      (invitation) => invitation.conversation_id === roomId,
    );
  const signature = JSON.stringify([
    state.currentUser?.user_id || "",
    roomId,
    invitations.slice(0, 30),
  ]);
  if (signature === state.invitationRenderSignature) return;
  state.invitationRenderSignature = signature;
  elements.agentInvitationList.replaceChildren();
  elements.agentInvitationCount.textContent = `${invitations.length} 个邀请`;
  if (!invitations.length) {
    elements.agentInvitationList.append(makeElement("p", "muted-copy", "还没有生成接入邀请。"));
    return;
  }
  for (const invitation of invitations.slice(0, 30)) {
    const card = makeElement("article", `session-card invitation-${invitation.status}`);
    const main = makeElement("div", "session-main");
    main.append(makeElement(
      "strong",
      "",
      invitation.last_accepted_display_name
        ? `${invitation.product} · 最近接入 ${invitation.last_accepted_display_name}`
        : `${invitation.product} · 待接受`,
    ));
    main.append(makeElement(
      "span",
      "",
      `${invitation.conversation_id} · ${invitation.requested_mode === "resident" ? "自动值守" : "基础接入"} · ${invitation.reusable ? "多人复用" : "单次使用"} · ${invitation.effective_adapter_kind || invitation.tui_adapter_kind || invitation.adapter_kind}`,
    ));
    main.append(makeElement("small", "", invitationStatusLabel(invitation)));
    card.append(main);
    if (invitation.status !== "revoked") {
      const revoke = makeElement("button", "revoke-button", "撤销");
      revoke.type = "button";
      revoke.addEventListener("click", () => revokeAgentInvitation(invitation.invitation_id, revoke));
      card.append(revoke);
    }
    elements.agentInvitationList.append(card);
  }
}

function connectorHealthStateLabel(value) {
  return {
    healthy: "正常",
    degraded: "需关注",
    offline: "链路离线",
    failed: "异常",
    setup: "接入中",
    manual: "手动接入",
  }[value] || value || "未知";
}

function connectorRuntimeStateLabel(value) {
  return {
    online: "在线",
    ready: "可用",
    idle: "空闲",
    busy: "工作中",
    retrying: "重试中",
    degraded: "降级",
    error: "异常",
    offline: "离线",
    unavailable: "不可读取",
    unknown: "等待首次探活",
  }[value] || value || "未知";
}

function connectorRuntimeErrorLabel(value) {
  return {
    adapter_contract_error: "本体回合未满足结构化回复契约",
    adapter_exit: "adapter 非零退出",
    adapter_missing: "adapter 无法启动或文件不存在",
    adapter_session_error: "本体 TUI/session 通道异常",
    adapter_timeout: "adapter 执行超时",
    adapter_unknown: "adapter 出现未分类错误",
    queue_unavailable: "supervisor 队列不可读取",
    worker_restarted: "worker 曾发生恢复重启",
  }[value] || (value ? `未知状态码：${value}` : "无");
}

function appendConnectorRuntimeDetails(card, connector) {
  const runtime = connector.runtime_diagnostics || {};
  const details = makeElement("details", "connector-runtime-details");
  details.append(makeElement(
    "summary",
    "connector-runtime-summary",
    runtime.available
      ? `远端故障详情 · ${runtime.fresh ? "刚刚更新" : "报告已过期"}`
      : "远端故障详情 · 等待 listener 自然升级",
  ));
  if (!runtime.available) {
    details.append(makeElement(
      "p",
      "connector-runtime-empty",
      "旧连接仍可正常聊天；它下次自然重启或重新接入后会自动上报结构化详情。",
    ));
    card.append(details);
    return;
  }
  const queue = runtime.queue || {};
  const worker = runtime.worker || {};
  const stages = makeElement("div", "connector-runtime-stages");
  const stageRows = [
    [
      "Bridge → listener",
      connectorRuntimeStateLabel(runtime.listener?.state),
      `最近报告 ${formatAge(runtime.report_age_seconds || 0)} · v${runtime.software_version || "?"} / ${runtime.platform || "?"}`,
      runtime.listener?.state || "unknown",
    ],
    [
      "本机 supervisor 队列",
      connectorRuntimeStateLabel(queue.state),
      `等待 ${queue.pending_count || 0} · 执行中 ${queue.inflight_count || 0} · 延后 ${queue.deferred_count || 0} · 重试 ${queue.retrying_count || 0}`,
      queue.state || "unknown",
    ],
    [
      "聊天 worker / adapter",
      connectorRuntimeStateLabel(worker.state),
      `${worker.kind || "unknown"} · 心跳 ${worker.last_seen_age_seconds == null ? "尚未出现" : formatAge(worker.last_seen_age_seconds)} · 活跃回合 ${worker.active_adapter_runs || 0}`,
      worker.state || "unknown",
    ],
    [
      "真实 TUI",
      connectorRuntimeStateLabel(connector.native_tui?.effective_state || "unknown"),
      connector.native_tui?.last_seen_age_seconds == null
        ? "当前产品未报告真实 TUI 心跳"
        : `最近探活 ${formatAge(connector.native_tui.last_seen_age_seconds)}`,
      connector.native_tui?.effective_state || "unknown",
    ],
  ];
  for (const [label, value, note, tone] of stageRows) {
    const row = makeElement("div", `connector-runtime-stage ${tone}`);
    row.append(makeElement("strong", "", label));
    row.append(makeElement("span", "connector-runtime-stage-state", value));
    row.append(makeElement("small", "", note));
    stages.append(row);
  }
  details.append(stages);
  if (queue.oldest_pending_age_seconds != null || queue.oldest_inflight_age_seconds != null) {
    const ages = [];
    if (queue.oldest_pending_age_seconds != null) {
      ages.push(`最早等待 ${formatAge(queue.oldest_pending_age_seconds)}`);
    }
    if (queue.oldest_inflight_age_seconds != null) {
      ages.push(`最长执行 ${formatAge(queue.oldest_inflight_age_seconds)}`);
    }
    details.append(makeElement("p", "connector-runtime-note", ages.join(" · ")));
  }
  if (worker.last_error_code) {
    details.append(makeElement(
      "p",
      "connector-runtime-error",
      `最近安全状态码：${connectorRuntimeErrorLabel(worker.last_error_code)}`,
    ));
  }
  details.append(makeElement(
    "p",
    "connector-runtime-privacy",
    "仅接收状态码、数量和时长；不接收远端日志、路径、聊天正文、密钥或 TUI 权限。",
  ));
  card.append(details);
}

function renderConnectorHealth() {
  const visible = isAdmin();
  elements.connectorHealthSection.hidden = !visible;
  if (!visible) return;
  const health = state.connectorHealth;
  const signature = JSON.stringify(health || {});
  if (signature === state.connectorHealthRenderSignature) return;
  state.connectorHealthRenderSignature = signature;
  elements.connectorHealthSummary.replaceChildren();
  elements.connectorHealthList.replaceChildren();
  if (!health) {
    elements.connectorHealthFeedback.textContent = "等待首次诊断…";
    return;
  }
  const summaryItems = [
    [`${health.online_count || 0}/${health.count || 0}`, "listener 在线", "healthy"],
    [health.attention_count || 0, "需要关注", (health.attention_count || 0) ? "warning" : "healthy"],
    [health.backlog?.required_pending_count || 0, "必须回复待处理", (health.backlog?.required_pending_count || 0) ? "warning" : "healthy"],
    [health.tasks?.active_count || 0, "进行中任务", (health.tasks?.expired_lease_count || 0) ? "failed" : "task"],
  ];
  for (const [value, label, tone] of summaryItems) {
    const card = makeElement("span", `connector-health-summary-card ${tone}`);
    card.append(makeElement("strong", "", value));
    card.append(makeElement("small", "", label));
    elements.connectorHealthSummary.append(card);
  }

  const attentionRooms = (health.rooms || []).filter(
    (room) => Number(room.required_pending_count || 0) > 0
      || Number(room.needs_input_count || 0) > 0
      || Number(room.expired_lease_count || 0) > 0,
  );
  if (attentionRooms.length) {
    elements.connectorHealthList.append(makeElement("p", "connector-health-subtitle", "聊天室待处理"));
    for (const room of attentionRooms.slice(0, 8)) {
      const card = makeElement("article", "connector-health-room-card");
      card.append(makeElement("strong", "", room.conversation_id));
      const details = [];
      if (room.required_pending_count) details.push(`必须回复 ${room.required_pending_count}`);
      if (room.active_task_count) details.push(`进行中任务 ${room.active_task_count}`);
      if (room.needs_input_count) details.push(`等待输入 ${room.needs_input_count}`);
      if (room.expired_lease_count) details.push(`过期租约 ${room.expired_lease_count}`);
      card.append(makeElement("span", "", details.join(" · ")));
      elements.connectorHealthList.append(card);
    }
  }

  elements.connectorHealthList.append(makeElement("p", "connector-health-subtitle", "连接器明细"));
  const priorities = { failed: 0, offline: 1, degraded: 2, setup: 3, manual: 4, healthy: 5 };
  const connectors = [...(health.connectors || [])].sort((left, right) => (
    (priorities[left.health_state] ?? 9) - (priorities[right.health_state] ?? 9)
    || Number(right.oldest_required_age_seconds || 0) - Number(left.oldest_required_age_seconds || 0)
    || String(left.display_name || left.client_type).localeCompare(
      String(right.display_name || right.client_type),
      "zh-CN",
    )
  ));
  if (!connectors.length) {
    elements.connectorHealthList.append(makeElement("p", "muted-copy", "还没有可诊断的连接器。"));
  }
  for (const connector of connectors) {
    const card = makeElement("article", `connector-health-card ${connector.health_state || "unknown"}`);
    const heading = makeElement("div", "connector-health-card-heading");
    heading.append(makeElement("strong", "", connector.display_name || connector.client_type));
    heading.append(makeElement(
      "span",
      `connector-health-state ${connector.health_state || "unknown"}`,
      connectorHealthStateLabel(connector.health_state),
    ));
    card.append(heading);
    card.append(makeElement(
      "span",
      "connector-health-route",
      `${connector.conversation_id} · ${connector.effective_adapter_kind} · ${connector.active_session_count || 0} 个有效会话`,
    ));
    const listenerAge = connector.connector_last_seen_age_seconds == null
      ? "listener 从未探活"
      : `listener ${formatAge(connector.connector_last_seen_age_seconds)}`;
    card.append(makeElement(
      "small",
      "connector-health-facts",
      `${listenerAge} · 必须回复 ${connector.required_pending_count || 0} · 普通积压 ${connector.optional_pending_count || 0} · 活跃任务 ${connector.active_task_count || 0}`,
    ));
    const enrollment = connector.enrollment || {};
    card.append(makeElement(
      "small",
      "connector-health-facts",
      `设备凭证 v${enrollment.credential_version || 1} · 已轮换 ${enrollment.rotation_count || 0} 次${enrollment.rotation_required ? " · 等待本机自动轮换" : ""}`,
    ));
    if (connector.diagnostic_detail) {
      card.append(makeElement(
        "small",
        "connector-health-detail",
        `最近错误：${connector.diagnostic_detail}`,
      ));
    }
    if ((connector.issues || []).length) {
      const issues = makeElement("div", "connector-health-issues");
      for (const issue of connector.issues) {
        issues.append(makeElement(
          "span",
          `connector-health-issue ${issue.severity || "info"}`,
          issue.label,
        ));
      }
      card.append(issues);
    }
    appendConnectorRuntimeDetails(card, connector);
    const deviceActions = makeElement("div", "connector-device-actions");
    const rotate = makeElement(
      "button",
      "secondary-button compact-button",
      enrollment.rotation_required ? "等待设备轮换" : "轮换凭证",
    );
    rotate.type = "button";
    rotate.disabled = Boolean(enrollment.rotation_required);
    rotate.addEventListener("click", () => requestConnectorRotation(connector.connector_id, rotate));
    const revoke = makeElement(
      "button",
      "secondary-button compact-button danger-button",
      "撤销设备",
    );
    revoke.type = "button";
    revoke.addEventListener("click", () => revokeConnectorDevice(connector, revoke));
    deviceActions.append(rotate, revoke);
    card.append(deviceActions);
    elements.connectorHealthList.append(card);
  }
  elements.connectorHealthFeedback.classList.remove("error", "success");
  elements.connectorHealthFeedback.textContent = health.attention_count
    ? `诊断完成：${health.attention_count} 个自动值守连接需要关注。`
    : "诊断完成：中央 Bridge 未发现自动值守异常。";
}

function formatMonitoringValue(value, kind = "count") {
  const normalized = Number(value || 0);
  if (kind === "duration") return normalized > 0 ? formatAge(normalized) : "—";
  if (kind === "rate") return `${Math.round(normalized * 100)}%`;
  return String(Math.max(0, Math.round(normalized)));
}

function renderMonitoringTrend(label, samples, key, kind = "count") {
  const row = makeElement("section", "monitoring-trend-row");
  const values = samples.map((sample) => Number(sample[key] || 0));
  const maximum = Math.max(1, ...values);
  const heading = makeElement("div", "monitoring-trend-heading");
  heading.append(
    makeElement("strong", "", label),
    makeElement("span", "", formatMonitoringValue(values.at(-1) || 0, kind)),
  );
  row.append(heading);
  const bars = makeElement("div", "monitoring-trend-bars");
  for (let index = 0; index < values.length; index += 1) {
    const bar = makeElement("span", "monitoring-trend-bar");
    bar.style.height = `${Math.max(3, (values[index] / maximum) * 100)}%`;
    bar.title = `${fullTime(samples[index].captured_at)} · ${formatMonitoringValue(values[index], kind)}`;
    bars.append(bar);
  }
  row.append(bars);
  return row;
}

function notifyOperationalAlerts(alerts) {
  const current = new Set(alerts.map((alert) => alert.alert_id));
  if (state.monitoringKnownOpenAlerts !== null) {
    const newlyOpened = alerts.filter(
      (alert) => !state.monitoringKnownOpenAlerts.has(alert.alert_id)
        && !alert.acknowledged_at,
    );
    if (
      newlyOpened.length
      && "Notification" in window
      && Notification.permission === "granted"
      && document.hidden
    ) {
      new Notification("Agent Bridge 运行告警", {
        body: newlyOpened.slice(0, 3).map((alert) => alert.title).join("、"),
        tag: "agent-bridge-operational-alert",
      });
    }
  }
  state.monitoringKnownOpenAlerts = current;
}

function renderMonitoring() {
  const visible = isAdmin();
  if (!visible) {
    elements.monitoringAlertBadge.hidden = true;
    return;
  }
  const payload = state.monitoring;
  const signature = JSON.stringify(payload || {});
  if (signature === state.monitoringRenderSignature) return;
  state.monitoringRenderSignature = signature;
  elements.monitoringSummary.replaceChildren();
  elements.monitoringTrends.replaceChildren();
  elements.monitoringAlertList.replaceChildren();
  const coordination = payload?.runtime_coordination;
  if (coordination) {
    const runtimeItems = [
      [coordination.active_instance_count || 0, "本机服务实例", coordination.active_instance_count > 0],
      [coordination.current_role === "leader" ? "主维护" : "只提供请求", "当前实例职责", coordination.leader_healthy],
      [coordination.shared_request_rate_limits ? "已共享" : "未共享", "请求限流", coordination.shared_request_rate_limits],
      [coordination.deployment_scope === "single_node" ? "SQLite 单机" : coordination.deployment_scope, "存储边界", true],
    ];
    for (const [value, label, healthy] of runtimeItems) {
      const card = makeElement("span", `connector-health-summary-card ${healthy ? "healthy" : "warning"}`);
      card.append(
        makeElement("strong", "", String(value)),
        makeElement("small", "", label),
      );
      elements.monitoringSummary.append(card);
    }
  }
  if (!payload?.latest) {
    elements.monitoringAlertBadge.hidden = true;
    elements.monitoringFeedback.textContent = coordination?.leader_healthy
      ? "维护主实例在线，等待首次分钟采样…"
      : "暂未发现维护主实例，后台任务会在租约接管后恢复。";
    return;
  }
  const latest = payload.latest;
  const openAlerts = (payload.alerts || []).filter((alert) => alert.status === "open");
  notifyOperationalAlerts(openAlerts);
  elements.monitoringAlertBadge.hidden = openAlerts.length === 0;
  elements.monitoringAlertBadge.textContent = openAlerts.length > 99 ? "99+" : String(openAlerts.length);

  const summaryItems = [
    [Number(latest.connector_offline_count || 0) + Number(latest.connector_failed_count || 0), "当前不可用连接", "count"],
    [latest.required_pending_count || 0, "必须回复积压", "count"],
    [latest.task_backlog_count || 0, "任务积压", "count"],
    [latest.reply_latency_p95_seconds, "近 1 小时回复 P95", "duration"],
    [latest.native_queue_to_injected_p95_seconds, "排队→注入 P95", "duration"],
    [latest.native_injected_to_applied_p95_seconds, "TUI 处理 P95", "duration"],
    [latest.native_applied_to_reply_p95_seconds, "完成→发回 P95", "duration"],
  ];
  for (const [value, label, kind] of summaryItems) {
    const card = makeElement("span", `connector-health-summary-card ${(Number(value || 0) > 0 && kind === "count") ? "warning" : "healthy"}`);
    card.append(
      makeElement("strong", "", formatMonitoringValue(value, kind)),
      makeElement("small", "", label),
    );
    elements.monitoringSummary.append(card);
  }

  const allSamples = payload.samples || [];
  const stride = Math.max(1, Math.ceil(allSamples.length / 48));
  const samples = allSamples.filter((_, index) => index % stride === 0);
  if (allSamples.length && samples.at(-1)?.sample_minute !== allSamples.at(-1)?.sample_minute) {
    samples.push(allSamples.at(-1));
  }
  elements.monitoringTrends.append(
    renderMonitoringTrend("离线连接", samples, "connector_offline_count"),
    renderMonitoringTrend("必须回复积压", samples, "required_pending_count"),
    renderMonitoringTrend("任务积压", samples, "task_backlog_count"),
    renderMonitoringTrend("回复 P95", samples, "reply_latency_p95_seconds", "duration"),
    renderMonitoringTrend("排队→注入", samples, "native_queue_to_injected_p95_seconds", "duration"),
    renderMonitoringTrend("TUI 处理", samples, "native_injected_to_applied_p95_seconds", "duration"),
    renderMonitoringTrend("完成→发回", samples, "native_applied_to_reply_p95_seconds", "duration"),
  );

  elements.monitoringFeedback.classList.remove("error", "success");
  const runtimeLabel = coordination
    ? ` · ${coordination.active_instance_count || 0} 个本机实例 · ${coordination.current_role === "leader" ? "当前主维护" : "当前只提供请求"}`
    : "";
  elements.monitoringFeedback.textContent = `${payload.sample_count || 0} 个分钟样本 · ${openAlerts.length} 个未解决告警 · 保留 ${payload.retention_days || 30} 天${runtimeLabel}`;
  const shownAlerts = [
    ...openAlerts,
    ...(payload.alerts || []).filter((alert) => alert.status === "resolved").slice(0, 6),
  ];
  if (!shownAlerts.length) {
    elements.monitoringAlertList.append(makeElement("p", "muted-copy", "当前没有运行告警。"));
    return;
  }
  for (const alert of shownAlerts) {
    const card = makeElement(
      "article",
      `monitoring-alert-card ${alert.severity} ${alert.status}`,
    );
    const heading = makeElement("div", "monitoring-alert-heading");
    heading.append(
      makeElement("strong", "", alert.title),
      makeElement(
        "span",
        `monitoring-alert-state ${alert.status}`,
        alert.status === "resolved" ? "已自动恢复" : alert.severity === "critical" ? "严重" : "警告",
      ),
    );
    card.append(
      heading,
      makeElement("p", "monitoring-alert-detail", alert.detail),
      makeElement(
        "small",
        "monitoring-alert-meta",
        `首次 ${fullTime(alert.first_seen_at)} · 最近 ${fullTime(alert.last_seen_at)} · ${alert.occurrence_count} 个分钟样本`,
      ),
    );
    if (alert.status === "open") {
      const acknowledge = makeElement(
        "button",
        "secondary-button compact-button",
        alert.acknowledged_at ? `已由 ${alert.acknowledged_by_username || "管理员"} 确认` : "确认已看到",
      );
      acknowledge.type = "button";
      acknowledge.disabled = Boolean(alert.acknowledged_at);
      acknowledge.addEventListener("click", () => acknowledgeMonitoringAlert(alert.alert_id, acknowledge));
      card.append(acknowledge);
    }
    elements.monitoringAlertList.append(card);
  }
}

async function loadMonitoring({ force = false } = {}) {
  if (!isAdmin()) return null;
  const hours = Number(elements.monitoringWindow.value || 24);
  if (
    !force
    && state.monitoring
    && Number(state.monitoring.hours) === hours
    && Date.now() - state.monitoringLoadedAt < MONITORING_CACHE_MS
  ) {
    return state.monitoring;
  }
  const payload = await fetchJson(`/api/admin/monitoring?hours=${encodeURIComponent(hours)}`);
  state.monitoring = payload;
  state.monitoringLoadedAt = Date.now();
  state.monitoringRenderSignature = "";
  renderMonitoring();
  return payload;
}

async function acknowledgeMonitoringAlert(alertId, button) {
  button.disabled = true;
  try {
    await fetchJson(
      `/api/admin/monitoring/alerts/${encodeURIComponent(alertId)}/acknowledge`,
      {
        method: "POST",
        headers: { "X-Agent-Bridge-Intent": "acknowledge-operational-alert" },
      },
    );
    await loadMonitoring({ force: true });
  } catch (error) {
    elements.monitoringFeedback.classList.add("error");
    elements.monitoringFeedback.textContent = `告警确认失败：${error.message}`;
    button.disabled = false;
  }
}

async function loadConnectorHealth({ force = false } = {}) {
  if (!isAdmin()) return null;
  if (
    !force
    && state.connectorHealth
    && Date.now() - state.connectorHealthLoadedAt < CONNECTOR_HEALTH_CACHE_MS
  ) {
    return state.connectorHealth;
  }
  const payload = await fetchJson("/api/admin/connectors/health");
  state.connectorHealth = payload;
  state.connectorHealthLoadedAt = Date.now();
  state.connectorHealthRenderSignature = "";
  renderConnectorHealth();
  return payload;
}

async function requestConnectorRotation(connectorId, button) {
  button.disabled = true;
  elements.connectorHealthFeedback.classList.remove("error", "success");
  elements.connectorHealthFeedback.textContent = "已登记轮换要求，等待该设备下次连接时自动完成…";
  try {
    await fetchJson(
      `/api/admin/connectors/${encodeURIComponent(connectorId)}/rotation-request`,
      {
        method: "POST",
        headers: { "X-Agent-Bridge-Intent": "request-connector-rotation" },
      },
    );
    state.connectorHealth = null;
    state.connectorHealthLoadedAt = 0;
    state.connectorHealthRenderSignature = "";
    await loadConnectorHealth({ force: true });
    elements.connectorHealthFeedback.classList.add("success");
    elements.connectorHealthFeedback.textContent = "轮换要求已登记；设备会在自然重连时本地生成并切换新凭证。";
  } catch (error) {
    elements.connectorHealthFeedback.classList.add("error");
    elements.connectorHealthFeedback.textContent = `轮换登记失败：${error.message}`;
    button.disabled = false;
  }
}

async function revokeConnectorDevice(connector, button) {
  const label = connector.display_name || connector.client_type || connector.connector_id;
  if (!window.confirm(`确定撤销设备“${label}”吗？只会使这一个连接器及其会话失效，不会删除成员、聊天记录或同邀请接入的其他设备。`)) {
    return;
  }
  button.disabled = true;
  elements.connectorHealthFeedback.classList.remove("error", "success");
  elements.connectorHealthFeedback.textContent = `正在撤销设备“${label}”…`;
  try {
    await fetchJson(
      `/api/admin/connectors/${encodeURIComponent(connector.connector_id)}/revoke`,
      {
        method: "POST",
        headers: { "X-Agent-Bridge-Intent": "revoke-connector-device" },
      },
    );
    state.connectorHealth = null;
    state.connectorHealthLoadedAt = 0;
    state.connectorHealthRenderSignature = "";
    await loadConnectorHealth({ force: true });
    elements.connectorHealthFeedback.classList.add("success");
    elements.connectorHealthFeedback.textContent = `设备“${label}”已撤销；其他设备和聊天室历史未受影响。`;
  } catch (error) {
    elements.connectorHealthFeedback.classList.add("error");
    elements.connectorHealthFeedback.textContent = `撤销设备失败：${error.message}`;
    button.disabled = false;
  }
}

async function fetchAgentInvitations(roomId = null) {
  if (!isAdmin() && !roomId) return { invitations: [] };
  const query = new URLSearchParams({ limit: "100" });
  if (roomId) query.set("conversation_id", roomId);
  return fetchJson(`/api/agent-invitations?${query.toString()}`);
}

async function revokeAgentInvitation(invitationId, button) {
  button.disabled = true;
  try {
    await fetchJson(`/api/agent-invitations/${encodeURIComponent(invitationId)}/revoke`, {
      method: "POST",
      headers: { "X-Agent-Bridge-Intent": "revoke-agent-invitation" },
    });
    await refresh({ fullRoom: true });
    if (!isAdmin()) {
      const payload = await fetchAgentInvitations(elements.accessRoom.value);
      state.agentInvitations = payload.invitations || [];
      state.invitationRenderSignature = "";
      renderAgentInvitations();
    }
  } catch (error) {
    elements.accessFeedback.classList.add("error");
    elements.accessFeedback.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function renderNicknameRequests() {
  elements.nicknameSection.hidden = !isAdmin();
  if (!isAdmin()) return;
  const signature = JSON.stringify([
    state.currentUser?.user_id || "",
    state.nicknameRequests,
  ]);
  if (signature === state.nicknameRenderSignature) return;
  state.nicknameRenderSignature = signature;
  elements.nicknameRequestList.replaceChildren();
  elements.nicknameRequestCount.textContent = `${state.nicknameRequests.length} 个待处理`;
  if (!state.nicknameRequests.length) {
    elements.nicknameRequestList.append(makeElement("p", "muted-copy", "暂无昵称申请。"));
    return;
  }
  for (const request of state.nicknameRequests) {
    const card = makeElement("article", "session-card nickname-request-card");
    const main = makeElement("div", "session-main");
    main.append(makeElement("strong", "", `${request.current_display_name} → ${request.requested_display_name}`));
    main.append(makeElement("span", "", `${request.signature} · ${request.client_type}`));
    main.append(makeElement("small", "", `申请于 ${fullTime(request.requested_at)} · 下次最早 ${fullTime(request.next_request_at)}`));
    card.append(main);
    const actions = makeElement("div", "nickname-actions");
    const reject = makeElement("button", "secondary-button compact-button", "拒绝");
    reject.type = "button";
    reject.addEventListener("click", () => reviewNickname(request.request_id, "reject", reject));
    const approve = makeElement("button", "primary-button compact-button", "批准");
    approve.type = "button";
    approve.addEventListener("click", () => reviewNickname(request.request_id, "approve", approve));
    actions.append(reject, approve);
    card.append(actions);
    elements.nicknameRequestList.append(card);
  }
}

async function reviewNickname(requestId, action, button) {
  button.disabled = true;
  try {
    await fetchJson(`/api/nickname-requests/${encodeURIComponent(requestId)}/review`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "review-nickname",
      },
      body: JSON.stringify({ action }),
    });
    await refresh({ fullRoom: true });
  } catch (error) {
    elements.accessFeedback.classList.add("error");
    elements.accessFeedback.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function fetchNicknameRequests() {
  if (!isAdmin()) return { requests: [] };
  return fetchJson("/api/nickname-requests?status=pending");
}

async function revokeSession(sessionId, button) {
  button.disabled = true;
  try {
    await fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}/revoke`, {
      method: "POST",
      headers: { "X-Agent-Bridge-Intent": "revoke-session" },
    });
    await refresh();
  } catch (error) {
    elements.accessFeedback.classList.add("error");
    elements.accessFeedback.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function clearInactiveSessions() {
  elements.clearInactiveSessions.disabled = true;
  try {
    const result = await fetchJson("/api/sessions/cleanup", {
      method: "POST",
      headers: { "X-Agent-Bridge-Intent": "clear-inactive-sessions" },
    });
    elements.accessFeedback.classList.remove("error");
    elements.accessFeedback.classList.add("success");
    elements.accessFeedback.textContent = `已清理 ${result.cleared_count} 个失效凭证；历史审计关联仍保留。`;
    await refresh();
  } catch (error) {
    elements.accessFeedback.classList.add("error");
    elements.accessFeedback.textContent = error.message;
  } finally {
    elements.clearInactiveSessions.disabled = false;
  }
}
