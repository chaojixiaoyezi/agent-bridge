"use strict";


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
elements.openAdminAudit.addEventListener("click", openAdminAuditDialog);
elements.openHistoryGovernance.addEventListener("click", openHistoryGovernanceDialog);
elements.closeAdminAudit.addEventListener("click", () => elements.adminAuditDialog.close());
elements.adminAuditDialog.addEventListener("click", (event) => {
  if (event.target === elements.adminAuditDialog) elements.adminAuditDialog.close();
});
elements.adminAuditFilterForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await loadAdminAudit();
});
elements.refreshAdminAudit.addEventListener("click", () => loadAdminAudit());
elements.loadMoreAdminAudit.addEventListener("click", () => loadAdminAudit({ append: true }));
elements.closeHistoryGovernance.addEventListener("click", () => elements.historyGovernanceDialog.close());
elements.historyGovernanceDialog.addEventListener("click", (event) => {
  if (event.target === elements.historyGovernanceDialog) elements.historyGovernanceDialog.close();
});
elements.historySearchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await loadHistorySearch();
});
elements.historySearchMore.addEventListener("click", () => loadHistorySearch({ append: true }));
elements.exportRoomHistory.addEventListener("click", downloadRoomHistory);
elements.historyRetentionForm.addEventListener("submit", saveHistoryRetentionPolicy);
elements.historyRedactionPreviewForm.addEventListener("submit", previewHistoryRedaction);
elements.executeHistoryRedaction.addEventListener("click", executeHistoryRedaction);
for (const control of [
  elements.historyRedactionRoom,
  elements.historyRedactionReason,
  elements.historyRetentionMode,
  elements.historyRetentionDays,
]) {
  control.addEventListener("input", resetHistoryRedactionPreview);
}
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
  const hasStructuredContent = state.composerAttachments.length || state.composerLinks.length;
  if (!activeRoom || activeRoom.status !== "active") return;
  if (!message.trim() && !hasStructuredContent) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = "请输入文字，或添加文件、图片、链接。";
    return;
  }
  const slashTask = message.trimStart().startsWith("/任务");
  const taskMode = state.composerMode === "task" || slashTask;
  if (taskMode && !activeRoom.can_assign_tasks) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = "你没有在这个聊天室布置任务的权限。";
    return;
  }
  if (taskMode && hasStructuredContent) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = "文件、图片和链接请用聊天模式发送；发送后仍可转为任务。";
    return;
  }
  const mentionIds = selectedMentionIds(message);
  const wakeAll = Boolean(state.composerWakeAll && activeRoom.can_wake_all);
  let messageLinks;
  try {
    messageLinks = taskMode ? [] : composerLinksForMessage(message);
  } catch (error) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = error.message;
    return;
  }
  if (state.composerAttachments.length && !wakeAll && !mentionIds.length) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = "文件和图片必须先 @ 至少一个接收 Agent，或选择 @全员。";
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
          target_participant_ids: mentionIds,
          reply_to: state.composerReplyTo,
        }
      : {
          body: message,
          mentions: mentionIds,
          links: messageLinks,
          reply_to: state.composerReplyTo,
          wake_all_agents: wakeAll,
        };
    const requestOptions = {
      method: "POST",
      headers: {
        "X-Agent-Bridge-Intent": taskMode ? "send-task" : "send-message",
      },
    };
    if (state.composerAttachments.length) {
      const form = new FormData();
      form.set("body", message);
      form.set("mentions", JSON.stringify(mentionIds));
      form.set("links", JSON.stringify(messageLinks));
      form.set("reply_to", state.composerReplyTo || "");
      form.set("wake_all_agents", String(wakeAll));
      for (const item of state.composerAttachments) form.append("files", item.file, item.file.name);
      requestOptions.body = form;
    } else {
      requestOptions.headers["Content-Type"] = "application/json";
      requestOptions.body = JSON.stringify(payload);
    }
    await fetchJson(`/api/rooms/${encodeURIComponent(activeRoom.conversation_id)}/${path}`, requestOptions);
    elements.ownerMessageBody.value = "";
    state.composerMentions.clear();
    clearComposerAssets();
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
  renderComposerAssets();
  updateComposerContext();
  elements.ownerMessageBody.focus();
});

elements.chooseComposerFiles.addEventListener("click", () => {
  if (state.composerMode === "task") return;
  elements.composerFileInput.click();
});
elements.composerFileInput.addEventListener("change", () => {
  try {
    addComposerFiles(elements.composerFileInput.files || []);
    elements.ownerMessageFeedback.classList.remove("error");
    elements.ownerMessageFeedback.textContent = "已加入消息；请确认定向接收 Agent。";
  } catch (error) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = error.message;
  } finally {
    elements.composerFileInput.value = "";
  }
});
elements.toggleComposerLink.addEventListener("click", () => {
  if (state.composerMode === "task") return;
  elements.composerLinkEntry.hidden = !elements.composerLinkEntry.hidden;
  if (!elements.composerLinkEntry.hidden) elements.composerLinkUrl.focus();
});
elements.cancelComposerLink.addEventListener("click", () => {
  elements.composerLinkEntry.hidden = true;
  elements.composerLinkUrl.value = "";
});
function commitComposerLink() {
  try {
    addComposerLink(elements.composerLinkUrl.value);
    elements.ownerMessageFeedback.classList.remove("error");
    elements.ownerMessageFeedback.textContent = "链接已作为可点击卡片加入消息。";
  } catch (error) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = error.message;
  }
}
elements.addComposerLink.addEventListener("click", commitComposerLink);
elements.composerLinkUrl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    commitComposerLink();
  } else if (event.key === "Escape") {
    elements.cancelComposerLink.click();
  }
});
elements.ownerMessageForm.addEventListener("dragover", (event) => {
  if (!event.dataTransfer?.types.includes("Files") || state.composerMode === "task") return;
  event.preventDefault();
  elements.ownerMessageForm.classList.add("drop-target");
});
elements.ownerMessageForm.addEventListener("dragleave", () => {
  elements.ownerMessageForm.classList.remove("drop-target");
});
elements.ownerMessageForm.addEventListener("drop", (event) => {
  elements.ownerMessageForm.classList.remove("drop-target");
  if (!event.dataTransfer?.files?.length || state.composerMode === "task") return;
  event.preventDefault();
  try {
    addComposerFiles(event.dataTransfer.files);
  } catch (error) {
    elements.ownerMessageFeedback.classList.add("error");
    elements.ownerMessageFeedback.textContent = error.message;
  }
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
elements.ownerMessageBody.addEventListener("input", () => {
  updateMentionMenu();
  renderComposerAssets();
  updateComposerContext();
});
elements.ownerMessageBody.addEventListener("paste", (event) => {
  if (state.composerMode === "task") return;
  const pasted = event.clipboardData?.getData("text/plain")?.trim() || "";
  if (!/^https?:\/\/\S+$/iu.test(pasted)) return;
  try {
    addComposerLink(pasted);
  } catch (error) {
    return;
  }
  event.preventDefault();
  elements.ownerMessageFeedback.classList.remove("error");
  elements.ownerMessageFeedback.textContent = "已识别为链接卡片；不会当作普通文字，也不会抓取远程预览。";
});
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
  renderMonitoring();
  renderNicknameRequests();
  elements.agentAccessDialog.showModal();
  if (isAdmin()) {
    elements.connectorHealthFeedback.classList.remove("error", "success");
    elements.connectorHealthFeedback.textContent = "正在核对中央运行状态…";
    loadConnectorHealth().catch((error) => {
      elements.connectorHealthFeedback.classList.add("error");
      elements.connectorHealthFeedback.textContent = `诊断失败：${error.message}`;
    });
    loadMonitoring().catch((error) => {
      elements.monitoringFeedback.classList.add("error");
      elements.monitoringFeedback.textContent = `监控读取失败：${error.message}`;
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
elements.refreshMonitoring.addEventListener("click", async () => {
  elements.refreshMonitoring.disabled = true;
  elements.monitoringFeedback.classList.remove("error", "success");
  elements.monitoringFeedback.textContent = "正在读取持久监控样本…";
  try {
    await loadMonitoring({ force: true });
    elements.monitoringFeedback.classList.add("success");
  } catch (error) {
    elements.monitoringFeedback.classList.add("error");
    elements.monitoringFeedback.textContent = `监控读取失败：${error.message}`;
  } finally {
    elements.refreshMonitoring.disabled = false;
  }
});
elements.monitoringWindow.addEventListener("change", () => {
  loadMonitoring({ force: true }).catch((error) => {
    elements.monitoringFeedback.classList.add("error");
    elements.monitoringFeedback.textContent = `监控读取失败：${error.message}`;
  });
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
  if (changed.size === 1 && changed.has("receipts")) return "receipt";
  if (onlyContains(new Set(["messages", "rooms", "receipts", "highlights"]))) return "room";
  if (onlyContains(new Set(["messages", "rooms", "tasks", "receipts", "highlights"]))) return "task";
  if (["participants", "memberships", "online", "sessions", "connectors", "monitoring"].some(
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
          refreshReceipts: [
            "receipts",
            "memberships",
            "sessions",
            "connectors",
          ].some((facet) => changedFacets.includes(facet)),
          refreshHighlights: changedFacets.includes("highlights"),
          forceMonitoring: changedFacets.includes("monitoring"),
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
  if (state.timelineVirtual?.enabled) {
    renderMessages(state.messages, { forceBottom: true, forceVirtual: true });
    return;
  }
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
      updateTimelineVirtualWindow();
      updateNewMessageIndicator();
    });
  }
}, { passive: true });

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.currentUser) refresh({});
});
window.addEventListener("pagehide", () => state.ownerEvents?.close());

elements.themeSelect.addEventListener("change", () => applyTheme(elements.themeSelect.value));
for (const [index, choice] of elements.themeChoices.entries()) {
  choice.addEventListener("click", () => applyTheme(choice.dataset.themeValue));
  choice.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const step = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1;
    const targetIndex = (index + step + elements.themeChoices.length) % elements.themeChoices.length;
    const target = elements.themeChoices[targetIndex];
    applyTheme(target.dataset.themeValue);
    target.focus();
  });
}
const expandableToolMenus = [
  elements.globalToolsMenu,
  elements.layoutMenu,
  elements.roomSearchMenu,
  elements.roomToolsMenu,
];
for (const menu of expandableToolMenus) {
  menu.addEventListener("toggle", () => {
    if (!menu.open) return;
    for (const otherMenu of expandableToolMenus) {
      if (otherMenu !== menu) otherMenu.open = false;
    }
  });
}
for (const menu of [elements.globalToolsMenu, elements.roomToolsMenu]) {
  for (const button of menu.querySelectorAll("button")) {
    button.addEventListener("click", () => {
      if (button.matches(".theme-choice")) return;
      menu.open = false;
    });
  }
}
document.addEventListener("click", (event) => {
  for (const menu of expandableToolMenus) {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const menu of expandableToolMenus) menu.open = false;
});
updateNotificationButton();
bootstrapAuthentication();
