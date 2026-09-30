"""Rokid panel preview without ESP32, OCR or optical-flow dependencies."""
from __future__ import annotations

import asyncio
import base64
import signal
import math
from datetime import datetime
from pathlib import Path
from collections import deque

from .bridge_ui import WebUIServer
from .rokid_runtime import PhaseBRokidRuntime


class PanelRokidRuntime(PhaseBRokidRuntime):
    def __init__(self, config, *, ui_port=8080, record_live=False,
                 live_record_dir="live_sessions", **kwargs):
        super().__init__(config, **kwargs)
        self._record_stopped = False
        if record_live:
            from .recorder_live import LiveRecorder
            directory = Path(live_record_dir).resolve() / datetime.now().strftime("rokid_%Y%m%d_%H%M%S_%f")
            self.live_rec = LiveRecorder(directory)
            self.live_rec.attach_to_player(self.speaker)
        self.ui = WebUIServer(
            port=ui_port, host="127.0.0.1",
            stop_callback=lambda: self.request_shutdown(),
            sessions_root=Path(live_record_dir),
            mode_info={"mode": "live", "device": "rokid"},
        )
        original = self.manager.handle_result

        async def show_result(session, result):
            gate = self.manager.gate
            accepted = (session is self.manager.active
                        and session.spec.generation == gate.generation
                        and not (gate.drop_output_until_listen
                                 and not result.get("is_listen")))
            await original(session, result)
            if accepted:
                if self.live_rec is not None:
                    await asyncio.to_thread(self.live_rec.log_model_chunk,
                                            result.get("text", ""), bool(result.get("is_listen")),
                                            bool(result.get("end_of_turn")))
                await self.ui.emit({
                    "type": "result", "text": result.get("text", ""),
                    "is_listen": bool(result.get("is_listen")),
                    "end_of_turn": bool(result.get("end_of_turn")),
                })

        self.manager.handle_result = show_result
        self._asr_events = deque(maxlen=20)
        self.harness.on_message = self._observe_harness

    def _observe_harness(self, payload):
        if payload.get("type") == "asr.transcript":
            self._asr_events.append({"type": "result", "is_listen": True,
                                     "end_of_turn": True,
                                     "text": payload.get("utterance", "")})

    async def _preview_loop(self):
        last_sequence = -1
        last_session = None
        while True:
            session = self.manager.active
            if session and session.status == "running" and session.session_id != last_session:
                last_session = session.session_id
                await self.ui.emit({"type": "session_start", "session_id": last_session})
            while self._asr_events:
                event = self._asr_events.popleft()
                if self.live_rec is not None:
                    await asyncio.to_thread(self.live_rec.log_asr, event["text"])
                await self.ui.emit(event)
            frame = self.latest_frame
            if frame.jpeg and frame.sequence != last_sequence:
                last_sequence = frame.sequence
                if self.live_rec is not None:
                    await asyncio.to_thread(self.live_rec.on_frame, frame.jpeg, frame.sequence)
                await self.ui.emit({
                    "type": "chunk", "idx": frame.sequence,
                    "img_b64": base64.b64encode(frame.jpeg).decode("ascii"),
                    "preview_only": True, "img_age_ms": 0, "img_fetch_ok": True,
                    "user_db": 20 * math.log10(max(self.stats.audio_rms, 1e-6)),
                    "reject_reason": "",  # Preview is independent of model consumption.
                })
            await asyncio.sleep(0.1)

    async def start(self):
        await self.ui.start()
        try:
            if self.live_rec is not None:
                await asyncio.to_thread(self.live_rec.start)
            await super().start()
            self._tasks.append(asyncio.create_task(self._preview_loop()))
        except BaseException:
            await self.close()
            raise

    async def close(self):
        try:
            if self.live_rec is not None and not self._record_stopped:
                self._record_stopped = True
                await asyncio.to_thread(self.live_rec.stop, finalize_media=False)
            await super().close()
        finally:
            await self.ui.stop()


if __name__ == "__main__":
    from .rokid_runtime import main
    main()
