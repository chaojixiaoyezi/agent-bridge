from __future__ import annotations

import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from agent_bridge.store import MESSAGE_COOLDOWN_SECONDS, BridgeStore
from agent_bridge.viewer import create_app


pytestmark = pytest.mark.browser

CAPTCHA_ANSWER = "ABCDE"
ADMIN_PASSWORD = "AdminSecure1!"


def _browser_tests_enabled() -> bool:
    return os.environ.get("AGENT_BRIDGE_RUN_BROWSER_TESTS", "").strip() == "1"


def _seed_browser_database(database: Path) -> None:
    store = BridgeStore(database)
    for room_index, room in enumerate(("browser-room-one", "browser-room-two"), 1):
        sender = store.register(
            client_type=f"codex-browser-{room_index}",
            session_alias=f"浏览器测试 Agent {room_index}",
            conversation_id=room,
            create_room_if_missing=True,
        )
        session = store.register_agent_session(
            product="codex",
            username=f"browser-{room_index}",
            session_alias=f"浏览器测试 Agent {room_index}",
            conversation_id=room,
        )
        assert session["participant_id"] == sender["participant_id"]
        message_count = 72 if room_index == 1 else 4
        for message_index in range(message_count):
            store.send(
                authorized_session_id=str(session["session_id"]),
                sender_participant_id=str(session["participant_id"]),
                conversation_id=room,
                body_text=(
                    f"{room} browser performance message {message_index + 1}: "
                    "keep the room timeline bounded and stable while switching."
                ),
            )
            with store._transaction() as connection:
                connection.execute(
                    "UPDATE messages SET created_at = created_at - ? "
                    "WHERE message_id = (SELECT message_id FROM messages "
                    "WHERE conversation_id = ? AND sender_participant_id = ? "
                    "ORDER BY sequence DESC LIMIT 1)",
                    (
                        MESSAGE_COOLDOWN_SECONDS + 1.0,
                        room,
                        str(session["participant_id"]),
                    ),
                )


@contextmanager
def _running_viewer(database: Path):
    app = create_app(
        database,
        captcha_generator=lambda: CAPTCHA_ANSWER,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
    )
    thread = threading.Thread(
        target=lambda: server.run(sockets=[listener]),
        name="agent-bridge-browser-test-viewer",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2.0)
        listener.close()
        raise RuntimeError("browser test viewer did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        listener.close()


def _login_and_change_bootstrap_password(page, base_url: str) -> dict[str, float]:
    navigation_started = time.monotonic()
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#captcha-image").wait_for(state="visible")
    page.locator("#captcha-image[src^='data:image/png;base64,']").wait_for()
    dom_ready_ms = (time.monotonic() - navigation_started) * 1000

    authentication_started = time.monotonic()
    page.locator("#auth-username").fill("admin")
    page.locator("#auth-password").fill("admin")
    page.locator("#captcha-answer").fill(CAPTCHA_ANSWER)
    page.locator("#submit-auth").click()
    page.locator("#password-dialog").wait_for(state="visible")
    page.locator("#current-password").fill("admin")
    page.locator("#new-password").fill(ADMIN_PASSWORD)
    page.locator("#new-password-confirm").fill(ADMIN_PASSWORD)
    page.locator("#submit-password").click()
    page.locator("#app-shell").wait_for(state="visible")
    page.locator(".room-card").first.wait_for(state="visible")
    authenticated_ready_ms = (time.monotonic() - authentication_started) * 1000
    return {
        "dom_ready_ms": round(dom_ready_ms, 2),
        "authenticated_ready_ms": round(authenticated_ready_ms, 2),
    }


def test_real_browser_login_layout_room_switch_scroll_and_performance(tmp_path: Path) -> None:
    if not _browser_tests_enabled():
        pytest.skip("set AGENT_BRIDGE_RUN_BROWSER_TESTS=1 to run Chromium E2E")

    playwright_api = pytest.importorskip("playwright.sync_api")
    database = tmp_path / "bridge.db"
    _seed_browser_database(database)

    with _running_viewer(database) as base_url:
        with playwright_api.sync_playwright() as playwright:
            browser_channel = os.environ.get(
                "AGENT_BRIDGE_BROWSER_CHANNEL",
                "",
            ).strip()
            browser = playwright.chromium.launch(
                headless=True,
                **({"channel": browser_channel} if browser_channel else {}),
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            console_errors: list[str] = []
            runtime_errors: list[str] = []
            http_errors: list[tuple[int, str]] = []
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                and not message.text.startswith("Failed to load resource:")
                else None,
            )
            page.on(
                "pageerror",
                lambda error: runtime_errors.append(str(error)),
            )
            page.on(
                "response",
                lambda response: http_errors.append((response.status, response.url))
                if response.status >= 400
                else None,
            )

            measurements = _login_and_change_bootstrap_password(page, base_url)
            assert measurements["dom_ready_ms"] < 5_000
            assert measurements["authenticated_ready_ms"] < 8_000

            workspace = page.locator("#workspace")
            timeline = page.locator(".timeline-panel")
            rooms = page.locator(".rooms-panel")
            people = page.locator(".people-panel")
            assert timeline.bounding_box()["width"] > rooms.bounding_box()["width"] * 2
            assert page.locator("#global-tools-menu").get_attribute("open") is None
            assert page.locator("#room-tools-menu").get_attribute("open") is None
            playwright_api.expect(
                page.locator("#global-tools-menu > summary")
            ).to_contain_text("系统管理")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            panel_controls = page.evaluate(
                """() => {
                  const title = document.querySelector('.rooms-panel .panel-title-line').getBoundingClientRect();
                  const create = document.querySelector('#open-create-room').getBoundingClientRect();
                  const collapse = document.querySelector('#toggle-rooms-panel').getBoundingClientRect();
                  const overlaps = (a, b) => !(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top);
                  return {
                    titleCreateOverlap: overlaps(title, create),
                    createCollapseOverlap: overlaps(create, collapse),
                    createCollapseTopDelta: Math.abs(create.top - collapse.top),
                    createCollapseHeightDelta: Math.abs(create.height - collapse.height),
                  };
                }"""
            )
            assert panel_controls["titleCreateOverlap"] is False
            assert panel_controls["createCollapseOverlap"] is False
            assert panel_controls["createCollapseTopDelta"] < 0.5
            assert panel_controls["createCollapseHeightDelta"] < 0.5

            rooms_width_before = rooms.bounding_box()["width"]
            rooms_resizer = page.locator("#rooms-resizer")
            rooms_resizer_box = rooms_resizer.bounding_box()
            page.mouse.move(
                rooms_resizer_box["x"] + 3,
                rooms_resizer_box["y"] + 80,
            )
            page.mouse.down()
            page.mouse.move(
                rooms_resizer_box["x"] + 47,
                rooms_resizer_box["y"] + 80,
            )
            page.mouse.up()
            assert rooms.bounding_box()["width"] > rooms_width_before + 30
            assert int(page.evaluate("localStorage.agentBridgeRoomsWidth")) > 260

            composer_textarea = page.locator("#owner-message-body")
            composer_height_before = composer_textarea.bounding_box()["height"]
            composer_resizer = page.locator("#composer-resizer")
            composer_resizer_box = composer_resizer.bounding_box()
            page.mouse.move(
                composer_resizer_box["x"] + 30,
                composer_resizer_box["y"] + 5,
            )
            page.mouse.down()
            page.mouse.move(
                composer_resizer_box["x"] + 30,
                composer_resizer_box["y"] - 47,
            )
            page.mouse.up()
            assert composer_textarea.bounding_box()["height"] > composer_height_before + 40
            assert int(page.evaluate("localStorage.agentBridgeComposerHeight")) > 90

            page.locator("#toggle-composer-panel").click()
            playwright_api.expect(page.locator("#owner-message-form")).to_be_hidden()
            assert "composer-collapsed" in (workspace.get_attribute("class") or "")
            page.locator("#toggle-composer-panel").click()
            playwright_api.expect(page.locator("#owner-message-form")).to_be_visible()

            timeline_width_before_focus = timeline.bounding_box()["width"]
            page.locator("#layout-menu > summary").click()
            page.locator("#layout-density-compact").click()
            assert "compact-view" in (workspace.get_attribute("class") or "")
            assert page.locator(".room-preview").first.evaluate(
                "element => getComputedStyle(element).display"
            ) == "none"
            page.locator("#layout-density-detailed").click()
            assert "compact-view" not in (workspace.get_attribute("class") or "")
            page.locator("#toggle-focus-mode").click()
            assert "focus-mode" in (workspace.get_attribute("class") or "")
            playwright_api.expect(rooms).to_be_hidden()
            assert timeline.bounding_box()["width"] > timeline_width_before_focus + 150
            page.locator("#toggle-focus-mode").click()
            playwright_api.expect(rooms).to_be_visible()
            page.locator("#reset-workspace-layout").click()
            assert page.evaluate("localStorage.agentBridgeRoomsWidth") == "236"
            assert page.evaluate("localStorage.agentBridgeComposerHeight") == "58"
            page.locator("#layout-menu").evaluate("element => { element.open = false; }")

            page.locator(".room-card", has_text="browser-room-one").click()
            playwright_api.expect(page.locator("#active-room-title")).to_have_text(
                "browser-room-one"
            )
            playwright_api.expect(
                page.locator("#timeline article[data-message-id]")
            ).to_have_count(60)
            assert page.locator(".load-earlier-button").is_visible()
            page.locator("#toggle-composer-panel").click()
            playwright_api.expect(page.locator("#owner-message-form")).to_be_hidden()
            page.locator("button.message-reply-button", has_text="回复").last.click()
            playwright_api.expect(page.locator("#owner-message-form")).to_be_visible()
            playwright_api.expect(page.locator("#composer-context")).to_be_visible()
            page.locator("#cancel-composer-context").click()
            playwright_api.expect(
                page.locator("#room-tools-menu > summary")
            ).to_contain_text("房间管理")

            timeline_locator = page.locator("#timeline")
            page.wait_for_function(
                "element => element.scrollHeight - element.scrollTop "
                "- element.clientHeight < 80",
                arg=timeline_locator.element_handle(),
            )
            timeline_locator.evaluate("element => { element.scrollTop = 0; }")
            scroll_before = timeline_locator.evaluate("element => element.scrollTop")
            with page.expect_response("**/api/rooms?limit=200"):
                page.locator("#refresh-button").click()
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            scroll_after = timeline_locator.evaluate("element => element.scrollTop")
            assert abs(scroll_after - scroll_before) < 4

            room_switch_started = time.monotonic()
            page.locator(".room-card", has_text="browser-room-two").click()
            playwright_api.expect(page.locator("#active-room-title")).to_have_text(
                "browser-room-two"
            )
            playwright_api.expect(
                page.locator("#timeline article[data-message-id]")
            ).to_have_count(4)
            measurements["room_switch_ms"] = round(
                (time.monotonic() - room_switch_started) * 1000,
                2,
            )
            assert measurements["room_switch_ms"] < 3_000

            page.locator("#toggle-rooms-panel").click()
            assert "rooms-collapsed" in (workspace.get_attribute("class") or "")
            page.locator("#toggle-people-panel").click()
            assert "people-collapsed" not in (workspace.get_attribute("class") or "")
            page.wait_for_function(
                "element => element.getBoundingClientRect().width > 200",
                arg=people.element_handle(),
            )
            playwright_api.expect(page.locator(".person-actions").first).to_be_visible()
            assert page.locator(".person-name").first.bounding_box()["width"] > 100
            people_width_before = people.bounding_box()["width"]
            people_resizer = page.locator("#people-resizer")
            people_resizer_box = people_resizer.bounding_box()
            page.mouse.move(
                people_resizer_box["x"] + 8,
                people_resizer_box["y"] + 80,
            )
            page.mouse.down()
            page.mouse.move(
                people_resizer_box["x"] - 34,
                people_resizer_box["y"] + 80,
            )
            page.mouse.up()
            assert people.bounding_box()["width"] > people_width_before + 30
            assert int(page.evaluate("localStorage.agentBridgePeopleWidth")) > 290
            people_resizer.dblclick()
            assert page.evaluate("localStorage.agentBridgePeopleWidth") == "260"

            page.locator("#global-tools-menu").click()
            playwright_api.expect(
                page.locator("#global-tools-menu .tool-scope-pill")
            ).to_have_text("全局")
            playwright_api.expect(
                page.locator("#global-tools-menu .tool-popover-heading")
            ).to_contain_text("作用于所有聊天室")
            playwright_api.expect(page.locator(".theme-choice")).to_have_count(6)
            theme_backgrounds = {}
            for theme in ("paper", "mist", "aurora", "ocean", "violet", "ember"):
                theme_choice = page.locator(
                    f'.theme-choice[data-theme-value="{theme}"]'
                )
                theme_choice.click()
                assert page.locator("html").get_attribute("data-theme") == theme
                assert theme_choice.get_attribute("aria-checked") == "true"
                theme_backgrounds[theme] = page.evaluate(
                    "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()"
                )
            assert len(set(theme_backgrounds.values())) == 6

            page.locator('.theme-choice[data-theme-value="violet"]').click()
            assert page.locator("html").get_attribute("data-theme") == "violet"
            assert page.locator("#global-tools-menu").get_attribute("open") == ""
            assert page.locator(
                '.theme-choice[data-theme-value="violet"]'
            ).get_attribute("aria-checked") == "true"
            playwright_api.expect(page.locator("#theme-current-mode")).to_have_text(
                "深色"
            )
            assert page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--surface-strong').trim() !== ''"
            )

            page.locator("#open-agent-access").click()
            page.locator("#agent-access-dialog").wait_for(state="visible")
            playwright_api.expect(
                page.locator("#monitoring-summary .connector-health-summary-card")
            ).to_have_count(11)
            playwright_api.expect(
                page.locator("#monitoring-trends .monitoring-trend-row")
            ).to_have_count(7)
            page.locator("#close-agent-access").click()

            page.locator("#global-tools-menu").evaluate("element => { element.open = true; }")
            page.locator("#open-admin-audit").click()
            page.locator("#admin-audit-dialog").wait_for(state="visible")
            playwright_api.expect(
                page.locator("#admin-audit-summary .connector-health-summary-card")
            ).to_have_count(4)
            page.locator("#close-admin-audit").click()

            page.locator("#global-tools-menu").evaluate(
                "element => { element.open = true; }"
            )
            page.locator("#open-history-governance").click()
            page.locator("#history-governance-dialog").wait_for(state="visible")
            playwright_api.expect(
                page.locator("#history-retention-mode")
            ).to_have_value("forever")
            playwright_api.expect(
                page.locator("#history-search-results .history-result-card")
            ).to_have_count(50)
            assert "跨聊天室" in page.locator("#history-search-feedback").inner_text()
            page.locator("#close-history-governance").click()

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(100)
            assert page.locator("#owner-message-form").is_visible()
            assert page.locator("#active-room-title").is_visible()
            assert page.locator("#global-tools-menu > summary").bounding_box()["width"] == 32
            assert page.locator("#layout-menu > summary").bounding_box()["width"] == 32
            playwright_api.expect(page.locator("#rooms-resizer")).to_be_hidden()
            playwright_api.expect(page.locator("#people-resizer")).to_be_hidden()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            resource_bytes = page.evaluate(
                "performance.getEntriesByType('resource').reduce((sum, item) => sum + (item.transferSize || 0), 0)"
            )
            measurements["resource_transfer_bytes"] = int(resource_bytes)
            assert resource_bytes < 4_000_000
            unexpected_http_errors = [
                (status, url)
                for status, url in http_errors
                if not (
                    status == 401 and url.endswith("/api/auth/me")
                    or status == 404 and url.endswith("/favicon.ico")
                )
            ]
            assert not unexpected_http_errors, unexpected_http_errors
            assert not console_errors, console_errors
            assert not runtime_errors, runtime_errors
            print("browser-performance-baseline=" + json.dumps(measurements, sort_keys=True))
            browser.close()
