from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from extensions.assistive_harness.phase_b.bridge_ui import WebUIServer
from extensions.assistive_harness.phase_b.esp32_runtime import PhaseBEsp32Runtime, esp32_image_loop
from extensions.assistive_harness.phase_b.rokid_runtime import LatestFrame, RuntimeStats


class RuntimeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_rejections_are_silent_and_good_frame_recovers(self):
        for reason in ("unstable", "severe_shake", "no_frames", "orient_sideways"):
            with self.subTest(reason=reason):
                stop = asyncio.Event()
                harness = SimpleNamespace(send_frame=AsyncMock(), send=AsyncMock())
                speaker = SimpleNamespace(enqueue=AsyncMock(), resume=AsyncMock(),
                                          block_and_flush=AsyncMock())
                manager = SimpleNamespace(active=SimpleNamespace(status="running"),
                    gate=SimpleNamespace(restart_in_progress=False, generation=0),
                    _background_controls=set())
                frames = LatestFrame()
                rounds = []

                async def run_once(capture_fn):
                    rounds.append(1)
                    good = len(rounds) == 4
                    if good:
                        stop.set()
                    return SimpleNamespace(best=b"good" if good else b"bad", frames=[],
                        best_index=0, send=good, reason="send" if good else reason,
                        hint="test hint", seg={}, timings={"grab_ms": 1})

                with self.assertLogs("assistive_harness.phase_b.esp32", level="INFO") as logs:
                    await asyncio.wait_for(esp32_image_loop(
                        SimpleNamespace(), frames, harness, RuntimeStats(), 0.001, 0.02, stop,
                        funnel=SimpleNamespace(run_once=run_once), manager=manager,
                        speaker=speaker, force_measure=False), 1)
                self.assertEqual(len(rounds), 4)
                self.assertEqual(frames.jpeg, b"good")
                harness.send_frame.assert_awaited_once()
                harness.send.assert_not_awaited()
                speaker.enqueue.assert_not_awaited()
                speaker.block_and_flush.assert_not_awaited()
                speaker.resume.assert_not_awaited()
                self.assertFalse(manager._background_controls)
                self.assertTrue(any(f"reject({reason})" in line for line in logs.output))
                self.assertFalse(any("image loop error" in line for line in logs.output))

    async def test_preview_continues_while_model_is_queued_in_all_three_modes(self):
        for mode in ("plain", "select", "reject"):
            with self.subTest(mode=mode):
                stop, ready = asyncio.Event(), asyncio.Event()
                ui = WebUIServer()  # No port opened, no browser connected.
                harness = SimpleNamespace(send_frame=AsyncMock(), send=AsyncMock())
                captures = []

                async def capture(**kwargs):
                    captures.append(1)
                    if len(captures) >= 3:
                        stop.set()
                    return b"jpeg"

                async def run_once(capture_fn):
                    jpeg = await capture_fn()
                    return SimpleNamespace(best=jpeg, frames=[jpeg], best_index=0,
                        send=True, reason="accepted", timings={"grab_ms": 1})

                funnel = None if mode == "plain" else SimpleNamespace(run_once=run_once)
                await asyncio.wait_for(esp32_image_loop(
                    SimpleNamespace(capture=capture), LatestFrame(), harness, RuntimeStats(),
                    0.01, 0.02, stop, funnel=funnel, web_ui=ui, ready_evt=ready,
                    no_reject=(mode == "select")), 1)
                await asyncio.sleep(0)
                self.assertEqual(len(captures), 3)
                self.assertIsNotNone(ui._last_frame)
                self.assertFalse(ui._last_frame["img_sent"])
                harness.send_frame.assert_not_awaited()
                harness.send.assert_not_awaited()

    async def test_failed_or_stopped_startup_does_not_open_gate_or_record(self):
        for result, stopped in ((False, False), (True, True)):
            runtime = object.__new__(PhaseBEsp32Runtime)
            runtime._stop_evt = asyncio.Event()
            if stopped:
                runtime._stop_evt.set()
            runtime.live_rec = Mock()
            runtime._wait_gateway_ready = AsyncMock(return_value=result)
            ready = asyncio.Event()
            await runtime._gate_open_when_ready(ready)
            self.assertFalse(ready.is_set())
            runtime.live_rec.start.assert_not_called()

    async def test_cancelled_readiness_does_not_start_recording(self):
        runtime = object.__new__(PhaseBEsp32Runtime)
        runtime._stop_evt = asyncio.Event()
        runtime.live_rec = Mock()
        entered = asyncio.Event()

        async def wait():
            entered.set()
            await asyncio.Event().wait()

        runtime._wait_gateway_ready = wait
        ready = asyncio.Event()
        task = asyncio.create_task(runtime._gate_open_when_ready(ready))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(ready.is_set())
        runtime.live_rec.start.assert_not_called()

    async def test_shutdown_disconnects_before_video_finalization(self):
        calls = []
        runtime = object.__new__(PhaseBEsp32Runtime)
        runtime._stop_evt = asyncio.Event()
        runtime._web_ui = None
        runtime.live_rec = SimpleNamespace(stop=lambda: calls.append("record"))
        runtime._tcp_img = SimpleNamespace(_close=AsyncMock())
        runtime._probe = runtime._recorder = None
        with patch("extensions.assistive_harness.phase_b.rokid_runtime.PhaseBRokidRuntime.close",
                   new=AsyncMock(side_effect=lambda: calls.append("disconnect"))):
            await runtime.close()
        self.assertEqual(calls, ["disconnect", "record"])


if __name__ == "__main__":
    unittest.main()
