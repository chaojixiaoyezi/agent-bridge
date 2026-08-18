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
  monitoring: null,
  monitoringLoadedAt: 0,
  monitoringRenderSignature: "",
  monitoringKnownOpenAlerts: null,
  adminAudit: {
    events: [],
    facets: { categories: [], actors: [] },
    summary: {},
    has_more: false,
    next_before_sequence: null,
  },
  historyGovernance: {
    searchResults: [],
    searchHasMore: false,
    searchNextBefore: null,
    retention: null,
    redactionPreview: null,
  },
  roomsPanelCollapsed: false,
  peoplePanelCollapsed: true,
  workspaceDensity: "detailed",
  workspaceFocusMode: false,
  roomsPanelWidth: 236,
  peoplePanelWidth: 260,
  composerInputHeight: 58,
  composerPanelCollapsed: false,
};

const ROOM_SNAPSHOT_LIMIT = 4;
const ROOM_SNAPSHOT_FRESH_MS = 15_000;
const INITIAL_ROOM_MESSAGE_LIMIT = 60;
const INCREMENTAL_ROOM_MESSAGE_LIMIT = 100;
const CONNECTOR_HEALTH_CACHE_MS = 15_000;
const MONITORING_CACHE_MS = 60_000;

const elements = {
  appShell: document.querySelector("#app-shell"),
  workspace: document.querySelector("#workspace"),
  layoutMenu: document.querySelector("#layout-menu"),
  toggleRoomsPanel: document.querySelector("#toggle-rooms-panel"),
  togglePeoplePanel: document.querySelector("#toggle-people-panel"),
  roomsResizer: document.querySelector("#rooms-resizer"),
  peopleResizer: document.querySelector("#people-resizer"),
  composerResizer: document.querySelector("#composer-resizer"),
  toggleComposerPanel: document.querySelector("#toggle-composer-panel"),
  layoutToggleComposer: document.querySelector("#layout-toggle-composer"),
  layoutComposerStatus: document.querySelector("#layout-composer-status"),
  toggleFocusMode: document.querySelector("#toggle-focus-mode"),
  layoutFocusStatus: document.querySelector("#layout-focus-status"),
  layoutDensityCompact: document.querySelector("#layout-density-compact"),
  layoutDensityDetailed: document.querySelector("#layout-density-detailed"),
  resetWorkspaceLayout: document.querySelector("#reset-workspace-layout"),
  globalToolsMenu: document.querySelector("#global-tools-menu"),
  roomToolsMenu: document.querySelector("#room-tools-menu"),
  roomSearchMenu: document.querySelector("#room-search-menu"),
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
  themeChoices: [...document.querySelectorAll(".theme-choice")],
  themeCurrentMode: document.querySelector("#theme-current-mode"),
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
  openAdminAudit: document.querySelector("#open-admin-audit"),
  openHistoryGovernance: document.querySelector("#open-history-governance"),
  adminAuditDialog: document.querySelector("#admin-audit-dialog"),
  closeAdminAudit: document.querySelector("#close-admin-audit"),
  adminAuditFilterForm: document.querySelector("#admin-audit-filter-form"),
  adminAuditQuery: document.querySelector("#admin-audit-query"),
  adminAuditCategory: document.querySelector("#admin-audit-category"),
  adminAuditOutcome: document.querySelector("#admin-audit-outcome"),
  adminAuditActor: document.querySelector("#admin-audit-actor"),
  adminAuditHours: document.querySelector("#admin-audit-hours"),
  adminAuditRoom: document.querySelector("#admin-audit-room"),
  refreshAdminAudit: document.querySelector("#refresh-admin-audit"),
  adminAuditSummary: document.querySelector("#admin-audit-summary"),
  adminAuditFeedback: document.querySelector("#admin-audit-feedback"),
  adminAuditList: document.querySelector("#admin-audit-list"),
  loadMoreAdminAudit: document.querySelector("#load-more-admin-audit"),
  historyGovernanceDialog: document.querySelector("#history-governance-dialog"),
  closeHistoryGovernance: document.querySelector("#close-history-governance"),
  historySearchForm: document.querySelector("#history-search-form"),
  historySearchQuery: document.querySelector("#history-search-query"),
  historySearchRoom: document.querySelector("#history-search-room"),
  historySearchSender: document.querySelector("#history-search-sender"),
  historySearchKind: document.querySelector("#history-search-kind"),
  historySearchFrom: document.querySelector("#history-search-from"),
  historySearchTo: document.querySelector("#history-search-to"),
  historySearchFeedback: document.querySelector("#history-search-feedback"),
  historySearchResults: document.querySelector("#history-search-results"),
  historySearchMore: document.querySelector("#history-search-more"),
  historyExportRoom: document.querySelector("#history-export-room"),
  exportRoomHistory: document.querySelector("#export-room-history"),
  historyExportFeedback: document.querySelector("#history-export-feedback"),
  historyRetentionForm: document.querySelector("#history-retention-form"),
  historyRetentionMode: document.querySelector("#history-retention-mode"),
  historyRetentionDays: document.querySelector("#history-retention-days"),
  saveHistoryRetention: document.querySelector("#save-history-retention"),
  historyRetentionFeedback: document.querySelector("#history-retention-feedback"),
  historyRedactionPreviewForm: document.querySelector("#history-redaction-preview-form"),
  historyRedactionRoom: document.querySelector("#history-redaction-room"),
  historyRedactionReason: document.querySelector("#history-redaction-reason"),
  previewHistoryRedaction: document.querySelector("#preview-history-redaction"),
  historyRedactionFeedback: document.querySelector("#history-redaction-feedback"),
  historyRedactionConfirm: document.querySelector("#history-redaction-confirm"),
  historyRedactionSummary: document.querySelector("#history-redaction-summary"),
  historyRedactionPhrase: document.querySelector("#history-redaction-phrase"),
  historyRedactionConfirmation: document.querySelector("#history-redaction-confirmation"),
  executeHistoryRedaction: document.querySelector("#execute-history-redaction"),
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
  monitoringAlertBadge: document.querySelector("#monitoring-alert-badge"),
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
  monitoringWindow: document.querySelector("#monitoring-window"),
  refreshMonitoring: document.querySelector("#refresh-monitoring"),
  monitoringSummary: document.querySelector("#monitoring-summary"),
  monitoringTrends: document.querySelector("#monitoring-trends"),
  monitoringFeedback: document.querySelector("#monitoring-feedback"),
  monitoringAlertList: document.querySelector("#monitoring-alert-list"),
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
  for (const choice of elements.themeChoices) {
    const active = choice.dataset.themeValue === selected;
    choice.classList.toggle("active", active);
    choice.setAttribute("aria-checked", String(active));
    choice.tabIndex = active ? 0 : -1;
    if (active) elements.themeCurrentMode.textContent = choice.dataset.themeMode || "主题";
  }
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
  state.monitoring = null;
  state.monitoringLoadedAt = 0;
  state.monitoringRenderSignature = "";
  state.monitoringKnownOpenAlerts = null;
  state.adminAudit = {
    events: [],
    facets: { categories: [], actors: [] },
    summary: {},
    has_more: false,
    next_before_sequence: null,
  };
  state.historyGovernance = {
    searchResults: [],
    searchHasMore: false,
    searchNextBefore: null,
    retention: null,
    redactionPreview: null,
  };
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
    elements.adminAuditDialog,
    elements.historyGovernanceDialog,
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
  elements.openAdminAudit.hidden = !admin;
  elements.openHistoryGovernance.hidden = !admin;
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
  const visibleRoomTools = [
    elements.inviteAgent,
    elements.manageMembers,
    elements.repairResidents,
    elements.manageWakePolicy,
    elements.manageTaskPermissions,
    elements.openRoomHighlights,
    elements.renameRoom,
  ].some((button) => !button.hidden);
  elements.roomToolsMenu.hidden = !visibleRoomTools;
  elements.roomSearchMenu.hidden = !activeRoom;
  if (!visibleRoomTools) elements.roomToolsMenu.open = false;
  if (!activeRoom) elements.roomSearchMenu.open = false;
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
