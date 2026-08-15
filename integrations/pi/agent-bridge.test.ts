import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { canDeliverToPiSession } from "./agent-bridge.ts";

test("the current fresh Pi session is valid before its JSONL exists", () => {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), "agent-bridge-pi-"));
	const fresh = path.join(directory, "fresh-session.jsonl");
	const other = path.join(directory, "other-session.jsonl");

	try {
		assert.equal(fs.existsSync(fresh), false);
		assert.equal(canDeliverToPiSession(fresh, fresh), true);
		assert.equal(canDeliverToPiSession(fresh, other), false);

		fs.writeFileSync(fresh, "", { mode: 0o600 });
		assert.equal(canDeliverToPiSession(fresh, other), true);
	} finally {
		fs.rmSync(directory, { recursive: true, force: true });
	}
});
