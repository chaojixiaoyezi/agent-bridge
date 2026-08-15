"use strict";

const state = {
  currentUser: null,
  passwordPolicy: null,
  authMode: "login",
  passwordChangeRequired: false,
  webRegistrationMode: "open",
  emailDeliveryEnabled: false,
  captchaId: null,
  passwordResetCaptchaId: null,
  passwordResetToken: null,
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
  hasLaterMessages: false,
  unreadMessages: 0,
  composerMentions: new Map(),
  composerReplyTo: null,
  composerWakeAll: false,
  composerMode: "chat",
  taskPermissions: null,
  ownerEvents: null,
  fallbackRefreshTimer: null,
  queuedRefresh: null,
  eventRevisions: null,
  generatedAccessInstructions: "",
  messageRateLimits: null,
  rateConfiguration: null,
  rateParticipants: [],
  agentLifecycle: null,
  memberRooms: [],
  memberSelections: new Map(),
  roomWebUsers: [],
  roomWebPermissions: null,
  roomPermissionUsers: [],
  registrationCodes: [],
  generatedRegistrationCode: "",
  theme: "paper",
  roomRenderSignature: "",
  messageRenderSignature: "",
  participantRenderSignature: "",
  participantById: new Map(),
  accessRoomSignature: "",
  sessionRenderSignature: "",
  invitationRenderSignature: "",
  nicknameRenderSignature: "",
  participantFilter: "",
  expandedDormantRooms: new Set(),
  roomSnapshots: new Map(),
  roomSnapshotRestoredAt: 0,
  roomRequestController: null,
  roomSearchRequestController: null,
  roomSearchResults: [],
  roomSearchHasMore: false,
  roomSearchNextBefore: null,
  roomSearchFingerprint: "",
  roomSearchTargetMessageId: null,
  roomHighlights: { items: [], pins: [], decisions: [], count: 0 },
  highlightsLoadedRoom: null,
  avatarCatalog: null,
  avatarByKey: new Map(),
  profileAvatarKey: "auto",
  timelineScrollFrame: null,
  forwardMessageId: null,
  pendingCenter: {
    pending_responses: [],
    active_tasks: [],
    counts: { total: 0 },
    has_more: false,
  },
  connectorHealth: null,
  connectorHealthLoadedAt: 0,
  connectorHealthRenderSignature: "",
};

const ROOM_SNAPSHOT_LIMIT = 4;
const ROOM_SNAPSHOT_FRESH_MS = 15_000;
const INITIAL_ROOM_MESSAGE_LIMIT = 60;
const INCREMENTAL_ROOM_MESSAGE_LIMIT = 100;
const CONNECTOR_HEALTH_CACHE_MS = 15_000;

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
  roomMessageSearchForm: document.querySelector("#room-message-search-form"),
  roomMessageSearchParticipant: document.querySelector("#room-message-search-participant"),
  roomMessageSearchQuery: document.querySelector("#room-message-search-query"),
  roomMessageSearchAdvanced: document.querySelector("#room-message-search-advanced"),
  roomMessageSearchKind: document.querySelector("#room-message-search-kind"),
  roomMessageSearchNotification: document.querySelector("#room-message-search-notification"),
  roomMessageSearchThread: document.querySelector("#room-message-search-thread"),
  roomMessageSearchMarker: document.querySelector("#room-message-search-marker"),
  roomMessageSearchSequence: document.querySelector("#room-message-search-sequence"),
  roomMessageSearchFrom: document.querySelector("#room-message-search-from"),
  roomMessageSearchTo: document.querySelector("#room-message-search-to"),
  searchRoomMessages: document.querySelector("#search-room-messages"),
  clearRoomMessageSearch: document.querySelector("#clear-room-message-search"),
  roomMessageSearchFeedback: document.querySelector("#room-message-search-feedback"),
  roomMessageSearchResults: document.querySelector("#room-message-search-results"),
  peopleList: document.querySelector("#people-list"),
  participantCount: document.querySelector("#participant-count"),
  participantSearch: document.querySelector("#participant-search"),
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
  manageWakePolicy: document.querySelector("#manage-wake-policy"),
  manageTaskPermissions: document.querySelector("#manage-task-permissions"),
  openRoomHighlights: document.querySelector("#open-room-highlights"),
  renameRoom: document.querySelector("#rename-room"),
  themeSelect: document.querySelector("#theme-select"),
  composerChatMode: document.querySelector("#composer-chat-mode"),
  composerTaskMode: document.querySelector("#composer-task-mode"),
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
  forwardMessageDialog: document.querySelector("#forward-message-dialog"),
  forwardMessageForm: document.querySelector("#forward-message-form"),
  closeForwardMessage: document.querySelector("#close-forward-message"),
  cancelForwardMessage: document.querySelector("#cancel-forward-message"),
  forwardSourcePreview: document.querySelector("#forward-source-preview"),
  forwardTargetRoom: document.querySelector("#forward-target-room"),
  forwardNote: document.querySelector("#forward-note"),
  forwardMessageFeedback: document.querySelector("#forward-message-feedback"),
  submitForwardMessage: document.querySelector("#submit-forward-message"),
  openMessageRates: document.querySelector("#open-message-rates"),
  openRoomPermissions: document.querySelector("#open-room-permissions"),
  openRegistrationCodes: document.querySelector("#open-registration-codes"),
  openPendingCenter: document.querySelector("#open-pending-center"),
  pendingCenterBadge: document.querySelector("#pending-center-badge"),
  pendingCenterDialog: document.querySelector("#pending-center-dialog"),
  closePendingCenter: document.querySelector("#close-pending-center"),
  pendingCenterSummary: document.querySelector("#pending-center-summary"),
  pendingCenterFeedback: document.querySelector("#pending-center-feedback"),
  pendingCenterList: document.querySelector("#pending-center-list"),
  roomHighlightsDialog: document.querySelector("#room-highlights-dialog"),
  closeRoomHighlights: document.querySelector("#close-room-highlights"),
  roomHighlightsTitle: document.querySelector("#room-highlights-title"),
  roomHighlightsFeedback: document.querySelector("#room-highlights-feedback"),
  roomHighlightsList: document.querySelector("#room-highlights-list"),
  messageThreadDialog: document.querySelector("#message-thread-dialog"),
  closeMessageThread: document.querySelector("#close-message-thread"),
  messageThreadTitle: document.querySelector("#message-thread-title"),
  messageThreadFeedback: document.querySelector("#message-thread-feedback"),
  messageThreadList: document.querySelector("#message-thread-list"),
  registrationCodeDialog: document.querySelector("#registration-code-dialog"),
  closeRegistrationCodes: document.querySelector("#close-registration-codes"),
  registrationCodeForm: document.querySelector("#registration-code-form"),
  registrationCodeLabel: document.querySelector("#registration-code-label"),
  registrationCodeMaxUses: document.querySelector("#registration-code-max-uses"),
  registrationCodeHours: document.querySelector("#registration-code-hours"),
  registrationCodeFeedback: document.querySelector("#registration-code-feedback"),
  registrationCodeOutput: document.querySelector("#registration-code-output"),
  generatedRegistrationCode: document.querySelector("#generated-registration-code"),
  copyRegistrationCode: document.querySelector("#copy-registration-code"),
  createRegistrationCode: document.querySelector("#create-registration-code"),
  registrationCodeList: document.querySelector("#registration-code-list"),
  roomPermissionDialog: document.querySelector("#room-permission-dialog"),
  closeRoomPermissions: document.querySelector("#close-room-permissions"),
  taskPermissionDialog: document.querySelector("#task-permission-dialog"),
  closeTaskPermissions: document.querySelector("#close-task-permissions"),
  taskPermissionRoom: document.querySelector("#task-permission-room"),
  allowGlobalAdminTasks: document.querySelector("#allow-global-admin-tasks"),
  taskPermissionFeedback: document.querySelector("#task-permission-feedback"),
  taskPermissionResults: document.querySelector("#task-permission-results"),
  wakePolicyDialog: document.querySelector("#wake-policy-dialog"),
  wakePolicyForm: document.querySelector("#wake-policy-form"),
  closeWakePolicy: document.querySelector("#close-wake-policy"),
  wakePolicyRoom: document.querySelector("#wake-policy-room"),
  wakePolicyMode: document.querySelector("#wake-policy-mode"),
  wakePolicyDigestFields: document.querySelector("#wake-policy-digest-fields"),
  wakeDigestMinMessages: document.querySelector("#wake-digest-min-messages"),
  wakeDigestAfterMinutes: document.querySelector("#wake-digest-after-minutes"),
  wakePolicyFeedback: document.querySelector("#wake-policy-feedback"),
  saveWakePolicy: document.querySelector("#save-wake-policy"),
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
  agentLifecycleSection: document.querySelector("#agent-lifecycle-section"),
  roomWebMembersSection: document.querySelector("#room-web-members-section"),
  memberMigrationSection: document.querySelector("#member-migration-section"),
  agentLifecycleForm: document.querySelector("#agent-lifecycle-form"),
  agentInactivityDays: document.querySelector("#agent-inactivity-days"),
  unactivatedAgentInactivityDays: document.querySelector("#unactivated-agent-inactivity-days"),
  agentLifecycleFeedback: document.querySelector("#agent-lifecycle-feedback"),
  saveAgentLifecycle: document.querySelector("#save-agent-lifecycle"),
  memberMigrationForm: document.querySelector("#member-migration-form"),
  memberTargetRoom: document.querySelector("#member-target-room"),
  memberSearch: document.querySelector("#member-search"),
  memberRoomList: document.querySelector("#member-room-list"),
  memberManagementFeedback: document.querySelector("#member-management-feedback"),
  memberSelectionCount: document.querySelector("#member-selection-count"),
  migrateMembers: document.querySelector("#migrate-members"),
  webMemberRoom: document.querySelector("#web-member-room"),
  webMemberSearchForm: document.querySelector("#web-member-search-form"),
  webMemberSearch: document.querySelector("#web-member-search"),
  searchWebMembers: document.querySelector("#search-web-members"),
  webMemberFeedback: document.querySelector("#web-member-feedback"),
  webMemberResults: document.querySelector("#web-member-results"),
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
  connectorHealthSection: document.querySelector("#connector-health-section"),
  connectorHealthSummary: document.querySelector("#connector-health-summary"),
  connectorHealthFeedback: document.querySelector("#connector-health-feedback"),
  connectorHealthList: document.querySelector("#connector-health-list"),
  refreshConnectorHealth: document.querySelector("#refresh-connector-health"),
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
  registerAccessCodeWrap: document.querySelector("#register-access-code-wrap"),
  registerAccessCode: document.querySelector("#register-access-code"),
  registerEmailWrap: document.querySelector("#register-email-wrap"),
  registerEmail: document.querySelector("#register-email"),
  captchaImage: document.querySelector("#captcha-image"),
  captchaAnswer: document.querySelector("#captcha-answer"),
  refreshCaptcha: document.querySelector("#refresh-captcha"),
  authHelp: document.querySelector("#auth-help"),
  authFeedback: document.querySelector("#auth-feedback"),
  submitAuth: document.querySelector("#submit-auth"),
  openPasswordReset: document.querySelector("#open-password-reset"),
  passwordResetRequestDialog: document.querySelector("#password-reset-request-dialog"),
  passwordResetRequestForm: document.querySelector("#password-reset-request-form"),
  closePasswordResetRequest: document.querySelector("#close-password-reset-request"),
  passwordResetIdentifier: document.querySelector("#password-reset-identifier"),
  passwordResetCaptchaImage: document.querySelector("#password-reset-captcha-image"),
  passwordResetCaptchaAnswer: document.querySelector("#password-reset-captcha-answer"),
  refreshPasswordResetCaptcha: document.querySelector("#refresh-password-reset-captcha"),
  passwordResetRequestFeedback: document.querySelector("#password-reset-request-feedback"),
  submitPasswordResetRequest: document.querySelector("#submit-password-reset-request"),
  passwordResetConfirmDialog: document.querySelector("#password-reset-confirm-dialog"),
  passwordResetConfirmForm: document.querySelector("#password-reset-confirm-form"),
  closePasswordResetConfirm: document.querySelector("#close-password-reset-confirm"),
  passwordResetNewPassword: document.querySelector("#password-reset-new-password"),
  passwordResetNewPasswordConfirm: document.querySelector("#password-reset-new-password-confirm"),
  passwordResetPolicyCopy: document.querySelector("#password-reset-policy-copy"),
  passwordResetConfirmFeedback: document.querySelector("#password-reset-confirm-feedback"),
  submitPasswordResetConfirm: document.querySelector("#submit-password-reset-confirm"),
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
  accountEmailSection: document.querySelector("#account-email-section"),
  accountEmailStatus: document.querySelector("#account-email-status"),
  accountEmail: document.querySelector("#account-email"),
  accountEmailPassword: document.querySelector("#account-email-password"),
  sendEmailVerification: document.querySelector("#send-email-verification"),
  accountEmailFeedback: document.querySelector("#account-email-feedback"),
  profileAvatarCurrent: document.querySelector("#profile-avatar-current"),
  profileAvatarVendor: document.querySelector("#profile-avatar-vendor"),
  profileAvatarOptions: document.querySelector("#profile-avatar-options"),
  profileAvatarHelp: document.querySelector("#profile-avatar-help"),
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

function avatarInitial(label, clientType = "") {
  const source = String(label || clientType || "A").trim();
  return Array.from(source)[0] || "A";
}

function canonicalAvatarKey(key) {
  const normalized = String(key || "auto");
  return state.avatarByKey.get(normalized)?.resolved_key || normalized;
}

function avatarCatalogItem(avatarKey, clientType = "") {
  const normalizedKey = String(avatarKey || "auto");
  const direct = state.avatarByKey.get(normalizedKey);
  if (direct?.image_url) return direct;
  if (normalizedKey !== "auto" || !state.avatarCatalog) return direct || null;
  const normalizedClient = String(clientType || "").trim().toLocaleLowerCase("en-US");
  const defaults = Object.entries(state.avatarCatalog.product_defaults || {})
    .sort(([left], [right]) => right.length - left.length);
  for (const [product, defaultKey] of defaults) {
    if (normalizedClient === product || normalizedClient.startsWith(`${product}-`)) {
      return state.avatarByKey.get(defaultKey) || null;
    }
  }
  return direct || null;
}

function createAvatarElement({
  avatarKey = "auto",
  clientType = "",
  label = "",
  status = "",
  className = "",
} = {}) {
  const classes = ["avatar", status, className].filter(Boolean).join(" ");
  const wrapper = makeElement("div", classes);
  const initial = avatarInitial(label, clientType);
  const item = avatarCatalogItem(avatarKey, clientType);
  if (!item?.image_url) {
    wrapper.textContent = item?.mark || initial;
    return wrapper;
  }
  const image = document.createElement("img");
  image.src = item.image_url;
  image.alt = "";
  image.loading = "lazy";
  image.decoding = "async";
  image.addEventListener("error", () => {
    image.remove();
    wrapper.textContent = item.mark || initial;
  }, { once: true });
  wrapper.append(image);
  wrapper.title = `${item.vendor_label || "内置"} · ${item.label}`;
  return wrapper;
}

async function loadAvatarCatalog() {
  if (state.avatarCatalog) return state.avatarCatalog;
  const payload = await fetchJson("/api/avatars");
  state.avatarCatalog = payload;
  state.avatarByKey = new Map(
    (payload.avatars || []).map((item) => [item.key, item]),
  );
  return payload;
}

function renderProfileAvatarCurrent() {
  const key = state.profileAvatarKey || "auto";
  const item = state.avatarByKey.get(key);
  elements.profileAvatarCurrent.replaceChildren(createAvatarElement({
    avatarKey: key,
    clientType: state.currentUser?.username || "web-user",
    label: state.currentUser?.display_name || "用户",
  }));
  elements.profileAvatarHelp.textContent = item
    ? `已选：${item.vendor_label ? `${item.vendor_label} · ` : ""}${item.label}。Web 用户可随时更换；每次只加载当前系列。`
    : "每次只加载当前系列，节省流量。";
}

function renderProfileAvatarOptions(groupKey) {
  const group = (state.avatarCatalog?.groups || [])
    .find((item) => item.key === groupKey);
  elements.profileAvatarOptions.replaceChildren();
  if (!group) return;
  for (const item of group.avatars || []) {
    const key = canonicalAvatarKey(item.key);
    const selected = key === canonicalAvatarKey(state.profileAvatarKey);
    const button = makeElement(
      "button",
      `profile-avatar-option${selected ? " selected" : ""}`,
    );
    button.type = "button";
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(selected));
    button.title = `${group.label} · ${item.label}`;
    button.append(createAvatarElement({
      avatarKey: key,
      clientType: state.currentUser?.username || "web-user",
      label: item.label,
    }));
    button.append(makeElement("span", "", item.label));
    button.addEventListener("click", () => {
      state.profileAvatarKey = key;
      renderProfileAvatarCurrent();
      renderProfileAvatarOptions(group.key);
    });
    elements.profileAvatarOptions.append(button);
  }
}

function populateProfileAvatarPicker() {
  const groups = state.avatarCatalog?.groups || [];
  elements.profileAvatarVendor.replaceChildren();
  for (const group of groups) {
    const option = makeElement("option", "", group.label);
    option.value = group.key;
    elements.profileAvatarVendor.append(option);
  }
  state.profileAvatarKey = canonicalAvatarKey(
    state.currentUser?.avatar_key || "auto",
  );
  const selectedItem = state.avatarByKey.get(state.profileAvatarKey);
  const selectedGroup = groups.some((group) => group.key === selectedItem?.vendor)
    ? selectedItem.vendor
    : "neutral";
  elements.profileAvatarVendor.value = selectedGroup;
  renderProfileAvatarCurrent();
  renderProfileAvatarOptions(selectedGroup);
}

const THEMES = new Set(["paper", "mist", "aurora", "ocean", "violet", "ember"]);
const DATE_TIME_FORMATTERS = {
  shortTime: new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }),
  shortDate: new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }),
  fullTime: new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }),
  day: new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
  }),
  syncTime: new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }),
};

try {
  state.theme = window.localStorage.getItem("agentBridgeTheme") || "paper";
} catch (error) {
  state.theme = "paper";
}

function applyTheme(theme) {
  const selected = THEMES.has(theme) ? theme : "paper";
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
    return DATE_TIME_FORMATTERS.shortTime.format(date);
  }
  return DATE_TIME_FORMATTERS.shortDate.format(date);
}

function fullTime(timestamp) {
  if (!timestamp) return "—";
  return DATE_TIME_FORMATTERS.fullTime.format(new Date(timestamp * 1000));
}

function formatCooldown(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "—";
  if (seconds <= 0) return "不限频";
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${Number.isInteger(seconds) ? seconds : Number(seconds.toFixed(3))} 秒`;
}

function formatAge(value) {
  const seconds = Math.max(0, Number(value) || 0);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function roomSequence(item) {
  return Number(item?.room_sequence ?? item?.sequence ?? 0);
}

function dayLabel(timestamp) {
  return DATE_TIME_FORMATTERS.day.format(new Date(timestamp * 1000));
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
  state.roomRequestController?.abort();
  state.roomRequestController = null;
  state.roomSearchRequestController?.abort();
  state.roomSearchRequestController = null;
  if (state.fallbackRefreshTimer) {
    window.clearTimeout(state.fallbackRefreshTimer);
    state.fallbackRefreshTimer = null;
  }
}

function setAuthMode(mode) {
  state.authMode = mode === "register" && state.webRegistrationMode !== "closed"
    ? "register"
    : "login";
  const registering = state.authMode === "register";
  elements.showRegister.hidden = state.webRegistrationMode === "closed";
  elements.showLogin.classList.toggle("active", !registering);
  elements.showRegister.classList.toggle("active", registering);
  elements.showLogin.setAttribute("aria-selected", String(!registering));
  elements.showRegister.setAttribute("aria-selected", String(registering));
  elements.registerConfirmWrap.hidden = !registering;
  elements.authPasswordConfirm.required = registering;
  const registrationCodeRequired = registering
    && state.webRegistrationMode === "access_code";
  elements.registerAccessCodeWrap.hidden = !registrationCodeRequired;
  elements.registerAccessCode.required = registrationCodeRequired;
  elements.registerEmailWrap.hidden = !(registering && state.emailDeliveryEnabled);
  if (!registering) elements.registerEmail.value = "";
  elements.openPasswordReset.hidden = registering || !state.emailDeliveryEnabled;
  elements.authPassword.autocomplete = registering ? "new-password" : "current-password";
  elements.authTitle.textContent = registering ? "注册 Web 用户" : "登录 Agent Bridge";
  elements.submitAuth.textContent = registering ? "注册并登录" : "登录";
  elements.authHelp.textContent = registering
    ? `${state.passwordPolicy?.description || "密码需为 10–128 个字符，并至少包含四类字符中的三类。"}${registrationCodeRequired ? " 该部署还要求管理员提供的注册码。" : ""}`
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

async function loadPasswordResetCaptcha() {
  elements.refreshPasswordResetCaptcha.disabled = true;
  elements.passwordResetCaptchaImage.removeAttribute("src");
  try {
    const payload = await fetchJson("/api/auth/captcha", { suppressAuthRedirect: true });
    state.passwordResetCaptchaId = payload.captcha.captcha_id;
    elements.passwordResetCaptchaImage.src = payload.captcha.image;
    elements.passwordResetCaptchaAnswer.value = "";
  } catch (error) {
    state.passwordResetCaptchaId = null;
    elements.passwordResetRequestFeedback.classList.add("error");
    elements.passwordResetRequestFeedback.textContent = `验证码加载失败：${error.message}`;
  } finally {
    elements.refreshPasswordResetCaptcha.disabled = false;
  }
}

function takeEmailActionFromHash() {
  const raw = window.location.hash.startsWith("#")
    ? window.location.hash.slice(1)
    : "";
  const separator = raw.indexOf("=");
  if (separator < 1) return null;
  const action = raw.slice(0, separator);
  if (!["verify-email", "reset-password"].includes(action)) return null;
  let token;
  try {
    token = decodeURIComponent(raw.slice(separator + 1));
  } catch (_error) {
    token = "";
  }
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  return { action, token };
}

function showAuthScreen(message = "") {
  closeLiveConnections();
  state.currentUser = null;
  state.passwordChangeRequired = false;
  state.roomSnapshots.clear();
  state.loadedRoom = null;
  state.messages = [];
  state.participants = [];
  state.hasEarlierMessages = false;
  state.hasLaterMessages = false;
  state.roomHighlights = { items: [], pins: [], decisions: [], count: 0 };
  state.highlightsLoadedRoom = null;
  state.pendingCenter = {
    pending_responses: [],
    active_tasks: [],
    counts: { total: 0 },
    has_more: false,
  };
  state.connectorHealth = null;
  state.connectorHealthLoadedAt = 0;
  state.connectorHealthRenderSignature = "";
  renderPendingCenter();
  elements.appShell.hidden = true;
  for (const dialog of [
    elements.passwordDialog,
    elements.passwordResetRequestDialog,
    elements.passwordResetConfirmDialog,
    elements.accountDialog,
    elements.registrationCodeDialog,
    elements.createRoomDialog,
    elements.renameRoomDialog,
    elements.forwardMessageDialog,
    elements.taskPermissionDialog,
    elements.wakePolicyDialog,
    elements.messageRateDialog,
    elements.memberManagementDialog,
    elements.agentAccessDialog,
    elements.pendingCenterDialog,
    elements.roomHighlightsDialog,
    elements.messageThreadDialog,
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
  elements.openRegistrationCodes.hidden = !admin;
  elements.openAgentAccess.hidden = !admin;
  elements.agentInvitationSection.hidden = !admin;
  elements.connectorHealthSection.hidden = !admin;
  elements.agentSessionSection.hidden = !admin;
  elements.nicknameSection.hidden = !admin;
  elements.renameRoom.hidden = !(activeRoom?.can_rename_room);
  elements.inviteAgent.hidden = !(
    activeRoom?.can_invite_agents && activeRoom.status === "active"
  );
  elements.manageMembers.hidden = !(
    activeRoom?.can_manage_web_members && activeRoom.status === "active"
  );
  elements.repairResidents.hidden = !(admin && activeRoom && activeRoom.status === "active");
  elements.manageWakePolicy.hidden = !(
    activeRoom?.can_manage_wake_policy && activeRoom.status === "active"
  );
  elements.manageTaskPermissions.hidden = !(
    activeRoom?.can_manage_task_permissions && activeRoom.status === "active"
  );
  elements.openRoomHighlights.hidden = !activeRoom;
  elements.wakeAllAgents.hidden = !(activeRoom?.can_wake_all && activeRoom.status === "active");
  elements.openAccount.textContent = `${state.currentUser.display_name}${admin ? " · 管理员" : ""}`;
  const agentGlobal = state.messageRateLimits?.agent_global_cooldown_seconds ?? 15;
  const webGlobal = state.messageRateLimits?.web_user_global_cooldown_seconds ?? 60;
  const currentEffective = state.messageRateLimits?.current_user_effective_cooldown_seconds ?? webGlobal;
  elements.rateLimitPill.textContent = admin
    ? `管理员不限频 · Agent 整体 ${formatCooldown(agentGlobal)}`
    : `你的发言间隔：${formatCooldown(currentEffective)}`;
  elements.safetyRateCopy.textContent = admin
    ? `普通聊天不执行；切换“任务”或使用 /任务 才进入执行席位。管理员不限频；Agent 聊天整体间隔为 ${formatCooldown(agentGlobal)}。`
    : `普通聊天不执行；只有获权后切换“任务”或使用 /任务 才进入执行席位。你的聊天间隔为 ${formatCooldown(currentEffective)}。`;
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
  try {
    await loadAvatarCatalog();
  } catch (error) {
    console.error("avatar catalog unavailable", error);
  }
  await refresh({ fullRoom: true });
  connectOwnerEvents();
}

async function bootstrapAuthentication() {
  const emailAction = takeEmailActionFromHash();
  let startupMessage = "";
  try {
    const health = await fetchJson("/api/health", { suppressAuthRedirect: true });
    state.webRegistrationMode = health.web_registration_mode || "open";
    state.emailDeliveryEnabled = Boolean(health.email_delivery_enabled);
  } catch (healthError) {
    console.error("public auth policy unavailable", healthError);
  }
  if (emailAction?.action === "verify-email") {
    try {
      const payload = await fetchJson("/api/auth/email/verify", {
        method: "POST",
        suppressAuthRedirect: true,
        headers: {
          "Content-Type": "application/json",
          "X-Agent-Bridge-Intent": "verify-email",
        },
        body: JSON.stringify({ token: emailAction.token }),
      });
      startupMessage = payload.message;
    } catch (error) {
      startupMessage = error.message;
    }
  }
  if (emailAction?.action === "reset-password") {
    state.passwordResetToken = emailAction.token;
    elements.passwordResetConfirmForm.reset();
    elements.passwordResetConfirmFeedback.textContent = "";
    elements.passwordResetPolicyCopy.textContent = state.passwordPolicy?.description
      || "密码需为 10–128 个字符，并至少包含小写字母、大写字母、数字、符号中的三类。";
    if (!elements.passwordResetConfirmDialog.open) {
      elements.passwordResetConfirmDialog.showModal();
    }
    return;
  }
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
    showAuthScreen(
      startupMessage || (error.status === 401 ? "" : `无法检查登录状态：${error.message}`),
    );
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

function createMessageElement(message) {
  const article = makeElement("article", "message");
  article.dataset.messageId = message.message_id;
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
  article.append(makeElement("p", "message-body", message.body));

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
  article.append(makeElement("p", "receipt-label", `#${roomSequence(message)} · ${message.ack_count}/${message.receipt_count} 已确认/已通知`));

  if (message.refs.length) {
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
    const replyButton = makeElement("button", "message-reply-button", "回复");
    replyButton.type = "button";
    replyButton.addEventListener("click", () => startComposerReply(message));
    article.append(replyButton);
  }
  const rootMessage = message.reply_to
    ? state.messages.find((item) => item.message_id === message.reply_to)
    : message;
  const loadedReplyCount = state.messages.filter(
    (item) => item.reply_to === (rootMessage?.message_id || message.message_id),
  ).length;
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
  if (isAdmin() && message.message_kind !== "forward" && state.rooms.some(
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

function messageSignature(messages) {
  return `${state.selectedRoom || ""}:${state.hasEarlierMessages}:${state.hasLaterMessages}:${roomHighlightSignature()}:${messages.map((item) => `${item.message_id}:${item.sender_display_name || ""}:${item.sender_signature || ""}:${item.sender_avatar_key || "auto"}:${item.sender_seat || "unknown"}:${item.task?.updated_at || item.updated_at || 0}:${item.body_delivery?.delivered_count || 0}:${item.body_delivery?.applied_count || 0}:${item.ack_count || 0}:${item.receipt_count || 0}:${item.reply_count || 0}`).join("|")}`;
}

function renderMessages(
  messages,
  { forceBottom = false, addedCount = 0, targetMessageId = null } = {},
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
  const signature = messageSignature(messages);
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
  if (state.hasLaterMessages) {
    const latest = makeElement("button", "return-latest-button", "回到最新消息");
    latest.type = "button";
    latest.addEventListener("click", loadLatestMessages);
    fragment.append(latest);
  }
  elements.timeline.replaceChildren(fragment);

  if (targetMessageId) {
    const requestedRoom = state.selectedRoom;
    window.requestAnimationFrame(() => {
      if (state.selectedRoom !== requestedRoom) return;
      const target = [...elements.timeline.querySelectorAll("article[data-message-id]")]
        .find((item) => item.dataset.messageId === targetMessageId);
      if (!target) return;
      const desiredTop = target.offsetTop
        - Math.max(16, (elements.timeline.clientHeight - target.offsetHeight) / 2);
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
  } else if (anchor) {
    const anchored = [...elements.timeline.querySelectorAll("article[data-message-id]")]
      .find((item) => item.dataset.messageId === anchor.messageId);
    if (anchored) {
      const timelineTop = elements.timeline.getBoundingClientRect().top;
      elements.timeline.scrollTop += anchored.getBoundingClientRect().top - timelineTop - anchor.offset;
    } else {
      const heightDelta = elements.timeline.scrollHeight - previousScrollHeight;
      elements.timeline.scrollTop = Math.max(0, previousScrollTop + Math.min(0, heightDelta));
    }
    state.unreadMessages += addedCount;
  }
  updateNewMessageIndicator();
}

function appendMessages(messages, { forceBottom = false } = {}) {
  if (!messages.length) return;
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
  const counts = new Map(
    messages.map((message) => [message.message_id, message]),
  );
  for (const article of elements.timeline.querySelectorAll("article[data-message-id]")) {
    const message = counts.get(article.dataset.messageId);
    if (!message) continue;
    const label = article.querySelector(".receipt-label");
    if (label) {
      label.textContent = `#${roomSequence(message)} · ${message.ack_count}/${message.receipt_count} 已确认/已通知`;
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
  if (!isWebUser && !archived) {
    const mention = makeElement("button", "mention-button", "@");
    mention.type = "button";
    mention.title = `特别通知 ${person.display_name || person.client_type}`;
    mention.addEventListener("click", () => addComposerMention(person));
    head.append(mention);
    const activeRoom = state.rooms.find(
      (room) => room.conversation_id === state.selectedRoom,
    );
    if (activeRoom?.can_kick_agents && state.selectedRoom) {
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
    offline: "listener 离线",
    failed: "异常",
    setup: "接入中",
    manual: "手动接入",
  }[value] || value || "未知";
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
    elements.connectorHealthList.append(card);
  }
  elements.connectorHealthFeedback.classList.remove("error", "success");
  elements.connectorHealthFeedback.textContent = health.attention_count
    ? `诊断完成：${health.attention_count} 个自动值守连接需要关注。`
    : "诊断完成：中央 Bridge 未发现自动值守异常。";
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

function roomCreator(room) {
  if (room.owner_display_name) return `${room.owner_display_name} 创建`;
  if (room.creator_kind === "user") return "管理员创建";
  if (room.creator_kind === "legacy") return "历史房间";
  return `${room.creator_client_type || room.creator_participant_id || "Agent"} 创建`;
}

function updateComposer(room) {
  const canSpeak = Boolean(room && room.status === "active");
  if (state.composerMode === "task" && !room?.can_assign_tasks) {
    state.composerMode = "chat";
  }
  const effectiveCooldown = state.messageRateLimits?.current_user_effective_cooldown_seconds ?? 60;
  elements.ownerMessageBody.disabled = !canSpeak;
  elements.sendOwnerMessage.disabled = !canSpeak;
  elements.composerTaskMode.hidden = !(canSpeak && room?.can_assign_tasks);
  elements.composerChatMode.classList.toggle("active", state.composerMode === "chat");
  elements.composerTaskMode.classList.toggle("active", state.composerMode === "task");
  elements.sendOwnerMessage.textContent = state.composerMode === "task" ? "提交任务" : "发送";
  elements.wakeAllAgents.hidden = !(
    canSpeak && room?.can_wake_all && state.composerMode === "chat"
  );
  elements.ownerMessageBody.placeholder = canSpeak
    ? state.composerMode === "task"
      ? "描述要完成的任务；@ Agent 可指定候选，不 @ 则由群内一个 Agent 先领取并按需分工…"
      : `${state.currentUser?.display_name || "Web 用户"}发言（${isAdmin() ? "不限频" : `每个房间间隔 ${formatCooldown(effectiveCooldown)}`}）；Enter 发送，Shift+Enter 换行…`
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
  if (mode === "task" && !room?.can_assign_tasks) return;
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
  elements.roomRoute.textContent = abandoned ? "ABANDONED · HISTORY ONLY" : "ROOM · EVENT LIVE VIEW";
  elements.roomSummary.textContent = activeRoom
    ? abandoned
      ? `已废弃，Agent 不可进入 · ${activeRoom.participant_count} 个历史会话 · ${activeRoom.message_count} 条消息永久保留`
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
  state.participants = snapshot.participants;
  state.hasEarlierMessages = snapshot.hasEarlierMessages;
  state.hasLaterMessages = Boolean(snapshot.hasLaterMessages);
  state.unreadMessages = snapshot.unreadMessages;
  state.roomSearchTargetMessageId = snapshot.searchTargetMessageId || null;
  state.roomHighlights = snapshot.roomHighlights
    || { items: [], pins: [], decisions: [], count: 0 };
  state.highlightsLoadedRoom = snapshot.highlightsLoaded ? roomId : null;
  state.roomSnapshotRestoredAt = Number(snapshot.cachedAt || 0);
  state.participantRenderSignature = "";
  renderParticipants(state.participants);
  if (snapshot.timelineNodes?.length) {
    elements.timeline.replaceChildren(...snapshot.timelineNodes);
    state.messageRenderSignature = messageSignature(state.messages);
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
  state.composerMode = "chat";
  state.taskPermissions = null;
  clearComposerContext();
  clearRoomMessageSearch();
  hideMentionMenu();
  updateNewMessageIndicator();
  window.localStorage.setItem("agentBridgeSelectedRoom", roomId);
  renderRooms();
  const restored = restoreRoomSnapshot(roomId);
  if (!restored) {
    state.loadedRoom = null;
    state.messageRenderSignature = "";
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
    refreshParticipants: !snapshotFresh,
    refreshReceipts: restored && !snapshotFresh,
    refreshHighlights: !snapshotFresh,
  }).catch((error) => {
    if (error.name !== "AbortError") console.error(error);
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
  const total = Number(counts.total || 0);
  elements.pendingCenterBadge.hidden = total === 0;
  elements.pendingCenterBadge.textContent = total > 99 ? "99+" : String(total);
  elements.openPendingCenter.classList.toggle("has-pending", total > 0);

  elements.pendingCenterSummary.replaceChildren();
  for (const [label, count, tone] of [
    ["需要我回复", counts.incoming || 0, "urgent"],
    ["等待对方", counts.outgoing || 0, "waiting"],
    ["聊天室关注", counts.oversight || 0, "oversight"],
    ["进行中任务", counts.active_tasks || 0, "task"],
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
  if (!responseItems.length && !taskItems.length) {
    const empty = makeElement("div", "pending-center-empty");
    empty.append(
      makeElement("strong", "", "当前没有待处理事项"),
      makeElement("p", "", "必须回复的消息已经回应，聊天室任务也都已结束。"),
    );
    elements.pendingCenterList.append(empty);
    return;
  }

  if (responseItems.length) {
    elements.pendingCenterList.append(makeElement("h3", "pending-section-title", "必须回复"));
    for (const item of responseItems) {
      const directionLabels = {
        incoming: "需要我回复",
        outgoing: "等待对方",
        oversight: "聊天室关注",
      };
      const button = makeElement("button", `pending-center-item ${item.direction}`);
      button.type = "button";
      const heading = makeElement("span", "pending-item-heading");
      heading.append(
        makeElement("strong", "", item.conversation_id),
        makeElement("span", `pending-kind ${item.direction}`, directionLabels[item.direction] || "待回复"),
      );
      const sender = item.sender?.display_name || item.sender?.client_type || "未知成员";
      const target = item.target?.display_name || item.target?.client_type || "未知成员";
      button.append(
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
          `#${roomSequence(item)} · ${formatAge(item.age_seconds)} · ${item.delivery_state === "delivered" ? "已送达，等待回复" : "等待送达或处理"}`,
        ),
      );
      button.addEventListener("click", () => locatePendingCenterItem(item));
      elements.pendingCenterList.append(button);
    }
  }

  if (taskItems.length) {
    elements.pendingCenterList.append(makeElement("h3", "pending-section-title", "进行中任务"));
    const taskLabels = {
      queued: "等待领取",
      claimed: "已领取",
      running: "执行中",
      needs_input: "等待补充",
    };
    for (const task of taskItems) {
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
    refreshParticipants = true,
    refreshTaskState = false,
    refreshReceipts = false,
    refreshHighlights = false,
  } = {},
) {
  if (!state.selectedRoom) return;
  state.roomRequestController?.abort();
  const controller = new AbortController();
  state.roomRequestController = controller;
  const requestVersion = ++state.requestVersion;
  const selectedRoom = state.selectedRoom;
  const encodedRoom = encodeURIComponent(selectedRoom);
  const activeRoom = state.rooms.find((room) => room.conversation_id === selectedRoom);
  const initialLoad = fullRoom || state.loadedRoom !== selectedRoom;
  const browsingHistory = !initialLoad && state.hasLaterMessages;
  const lastLoadedSequence = state.messages[state.messages.length - 1]?.sequence || 0;
  const hasServerUpdates = Number(activeRoom?.last_sequence || 0) > lastLoadedSequence;
  const messageRequest = initialLoad
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
  const highlightRequest = initialLoad || refreshHighlights
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
    if (initialLoad) {
      state.messages = messagePayload.messages;
      state.hasEarlierMessages = Boolean(
        messagePayload.has_earlier ?? messagePayload.has_more,
      );
      state.hasLaterMessages = Boolean(messagePayload.has_later);
      state.loadedRoom = selectedRoom;
    } else {
      const knownIds = new Set(state.messages.map((message) => message.message_id));
      appendedMessages = messagePayload.messages.filter(
        (message) => !knownIds.has(message.message_id),
      );
      addedCount = appendedMessages.length;
      state.messages = mergeMessages(state.messages, messagePayload.messages);
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
    const appendOnly = !initialLoad
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
    if (!initialLoad && !refreshTaskState && messagePayload.has_more) {
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

const REFRESH_MODE_PRIORITY = { room: 1, task: 2, presence: 3, full: 4 };

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
      pendingCenterPayload,
    ] = await Promise.all([
      healthRequest,
      fetchJson("/api/rooms?limit=200"),
      sessionRequest,
      nicknameRequest,
      invitationRequest,
      connectorHealthRequest,
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
      window.setTimeout(() => refresh(queued), 0);
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

elements.showLogin.addEventListener("click", () => setAuthMode("login"));
elements.showRegister.addEventListener("click", () => setAuthMode("register"));
elements.refreshCaptcha.addEventListener("click", loadCaptcha);
elements.authDialog.addEventListener("cancel", (event) => event.preventDefault());
elements.openPasswordReset.addEventListener("click", () => {
  elements.passwordResetRequestForm.reset();
  elements.passwordResetRequestFeedback.textContent = "";
  elements.passwordResetRequestFeedback.classList.remove("error", "success");
  if (elements.authDialog.open) elements.authDialog.close();
  elements.passwordResetRequestDialog.showModal();
  loadPasswordResetCaptcha();
});
elements.closePasswordResetRequest.addEventListener("click", () => {
  elements.passwordResetRequestDialog.close();
  showAuthScreen();
});
elements.passwordResetRequestDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  elements.closePasswordResetRequest.click();
});
elements.refreshPasswordResetCaptcha.addEventListener("click", loadPasswordResetCaptcha);
elements.passwordResetRequestForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.passwordResetRequestForm.reportValidity() || !state.passwordResetCaptchaId) return;
  elements.submitPasswordResetRequest.disabled = true;
  elements.passwordResetRequestFeedback.classList.remove("error", "success");
  elements.passwordResetRequestFeedback.textContent = "正在提交…";
  try {
    const payload = await fetchJson("/api/auth/password-reset/request", {
      method: "POST",
      suppressAuthRedirect: true,
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "request-password-reset",
      },
      body: JSON.stringify({
        identifier: elements.passwordResetIdentifier.value.trim(),
        captcha_id: state.passwordResetCaptchaId,
        captcha_answer: elements.passwordResetCaptchaAnswer.value.trim(),
      }),
    });
    elements.passwordResetRequestFeedback.classList.add("success");
    elements.passwordResetRequestFeedback.textContent = payload.message;
    state.passwordResetCaptchaId = null;
    elements.passwordResetCaptchaImage.removeAttribute("src");
  } catch (error) {
    elements.passwordResetRequestFeedback.classList.add("error");
    elements.passwordResetRequestFeedback.textContent = error.message;
    await loadPasswordResetCaptcha();
  } finally {
    elements.submitPasswordResetRequest.disabled = false;
  }
});
elements.closePasswordResetConfirm.addEventListener("click", () => {
  state.passwordResetToken = null;
  elements.passwordResetConfirmDialog.close();
  showAuthScreen();
});
elements.passwordResetConfirmDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  elements.closePasswordResetConfirm.click();
});
elements.passwordResetConfirmForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.passwordResetConfirmForm.reportValidity() || !state.passwordResetToken) return;
  if (elements.passwordResetNewPassword.value !== elements.passwordResetNewPasswordConfirm.value) {
    elements.passwordResetConfirmFeedback.classList.add("error");
    elements.passwordResetConfirmFeedback.textContent = "两次输入的新密码不一致。";
    return;
  }
  elements.submitPasswordResetConfirm.disabled = true;
  elements.passwordResetConfirmFeedback.classList.remove("error", "success");
  elements.passwordResetConfirmFeedback.textContent = "正在更新…";
  try {
    const payload = await fetchJson("/api/auth/password-reset/confirm", {
      method: "POST",
      suppressAuthRedirect: true,
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "confirm-password-reset",
      },
      body: JSON.stringify({
        token: state.passwordResetToken,
        new_password: elements.passwordResetNewPassword.value,
      }),
    });
    state.passwordResetToken = null;
    elements.passwordResetConfirmDialog.close();
    showAuthScreen(payload.message);
  } catch (error) {
    elements.passwordResetConfirmFeedback.classList.add("error");
    elements.passwordResetConfirmFeedback.textContent = error.message;
  } finally {
    elements.submitPasswordResetConfirm.disabled = false;
  }
});

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
    const authenticationPayload = {
      username: elements.authUsername.value.trim(),
      password: elements.authPassword.value,
      captcha_id: state.captchaId,
      captcha_answer: elements.captchaAnswer.value.trim(),
    };
    if (registering && state.webRegistrationMode === "access_code") {
      authenticationPayload.registration_code = elements.registerAccessCode.value;
    }
    if (registering && state.emailDeliveryEnabled && elements.registerEmail.value.trim()) {
      authenticationPayload.email = elements.registerEmail.value.trim();
    }
    const payload = await fetchJson(`/api/auth/${registering ? "register" : "login"}`, {
      method: "POST",
      suppressAuthRedirect: true,
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": registering ? "register" : "login",
      },
      body: JSON.stringify(authenticationPayload),
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

elements.openAccount.addEventListener("click", async () => {
  if (!state.currentUser) return;
  elements.accountIdentity.textContent = `${state.currentUser.username} · ${isAdmin() ? "管理员" : "普通用户"}`;
  state.profileAvatarKey = state.currentUser.avatar_key || "auto";
  elements.profileDisplayName.value = state.currentUser.display_name;
  elements.profileSignature.value = state.currentUser.signature;
  elements.accountEmailSection.hidden = !state.emailDeliveryEnabled;
  elements.accountEmail.value = "";
  elements.accountEmailPassword.value = "";
  elements.accountEmailFeedback.textContent = "";
  elements.accountEmailFeedback.classList.remove("error", "success");
  if (state.currentUser.email_verified) {
    elements.accountEmailStatus.textContent = `已验证：${state.currentUser.email_masked}`;
  } else if (state.currentUser.email_verification_pending) {
    elements.accountEmailStatus.textContent = `等待验证：${state.currentUser.pending_email_masked}`;
  } else {
    elements.accountEmailStatus.textContent = "尚未绑定邮箱。";
  }
  elements.profileFeedback.textContent = "";
  elements.profileFeedback.classList.remove("error", "success");
  elements.accountDialog.showModal();
  try {
    await loadAvatarCatalog();
    populateProfileAvatarPicker();
  } catch (error) {
    elements.profileFeedback.classList.add("error");
    elements.profileFeedback.textContent = `头像目录加载失败：${error.message}`;
  }
});
elements.sendEmailVerification.addEventListener("click", async () => {
  if (!elements.accountEmail.value.trim() || !elements.accountEmailPassword.value) {
    elements.accountEmailFeedback.classList.add("error");
    elements.accountEmailFeedback.textContent = "请输入新邮箱和当前密码。";
    return;
  }
  elements.sendEmailVerification.disabled = true;
  elements.accountEmailFeedback.classList.remove("error", "success");
  elements.accountEmailFeedback.textContent = "正在提交…";
  try {
    const payload = await fetchJson("/api/auth/email/request", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "request-email-verification",
      },
      body: JSON.stringify({
        email: elements.accountEmail.value.trim(),
        current_password: elements.accountEmailPassword.value,
      }),
    });
    state.currentUser = payload.user;
    elements.accountEmailPassword.value = "";
    elements.accountEmailStatus.textContent = `等待验证：${state.currentUser.pending_email_masked}`;
    elements.accountEmailFeedback.classList.add("success");
    elements.accountEmailFeedback.textContent = payload.message;
  } catch (error) {
    elements.accountEmailFeedback.classList.add("error");
    elements.accountEmailFeedback.textContent = error.message;
  } finally {
    elements.sendEmailVerification.disabled = false;
  }
});
elements.profileAvatarVendor.addEventListener("change", () => {
  renderProfileAvatarOptions(elements.profileAvatarVendor.value);
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
        avatar_key: state.profileAvatarKey,
      }),
    });
    state.currentUser = payload.user;
    applyUserPermissions();
    elements.profileFeedback.classList.add("success");
    elements.profileFeedback.textContent = "昵称、签名和头像已保存。";
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
elements.openRegistrationCodes.addEventListener("click", openRegistrationCodeDialog);
elements.closeRegistrationCodes.addEventListener("click", () => {
  state.generatedRegistrationCode = "";
  elements.generatedRegistrationCode.textContent = "";
  elements.registrationCodeOutput.hidden = true;
  elements.registrationCodeDialog.close();
});
elements.registrationCodeDialog.addEventListener("click", (event) => {
  if (event.target !== elements.registrationCodeDialog) return;
  state.generatedRegistrationCode = "";
  elements.generatedRegistrationCode.textContent = "";
  elements.registrationCodeOutput.hidden = true;
  elements.registrationCodeDialog.close();
});
elements.registrationCodeDialog.addEventListener("close", () => {
  state.generatedRegistrationCode = "";
  elements.generatedRegistrationCode.textContent = "";
  elements.registrationCodeOutput.hidden = true;
});
elements.registrationCodeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.registrationCodeForm.reportValidity()) return;
  elements.createRegistrationCode.disabled = true;
  elements.registrationCodeFeedback.classList.remove("error", "success");
  elements.registrationCodeFeedback.textContent = "正在生成注册码…";
  try {
    const payload = await fetchJson("/api/admin/web-registration-codes", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "create-registration-code",
      },
      body: JSON.stringify({
        label: elements.registrationCodeLabel.value.trim(),
        max_uses: Number(elements.registrationCodeMaxUses.value),
        expires_in_hours: Number(elements.registrationCodeHours.value),
      }),
    });
    state.generatedRegistrationCode = payload.registration_code.code;
    elements.generatedRegistrationCode.textContent = state.generatedRegistrationCode;
    elements.registrationCodeOutput.hidden = false;
    elements.registrationCodeFeedback.classList.add("success");
    elements.registrationCodeFeedback.textContent = "注册码已生成。请立即复制；关闭窗口后不会再次显示明文。";
    elements.registrationCodeLabel.value = "";
    await loadRegistrationCodes();
  } catch (error) {
    elements.registrationCodeFeedback.classList.add("error");
    elements.registrationCodeFeedback.textContent = error.message;
  } finally {
    elements.createRegistrationCode.disabled = false;
  }
});
elements.copyRegistrationCode.addEventListener("click", async () => {
  if (!state.generatedRegistrationCode) return;
  try {
    await navigator.clipboard.writeText(state.generatedRegistrationCode);
    elements.registrationCodeFeedback.classList.remove("error");
    elements.registrationCodeFeedback.classList.add("success");
    elements.registrationCodeFeedback.textContent = "注册码已复制。";
  } catch (error) {
    elements.registrationCodeFeedback.classList.remove("success");
    elements.registrationCodeFeedback.classList.add("error");
    elements.registrationCodeFeedback.textContent = "浏览器未允许复制，请手动复制上方注册码。";
  }
});
elements.manageTaskPermissions.addEventListener("click", openTaskPermissionDialog);
elements.manageWakePolicy.addEventListener("click", openWakePolicyDialog);
elements.closeWakePolicy.addEventListener("click", () => elements.wakePolicyDialog.close());
elements.wakePolicyDialog.addEventListener("click", (event) => {
  if (event.target === elements.wakePolicyDialog) elements.wakePolicyDialog.close();
});
elements.wakePolicyMode.addEventListener("change", updateWakePolicyFields);
elements.wakePolicyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const room = state.rooms.find((item) => item.conversation_id === state.selectedRoom);
  if (!room?.can_manage_wake_policy || !elements.wakePolicyForm.reportValidity()) return;
  elements.saveWakePolicy.disabled = true;
  elements.wakePolicyFeedback.classList.remove("error", "success");
  elements.wakePolicyFeedback.textContent = "正在保存…";
  try {
    const policy = await fetchJson(
      `/api/rooms/${encodeURIComponent(room.conversation_id)}/wake-policy`,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-Agent-Bridge-Intent": "manage-wake-policy",
        },
        body: JSON.stringify({
          mode: elements.wakePolicyMode.value,
          digest_min_messages: Number(elements.wakeDigestMinMessages.value),
          digest_after_seconds: Number(elements.wakeDigestAfterMinutes.value) * 60,
        }),
      },
    );
    room.wake_policy = policy;
    elements.wakePolicyFeedback.classList.add("success");
    const labels = { mention: "只在 @ 时唤醒", digest: "成批唤醒", all: "每条普通消息唤醒" };
    elements.wakePolicyFeedback.textContent = `已保存：${labels[policy.mode] || policy.mode}；仍不强制回复。`;
  } catch (error) {
    elements.wakePolicyFeedback.classList.add("error");
    elements.wakePolicyFeedback.textContent = error.message;
  } finally {
    elements.saveWakePolicy.disabled = false;
  }
});
elements.closeTaskPermissions.addEventListener("click", () => elements.taskPermissionDialog.close());
elements.taskPermissionDialog.addEventListener("click", (event) => {
  if (event.target === elements.taskPermissionDialog) elements.taskPermissionDialog.close();
});
elements.allowGlobalAdminTasks.addEventListener("change", async () => {
  if (!state.selectedRoom) return;
  elements.allowGlobalAdminTasks.disabled = true;
  elements.taskPermissionFeedback.classList.remove("error", "success");
  elements.taskPermissionFeedback.textContent = "正在保存…";
  try {
    state.taskPermissions = await fetchJson(
      `/api/rooms/${encodeURIComponent(state.selectedRoom)}/task-policy`,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-Agent-Bridge-Intent": "manage-task-permissions",
        },
        body: JSON.stringify({
          allow_global_admin: elements.allowGlobalAdminTasks.checked,
        }),
      },
    );
    renderTaskPermissionMembers();
    elements.taskPermissionFeedback.classList.add("success");
    elements.taskPermissionFeedback.textContent = "全局管理员任务权限已保存。";
    await refresh({});
  } catch (error) {
    elements.taskPermissionFeedback.classList.add("error");
    elements.taskPermissionFeedback.textContent = error.message;
    elements.allowGlobalAdminTasks.checked = Boolean(
      state.taskPermissions?.allow_global_admin,
    );
  } finally {
    elements.allowGlobalAdminTasks.disabled = false;
  }
});
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
elements.webMemberRoom.addEventListener("change", loadRoomWebUsers);
elements.webMemberSearchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await loadRoomWebUsers();
});
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
      body: JSON.stringify({
        inactivity_days: Number(elements.agentInactivityDays.value),
        unactivated_inactivity_days: Number(elements.unactivatedAgentInactivityDays.value),
      }),
    });
    state.agentLifecycle = payload;
    state.memberSelections = new Map();
    await Promise.all([
      refresh({ fullRoom: true }),
      loadMemberManagementData(),
    ]);
    elements.agentLifecycleFeedback.classList.add("success");
    elements.agentLifecycleFeedback.textContent = payload.expired_count > 0
      ? `已保存：正常成员 ${payload.inactivity_days} 天、未激活成员 ${payload.unactivated_inactivity_days} 天，并处理 ${payload.expired_count} 个过期 Agent。`
      : `已保存：正常成员 ${payload.inactivity_days} 天、未激活成员 ${payload.unactivated_inactivity_days} 天；当前没有新增过期 Agent。`;
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
    const unavailable = payload.migration.room_seats?.unavailable?.length || 0;
    elements.memberManagementFeedback.textContent = unavailable
      ? `已将 ${payload.migration.membership_count} 个成员资格复制加入 ${target}，来源聊天室保持不变；${unavailable} 个值守席位需重新邀请。`
      : `已将 ${payload.migration.membership_count} 个成员资格复制加入 ${target}，独立值守席位已就绪，来源聊天室保持不变。`;
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
    state.roomSnapshots.delete(state.selectedRoom);
    state.loadedRoom = null;
    state.messages = [];
    state.participants = [];
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
  const room = state.rooms.find((item) => item.conversation_id === state.selectedRoom);
  if (!room?.can_rename_room || !state.selectedRoom) return;
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
  const room = state.rooms.find((item) => item.conversation_id === state.selectedRoom);
  if (!elements.renameRoomForm.reportValidity() || !state.selectedRoom || !room?.can_rename_room) return;
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
    state.roomSnapshots.delete(previousRoom);
    state.roomSnapshots.delete(state.selectedRoom);
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

function closeForwardDialog() {
  state.forwardMessageId = null;
  if (elements.forwardMessageDialog.open) elements.forwardMessageDialog.close();
}

function openForwardDialog(message) {
  if (!isAdmin() || !message) return;
  state.forwardMessageId = message.message_id;
  elements.forwardMessageForm.reset();
  elements.forwardTargetRoom.replaceChildren();
  const targets = state.rooms.filter(
    (room) => room.status === "active" && room.conversation_id !== message.conversation_id,
  );
  for (const room of targets) {
    const option = document.createElement("option");
    option.value = room.conversation_id;
    option.textContent = room.conversation_id;
    elements.forwardTargetRoom.append(option);
  }
  const sender = message.sender_display_name || message.sender_client_type;
  elements.forwardSourcePreview.textContent = `来源「${message.conversation_id}」#${roomSequence(message)} · ${sender}：${message.body.slice(0, 180)}`;
  elements.forwardMessageFeedback.textContent = "";
  elements.forwardMessageFeedback.classList.remove("error", "success");
  if (!targets.length) return;
  elements.forwardMessageDialog.showModal();
  window.setTimeout(() => elements.forwardTargetRoom.focus(), 0);
}

elements.closeForwardMessage.addEventListener("click", closeForwardDialog);
elements.cancelForwardMessage.addEventListener("click", closeForwardDialog);
elements.forwardMessageDialog.addEventListener("click", (event) => {
  if (event.target === elements.forwardMessageDialog) closeForwardDialog();
});
elements.forwardMessageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!isAdmin() || !state.forwardMessageId || !elements.forwardMessageForm.reportValidity()) return;
  elements.submitForwardMessage.disabled = true;
  elements.forwardMessageFeedback.classList.remove("error", "success");
  elements.forwardMessageFeedback.textContent = "正在建立可追溯转发…";
  try {
    const target = elements.forwardTargetRoom.value;
    await fetchJson(`/api/messages/${encodeURIComponent(state.forwardMessageId)}/forward`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": "forward-message",
      },
      body: JSON.stringify({
        target_conversation_id: target,
        note: elements.forwardNote.value.trim(),
      }),
    });
    elements.forwardMessageFeedback.classList.add("success");
    elements.forwardMessageFeedback.textContent = `已显式转发到「${target}」。`;
    await refresh({});
    closeForwardDialog();
  } catch (error) {
    elements.forwardMessageFeedback.classList.add("error");
    elements.forwardMessageFeedback.textContent = error.message;
  } finally {
    elements.submitForwardMessage.disabled = false;
  }
});

elements.ownerMessageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const activeRoom = state.rooms.find((room) => room.conversation_id === state.selectedRoom);
  const message = elements.ownerMessageBody.value;
  if (!activeRoom || activeRoom.status !== "active" || !message.trim()) return;
  const slashTask = message.trimStart().startsWith("/任务");
  const taskMode = state.composerMode === "task" || slashTask;
  if (taskMode && !activeRoom.can_assign_tasks) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = "你没有在这个聊天室布置任务的权限。";
    return;
  }
  elements.sendOwnerMessage.disabled = true;
  elements.ownerMessageFeedback.classList.remove("error", "success");
  elements.ownerMessageFeedback.textContent = "正在发送…";
  try {
    const path = taskMode ? "tasks" : "messages";
    const payload = taskMode
      ? {
          body: message,
          target_participant_ids: selectedMentionIds(message),
          reply_to: state.composerReplyTo,
        }
      : {
          body: message,
          mentions: selectedMentionIds(message),
          reply_to: state.composerReplyTo,
          wake_all_agents: Boolean(state.composerWakeAll && activeRoom.can_wake_all),
        };
    await fetchJson(`/api/rooms/${encodeURIComponent(activeRoom.conversation_id)}/${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Bridge-Intent": taskMode ? "send-task" : "send-message",
      },
      body: JSON.stringify(payload),
    });
    elements.ownerMessageBody.value = "";
    state.composerMentions.clear();
    clearComposerContext();
    hideMentionMenu();
    elements.ownerMessageFeedback.classList.add("success");
    elements.ownerMessageFeedback.textContent = taskMode ? "任务已提交" : "已发送";
    await refresh({
      mode: taskMode ? "task" : "room",
      forceRoomBottom: true,
    });
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
elements.composerChatMode.addEventListener("click", () => setComposerMode("chat"));
elements.composerTaskMode.addEventListener("click", () => setComposerMode("task"));
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

async function openAgentAccessDialog(roomId = null) {
  const room = state.rooms.find((item) => item.conversation_id === roomId);
  if (!isAdmin() && !room?.can_invite_agents) return;
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
  renderConnectorHealth();
  renderNicknameRequests();
  elements.agentAccessDialog.showModal();
  if (isAdmin()) {
    elements.connectorHealthFeedback.classList.remove("error", "success");
    elements.connectorHealthFeedback.textContent = "正在核对中央运行状态…";
    loadConnectorHealth().catch((error) => {
      elements.connectorHealthFeedback.classList.add("error");
      elements.connectorHealthFeedback.textContent = `诊断失败：${error.message}`;
    });
  }
  if (!isAdmin()) {
    try {
      const payload = await fetchAgentInvitations(elements.accessRoom.value);
      state.agentInvitations = payload.invitations || [];
      state.invitationRenderSignature = "";
      renderAgentInvitations();
    } catch (error) {
      elements.accessFeedback.classList.add("error");
      elements.accessFeedback.textContent = error.message;
    }
  }
  window.setTimeout(() => elements.accessProduct.focus(), 0);
}

elements.openAgentAccess.addEventListener("click", () => openAgentAccessDialog(state.selectedRoom));
elements.inviteAgent.addEventListener("click", () => openAgentAccessDialog(state.selectedRoom));

function closeAgentAccessDialog() {
  if (elements.agentAccessDialog.open) elements.agentAccessDialog.close();
}

elements.closeAgentAccess.addEventListener("click", closeAgentAccessDialog);
elements.clearInactiveSessions.addEventListener("click", clearInactiveSessions);
elements.refreshConnectorHealth.addEventListener("click", async () => {
  elements.refreshConnectorHealth.disabled = true;
  elements.connectorHealthFeedback.classList.remove("error", "success");
  elements.connectorHealthFeedback.textContent = "正在重新核对中央运行状态…";
  try {
    await loadConnectorHealth({ force: true });
    elements.connectorHealthFeedback.classList.add("success");
  } catch (error) {
    elements.connectorHealthFeedback.classList.add("error");
    elements.connectorHealthFeedback.textContent = `诊断失败：${error.message}`;
  } finally {
    elements.refreshConnectorHealth.disabled = false;
  }
});
elements.agentAccessDialog.addEventListener("click", (event) => {
  if (event.target === elements.agentAccessDialog) closeAgentAccessDialog();
});
elements.accessRoom.addEventListener("change", async () => {
  if (isAdmin()) return;
  try {
    const payload = await fetchAgentInvitations(elements.accessRoom.value);
    state.agentInvitations = payload.invitations || [];
    state.invitationRenderSignature = "";
    renderAgentInvitations();
  } catch (error) {
    elements.accessFeedback.classList.add("error");
    elements.accessFeedback.textContent = error.message;
  }
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
    if (payload.access.quick_start?.kind === "claude-code-direct-accept") {
      elements.accessFeedback.textContent = `${inviteKind}已生成；Claude Code 可直接执行一键命令，无需重启现有 MCP/TUI。`;
    } else if (payload.access.quick_start?.kind === "deepseek-harness-cordis-patch") {
      elements.accessFeedback.textContent = `${inviteKind}已生成；DeepSeek Harness 可通过 Cordis HMR 热加载 MCP，无需重启。`;
    } else {
      elements.accessFeedback.textContent = payload.access.resident_capable
        && payload.access.requested_mode === "resident"
        ? `${inviteKind}已生成；Agent 接受后会各自配置 listener 和产品适配器。`
        : `${inviteKind}已生成；该产品当前为基础接入，尚不能自动唤醒。`;
    }
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

function changedRevisionFacets(nextRevisions) {
  const previous = state.eventRevisions;
  state.eventRevisions = nextRevisions;
  if (!previous || !nextRevisions) return [];
  const keys = new Set([...Object.keys(previous), ...Object.keys(nextRevisions)]);
  return [...keys].filter(
    (key) => JSON.stringify(previous[key]) !== JSON.stringify(nextRevisions[key]),
  );
}

function refreshModeForEvent(changedFacets) {
  if (!changedFacets.length) return null;
  const changed = new Set(changedFacets);
  const onlyContains = (allowed) => [...changed].every((item) => allowed.has(item));
  if (onlyContains(new Set(["messages", "rooms", "receipts", "highlights"]))) return "room";
  if (onlyContains(new Set(["messages", "rooms", "tasks", "receipts", "highlights"]))) return "task";
  if (["participants", "memberships", "online", "sessions", "connectors"].some(
    (facet) => changed.has(facet),
  )) {
    return changed.has("tasks") ? "full" : "presence";
  }
  return "full";
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
    const namedRevisions = payload.state_revisions && typeof payload.state_revisions === "object"
      ? payload.state_revisions
      : null;
    const changedFacets = changedRevisionFacets(namedRevisions);
    const initialNeedsRefresh = changedRooms.some((changed) => {
      const local = state.rooms.find((room) => room.conversation_id === changed.conversation_id);
      return Number(changed.last_sequence || 0) > Number(local?.last_sequence || 0);
    })
      || changedFacets.length > 0
      || (isAdmin() && Number(payload.pending_nickname_requests || 0) !== state.nicknameRequests.length);
    if (receivedInitialState || initialNeedsRefresh) {
      if (receivedInitialState) notifyOwner(changedRooms);
      const mode = namedRevisions
        ? (refreshModeForEvent(changedFacets) || (initialNeedsRefresh ? "room" : null))
        : "full";
      if (mode) {
        await refresh({
          mode,
          refreshTaskState: changedFacets.includes("nicknames")
            || changedFacets.includes("participants"),
          refreshReceipts: changedFacets.includes("receipts"),
          refreshHighlights: changedFacets.includes("highlights"),
        });
      }
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
