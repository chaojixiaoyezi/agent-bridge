import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type {
	ExtensionAPI,
	ExtensionCommandContext,
} from "@earendil-works/pi-coding-agent";

type RelayCommand = {
	type: "submit" | "steer";
	request_id: string;
	session_id: string;
	text: string;
};

type Binding = {
	adapter_kind: "pi";
	endpoint_id: string;
	native_session_id: string;
	transport: {
		kind: "pi-extension";
		command_file: string;
		event_file: string;
		session_file: string;
	};
};

type ActiveRequest = {
	id: string;
	eventFile: string;
	prompt: string;
	targetSessionFile: string;
	started: boolean;
	pendingSteers: string[];
};

type SharedRelayState = {
	commandContext?: ExtensionCommandContext;
	currentSessionFile?: string;
	endpointId?: string;
	activeRequest?: ActiveRequest;
};

const SHARED_STATE_KEY = "__agentBridgePiRelayStateV1";

function sharedRelayState(): SharedRelayState {
	const processGlobal = globalThis as typeof globalThis & {
		[SHARED_STATE_KEY]?: SharedRelayState;
	};
	processGlobal[SHARED_STATE_KEY] ??= {};
	return processGlobal[SHARED_STATE_KEY];
}

export function canDeliverToPiSession(
	targetSessionFile: string,
	currentSessionFile?: string,
): boolean {
	if (fs.existsSync(targetSessionFile)) return true;
	return Boolean(
		currentSessionFile &&
			path.resolve(currentSessionFile) === path.resolve(targetSessionFile),
	);
}

function assistantText(message: unknown): string {
	if (!message || typeof message !== "object") return "";
	const value = message as { role?: unknown; content?: unknown };
	if (value.role !== "assistant") return "";
	if (typeof value.content === "string") return value.content;
	if (!Array.isArray(value.content)) return "";
	return value.content
		.filter((part): part is { type: "text"; text: string } =>
			Boolean(
				part &&
					typeof part === "object" &&
					(part as { type?: unknown }).type === "text" &&
					typeof (part as { text?: unknown }).text === "string",
			),
		)
		.map((part) => part.text)
		.join("");
}

function lastAssistantText(messages: unknown): string {
	if (!Array.isArray(messages)) return "";
	for (let index = messages.length - 1; index >= 0; index -= 1) {
		const text = assistantText(messages[index]);
		if (text.trim()) return text.trim();
	}
	return "";
}

function appendEvent(filePath: string, event: Record<string, unknown>): void {
	fs.appendFileSync(filePath, `${JSON.stringify(event)}\n`, {
		encoding: "utf8",
		mode: 0o600,
	});
}

function writeHeartbeat(binding: Binding, at: number): void {
	const filePath = `${binding.transport.event_file}.heartbeat`;
	const temporary = `${filePath}.${process.pid}.tmp`;
	fs.writeFileSync(
		temporary,
		`${JSON.stringify({
			endpoint_id: binding.endpoint_id,
			session_id: binding.native_session_id,
			at,
		})}\n`,
		{ encoding: "utf8", mode: 0o600 },
	);
	fs.renameSync(temporary, filePath);
	fs.chmodSync(filePath, 0o600);
}

function connectorRoots(): string[] {
	const override = process.env.AGENT_BRIDGE_CONNECTOR_HOME?.trim();
	if (override) return [path.resolve(override)];
	if (process.platform === "darwin") {
		return [
			path.join(
				os.homedir(),
				"Library",
				"Application Support",
				"AgentBridge",
				"connectors",
			),
		];
	}
	return [
		path.join(os.homedir(), ".local", "state", "agent-bridge", "connectors"),
	];
}

function discoveredBindingFiles(): string[] {
	const configured = process.env.AGENT_BRIDGE_TUI_BINDING_FILE?.trim();
	const files = configured ? [path.resolve(configured)] : [];
	for (const root of connectorRoots()) {
		if (!fs.existsSync(root)) continue;
		for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
			if (!entry.isDirectory()) continue;
			files.push(path.join(root, entry.name, "tui-binding.json"));
		}
	}
	return [...new Set(files)];
}

function readBinding(configPath: string): Binding {
	const value = JSON.parse(
		fs.readFileSync(configPath, "utf8"),
	) as Partial<Binding>;
	if (value.adapter_kind !== "pi" || value.transport?.kind !== "pi-extension") {
		throw new Error("not a Pi Agent Bridge binding");
	}
	for (const candidate of [
		value.transport.command_file,
		value.transport.event_file,
		value.transport.session_file,
	]) {
		if (typeof candidate !== "string" || !path.isAbsolute(candidate)) {
			throw new Error("Pi Agent Bridge binding paths must be absolute");
		}
	}
	if (!value.endpoint_id || !value.native_session_id) {
		throw new Error("Pi Agent Bridge binding identity is incomplete");
	}
	return value as Binding;
}

export default function (pi: ExtensionAPI) {
	const shared = sharedRelayState();
	const offsets = new Map<string, number>();
	const bindings = new Map<string, Binding>();
	const draining = new Set<string>();
	const watchedFiles = new Map<string, fs.StatsListener>();
	const terminalRequests = new Set<string>();

	const deliver = async (binding: Binding, command: RelayCommand) => {
		if (command.session_id !== binding.native_session_id) {
			throw new Error("relay command session does not match this room binding");
		}
		if (command.type === "steer") {
			if (
				!shared.activeRequest ||
				shared.activeRequest.id !== command.request_id
			) {
				throw new Error("Pi TUI has no matching active Bridge request");
			}
			if (shared.activeRequest.started) {
				pi.sendUserMessage(command.text, { deliverAs: "steer" });
			} else {
				shared.activeRequest.pendingSteers.push(command.text);
			}
			return;
		}
		if (shared.activeRequest) {
			throw new Error("Pi TUI already has an active Bridge request");
		}
		const commandContext = shared.commandContext;
		const currentSession =
			commandContext?.sessionManager.getSessionFile() ??
			shared.currentSessionFile;
		// Pi assigns a stable session path before the first user message, but
		// creates the JSONL only when that first message is accepted. The current
		// live session is therefore a valid target even when its file has not been
		// materialized yet. A non-current missing file still cannot be resumed.
		if (
			!canDeliverToPiSession(binding.transport.session_file, currentSession)
		) {
			throw new Error("bound Pi session file does not exist");
		}
		shared.activeRequest = {
			id: command.request_id,
			eventFile: binding.transport.event_file,
			prompt: command.text,
			targetSessionFile: binding.transport.session_file,
			started: false,
			pendingSteers: [],
		};
		if (
			currentSession &&
			path.resolve(currentSession) ===
				path.resolve(binding.transport.session_file)
		) {
			pi.sendUserMessage(command.text, { deliverAs: "followUp" });
			return;
		}
		if (!commandContext) {
			throw new Error("run /agent-bridge-bind once in this Pi TUI");
		}
		await commandContext.waitForIdle();
		const result = await commandContext.switchSession(
			binding.transport.session_file,
			{
				withSession: async (ctx) => {
					// Pi recreates every extension on session replacement. Keep the fresh
					// command context in process-global state so the new extension instance
					// can switch to another room later without another manual bind.
					shared.commandContext = ctx;
					shared.currentSessionFile = ctx.sessionManager.getSessionFile();
					await ctx.sendUserMessage(command.text, { deliverAs: "followUp" });
				},
			},
		);
		if (result.cancelled) throw new Error("Pi session switch was cancelled");
	};

	const drain = async (binding: Binding) => {
		const filePath = binding.transport.command_file;
		if (draining.has(filePath) || !fs.existsSync(filePath)) return;
		draining.add(filePath);
		try {
			const start = offsets.get(filePath) ?? 0;
			const buffer = fs.readFileSync(filePath);
			if (buffer.length <= start) return;
			const content = buffer.subarray(start).toString("utf8");
			const completeLength = content.lastIndexOf("\n") + 1;
			if (completeLength === 0) return;
			offsets.set(
				filePath,
				start + Buffer.byteLength(content.slice(0, completeLength)),
			);
			for (const line of content.slice(0, completeLength).split("\n")) {
				if (!line.trim()) continue;
				let command: RelayCommand | undefined;
				try {
					command = JSON.parse(line) as RelayCommand;
					if (terminalRequests.has(command.request_id)) continue;
					if (
						command.type === "submit" &&
						shared.activeRequest?.id === command.request_id
					) {
						continue;
					}
					await deliver(binding, command);
				} catch (error) {
					const requestId = command?.request_id ?? "unknown";
					appendEvent(binding.transport.event_file, {
						type: "error",
						request_id: requestId,
						error: error instanceof Error ? error.message : String(error),
					});
					if (requestId !== "unknown") terminalRequests.add(requestId);
					if (shared.activeRequest?.id === requestId) {
						shared.activeRequest = undefined;
					}
				}
			}
		} finally {
			draining.delete(filePath);
		}
	};

	const installBinding = (configPath: string): boolean => {
		if (!fs.existsSync(configPath)) return false;
		const binding = readBinding(configPath);
		if (shared.endpointId && binding.endpoint_id !== shared.endpointId) {
			return false;
		}
		const commandFile = binding.transport.command_file;
		if (bindings.has(commandFile)) return false;
		for (const filePath of [commandFile, binding.transport.event_file]) {
			if (!fs.existsSync(filePath))
				fs.writeFileSync(filePath, "", { mode: 0o600 });
			fs.chmodSync(filePath, 0o600);
		}
		for (const line of fs
			.readFileSync(binding.transport.event_file, "utf8")
			.split("\n")) {
			if (!line.trim()) continue;
			try {
				const event = JSON.parse(line) as {
					type?: unknown;
					request_id?: unknown;
				};
				if (
					["complete", "error", "failed"].includes(String(event.type ?? "")) &&
					typeof event.request_id === "string"
				) {
					terminalRequests.add(event.request_id);
				}
			} catch {
				// Ignore a partial or unrelated historical line.
			}
		}
		bindings.set(commandFile, binding);
		// Replay from the beginning, but only for request ids without a durable
		// terminal event. This recovers commands written while Pi was offline
		// without rerunning already-completed historical turns.
		offsets.set(commandFile, 0);
		const watcher: fs.StatsListener = () => void drain(binding);
		watchedFiles.set(commandFile, watcher);
		fs.watchFile(commandFile, { interval: 250 }, watcher);
		void drain(binding);
		return true;
	};

	const discoverBindings = (): number => {
		if (!shared.endpointId) return 0;
		let installed = 0;
		for (const configPath of discoveredBindingFiles()) {
			try {
				const binding = readBinding(configPath);
				if (
					binding.endpoint_id === shared.endpointId &&
					installBinding(configPath)
				) {
					installed += 1;
				}
			} catch {
				// Ignore unrelated or partially-written connector directories. An
				// explicit /agent-bridge-bind call still reports its validation error.
			}
		}
		return installed;
	};

	const selectEndpoint = (configPath?: string): string => {
		let selected: string | undefined;
		if (configPath) {
			selected = readBinding(configPath).endpoint_id;
		} else if (shared.endpointId) {
			selected = shared.endpointId;
		} else {
			const candidates: Binding[] = [];
			for (const candidatePath of discoveredBindingFiles()) {
				try {
					candidates.push(readBinding(candidatePath));
				} catch {
					// Ignore unrelated connector directories.
				}
			}
			const currentSession = shared.currentSessionFile
				? path.resolve(shared.currentSessionFile)
				: undefined;
			const currentEndpoints = new Set(
				candidates
					.filter(
						(binding) =>
							currentSession &&
							path.resolve(binding.transport.session_file) === currentSession,
					)
					.map((binding) => binding.endpoint_id),
			);
			const allEndpoints = new Set(
				candidates.map((binding) => binding.endpoint_id),
			);
			if (currentEndpoints.size === 1) {
				selected = [...currentEndpoints][0];
			} else if (allEndpoints.size === 1) {
				selected = [...allEndpoints][0];
			} else if (allEndpoints.size > 1) {
				throw new Error(
					"multiple Pi endpoint identities exist; pass this invitation's tui-binding.json path",
				);
			}
		}
		if (!selected) {
			throw new Error(
				"no Pi room binding found; pass this invitation's tui-binding.json path",
			);
		}
		if (shared.endpointId && shared.endpointId !== selected) {
			throw new Error(
				"this Pi TUI is already bound to another endpoint identity; use a separate TUI",
			);
		}
		shared.endpointId = selected;
		return selected;
	};

	const emitHeartbeats = (): void => {
		const now = Date.now() / 1000;
		for (const binding of bindings.values()) {
			try {
				writeHeartbeat(binding, now);
			} catch {
				// The Python worker will expose a stale heartbeat as offline. A
				// telemetry write must never terminate the user's active Pi TUI.
			}
		}
	};

	pi.registerCommand("agent-bridge-bind", {
		description:
			"Activate this Pi TUI for all local Agent Bridge room bindings",
		handler: async (args, ctx) => {
			shared.commandContext = ctx;
			shared.currentSessionFile = ctx.sessionManager.getSessionFile();
			const configPath = args.trim();
			const resolvedConfigPath = configPath
				? path.resolve(configPath)
				: undefined;
			selectEndpoint(resolvedConfigPath);
			let installed =
				resolvedConfigPath && installBinding(resolvedConfigPath) ? 1 : 0;
			installed += discoverBindings();
			ctx.ui.notify(
				`Agent Bridge 端点 ${shared.endpointId} 已激活 ${bindings.size} 个房间绑定（新增 ${installed}）`,
				"info",
			);
		},
	});

	pi.on("session_start", (_event, ctx) => {
		shared.currentSessionFile = ctx.sessionManager.getSessionFile();
		if (!shared.endpointId) {
			try {
				selectEndpoint();
			} catch {
				return;
			}
		}
		discoverBindings();
		emitHeartbeats();
	});

	pi.on("before_agent_start", (event) => {
		if (shared.activeRequest && event.prompt === shared.activeRequest.prompt) {
			shared.activeRequest.started = true;
		}
	});

	pi.on("agent_start", () => {
		if (
			!shared.activeRequest?.started ||
			shared.activeRequest.pendingSteers.length === 0
		) {
			return;
		}
		for (const text of shared.activeRequest.pendingSteers.splice(0)) {
			pi.sendUserMessage(text, { deliverAs: "steer" });
		}
	});

	pi.on("agent_end", (event) => {
		if (!shared.activeRequest?.started) return;
		const requestId = shared.activeRequest.id;
		appendEvent(shared.activeRequest.eventFile, {
			type: "complete",
			request_id: requestId,
			text: lastAssistantText(event.messages),
		});
		terminalRequests.add(requestId);
		shared.activeRequest = undefined;
	});

	pi.on("session_shutdown", (event) => {
		for (const [filePath, watcher] of watchedFiles) {
			fs.unwatchFile(filePath, watcher);
		}
		clearInterval(discoveryTimer);
		clearInterval(heartbeatTimer);
		shared.commandContext = undefined;
		shared.currentSessionFile = event.targetSessionFile;
		const active = shared.activeRequest;
		if (!active) return;
		const replacementMatches =
			event.reason === "resume" &&
			Boolean(event.targetSessionFile) &&
			path.resolve(event.targetSessionFile as string) ===
				path.resolve(active.targetSessionFile) &&
			!active.started;
		if (replacementMatches) return;
		appendEvent(active.eventFile, {
			type: "error",
			request_id: active.id,
			error: "Pi TUI session closed before the Bridge turn completed",
		});
		terminalRequests.add(active.id);
		shared.activeRequest = undefined;
	});

	const discoveryTimer = setInterval(discoverBindings, 5_000);
	discoveryTimer.unref();
	const heartbeatTimer = setInterval(emitHeartbeats, 10_000);
	heartbeatTimer.unref();
	emitHeartbeats();
}
