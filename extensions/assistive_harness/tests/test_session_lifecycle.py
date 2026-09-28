"""Deterministic lifecycle tests: no model, camera, GPU or audio device needed."""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import numpy as np

from extensions.assistive_harness.phase_b.realtime_session import RealtimeDuplexSession
from extensions.assistive_harness.phase_b.rokid_runtime import (
    DropOldestAudioQueue, LatestFrame, OutputGate, RokidRuntimeConfig, SessionSpec,
)
from extensions.assistive_harness.tests.test_phase_b_rokid import (
    CONFIG, FakeSession, FakeSpeaker, FakeTelemetry,
)
from extensions.assistive_harness.phase_b.rokid_runtime import GatewaySessionManager
from extensions.assistive_harness.registry import SkillRegistry


class FakeSocket:
    def __init__(self, *, queued=False, acknowledge=True):
        self.closed = False
        self.incoming = asyncio.Queue()
        self.sent = []
        self.close_requested = asyncio.Event()
        self.acknowledge = acknowledge
        if not queued:
            self.feed({"type": "session.queue_done"})

    def feed(self, payload):
        self.incoming.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(payload)))

    async def send_json(self, payload):
        self.sent.append(payload)
        if payload["type"] == "session.init":
            self.feed({"type": "session.created", "session_id": "test-session"})
        elif payload["type"] == "session.close":
            self.close_requested.set()
            self.feed({"type": "response.output.delta", "kind": "text", "text": "late output"})
            if self.acknowledge:
                self.feed({"type": "session.closed", "reason": "client_closed"})

    async def receive(self):
        return await self.incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.receive()
        if message.type == aiohttp.WSMsgType.CLOSED:
            raise StopAsyncIteration
        return message

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


class FakeClient:
    def __init__(self, ws):
        self.ws = ws

    def ws_connect(self, *args, **kwargs):
        return self.ws

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class RealtimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self):
        return RealtimeDuplexSession(
            RokidRuntimeConfig(prepare_timeout_s=0.05, close_timeout_s=0.08),
            SessionSpec(0, "idle_chat", {}, "test"),
            DropOldestAudioQueue(4), LatestFrame(), OutputGate(), AsyncMock(),
        )

    async def test_early_ws_ack_alone_cannot_confirm_cleanup(self):
        ws = FakeSocket(acknowledge=False)
        session = self.make_session()
        with patch("aiohttp.ClientSession", return_value=FakeClient(ws)):
            await session.start()
            stopping = asyncio.create_task(session.stop("light"))
            await ws.close_requested.wait()
            await asyncio.sleep(0.01)
            self.assertFalse(stopping.done())
            session.on_result.assert_not_awaited()
            ws.feed({"type": "session.closed"})
            with self.assertRaises(RuntimeError):
                await stopping
        self.assertTrue(session._task.done())
        self.assertTrue(session._server_closed.is_set())
        self.assertTrue(session.close_uncertain)

    async def test_pending_audio_cannot_be_sent_after_close(self):
        session = self.make_session()
        ws = FakeSocket()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_chunk(carry):
            entered.set()
            await release.wait()
            return np.zeros(16000, dtype=np.float32)

        session._next_audio_chunk = delayed_chunk
        with patch("aiohttp.ClientSession", return_value=FakeClient(ws)):
            await session.start()
            await entered.wait()
            with self.assertRaises(RuntimeError):
                await session.stop("light")
            release.set()
        self.assertEqual([p["type"] for p in ws.sent], ["session.init", "session.close"])

    async def test_waiting_send_lock_cannot_cross_close_boundary(self):
        session = self.make_session()
        ws = FakeSocket()
        session.ws = ws
        await session._send_lock.acquire()
        sending = asyncio.create_task(session._send_json({"type": "input.append"}))
        await asyncio.sleep(0)
        session._closing = True
        session._send_lock.release()
        await sending
        self.assertEqual(ws.sent, [])

    async def test_queued_startup_timeout_closes_connection_before_returning(self):
        session = self.make_session()
        ws = FakeSocket(queued=True)
        with patch("aiohttp.ClientSession", return_value=FakeClient(ws)):
            with self.assertRaises(asyncio.TimeoutError):
                await session.start()
        self.assertTrue(session._task.done())
        self.assertTrue(ws.closed)
        self.assertFalse(session.close_uncertain)
        self.assertEqual(ws.sent, [])

    async def test_cancelled_startup_does_not_leave_queued_connection(self):
        session = self.make_session()
        ws = FakeSocket(queued=True)
        with patch("aiohttp.ClientSession", return_value=FakeClient(ws)):
            starting = asyncio.create_task(session.start())
            while session.ws is None:
                await asyncio.sleep(0)
            starting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await starting
        self.assertTrue(session._task.done())
        self.assertTrue(ws.closed)

    async def test_missing_close_ack_is_error_not_permission_to_restart(self):
        session = self.make_session()
        ws = FakeSocket(acknowledge=False)
        with patch("aiohttp.ClientSession", return_value=FakeClient(ws)):
            await session.start()
            with self.assertRaises(RuntimeError):
                await session.stop("light")
        self.assertTrue(session.close_uncertain)
        self.assertTrue(session._task.done())
        self.assertTrue(ws.closed)

    async def test_server_disconnect_reaps_sender(self):
        session = self.make_session()
        ws = FakeSocket()
        with patch("aiohttp.ClientSession", return_value=FakeClient(ws)):
            await session.start()
            await ws.close()
            await asyncio.wait_for(session._stopped.wait(), 0.2)
        self.assertTrue(session._sender.done())

    async def test_http_cleanup_barrier_outlasts_early_ws_ack(self):
        from aiohttp import web
        session = self.make_session()
        session.config.close_timeout_s = 1.0
        ws = FakeSocket()
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def close_handler(request):
            requests.append(request.match_info["sid"])
            ws.feed({"type": "session.closed"})
            entered.set()
            await release.wait()
            return web.json_response({"ok": True, "closed": True, "session_id": requests[-1]})

        app = web.Application()
        app.router.add_post("/sessions/{sid}/close", close_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        session.config.backend_close_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            with patch("aiohttp.ClientSession", return_value=FakeClient(ws)):
                await session.start()
            stopping = asyncio.create_task(session.stop("light"))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(session._stopped.wait(), 1)
            self.assertTrue(session._server_closed.is_set())
            self.assertFalse(stopping.done())
            self.assertFalse(session._cleanup_confirmed)
            release.set()
            await stopping
            self.assertTrue(session._cleanup_confirmed)
            self.assertFalse(session.close_uncertain)
            self.assertEqual(requests, ["test-session"])
            self.assertNotIn("session.close", [msg["type"] for msg in ws.sent])
        finally:
            release.set()
            await runner.cleanup()

    async def test_http_failure_blocks_reuse_despite_early_ws_ack(self):
        session = self.make_session()
        session.config.backend_close_url = "http://127.0.0.1:22500"
        ws = FakeSocket()
        async def failing_close(reason):
            ws.feed({"type": "session.closed"})
            await session._server_closed.wait()
            raise asyncio.TimeoutError()
        session._close_backend = failing_close
        with patch("aiohttp.ClientSession", return_value=FakeClient(ws)):
            await session.start()
            with self.assertRaises(RuntimeError):
                await session.stop("light")
        self.assertTrue(session.close_uncertain)
        self.assertFalse(session._cleanup_confirmed)


class ManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, factory=FakeSession):
        return GatewaySessionManager(
            RokidRuntimeConfig(skills_config=str(CONFIG), play_audio=False),
            SkillRegistry(CONFIG), DropOldestAudioQueue(4), LatestFrame(),
            FakeSpeaker(), session_factory=factory,
        )

    async def test_uncertain_close_blocks_this_and_later_replacements(self):
        created = []

        class BadCloseSession(FakeSession):
            async def stop(self, mode):
                raise RuntimeError("close was not acknowledged")

        def factory(*args):
            session = BadCloseSession(*args)
            created.append(session)
            return session

        manager = self.make_manager(factory)
        await manager.start_initial()
        event = {"accepted": True, "intent": "reset_session", "event_id": 1}
        self.assertFalse((await manager.handle_control(event))["ok"])
        self.assertFalse((await manager.handle_control(event))["ok"])
        self.assertEqual(len(created), 1)
        self.assertTrue(manager.health()["gateway_error"])

    async def test_resume_does_not_release_restart_gate(self):
        manager = self.make_manager()
        manager.gate.stop()
        manager.gate.restart_in_progress = True
        ack = await manager.resume_speech({"event_id": 1})
        self.assertFalse(ack["ok"])
        self.assertTrue(manager.gate.speech_hold_active)

    async def test_control_timing_separates_playback_block_from_completion(self):
        from extensions.assistive_harness.phase_b.rokid_runtime import now_ms
        manager = self.make_manager()
        manager.harness = FakeTelemetry()
        entered, release = asyncio.Event(), asyncio.Event()

        async def clear_playback(reason):
            entered.set()
            await release.wait()

        manager._clear_playback_state = clear_playback
        stamp = now_ms()
        event = {"accepted": True, "intent": "stop_speech", "event_id": 12,
                 "asr_event_id": 11, "asr_final_at_ms": stamp - 30,
                 "control_sent_at_ms": stamp - 20, "control_received_at_ms": stamp - 5}
        task = asyncio.create_task(manager.handle_control(event))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(manager.speaker.blocked)
        self.assertFalse(task.done())
        self.assertFalse(any(m["type"] == "control.timing" for m in manager.harness.messages))
        release.set()
        self.assertTrue((await task)["ok"])
        timing = next(m for m in manager.harness.messages if m["type"] == "control.timing")
        self.assertEqual((timing["asr_event_id"], timing["event_id"]), (11, 12))
        self.assertEqual(timing["asr_to_send_ms"], 10)
        self.assertEqual(timing["send_to_receive_ms"], 15)
        self.assertLessEqual(timing["start_to_playback_blocked_ms"], timing["execution_ms"])
        self.assertNotIn("_control_started_mono", event)

    async def test_control_error_is_preserved_and_not_reported_as_success(self):
        manager = self.make_manager()
        manager.harness = FakeTelemetry()
        manager._handle_control = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            await manager.handle_control({"event_id": 1})
        timing = manager.harness.messages[-1]
        self.assertFalse(timing["ok"])
        self.assertEqual(timing["outcome"], "error")
        self.assertNotIn("send_to_receive_ms", timing)

    async def test_old_funnel_resume_does_not_change_new_session(self):
        manager = self.make_manager()
        manager.gate.generation = 2
        manager.gate.stop()
        ack = await manager.handle_control({"event_id": 1, "accepted": True,
            "source": "funnel", "generation": 1, "intent": "resume_speech"})
        self.assertFalse(ack["ok"])
        self.assertTrue(manager.gate.speech_hold_active)


if __name__ == "__main__":
    unittest.main()
