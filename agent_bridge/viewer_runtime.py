from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress

from starlette.applications import Starlette

from .resident_health import (
    configure_existing_connector_from_disk,
    local_resident_snapshot,
    repair_known_identity_services,
)
from .store import BridgeStore, RUNTIME_HEARTBEAT_INTERVAL_SECONDS


BLOCKING_IO_MAX_WORKERS = 128


def build_viewer_runtime(
    *,
    store: BridgeStore,
    runtime_instance_id: str,
    runtime_node_name: str,
    runtime_version: str,
    enable_resident_repair: bool,
) -> tuple[Callable, asyncio.Event]:
    runtime_leader = asyncio.Event()

    async def refresh_runtime_leadership(application: Starlette) -> bool:
        state = await asyncio.to_thread(
            store.coordinate_runtime_instance,
            instance_id=runtime_instance_id,
            node_name=runtime_node_name,
            process_id=os.getpid(),
            software_version=runtime_version,
        )
        is_leader = bool(state["leader"])
        if is_leader:
            runtime_leader.set()
        else:
            runtime_leader.clear()
        application.state.runtime_leader = is_leader
        application.state.runtime_fencing_token = int(state["fencing_token"])
        return is_leader

    async def runtime_leadership_confirmed(application: Starlette) -> bool:
        try:
            return await refresh_runtime_leadership(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A process that cannot renew the shared lease must stop all
            # singleton work until the database proves leadership again.
            runtime_leader.clear()
            application.state.runtime_leader = False
            return False

    async def runtime_coordination(application: Starlette) -> None:
        while True:
            await asyncio.sleep(RUNTIME_HEARTBEAT_INTERVAL_SECONDS)
            await runtime_leadership_confirmed(application)

    async def lifecycle_maintenance(application: Starlette) -> None:
        while True:
            if await runtime_leadership_confirmed(application):
                try:
                    await asyncio.to_thread(store.clear_inactive_sessions)
                    # Room abandonment is lifecycle maintenance, not part of
                    # every latency-sensitive Agent read.  Running it once per
                    # minute also prevents many long-poll clients racing to do
                    # the same global sweep.
                    await asyncio.to_thread(store.archive_stale_rooms)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transient SQLite lock must never stop the chat server
                    # or permanently disable the next lifecycle sweep.
                    pass
            await asyncio.sleep(60)

    async def resident_maintenance(application: Starlette) -> None:
        while True:
            if not await runtime_leadership_confirmed(application):
                await asyncio.sleep(RUNTIME_HEARTBEAT_INTERVAL_SECONDS)
                continue
            try:
                snapshot = await asyncio.to_thread(
                    local_resident_snapshot,
                    force=True,
                )
                for client_type, detail in snapshot.items():
                    connectors = detail.get("connectors") or {}
                    if not connectors and detail.get("resident_status") != "online":
                        await asyncio.to_thread(
                            repair_known_identity_services,
                            client_type,
                        )
                        continue
                    for connector in connectors.values():
                        chat_online = connector.get("resident_status") == "online"
                        task_configured = bool(connector.get("task_configured"))
                        task_running = bool(connector.get("task_running"))
                        task_component_ready = bool(
                            connector.get("task_component_ready")
                        )
                        if chat_online and task_running and task_component_ready:
                            continue
                        if chat_online and (
                            not task_configured or not task_component_ready
                        ):
                            # Existing v0.11 connectors already keep chat healthy.
                            # Install or protocol-upgrade only the task seat so an
                            # upgrade never restarts listener/worker or interrupts
                            # room traffic.
                            await asyncio.to_thread(
                                configure_existing_connector_from_disk,
                                client_type,
                                connector_id=connector.get("connector_id"),
                                conversation_id=connector.get("conversation_id"),
                                activate_task_only=True,
                            )
                            continue
                        await asyncio.to_thread(
                            repair_known_identity_services,
                            client_type,
                            connector_id=connector.get("connector_id"),
                            conversation_id=connector.get("conversation_id"),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep chat serving even if launchd/systemd is transiently busy.
                pass
            await asyncio.sleep(30)

    async def operational_monitoring(application: Starlette) -> None:
        while True:
            started_at = time.monotonic()
            if await runtime_leadership_confirmed(application):
                try:
                    await asyncio.to_thread(store.record_operational_sample)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Monitoring is deliberately sidecar-only: a sampling
                    # failure must never interrupt chat, delivery, or tasks.
                    pass
            elapsed = time.monotonic() - started_at
            await asyncio.sleep(max(5.0, 60.0 - elapsed))

    @asynccontextmanager
    async def lifespan(application: Starlette):
        # Long polls must not consume the entire executor and delay an explicit
        # mention read until the MCP client's ten-second transport timeout.
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(
                max_workers=BLOCKING_IO_MAX_WORKERS,
                thread_name_prefix="agent-bridge-io",
            )
        )
        await refresh_runtime_leadership(application)
        coordinator = asyncio.create_task(
            runtime_coordination(application),
            name="agent-bridge-runtime-coordination",
        )
        maintenance = asyncio.create_task(
            lifecycle_maintenance(application),
            name="agent-bridge-lifecycle-maintenance",
        )
        resident_repair = (
            asyncio.create_task(
                resident_maintenance(application),
                name="agent-bridge-resident-maintenance",
            )
            if enable_resident_repair
            else None
        )
        monitoring = asyncio.create_task(
            operational_monitoring(application),
            name="agent-bridge-operational-monitoring",
        )
        try:
            yield
        finally:
            runtime_leader.clear()
            application.state.runtime_leader = False
            coordinator.cancel()
            maintenance.cancel()
            monitoring.cancel()
            if resident_repair is not None:
                resident_repair.cancel()
            with suppress(asyncio.CancelledError):
                await coordinator
            with suppress(asyncio.CancelledError):
                await maintenance
            with suppress(asyncio.CancelledError):
                await monitoring
            if resident_repair is not None:
                with suppress(asyncio.CancelledError):
                    await resident_repair
            try:
                await asyncio.to_thread(
                    store.stop_runtime_instance,
                    instance_id=runtime_instance_id,
                )
            except Exception:
                # The lease expires automatically after a crash or an
                # unavailable shutdown database; graceful release is best effort.
                pass

    return lifespan, runtime_leader
