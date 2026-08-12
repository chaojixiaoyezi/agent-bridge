"use strict";

const state = {
  currentUser: null,
  passwordPolicy: null,
  authMode: "login",
  passwordChangeRequired: false,
  rooms: [],
  selectedRoom: window.localStorage.getItem("agentBridgeSelectedRoom") || null,
  filter: "",
  refreshing: false,
  requestVersion: 0,
  sessions: [],
  sessionStats: { active_count: 0, clearable_count: 0 },
  agentInvitations: [],
  nicknameRequests: [],
  nicknameApprovalsAvailable: true,
  participants: [],
  messages: [],
  loadedRoom: null,
  hasEarlierMessages: false,
  unreadMessages: 0,
  composerMentions: new Map(),
  composerReplyTo: null,
  composerWakeAll: false,
  ownerEvents: null,
  fallbackRefreshTimer: null,
  refreshQueued: false,
  generatedAccessInstructions: "",
  messageRateLimits: null,
  rateConfiguration: null,
  rateParticipants: [],
  agentLifecycle: null,
  memberRooms: [],
  memberSelections: new Map(),
  roomPermissionUsers: [],
  theme: "aurora",
  messageRenderSignature: "",
  participantRenderSignature: "",
  timelineScrollFrame: null,
};

const elements = {
  appShell: document.querySelector("#app-shell"),
  roomList: document.querySelector("#room-list"),
  roomCount: document.querySelector("#room-count"),
  search: document.querySelector("#room-search"),
  timeline: document.querySelector("#timeline"),
  ownerMessageForm: document.querySelector("#owner-message-form"),
  ownerMessageBody: document.querySelector("#owner-message-body"),
  ownerMessageFeedback: document.querySelector("#owner-message-feedback"),
  composerContext: document.querySelector("#composer-context"),
  composerContextTitle: document.querySelector("#composer-context-title"),
  composerContextBody: document.querySelector("#composer-context-body"),
  cancelComposerContext: document.querySelector("#cancel-composer-context"),
  wakeAllAgents: document.querySelector("#wake-all-agents"),
  sendOwnerMessage: document.querySelector("#send-owner-message"),
  roomTitle: document.querySelector("#active-room-title"),
  roomRoute: document.querySelector("#room-route"),
  roomSummary: document.querySelector("#room-summary"),
  peopleList: document.querySelector("#people-list"),
  participantCount: document.querySelector("#participant-count"),
  statusDot: document.querySelector("#status-dot"),
  connectionLabel: document.querySelector("#connection-label"),
  lastSync: document.querySelector("#last-sync"),
  refreshButton: document.querySelector("#refresh-button"),
  rateLimitPill: document.querySelector("#rate-limit-pill"),
  safetyRateCopy: document.querySelector("#safety-rate-copy"),
  openAccount: document.querySelector("#open-account"),
  inviteAgent: document.querySelector("#invite-agent"),
  manageMembers: document.querySelector("#manage-members"),
  repairResidents: document.querySelector("#repair-residents"),
  renameRoom: document.querySelector("#rename-room"),
  themeSelect: document.querySelector("#theme-select"),
  openCreateRoom: document.querySelector("#open-create-room"),
  createRoomDialog: document.querySelector("#create-room-dialog"),
  createRoomForm: document.querySelector("#create-room-form"),
  newRoomId: document.querySelector("#new-room-id"),
  createRoomFeedback: document.querySelector("#create-room-feedback"),
  createRoomPolicy: document.querySelector("#create-room-policy"),
  submitCreateRoom: document.querySelector("#submit-create-room"),
  closeCreateRoom: document.querySelector("#close-create-room"),
  cancelCreateRoom: document.querySelector("#cancel-create-room"),
  renameRoomDialog: document.querySelector("#rename-room-dialog"),
  renameRoomForm: document.querySelector("#rename-room-form"),
  renamedRoomId: document.querySelector("#renamed-room-id"),
  renameRoomFeedback: document.querySelector("#rename-room-feedback"),
  submitRenameRoom: document.querySelector("#submit-rename-room"),
  closeRenameRoom: document.querySelector("#close-rename-room"),
  cancelRenameRoom: document.querySelector("#cancel-rename-room"),
  openMessageRates: document.querySelector("#open-message-rates"),
  openRoomPermissions: document.querySelector("#open-room-permissions"),
  roomPermissionDialog: document.querySelector("#room-permission-dialog"),
  closeRoomPermissions: document.querySelector("#close-room-permissions"),
  roomPermissionSearchForm: document.querySelector("#room-permission-search-form"),
  roomPermissionSearch: document.querySelector("#room-permission-search"),
  searchRoomPermissions: document.querySelector("#search-room-permissions"),
  roomPermissionFeedback: document.querySelector("#room-permission-feedback"),
  roomPermissionResults: document.querySelector("#room-permission-results"),
  messageRateDialog: document.querySelector("#message-rate-dialog"),
  closeMessageRates: document.querySelector("#close-message-rates"),
  messageRateGlobalForm: document.querySelector("#message-rate-global-form"),
  agentGlobalRate: document.querySelector("#agent-global-rate"),
  webUserGlobalRate: document.querySelector("#web-user-global-rate"),
  messageRateGlobalFeedback: document.querySelector("#message-rate-global-feedback"),
  saveGlobalMessageRates: document.querySelector("#save-global-message-rates"),
  messageRateSearchForm: document.querySelector("#message-rate-search-form"),
  messageRateSearch: document.querySelector("#message-rate-search"),
  messageRateKind: document.querySelector("#message-rate-kind"),
  searchMessageRates: document.querySelector("#search-message-rates"),
  messageRateSearchFeedback: document.querySelector("#message-rate-search-feedback"),
  messageRateResults: document.querySelector("#message-rate-results"),
  memberManagementDialog: document.querySelector("#member-management-dialog"),
  closeMemberManagement: document.querySelector("#close-member-management"),
  agentLifecycleForm: document.querySelector("#agent-lifecycle-form"),
  agentInactivityDays: document.querySelector("#agent-inactivity-days"),
  agentLifecycleFeedback: document.querySelector("#agent-lifecycle-feedback"),
  saveAgentLifecycle: document.querySelector("#save-agent-lifecycle"),
  memberMigrationForm: document.querySelector("#member-migration-form"),
  memberTargetRoom: document.querySelector("#member-target-room"),
  memberSearch: document.querySelector("#member-search"),
  memberRoomList: document.querySelector("#member-room-list"),
  memberManagementFeedback: document.querySelector("#member-management-feedback"),
  memberSelectionCount: document.querySelector("#member-selection-count"),
  migrateMembers: document.querySelector("#migrate-members"),
  openAgentAccess: document.querySelector("#open-agent-access"),
  agentAccessDialog: document.querySelector("#agent-access-dialog"),
  closeAgentAccess: document.querySelector("#close-agent-access"),
  agentAccessForm: document.querySelector("#agent-access-form"),
  accessRoom: document.querySelector("#access-room"),
  accessProduct: document.querySelector("#access-product"),
  accessMode: document.querySelector("#access-mode"),
  agentAccessPolicy: document.querySelector("#agent-access-policy"),
  accessFeedback: document.querySelector("#access-feedback"),
  accessOutput: document.querySelector("#access-output"),
  copyAccess: document.querySelector("#copy-access"),
  generateAccess: document.querySelector("#generate-access"),
  agentInvitationSection: document.querySelector("#agent-invitation-section"),
  agentInvitationList: document.querySelector("#agent-invitation-list"),
  agentInvitationCount: document.querySelector("#agent-invitation-count"),
  agentSessionSection: document.querySelector("#agent-session-section"),
  sessionList: document.querySelector("#session-list"),
  activeSessionCount: document.querySelector("#active-session-count"),
  clearInactiveSessions: document.querySelector("#clear-inactive-sessions"),
  nicknameSection: document.querySelector("#nickname-section"),
  nicknameRequestList: document.querySelector("#nickname-request-list"),
  nicknameRequestCount: document.querySelector("#nickname-request-count"),
  enableNotifications: document.querySelector("#enable-notifications"),
  newMessageIndicator: document.querySelector("#new-message-indicator"),
  newMessageCount: document.querySelector("#new-message-count"),
  mentionMenu: document.querySelector("#mention-menu"),
  authDialog: document.querySelector("#auth-dialog"),
  authForm: document.querySelector("#auth-form"),
  authTitle: document.querySelector("#auth-title"),
  showLogin: document.querySelector("#show-login"),
  showRegister: document.querySelector("#show-register"),
  authUsername: document.querySelector("#auth-username"),
  authPassword: document.querySelector("#auth-password"),
  authPasswordConfirm: document.querySelector("#auth-password-confirm"),
  registerConfirmWrap: document.querySelector("#register-confirm-wrap"),
  captchaImage: document.querySelector("#captcha-image"),
  captchaAnswer: document.querySelector("#captcha-answer"),
  refreshCaptcha: document.querySelector("#refresh-captcha"),
  authHelp: document.querySelector("#auth-help"),
  authFeedback: document.querySelector("#auth-feedback"),
  submitAuth: document.querySelector("#submit-auth"),
  passwordDialog: document.querySelector("#password-dialog"),
  passwordForm: document.querySelector("#password-form"),
  passwordTitle: document.querySelector("#password-title"),
  closePassword: document.querySelector("#close-password"),
  currentPassword: document.querySelector("#current-password"),
  newPassword: document.querySelector("#new-password"),
  newPasswordConfirm: document.querySelector("#new-password-confirm"),
  passwordPolicyCopy: document.querySelector("#password-policy-copy"),
  passwordFeedback: document.querySelector("#password-feedback"),
  submitPassword: document.querySelector("#submit-password"),
  accountDialog: document.querySelector("#account-dialog"),
  profileForm: document.querySelector("#profile-form"),
  closeAccount: document.querySelector("#close-account"),
  accountIdentity: document.querySelector("#account-identity"),
  profileDisplayName: document.querySelector("#profile-display-name"),
  profileSignature: document.querySelector("#profile-signature"),
  profileFeedback: document.querySelector("#profile-feedback"),
  submitProfile: document.querySelector("#submit-profile"),
  openPassword: document.querySelector("#open-password"),
  logout: document.querySelector("#logout"),
};

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

const THEMES = new Set(["aurora", "ocean", "violet", "ember"]);

try {
  state.theme = window.localStorage.getItem("agentBridgeTheme") || "aurora";
} catch (error) {
  state.theme = "aurora";
}

function applyTheme(theme) {
  const selected = THEMES.has(theme) ? theme : "aurora";
  state.theme = selected;
  document.documentElement.dataset.theme = selected;
  elements.themeSelect.value = selected;
  try {
    window.localStorage.setItem("agentBridgeTheme", selected);
  } catch (error) {
    // Private browsing or a hardened WebView may disable persistent storage.
  }
}

applyTheme(state.theme);

function shortTime(timestamp) {
  if (!timestamp) return "—";
  const date = new Date(timestamp * 1000);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
}

function fullTime(timestamp) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(timestamp * 1000));
}

function formatCooldown(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "—";
  if (seconds <= 0) return "不限频";
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${Number.isInteger(seconds) ? seconds : Number(seconds.toFixed(3))} 秒`;
}

function dayLabel(timestamp) {
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "short" }).format(new Date(timestamp * 1000));
}

async function fetchJson(path, options = {}) {
  const { suppressAuthRedirect = false, ...fetchOptions } = options;
  const headers = new Headers(fetchOptions.headers || {});
  headers.set("Accept", "application/json");
  const response = await fetch(path, {
    ...fetchOptions,
    cache: "no-store",
    credentials: "same-origin",
    headers,
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    if (response.ok) throw error;
  }
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.retryAfterSeconds = payload.retry_after_seconds;
    if (response.status === 401 && !suppressAuthRedirect && state.currentUser) {
      window.setTimeout(() => handleAuthenticationLost(), 0);
    }
    throw error;
  }
  return payload;
}

function isAdmin() {
  return Boolean(state.currentUser?.is_admin);
}

function closeLiveConnections() {
  state.ownerEvents?.close();
  state.ownerEvents = null;
  if (state.fallbackRefreshTimer) {
    window.clearTimeout(state.fallbackRefreshTimer);
    state.fallbackRefreshTimer = null;
  }
}

function setAuthMode(mode) {
  state.authMode = mode === "register" ? "register" : "login";
  const registering = state.authMode === "register";
  elements.showLogin.classList.toggle("active", !registering);
  elements.showRegister.classList.toggle("active", registering);
  elements.showLogin.setAttribute("aria-selected", String(!registering));
  elements.showRegister.setAttribute("aria-selected", String(registering));
  elements.registerConfirmWrap.hidden = !registering;
  elements.authPasswordConfirm.required = registering;
  elements.authPassword.autocomplete = registering ? "new-password" : "current-password";
  elements.authTitle.textContent = registering ? "注册 Web 用户" : "登录 Agent Bridge";
  elements.submitAuth.textContent = registering ? "注册并登录" : "登录";
  elements.authHelp.textContent = registering
    ? (state.passwordPolicy?.description || "密码需为 10–128 个字符，并至少包含四类字符中的三类。")
    : "默认管理员首次登录使用 admin/admin，登录后必须立即设置符合复杂度要求的新密码。";
  elements.authFeedback.textContent = "";
}

async function loadCaptcha() {
  elements.refreshCaptcha.disabled = true;
  elements.captchaImage.removeAttribute("src");
  try {
    const payload = await fetchJson("/api/auth/captcha", { suppressAuthRedirect: true });
    state.captchaId = payload.captcha.captcha_id;
    elements.captchaImage.src = payload.captcha.image;
    elements.captchaAnswer.value = "";
  } catch (error) {
    state.captchaId = null;
    elements.authFeedback.classList.add("error");
    elements.authFeedback.textContent = `验证码加载失败：${error.message}`;
  } finally {
    elements.refreshCaptcha.disabled = false;
  }
}

function showAuthScreen(message = "") {
  closeLiveConnections();
  state.currentUser = null;
  state.passwordChangeRequired = false;
  elements.appShell.hidden = true;
  for (const dialog of [
    elements.passwordDialog,
    elements.accountDialog,
    elements.createRoomDialog,
    elements.renameRoomDialog,
    elements.messageRateDialog,
    elements.memberManagementDialog,
    elements.agentAccessDialog,
  ]) {
    if (dialog.open) dialog.close();
  }
  elements.authForm.reset();
  setAuthMode("login");
  elements.authFeedback.classList.remove("error", "success");
  elements.authFeedback.textContent = message;
  if (!elements.authDialog.open) elements.authDialog.showModal();
  loadCaptcha();
  window.setTimeout(() => elements.authUsername.focus(), 0);
}

function handleAuthenticationLost() {
  if (!state.currentUser) return;
  showAuthScreen("登录已失效，请重新登录。聊天室数据没有丢失。");
}

function applyUserPermissions() {
  const admin = isAdmin();
  const activeRoom = state.rooms.find((room) => room.conversation_id === state.selectedRoom);
  elements.openCreateRoom.hidden = !(admin || state.currentUser?.can_create_rooms);
  elements.openMessageRates.hidden = !admin;
  elements.openRoomPermissions.hidden = !admin;
  elements.openAgentAccess.hidden = !admin;
  elements.agentInvitationSection.hidden = !admin;
  elements.agentSessionSection.hidden = !admin;
  elements.nicknameSection.hidden = !admin;
  elements.renameRoom.hidden = !(admin && activeRoom);
  elements.inviteAgent.hidden = !(admin && activeRoom && activeRoom.status === "active");
  elements.manageMembers.hidden = !(admin && activeRoom && activeRoom.status === "active");
  elements.repairResidents.hidden = !(admin && activeRoom && activeRoom.status === "active");
  elements.wakeAllAgents.hidden = !(activeRoom?.can_wake_all && activeRoom.status === "active");
  elements.openAccount.textContent = `${state.currentUser.display_name}${admin ? " · 管理员" : ""}`;
  const agentGlobal = state.messageRateLimits?.agent_global_cooldown_seconds ?? 15;
  const webGlobal = state.messageRateLimits?.web_user_global_cooldown_seconds ?? 60;
  const currentEffective = state.messageRateLimits?.current_user_effective_cooldown_seconds ?? webGlobal;
  elements.rateLimitPill.textContent = admin
    ? `管理员不限频 · Agent 整体 ${formatCooldown(agentGlobal)}`
    : `你的发言间隔：${formatCooldown(currentEffective)}`;
  elements.safetyRateCopy.textContent = admin
    ? `消息都是普通聊天，不执行正文、路径或附件。管理员不限频；Agent 整体间隔为 ${formatCooldown(agentGlobal)}。`
    : `消息都是普通聊天，不执行正文、路径或附件。你的当前间隔为 ${formatCooldown(currentEffective)}；普通用户整体间隔为 ${formatCooldown(webGlobal)}。`;
}

function openPasswordDialog(required = false) {
  state.passwordChangeRequired = Boolean(required);
  elements.passwordForm.reset();
  elements.passwordFeedback.classList.remove("error", "success");
  elements.passwordFeedback.textContent = required ? "首次使用管理员账户，请先设置新密码。" : "";
  elements.passwordTitle.textContent = required ? "首次登录：设置新密码" : "修改密码";
  elements.closePassword.hidden = required;
  elements.passwordPolicyCopy.textContent = state.passwordPolicy?.description
    || "密码需为 10–128 个字符，并至少包含小写字母、大写字母、数字、符号中的三类。";
  if (elements.accountDialog.open) elements.accountDialog.close();
  if (required) elements.appShell.hidden = true;
  if (!elements.passwordDialog.open) elements.passwordDialog.showModal();
  window.setTimeout(() => elements.currentPassword.focus(), 0);
}

async function enterApplication() {
  state.passwordChangeRequired = false;
  if (elements.authDialog.open) elements.authDialog.close();
  if (elements.passwordDialog.open) elements.passwordDialog.close();
  applyUserPermissions();
  elements.appShell.hidden = false;
  await refresh({ fullRoom: true });
  connectOwnerEvents();
}

async function bootstrapAuthentication() {
  try {
    const payload = await fetchJson("/api/auth/me", { suppressAuthRedirect: true });
    state.currentUser = payload.user;
    state.passwordPolicy = payload.password_policy;
    if (state.currentUser.must_change_password) {
      openPasswordDialog(true);
      return;
    }
    await enterApplication();
  } catch (error) {
    if (error.status !== 401) console.error(error);
    showAuthScreen(error.status === 401 ? "" : `无法检查登录状态：${error.message}`);
  }
}

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

function renderRooms() {
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
}

function isNearTimelineBottom() {
  return elements.timeline.scrollHeight - elements.timeline.scrollTop - elements.timeline.clientHeight < 80;
}

function captureTimelineAnchor() {
  const timelineTop = elements.timeline.getBoundingClientRect().top;
  const articles = elements.timeline.querySelectorAll("article[data-message-id]");
  for (const article of articles) {
    const rect = article.getBoundingClientRect();
    if (rect.bottom > timelineTop) {
      return { messageId: article.dataset.messageId, offset: rect.top - timelineTop };
    }
  }
  return null;
}

function routeLabel(message) {
  if (message.wake_all_agents) return "@全员 · 唤醒 Agent";
  if (message.audience_kind === "participant") return "@成员";
  if (message.audience_kind === "role") return `@角色 · ${message.audience_value}`;
  if (message.audience_kind === "broadcast") return "房间广播";
  return message.mentions?.length ? "群聊 · 含 @" : "群聊";
}

function participantName(participantId) {
  const participant = state.participants.find((item) => item.participant_id === participantId);
  return participant?.display_name || participant?.client_type || participantId;
}

function createMessageElement(message) {
  const article = makeElement("article", "message");
  article.dataset.messageId = message.message_id;
  const head = makeElement("div", "message-head");
  const senderLine = makeElement("div", "sender-line");
  senderLine.append(makeElement("strong", "", message.sender_display_name || message.sender_client_type));
  const signature = message.sender_signature || message.sender_alias || "未填写签名";
  senderLine.append(makeElement("span", "client-label", `${signature} · ${message.sender_client_type}`));
  senderLine.append(makeElement("span", "route-badge", routeLabel(message)));
  if (message.authorization) {
    senderLine.append(makeElement("span", "authorization-badge revoked", "授权待提交"));
  }
  head.append(senderLine);
  head.append(makeElement("time", "message-time", fullTime(message.created_at)));
  article.append(head);
  article.append(makeElement("p", "message-body", message.body));

  if (message.mentions?.length) {
    article.append(makeElement("p", "mention-label", `特别通知：${message.mentions.map(participantName).join("、")}`));
  }
  if (message.reply_to) {
    const original = state.messages.find((item) => item.message_id === message.reply_to);
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
  article.append(makeElement("p", "receipt-label", `#${message.sequence} · ${message.ack_count}/${message.receipt_count} 已确认/已通知`));

  if (message.refs.length) {
    const refs = makeElement("div", "ref-list");
    for (const ref of message.refs) {
      const label = ref.label ? `${ref.label} · ` : "";
      refs.append(makeElement("div", "ref-item", `${label}${ref.path}${ref.sha256 ? ` · sha256:${ref.sha256}` : ""}`));
    }
    article.append(refs);
  }
  const selectedRoom = state.rooms.find((room) => room.conversation_id === state.selectedRoom);
  if (!message.reply_to && selectedRoom?.status === "active") {
    const replyButton = makeElement("button", "message-reply-button", "回复");
    replyButton.type = "button";
    replyButton.addEventListener("click", () => startComposerReply(message));
    article.append(replyButton);
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

function renderMessages(messages, { forceBottom = false, addedCount = 0 } = {}) {
  const hadRenderedMessages = Boolean(
    elements.timeline.querySelector("article[data-message-id]"),
  );
  const wasNearBottom = hadRenderedMessages ? isNearTimelineBottom() : true;
  const anchor = !wasNearBottom && !forceBottom ? captureTimelineAnchor() : null;
  const signature = `${state.selectedRoom || ""}:${state.hasEarlierMessages}:${messages.map((item) => `${item.message_id}:${item.updated_at || item.ack_count || 0}:${item.ack_count || 0}`).join("|")}`;
  if (!forceBottom && addedCount === 0 && signature === state.messageRenderSignature) {
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

  let activeDay = "";
  for (const message of messages) {
    const nextDay = dayLabel(message.created_at);
    if (nextDay !== activeDay) {
      activeDay = nextDay;
      fragment.append(makeElement("div", "day-divider", activeDay));
    }
    fragment.append(createMessageElement(message));
  }
  elements.timeline.replaceChildren(fragment);

  if (forceBottom || wasNearBottom) {
    elements.timeline.scrollTop = elements.timeline.scrollHeight;
    state.unreadMessages = 0;
  } else if (anchor) {
    const anchored = [...elements.timeline.querySelectorAll("article[data-message-id]")]
      .find((item) => item.dataset.messageId === anchor.messageId);
    if (anchored) {
      const timelineTop = elements.timeline.getBoundingClientRect().top;
      elements.timeline.scrollTop += anchored.getBoundingClientRect().top - timelineTop - anchor.offset;
    }
    state.unreadMessages += addedCount;
  }
  updateNewMessageIndicator();
}

function renderParticipants(participants) {
  state.participants = participants;
  const signature = participants.map((item) => `${item.participant_id}:${item.status}:${item.membership_active}:${item.resident_status}:${item.display_name}:${item.signature}`).join("|");
  if (signature === state.participantRenderSignature) return;
  state.participantRenderSignature = signature;
  elements.peopleList.replaceChildren();
  elements.participantCount.textContent = String(participants.length);
  if (!participants.length) {
    elements.peopleList.append(makeElement("p", "muted-copy", "这个聊天室还没有活跃成员。"));
    return;
  }
  for (const person of participants) {
    const archived = person.room_status === "abandoned" || !person.membership_active;
    const card = makeElement("article", `person-card${archived ? " archived" : ""}`);
    const head = makeElement("div", "person-head");
    const username = person.client_type.includes("-")
      ? person.client_type.slice(person.client_type.indexOf("-") + 1)
      : person.client_type;
    const initial = Array.from(username)[0] || "A";
    head.append(makeElement("div", `avatar ${person.status}`, initial));
    const name = makeElement("div", "person-name");
    name.append(makeElement("strong", "", person.display_name || person.client_type));
    name.append(makeElement("span", "", person.signature || "未填写签名"));
    name.append(makeElement("span", "identity-line", person.client_type));
    head.append(name);
    head.append(makeElement("span", `presence-dot ${person.status}`));
    const isWebUser = person.client_type.startsWith("web-user");
    if (!isWebUser && !archived) {
      const mention = makeElement("button", "mention-button", "@");
      mention.type = "button";
      mention.title = `特别通知 ${person.display_name || person.client_type}`;
      mention.addEventListener("click", () => addComposerMention(person));
      head.append(mention);
      if (isAdmin() && state.selectedRoom) {
        const kick = makeElement("button", "person-kick-button", "踢");
        kick.type = "button";
        kick.title = `将 ${person.display_name || person.client_type} 踢出当前聊天室`;
        kick.addEventListener("click", () => kickAgentFromRoom(
          state.selectedRoom,
          person,
          kick,
        ));
        head.append(kick);
      }
    }
    card.append(head);
    if (person.roles.length) {
      const roles = makeElement("div", "roles");
      for (const role of person.roles) roles.append(makeElement("span", "role-chip", role));
      card.append(roles);
    }
    let authLabel;
    if (isWebUser) {
      authLabel = "网页用户";
    } else if (person.resident_status === "online") {
      authLabel = `自动值守在线 · ${person.connector_adapter_kind}`;
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
    if (archived) card.append(makeElement("p", "membership-label", "历史成员 · 已不可进入"));
    elements.peopleList.append(card);
  }
}

function populateAccessRooms() {
  const previous = elements.accessRoom.value || state.selectedRoom || "";
  elements.accessRoom.replaceChildren();
  for (const room of state.rooms.filter((item) => item.status === "active")) {
    const option = makeElement("option", "", room.conversation_id);
    option.value = room.conversation_id;
    elements.accessRoom.append(option);
  }
  if ([...elements.accessRoom.options].some((option) => option.value === previous)) {
    elements.accessRoom.value = previous;
  }
}

function renderSessions() {
  elements.agentSessionSection.hidden = !isAdmin();
  if (!isAdmin()) return;
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
  elements.agentInvitationSection.hidden = !isAdmin();
  if (!isAdmin()) return;
  elements.agentInvitationList.replaceChildren();
  elements.agentInvitationCount.textContent = `${state.agentInvitations.length} 个邀请`;
  if (!state.agentInvitations.length) {
    elements.agentInvitationList.append(makeElement("p", "muted-copy", "还没有生成接入邀请。"));
    return;
  }
  for (const invitation of state.agentInvitations.slice(0, 30)) {
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
      `${invitation.conversation_id} · ${invitation.requested_mode === "resident" ? "自动值守" : "基础接入"} · ${invitation.reusable ? "多人复用" : "单次使用"} · ${invitation.adapter_kind}`,
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

async function fetchAgentInvitations() {
  if (!isAdmin()) return { invitations: [] };
  return fetchJson("/api/agent-invitations?limit=100");
}

async function revokeAgentInvitation(invitationId, button) {
  button.disabled = true;
  try {
    await fetchJson(`/api/agent-invitations/${encodeURIComponent(invitationId)}/revoke`, {
      method: "POST",
      headers: { "X-Agent-Bridge-Intent": "revoke-agent-invitation" },
    });
    await refresh({ fullRoom: true });
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

function roomCreator(room) {
  if (room.owner_display_name) return `${room.owner_display_name} 创建`;
  if (room.creator_kind === "user") return "管理员创建";
  if (room.creator_kind === "legacy") return "历史房间";
  return `${room.creator_client_type || room.creator_participant_id || "Agent"} 创建`;
}

function updateComposer(room) {
  const canSpeak = Boolean(room && room.status === "active");
  const effectiveCooldown = state.messageRateLimits?.current_user_effective_cooldown_seconds ?? 60;
  elements.ownerMessageBody.disabled = !canSpeak;
  elements.sendOwnerMessage.disabled = !canSpeak;
  elements.wakeAllAgents.hidden = !(canSpeak && room?.can_wake_all);
  elements.ownerMessageBody.placeholder = canSpeak
    ? `${state.currentUser?.display_name || "Web 用户"}发言（${isAdmin() ? "不限频" : `每个房间间隔 ${formatCooldown(effectiveCooldown)}`}）；Enter 发送，Shift+Enter 换行…`
    : room ? "废弃聊天室仅保留历史，不能继续发言。" : "先选择一个使用中的聊天室。";
  if (!canSpeak) elements.ownerMessageFeedback.textContent = "";
  updateComposerContext();
}

function updateComposerContext() {
  const replied = state.composerReplyTo
    ? state.messages.find((message) => message.message_id === state.composerReplyTo)
    : null;
  if (replied) {
    elements.composerContext.hidden = false;
    elements.composerContextTitle.textContent = `回复 ${replied.sender_display_name || replied.sender_client_type}`;
    elements.composerContextBody.textContent = replied.body.slice(0, 140);
  } else if (state.composerWakeAll) {
    elements.composerContext.hidden = false;
    elements.composerContextTitle.textContent = "@全员：唤醒本聊天室所有 Agent";
    elements.composerContextBody.textContent = "Agent 会全部收到唤醒，但可以根据兴趣选择是否回复。";
  } else {
    elements.composerContext.hidden = true;
    elements.composerContextTitle.textContent = "";
    elements.composerContextBody.textContent = "";
  }
  elements.wakeAllAgents?.classList.toggle("active", state.composerWakeAll);
}

function clearComposerContext() {
  state.composerReplyTo = null;
  state.composerWakeAll = false;
  updateComposerContext();
}

function startComposerReply(message) {
  if (!message || message.reply_to) return;
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
    .filter((person) => !person.client_type.startsWith("web-user") && person.membership_active)
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
    if (bodyText.includes(token)) ids.push(participantId);
  }
  return ids;
}

async function selectRoom(roomId) {
  state.selectedRoom = roomId;
  state.loadedRoom = null;
  state.messageRenderSignature = "";
  state.participantRenderSignature = "";
  state.messages = [];
  state.participants = [];
  state.hasEarlierMessages = false;
  state.unreadMessages = 0;
  state.composerMentions.clear();
  clearComposerContext();
  hideMentionMenu();
  updateNewMessageIndicator();
  window.localStorage.setItem("agentBridgeSelectedRoom", roomId);
  renderRooms();
  await refreshActiveRoom(true, true);
}

function mergeMessages(existing, incoming) {
  const byId = new Map(existing.map((message) => [message.message_id, message]));
  for (const message of incoming) byId.set(message.message_id, message);
  return [...byId.values()].sort((left, right) => left.sequence - right.sequence);
}

async function loadEarlierMessages(event) {
  const button = event?.currentTarget;
  const firstSequence = state.messages[0]?.sequence;
  if (!state.selectedRoom || !firstSequence) return;
  if (button) {
    button.disabled = true;
    button.textContent = "正在加载…";
  }
  try {
    const encodedRoom = encodeURIComponent(state.selectedRoom);
    const payload = await fetchJson(
      `/api/rooms/${encodedRoom}/messages?limit=200&before_sequence=${encodeURIComponent(firstSequence)}`,
    );
    state.messages = mergeMessages(state.messages, payload.messages);
    state.hasEarlierMessages = payload.has_more;
    renderMessages(state.messages);
  } catch (error) {
    console.error(error);
    if (button) button.textContent = "加载失败 · 重试";
  } finally {
    if (button) button.disabled = false;
  }
}

async function refreshActiveRoom(forceScroll = false, fullRoom = false) {
  if (!state.selectedRoom) return;
  const requestVersion = ++state.requestVersion;
  const selectedRoom = state.selectedRoom;
  const encodedRoom = encodeURIComponent(selectedRoom);
  const activeRoom = state.rooms.find((room) => room.conversation_id === selectedRoom);
  const initialLoad = fullRoom || state.loadedRoom !== selectedRoom;
  const lastLoadedSequence = state.messages[state.messages.length - 1]?.sequence || 0;
  const hasServerUpdates = Number(activeRoom?.last_sequence || 0) > lastLoadedSequence;
  const messageRequest = initialLoad
    ? fetchJson(`/api/rooms/${encodedRoom}/messages?limit=120`)
    : hasServerUpdates
      ? fetchJson(`/api/rooms/${encodedRoom}/messages?limit=200&after_sequence=${encodeURIComponent(lastLoadedSequence)}`)
      : Promise.resolve(null);
  const [messagePayload, participantPayload] = await Promise.all([
    messageRequest,
    fetchJson(`/api/rooms/${encodedRoom}/participants`),
  ]);
  if (requestVersion !== state.requestVersion) return;
  elements.roomTitle.textContent = selectedRoom;
  const abandoned = activeRoom?.status === "abandoned";
  elements.roomRoute.textContent = abandoned ? "ABANDONED · HISTORY ONLY" : "ROOM · EVENT LIVE VIEW";
  elements.roomSummary.textContent = activeRoom
    ? abandoned
      ? `已废弃，Agent 不可进入 · ${activeRoom.participant_count} 个历史会话 · ${activeRoom.message_count} 条消息永久保留`
      : `${roomCreator(activeRoom)} · ${Number(activeRoom.current_participant_count ?? activeRoom.participant_count ?? 0)} 个会话 · ${activeRoom.message_count} 条持久消息`
    : "本机聊天室";
  renderParticipants(participantPayload.participants);
  if (messagePayload) {
    let addedCount = 0;
    if (initialLoad) {
      state.messages = messagePayload.messages;
      state.hasEarlierMessages = messagePayload.has_more;
      state.loadedRoom = selectedRoom;
    } else {
      const knownIds = new Set(state.messages.map((message) => message.message_id));
      addedCount = messagePayload.messages.filter((message) => !knownIds.has(message.message_id)).length;
      state.messages = mergeMessages(state.messages, messagePayload.messages);
    }
    renderMessages(state.messages, { forceBottom: forceScroll, addedCount });
    if (!initialLoad && messagePayload.has_more) {
      window.setTimeout(() => refresh({}), 0);
    }
  } else if (forceScroll) {
    elements.timeline.scrollTop = elements.timeline.scrollHeight;
  }
  updateComposer(activeRoom);
  applyUserPermissions();
}

async function refresh(options = {}) {
  if (!state.currentUser) return;
  if (state.refreshing) {
    state.refreshQueued = true;
    return;
  }
  state.refreshing = true;
  elements.refreshButton.classList.add("spinning");
  try {
    const sessionRequest = isAdmin()
      ? fetchJson("/api/sessions")
      : Promise.resolve({ sessions: [], stats: { active_count: 0, clearable_count: 0 } });
    const invitationRequest = isAdmin()
      ? fetchAgentInvitations()
      : Promise.resolve({ invitations: [] });
    const [
      healthPayload,
      roomPayload,
      sessionPayload,
      nicknamePayload,
      invitationPayload,
    ] = await Promise.all([
      fetchJson("/api/health"),
      fetchJson("/api/rooms?limit=200"),
      sessionRequest,
      fetchNicknameRequests(),
      invitationRequest,
    ]);
    state.messageRateLimits = healthPayload.message_rate_limits || null;
    if (healthPayload.current_user) state.currentUser = healthPayload.current_user;
    state.rooms = roomPayload.rooms;
    state.sessions = sessionPayload.sessions;
    state.sessionStats = sessionPayload.stats || { active_count: 0, clearable_count: 0 };
    state.nicknameRequests = nicknamePayload.requests;
    state.agentInvitations = invitationPayload.invitations;
    if (!state.selectedRoom || !state.rooms.some((room) => room.conversation_id === state.selectedRoom)) {
      state.selectedRoom = state.rooms[0]?.conversation_id || null;
      if (state.selectedRoom) window.localStorage.setItem("agentBridgeSelectedRoom", state.selectedRoom);
    }
    renderRooms();
    populateAccessRooms();
    renderSessions();
    renderAgentInvitations();
    renderNicknameRequests();
    applyUserPermissions();
    if (state.selectedRoom) {
      await refreshActiveRoom(
        options.forceRoomBottom === true,
        options.fullRoom === true,
      );
    }
    setConnection(true, `${healthPayload.room_states.active} 使用中 · ${healthPayload.room_states.abandoned} 已废弃`);
    elements.lastSync.textContent = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date());
  } catch (error) {
    setConnection(false, "连接中断");
    elements.lastSync.textContent = "重试中";
    console.error(error);
  } finally {
    state.refreshing = false;
    window.setTimeout(() => elements.refreshButton.classList.remove("spinning"), 300);
    if (state.refreshQueued) {
      state.refreshQueued = false;
      window.setTimeout(() => refresh({}), 0);
    }
  }
}

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
    ? (elements.memberTargetRoom.value || state.selectedRoom || "")
    : (state.selectedRoom || "");
  const [lifecycle, members] = await Promise.all([
    fetchJson("/api/agent-lifecycle"),
    fetchJson("/api/admin/room-members"),
  ]);
  state.agentLifecycle = lifecycle;
  state.memberRooms = members.rooms || [];
  elements.agentInactivityDays.min = String(lifecycle.minimum_days || 1);
  elements.agentInactivityDays.max = String(lifecycle.maximum_days || 3650);
  elements.agentInactivityDays.value = String(lifecycle.inactivity_days || members.inactivity_days || 10);
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
}

async function openMemberManagementDialog() {
  if (!isAdmin()) return;
  state.memberSelections = new Map();
  elements.memberSearch.value = "";
  elements.agentLifecycleFeedback.textContent = "正在载入设置…";
  elements.agentLifecycleFeedback.classList.remove("error", "success");
  elements.memberManagementFeedback.textContent = "";
  elements.memberManagementFeedback.classList.remove("error", "success");
  elements.memberRoomList.replaceChildren();
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
  if (!isAdmin()) return;
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

elements.showLogin.addEventListener("click", () => setAuthMode("login"));
elements.showRegister.addEventListener("click", () => setAuthMode("register"));
elements.refreshCaptcha.addEventListener("click", loadCaptcha);
elements.authDialog.addEventListener("cancel", (event) => event.preventDefault());

elements.authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.authForm.reportValidity() || !state.captchaId) return;
  const registering = state.authMode === "register";
  if (registering && elements.authPassword.value !== elements.authPasswordConfirm.value) {
    elements.authFeedback.classList.add("error");
    elements.authFeedback.textContent = "两次输入的密码不一致。";
    return;
  }
  elements.submitAuth.disabled = true;
  elements.authFeedback.classList.remove("error", "success");
  elements.authFeedback.textContent = registering ? "正在注册…" : "正在登录…";
  try {
    const payload = await fetchJson(`/api/auth/${registering ? "register" : "login"}`, {
      method: "POST",
      suppressAuthRedirect: true,
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": registering ? "register" : "login",
      },
      body: JSON.stringify({
        username: elements.authUsername.value.trim(),
        password: elements.authPassword.value,
        captcha_id: state.captchaId,
        captcha_answer: elements.captchaAnswer.value.trim(),
      }),
    });
    state.currentUser = payload.user;
    state.passwordPolicy = payload.password_policy;
    if (elements.authDialog.open) elements.authDialog.close();
    if (state.currentUser.must_change_password) {
      openPasswordDialog(true);
    } else {
      await enterApplication();
    }
  } catch (error) {
    elements.authFeedback.classList.add("error");
    elements.authFeedback.textContent = error.message;
    await loadCaptcha();
  } finally {
    elements.submitAuth.disabled = false;
  }
});

elements.passwordDialog.addEventListener("cancel", (event) => {
  if (state.passwordChangeRequired) event.preventDefault();
});
elements.closePassword.addEventListener("click", () => {
  if (!state.passwordChangeRequired && elements.passwordDialog.open) elements.passwordDialog.close();
});
elements.passwordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.passwordForm.reportValidity()) return;
  if (elements.newPassword.value !== elements.newPasswordConfirm.value) {
    elements.passwordFeedback.classList.add("error");
    elements.passwordFeedback.textContent = "两次输入的新密码不一致。";
    return;
  }
  const wasRequired = state.passwordChangeRequired;
  elements.submitPassword.disabled = true;
  elements.passwordFeedback.classList.remove("error", "success");
  elements.passwordFeedback.textContent = "正在保存…";
  try {
    const payload = await fetchJson("/api/auth/password", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "change-password",
      },
      body: JSON.stringify({
        current_password: elements.currentPassword.value,
        new_password: elements.newPassword.value,
      }),
    });
    state.currentUser = payload.user;
    state.passwordPolicy = payload.password_policy;
    state.passwordChangeRequired = false;
    elements.passwordFeedback.classList.add("success");
    elements.passwordFeedback.textContent = "密码已更新。";
    if (wasRequired) {
      await enterApplication();
    } else {
      elements.passwordDialog.close();
    }
  } catch (error) {
    elements.passwordFeedback.classList.add("error");
    elements.passwordFeedback.textContent = error.message;
  } finally {
    elements.submitPassword.disabled = false;
  }
});

elements.openAccount.addEventListener("click", () => {
  if (!state.currentUser) return;
  elements.accountIdentity.textContent = `${state.currentUser.username} · ${isAdmin() ? "管理员" : "普通用户"}`;
  elements.profileDisplayName.value = state.currentUser.display_name;
  elements.profileSignature.value = state.currentUser.signature;
  elements.profileFeedback.textContent = "";
  elements.profileFeedback.classList.remove("error", "success");
  elements.accountDialog.showModal();
});
elements.closeAccount.addEventListener("click", () => elements.accountDialog.close());
elements.accountDialog.addEventListener("click", (event) => {
  if (event.target === elements.accountDialog) elements.accountDialog.close();
});
elements.profileForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.profileForm.reportValidity()) return;
  elements.submitProfile.disabled = true;
  elements.profileFeedback.classList.remove("error", "success");
  elements.profileFeedback.textContent = "正在保存…";
  try {
    const payload = await fetchJson("/api/auth/profile", {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "update-profile",
      },
      body: JSON.stringify({
        display_name: elements.profileDisplayName.value.trim(),
        signature: elements.profileSignature.value.trim(),
      }),
    });
    state.currentUser = payload.user;
    applyUserPermissions();
    elements.profileFeedback.classList.add("success");
    elements.profileFeedback.textContent = "昵称和签名已保存。";
    await refresh({ fullRoom: true });
  } catch (error) {
    elements.profileFeedback.classList.add("error");
    elements.profileFeedback.textContent = error.message;
  } finally {
    elements.submitProfile.disabled = false;
  }
});
elements.openPassword.addEventListener("click", () => openPasswordDialog(false));
elements.logout.addEventListener("click", async () => {
  elements.logout.disabled = true;
  try {
    await fetchJson("/api/auth/logout", {
      method: "POST",
      headers: { "X-Agent-Bridge-Intent": "logout" },
    });
  } catch (error) {
    console.error(error);
  } finally {
    elements.logout.disabled = false;
    showAuthScreen("已退出登录。");
  }
});

elements.search.addEventListener("input", (event) => {
  state.filter = event.target.value;
  renderRooms();
});
elements.refreshButton.addEventListener("click", () => refresh({ fullRoom: true }));
elements.openMessageRates.addEventListener("click", openMessageRateDialog);
elements.openRoomPermissions.addEventListener("click", openRoomPermissionDialog);
elements.closeRoomPermissions.addEventListener("click", () => elements.roomPermissionDialog.close());
elements.roomPermissionDialog.addEventListener("click", (event) => {
  if (event.target === elements.roomPermissionDialog) elements.roomPermissionDialog.close();
});
elements.roomPermissionSearchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!isAdmin()) return;
  elements.searchRoomPermissions.disabled = true;
  elements.roomPermissionFeedback.classList.remove("error", "success");
  elements.roomPermissionFeedback.textContent = "正在搜索…";
  try {
    await searchRoomPermissionUsers();
    elements.roomPermissionFeedback.textContent = `找到 ${state.roomPermissionUsers.length} 个普通用户。`;
  } catch (error) {
    elements.roomPermissionFeedback.classList.add("error");
    elements.roomPermissionFeedback.textContent = error.message;
  } finally {
    elements.searchRoomPermissions.disabled = false;
  }
});
elements.closeMessageRates.addEventListener("click", () => elements.messageRateDialog.close());
elements.messageRateDialog.addEventListener("click", (event) => {
  if (event.target === elements.messageRateDialog) elements.messageRateDialog.close();
});
elements.messageRateGlobalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!isAdmin() || !elements.messageRateGlobalForm.reportValidity()) return;
  elements.saveGlobalMessageRates.disabled = true;
  elements.messageRateGlobalFeedback.classList.remove("error", "success");
  elements.messageRateGlobalFeedback.textContent = "正在保存整体设置…";
  try {
    const updates = [
      ["agent", Number(elements.agentGlobalRate.value)],
      ["web_user", Number(elements.webUserGlobalRate.value)],
    ];
    await Promise.all(updates.map(([actorKind, cooldownSeconds]) => fetchJson(
      `/api/message-rates/global/${encodeURIComponent(actorKind)}`,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-Agent-Bridge-Intent": "update-global-message-rate",
        },
        body: JSON.stringify({ cooldown_seconds: cooldownSeconds }),
      },
    )));
    await Promise.all([
      loadMessageRateConfiguration(),
      searchMessageRateParticipants(),
      refresh({}),
    ]);
    elements.messageRateGlobalFeedback.classList.add("success");
    elements.messageRateGlobalFeedback.textContent = "整体设置已保存，并已应用到聊天室。";
  } catch (error) {
    elements.messageRateGlobalFeedback.classList.add("error");
    elements.messageRateGlobalFeedback.textContent = error.message;
  } finally {
    elements.saveGlobalMessageRates.disabled = false;
  }
});
elements.messageRateSearchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!isAdmin() || !elements.messageRateSearchForm.reportValidity()) return;
  elements.searchMessageRates.disabled = true;
  elements.messageRateSearchFeedback.classList.remove("error", "success");
  elements.messageRateSearchFeedback.textContent = "正在搜索…";
  try {
    await searchMessageRateParticipants();
    elements.messageRateSearchFeedback.textContent = `找到 ${state.rateParticipants.length} 个对象。`;
  } catch (error) {
    elements.messageRateSearchFeedback.classList.add("error");
    elements.messageRateSearchFeedback.textContent = error.message;
  } finally {
    elements.searchMessageRates.disabled = false;
  }
});
elements.manageMembers.addEventListener("click", openMemberManagementDialog);
elements.repairResidents.addEventListener("click", async () => {
  if (!isAdmin() || !state.selectedRoom) return;
  const room = state.selectedRoom;
  elements.repairResidents.disabled = true;
  elements.repairResidents.textContent = "修复中…";
  try {
    const payload = await fetchJson(`/api/rooms/${encodeURIComponent(room)}/residents/repair`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "repair-room-residents",
      },
      body: JSON.stringify({}),
    });
    await refreshActiveRoom(false, false);
    const unavailable = payload.unavailable?.length || 0;
    window.alert(unavailable
      ? `值守检查完成：${payload.online_count} 个在线，${unavailable} 个本机无私有配置，需重新邀请。`
      : `值守检查完成：${payload.online_count} 个已在线。`);
  } catch (error) {
    window.alert(`值守修复失败：${error.message}`);
  } finally {
    elements.repairResidents.disabled = false;
    elements.repairResidents.textContent = "修复值守";
  }
});
elements.closeMemberManagement.addEventListener("click", () => elements.memberManagementDialog.close());
elements.memberManagementDialog.addEventListener("click", (event) => {
  if (event.target === elements.memberManagementDialog) elements.memberManagementDialog.close();
});
elements.memberTargetRoom.addEventListener("change", renderMemberRooms);
elements.memberSearch.addEventListener("input", renderMemberRooms);
elements.agentLifecycleForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!isAdmin() || !elements.agentLifecycleForm.reportValidity()) return;
  elements.saveAgentLifecycle.disabled = true;
  elements.agentLifecycleFeedback.classList.remove("error", "success");
  elements.agentLifecycleFeedback.textContent = "正在保存并检查已过期 Agent…";
  try {
    const payload = await fetchJson("/api/agent-lifecycle", {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "update-agent-lifecycle",
      },
      body: JSON.stringify({ inactivity_days: Number(elements.agentInactivityDays.value) }),
    });
    state.agentLifecycle = payload;
    state.memberSelections = new Map();
    await Promise.all([
      refresh({ fullRoom: true }),
      loadMemberManagementData(),
    ]);
    elements.agentLifecycleFeedback.classList.add("success");
    elements.agentLifecycleFeedback.textContent = payload.expired_count > 0
      ? `已保存为 ${payload.inactivity_days} 天，并处理 ${payload.expired_count} 个过期 Agent。`
      : `已保存为 ${payload.inactivity_days} 天；当前没有新增过期 Agent。`;
  } catch (error) {
    elements.agentLifecycleFeedback.classList.add("error");
    elements.agentLifecycleFeedback.textContent = error.message;
  } finally {
    elements.saveAgentLifecycle.disabled = false;
  }
});
elements.memberMigrationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!isAdmin() || !elements.memberMigrationForm.reportValidity()) return;
  const target = elements.memberTargetRoom.value;
  const selections = [];
  for (const [source, participantIds] of state.memberSelections.entries()) {
    if (source === target || participantIds.size === 0) continue;
    selections.push({
      source_conversation_id: source,
      participant_ids: [...participantIds],
    });
  }
  const selectionCount = selections.reduce((total, item) => total + item.participant_ids.length, 0);
  if (!selectionCount) {
    updateMemberSelectionCount();
    return;
  }
  if (!window.confirm(`确认把所选 ${selectionCount} 个 Agent 复制加入“${target}”？来源聊天室会完整保留。`)) return;
  elements.migrateMembers.disabled = true;
  elements.memberManagementFeedback.classList.remove("error", "success");
  elements.memberManagementFeedback.textContent = "正在复制加入目标聊天室…";
  try {
    const payload = await fetchJson("/api/room-memberships/migrate", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "migrate-agents",
      },
      body: JSON.stringify({
        target_conversation_id: target,
        selections,
      }),
    });
    state.memberSelections = new Map();
    await refresh({ fullRoom: true });
    await loadMemberManagementData();
    elements.memberManagementFeedback.classList.add("success");
    elements.memberManagementFeedback.textContent = `已将 ${payload.migration.membership_count} 个成员资格复制加入 ${target}，来源聊天室保持不变。`;
  } catch (error) {
    elements.memberManagementFeedback.classList.add("error");
    elements.memberManagementFeedback.textContent = error.message;
  } finally {
    updateMemberSelectionCount();
  }
});
elements.openCreateRoom.addEventListener("click", () => {
  if (!(isAdmin() || state.currentUser?.can_create_rooms)) return;
  elements.createRoomFeedback.textContent = "";
  elements.createRoomForm.reset();
  elements.createRoomPolicy.textContent = isAdmin()
    ? "管理员创建房间不限数量。"
    : `你已获创建权限，最多可同时拥有 ${state.currentUser.room_limit} 个使用中的聊天室；你将成为所建房间的聊天室管理员。`;
  elements.createRoomDialog.showModal();
  window.setTimeout(() => elements.newRoomId.focus(), 0);
});

function closeCreateDialog() {
  if (elements.createRoomDialog.open) elements.createRoomDialog.close();
}

elements.closeCreateRoom.addEventListener("click", closeCreateDialog);
elements.cancelCreateRoom.addEventListener("click", closeCreateDialog);
elements.createRoomDialog.addEventListener("click", (event) => {
  if (event.target === elements.createRoomDialog) closeCreateDialog();
});

elements.createRoomForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.createRoomForm.reportValidity()) return;
  const conversationId = elements.newRoomId.value.trim();
  elements.submitCreateRoom.disabled = true;
  elements.createRoomFeedback.classList.remove("error", "success");
  elements.createRoomFeedback.textContent = "正在创建…";
  try {
    const payload = await fetchJson("/api/rooms", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "create-room",
      },
      body: JSON.stringify({ conversation_id: conversationId }),
    });
    state.selectedRoom = payload.room.conversation_id;
    window.localStorage.setItem("agentBridgeSelectedRoom", state.selectedRoom);
    elements.createRoomFeedback.classList.add("success");
    elements.createRoomFeedback.textContent = "创建成功";
    await refresh({ fullRoom: true, forceRoomBottom: true });
    closeCreateDialog();
  } catch (error) {
    elements.createRoomFeedback.classList.add("error");
    elements.createRoomFeedback.textContent = roomErrorMessage(error.message);
  } finally {
    elements.submitCreateRoom.disabled = false;
  }
});

function closeRenameDialog() {
  if (elements.renameRoomDialog.open) elements.renameRoomDialog.close();
}

elements.renameRoom.addEventListener("click", () => {
  if (!isAdmin() || !state.selectedRoom) return;
  elements.renameRoomForm.reset();
  elements.renamedRoomId.value = state.selectedRoom;
  elements.renameRoomFeedback.textContent = "";
  elements.renameRoomFeedback.classList.remove("error", "success");
  elements.renameRoomDialog.showModal();
  window.setTimeout(() => elements.renamedRoomId.select(), 0);
});
elements.closeRenameRoom.addEventListener("click", closeRenameDialog);
elements.cancelRenameRoom.addEventListener("click", closeRenameDialog);
elements.renameRoomDialog.addEventListener("click", (event) => {
  if (event.target === elements.renameRoomDialog) closeRenameDialog();
});
elements.renameRoomForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.renameRoomForm.reportValidity() || !state.selectedRoom || !isAdmin()) return;
  const previousRoom = state.selectedRoom;
  const renamedRoom = elements.renamedRoomId.value.trim();
  elements.submitRenameRoom.disabled = true;
  elements.renameRoomFeedback.classList.remove("error", "success");
  elements.renameRoomFeedback.textContent = "正在迁移聊天室关联数据…";
  try {
    const payload = await fetchJson(`/api/rooms/${encodeURIComponent(previousRoom)}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "rename-room",
      },
      body: JSON.stringify({ new_conversation_id: renamedRoom }),
    });
    state.selectedRoom = payload.room.conversation_id;
    state.loadedRoom = null;
    state.messages = [];
    state.participants = [];
    window.localStorage.setItem("agentBridgeSelectedRoom", state.selectedRoom);
    elements.renameRoomFeedback.classList.add("success");
    elements.renameRoomFeedback.textContent = "聊天室已重命名。";
    await refresh({ fullRoom: true, forceRoomBottom: true });
    closeRenameDialog();
  } catch (error) {
    elements.renameRoomFeedback.classList.add("error");
    elements.renameRoomFeedback.textContent = roomErrorMessage(error.message);
  } finally {
    elements.submitRenameRoom.disabled = false;
  }
});

elements.ownerMessageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const activeRoom = state.rooms.find((room) => room.conversation_id === state.selectedRoom);
  const message = elements.ownerMessageBody.value;
  if (!activeRoom || activeRoom.status !== "active" || !message.trim()) return;
  elements.sendOwnerMessage.disabled = true;
  elements.ownerMessageFeedback.classList.remove("error", "success");
  elements.ownerMessageFeedback.textContent = "正在发送…";
  try {
    await fetchJson(`/api/rooms/${encodeURIComponent(activeRoom.conversation_id)}/messages`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "send-message",
      },
      body: JSON.stringify({
        body: message,
        mentions: selectedMentionIds(message),
        reply_to: state.composerReplyTo,
        wake_all_agents: Boolean(state.composerWakeAll && activeRoom.can_wake_all),
      }),
    });
    elements.ownerMessageBody.value = "";
    state.composerMentions.clear();
    clearComposerContext();
    hideMentionMenu();
    elements.ownerMessageFeedback.classList.add("success");
    elements.ownerMessageFeedback.textContent = "已发送";
    await refresh({ forceRoomBottom: true });
    elements.ownerMessageBody.focus();
  } catch (error) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = error.status === 429
      ? `请再等 ${Math.max(1, Math.ceil(error.retryAfterSeconds || 1))} 秒`
      : error.message;
  } finally {
    updateComposer(state.rooms.find((room) => room.conversation_id === state.selectedRoom));
  }
});

elements.cancelComposerContext.addEventListener("click", clearComposerContext);
elements.wakeAllAgents.addEventListener("click", () => {
  const activeRoom = state.rooms.find((room) => room.conversation_id === state.selectedRoom);
  if (!activeRoom?.can_wake_all || activeRoom.status !== "active") return;
  state.composerWakeAll = !state.composerWakeAll;
  if (state.composerWakeAll) state.composerReplyTo = null;
  updateComposerContext();
  elements.ownerMessageBody.focus();
});

elements.ownerMessageBody.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    hideMentionMenu();
    return;
  }
  if (event.key === "Enter" && !elements.mentionMenu.hidden && !event.shiftKey && !event.isComposing) {
    const firstOption = elements.mentionMenu.querySelector("button");
    if (firstOption) {
      event.preventDefault();
      firstOption.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      return;
    }
  }
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    elements.ownerMessageForm.requestSubmit();
  }
});
elements.ownerMessageBody.addEventListener("input", updateMentionMenu);
elements.ownerMessageBody.addEventListener("click", updateMentionMenu);
elements.ownerMessageBody.addEventListener("blur", () => window.setTimeout(hideMentionMenu, 120));

function openAgentAccessDialog(roomId = null) {
  if (!isAdmin()) return;
  elements.accessFeedback.textContent = "";
  elements.accessFeedback.classList.remove("error", "success");
  elements.accessOutput.value = "";
  elements.copyAccess.disabled = true;
  state.generatedAccessInstructions = "";
  populateAccessRooms();
  if (roomId && [...elements.accessRoom.options].some((option) => option.value === roomId)) {
    elements.accessRoom.value = roomId;
  }
  renderSessions();
  renderAgentInvitations();
  renderNicknameRequests();
  elements.agentAccessDialog.showModal();
  window.setTimeout(() => elements.accessProduct.focus(), 0);
}

elements.openAgentAccess.addEventListener("click", () => openAgentAccessDialog(state.selectedRoom));
elements.inviteAgent.addEventListener("click", () => openAgentAccessDialog(state.selectedRoom));

function closeAgentAccessDialog() {
  if (elements.agentAccessDialog.open) elements.agentAccessDialog.close();
}

elements.closeAgentAccess.addEventListener("click", closeAgentAccessDialog);
elements.clearInactiveSessions.addEventListener("click", clearInactiveSessions);
elements.agentAccessDialog.addEventListener("click", (event) => {
  if (event.target === elements.agentAccessDialog) closeAgentAccessDialog();
});

elements.agentAccessForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.agentAccessForm.reportValidity()) return;
  const room = elements.accessRoom.value;
  const product = elements.accessProduct.value.trim();
  const mode = elements.accessMode.value;
  const reusable = elements.agentAccessPolicy.value === "reusable";
  elements.generateAccess.disabled = true;
  elements.accessFeedback.classList.remove("error", "success");
  elements.accessFeedback.textContent = "正在生成…";
  try {
    const payload = await fetchJson("/api/agent-access", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "generate-agent-access",
      },
      body: JSON.stringify({
        conversation_id: room,
        product,
        mode,
        reusable,
      }),
    });
    state.generatedAccessInstructions = payload.access.instructions;
    elements.accessOutput.value = state.generatedAccessInstructions;
    elements.copyAccess.disabled = false;
    elements.accessFeedback.classList.add("success");
    state.agentInvitations.unshift(payload.access.invitation);
    renderAgentInvitations();
    const inviteKind = payload.access.reusable ? "多人复用邀请" : "单次邀请";
    elements.accessFeedback.textContent = payload.access.resident_capable
      && payload.access.requested_mode === "resident"
      ? `${inviteKind}已生成；Agent 接受后会各自配置 listener 和产品适配器。`
      : `${inviteKind}已生成；该产品当前为基础接入，尚不能自动唤醒。`;
  } catch (error) {
    elements.accessFeedback.classList.add("error");
    elements.accessFeedback.textContent = error.message;
  } finally {
    elements.generateAccess.disabled = false;
  }
});

elements.copyAccess.addEventListener("click", async () => {
  if (!state.generatedAccessInstructions) return;
  try {
    await navigator.clipboard.writeText(state.generatedAccessInstructions);
    elements.accessFeedback.classList.remove("error");
    elements.accessFeedback.classList.add("success");
    elements.accessFeedback.textContent = "接入说明已复制，可以直接发给 Agent。";
  } catch (error) {
    elements.accessFeedback.classList.remove("success");
    elements.accessFeedback.classList.add("error");
    elements.accessFeedback.textContent = "浏览器未允许复制，请从文本框手动复制。";
  }
});

function updateNotificationButton() {
  if (!("Notification" in window)) {
    elements.enableNotifications.hidden = true;
    return;
  }
  if (Notification.permission === "granted") {
    elements.enableNotifications.textContent = "通知已开启";
    elements.enableNotifications.disabled = true;
  } else if (Notification.permission === "denied") {
    elements.enableNotifications.textContent = "通知已被浏览器阻止";
    elements.enableNotifications.disabled = true;
  } else {
    elements.enableNotifications.textContent = "开启通知";
    elements.enableNotifications.disabled = false;
  }
}

elements.enableNotifications.addEventListener("click", async () => {
  if (!("Notification" in window)) return;
  await Notification.requestPermission();
  updateNotificationButton();
});

function notifyOwner(changedRooms) {
  if (!("Notification" in window) || Notification.permission !== "granted" || !document.hidden) return;
  const messageCount = changedRooms.reduce((total, room) => total + Number(room.message_count || 0), 0);
  if (!messageCount) return;
  const roomNames = changedRooms.slice(0, 3).map((room) => room.conversation_id).join("、");
  new Notification("Agent Bridge 有新消息", {
    body: `${roomNames}${changedRooms.length > 3 ? " 等聊天室" : ""} · ${messageCount} 条`,
    tag: "agent-bridge-room-activity",
  });
}

function scheduleFallbackRefresh() {
  if (!state.currentUser) return;
  if (state.fallbackRefreshTimer) return;
  state.fallbackRefreshTimer = window.setTimeout(async () => {
    state.fallbackRefreshTimer = null;
    await refresh({});
    if (!state.ownerEvents || state.ownerEvents.readyState !== EventSource.OPEN) {
      scheduleFallbackRefresh();
    }
  }, 30000);
}

function connectOwnerEvents() {
  if (!state.currentUser) return;
  if (!("EventSource" in window)) {
    scheduleFallbackRefresh();
    return;
  }
  if (state.ownerEvents) state.ownerEvents.close();
  const source = new EventSource("/api/events");
  state.ownerEvents = source;
  let receivedInitialState = false;

  const handleState = async (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      console.error(error);
      return;
    }
    const changedRooms = Array.isArray(payload.changed_rooms) ? payload.changed_rooms : [];
    const initialNeedsRefresh = changedRooms.some((changed) => {
      const local = state.rooms.find((room) => room.conversation_id === changed.conversation_id);
      return Number(changed.last_sequence || 0) > Number(local?.last_sequence || 0);
    }) || (isAdmin() && Number(payload.pending_nickname_requests || 0) !== state.nicknameRequests.length);
    if (receivedInitialState || initialNeedsRefresh) {
      if (receivedInitialState) notifyOwner(changedRooms);
      await refresh({});
    }
    receivedInitialState = true;
  };

  source.addEventListener("state", handleState);
  source.addEventListener("state_changed", handleState);
  source.addEventListener("session_closed", () => handleAuthenticationLost());
  source.onopen = () => {
    if (state.fallbackRefreshTimer) {
      window.clearTimeout(state.fallbackRefreshTimer);
      state.fallbackRefreshTimer = null;
    }
  };
  source.onerror = () => {
    setConnection(false, "事件连接重试中");
    scheduleFallbackRefresh();
  };
}

elements.newMessageIndicator.addEventListener("click", () => {
  elements.timeline.scrollTo({
    top: elements.timeline.scrollHeight,
    behavior: "smooth",
  });
  state.unreadMessages = 0;
  updateNewMessageIndicator();
});
elements.timeline.addEventListener("scroll", () => {
  if (isNearTimelineBottom()) {
    state.unreadMessages = 0;
  }
  if (!state.timelineScrollFrame) {
    state.timelineScrollFrame = window.requestAnimationFrame(() => {
      state.timelineScrollFrame = null;
      updateNewMessageIndicator();
    });
  }
}, { passive: true });

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.currentUser) refresh({});
});
window.addEventListener("pagehide", () => state.ownerEvents?.close());

elements.themeSelect.addEventListener("change", () => applyTheme(elements.themeSelect.value));
updateNotificationButton();
bootstrapAuthentication();
