import asyncio
import io
import unittest
import tempfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import numpy as np
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from extensions.assistive_harness.phase_b.rokid_panel_runtime import PanelRokidRuntime
from extensions.assistive_harness.phase_b.rokid_runtime import RokidRuntimeConfig, parse_args
from extensions.assistive_harness.tests.test_phase_b_rokid import CONFIG, FakeSpeaker, FakeSession


class RokidPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_apk_ingress_preview_and_model_response(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch("extensions.assistive_harness.phase_b.recorder_live.LiveRecorder.finalize_mp4"))
        stack.enter_context(patch("extensions.assistive_harness.phase_b.recorder_live.LiveRecorder.finalize_multiframe_mp4"))
        runtime = PanelRokidRuntime(
            RokidRuntimeConfig(skills_config=str(CONFIG), image_rotate_cw=0,
                               idle_prompt="panel prompt", play_audio=False),
            ui_port=0, speaker=FakeSpeaker(), session_factory=FakeSession,
            record_live=True, live_record_dir=directory,
        )
        runtime.harness.connected = True
        runtime.harness.run = AsyncMock()
        runtime.harness.send = AsyncMock()
        runtime.harness.send_audio = AsyncMock()
        client = TestClient(TestServer(runtime.create_app()))
        await client.start_server()
        try:
            for _ in range(50):
                if runtime.manager.active and runtime.manager.active.status == "running":
                    break
                await asyncio.sleep(.01)
            self.assertEqual(runtime.manager.active.spec.system_prompt, "panel prompt")
            self.assertFalse(runtime.health()["device_input_ready"])
            buf = io.BytesIO()
            Image.new("RGB", (24, 16), "red").save(buf, format="JPEG")
            response = await client.post("/rokid/image", data=buf.getvalue())
            self.assertEqual(response.status, 200)
            ws = await client.ws_connect("/rokid/audio")
            await ws.send_bytes(np.full(1600, 100, dtype="<i2").tobytes())
            for _ in range(50):
                if runtime.ui._last_frame and runtime.harness.send_audio.await_count:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(runtime.ui._last_frame["img_b64"])
            self.assertEqual(runtime.harness.send_audio.await_count, 1)
            self.assertEqual(runtime.health()["audio_packets_in"], 1)
            runtime.ui.emit = AsyncMock()
            await runtime.manager.handle_result(runtime.manager.active, {"text": "看到了"})
            await runtime.speaker.enqueue(np.ones(2400, dtype=np.float32) * .01, 0)
            self.assertTrue(any(call.args[0].get("text") == "看到了"
                                for call in runtime.ui.emit.await_args_list))
            # Leave the glasses socket open, as with a real device when STOP is pressed.
        finally:
            await asyncio.wait_for(client.server.close(), timeout=5)
            await client.close()
        self.assertTrue(runtime._closed)
        recording = next(Path(directory).iterdir())
        self.assertTrue(list((recording / "images").glob("*.jpg")))
        self.assertGreater((recording / "live_user.wav").stat().st_size, 44)
        self.assertGreater((recording / "live_ai.wav").stat().st_size, 44)
        self.assertIn("看到了", (recording / "model_chunks.jsonl").read_text(encoding="utf-8"))

    def test_realtime_cli_and_close_barrier_options(self):
        with patch("sys.argv", ["rokid", "--gateway-proto", "realtime", "--ui-port", "8080",
                                "--backend-close-url", "http://127.0.0.1:22500", "--record-live"]):
            args = parse_args()
        self.assertEqual(args.gateway_proto, "realtime")
        self.assertEqual(args.ui_port, 8080)
        self.assertTrue(args.record_live)
        self.assertEqual(args.backend_close_url, "http://127.0.0.1:22500")
