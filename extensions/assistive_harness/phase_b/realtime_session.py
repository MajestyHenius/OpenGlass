#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realtime_session.py —— 把 duplex 会话接到 V2 gateway 的 /v1/realtime 协议。

为什么需要它
------------
V1 gateway 有 `/ws/duplex/{session_id}`；**V2 gateway 只有 `/v1/realtime`**
（实测：V2 gateway.py 里唯一的 @app.websocket 是 /v1/realtime，
 连 /ws/duplex/... 会被 FastAPI 直接 403 拒绝握手）。
所以 rokid_runtime / esp32_runtime 里那套 GatewayDuplexSession 在 V2 下连不上，
demo_esp32_duplex_0703 同样连不上（它连的也是 /ws/duplex）。

做法
----
`GatewaySessionManager` 有现成的替换点 `session_factory`，
所以这里**继承 GatewayDuplexSession，只覆盖协议相关的方法**，
其余（音频攒块、状态管理、start/_run 骨架、stop 的收尾）全部复用。
rokid_runtime.py 和 esp32_runtime.py 都不用改，只在构造 manager 时传入本类。

协议对照（V1 内部  →  V2 对外）
------------------------------
  连接      URL 带 session_id          →  连上后 session.init，服务端回 session.created
  排队      queued / queue_done        →  session.queued / session.queue_done
  上行      audio_chunk                →  input.append
              audio_base64             →    input.audio
              frame_base64_list        →    input.video_frames
              force_listen             →    input.force_listen
  下行      {is_listen,text,audio_data}→  response.output.delta 的 kind: listen/text/audio
  结束      stop                       →  session.close

注意
----
· V2 的 session_id 由服务端在 session.created 里给，客户端不能自己指定。
  但 manager/日志里到处用 self.session_id，所以保留本地生成的那个作为占位，
  拿到服务端的之后覆盖掉。
· full-duplex 下没有 response.done（文档说它只用于 mode=chat），
  turn 边界靠 delta 里的 turn_id 变化判断。
· kind=audio 的 PCM 是 24kHz mono float32 base64，和 V1 的 audio_data 同格式，
  所以 on_result 那一侧不用改。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import aiohttp
import numpy as np

from .rokid_runtime import (
    LOG,
    GatewayDuplexSession,
    SAMPLE_RATE_IN,
    float32_to_base64,
    now_ms,
)


class RealtimeDuplexSession(GatewayDuplexSession):
    """V2 /v1/realtime 协议版的 duplex 会话。构造签名与父类完全一致。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._server_closed = asyncio.Event()
        self._init_sent = False
        self._backend_session_id = None
        self._cleanup_confirmed = False
        self._stop_lock = asyncio.Lock()
        self._sender = None
        self.close_uncertain = False

    async def _send_json(self, payload: dict[str, Any]) -> None:
        # 与 session.close 共用锁；即使发送者已经在等锁，也不能越过关闭边界。
        async with self._send_lock:
            if self._closing and payload.get("type") != "session.close":
                return
            if self.ws is None or self.ws.closed:
                raise RuntimeError("gateway session is not connected")
            if payload.get("type") == "session.init":
                self._init_sent = True
            await self.ws.send_json(payload)

    async def start(self) -> None:
        try:
            await super().start()
        except (Exception, asyncio.CancelledError):
            # wait_for(shield(_ready)) 超时不会停止 _run，必须在重试前收回它。
            try:
                await self.stop("startup_failed")
            except Exception as exc:
                LOG.warning("[GW-LIFECYCLE] startup cleanup: %s", exc)
            raise

    # ------------------------------------------------------------ 连接
    async def _run(self) -> None:
        io_tasks = []
        scheme = "wss" if self.config.gateway_tls else "ws"
        # mode=video：连续音频 + 可带视频帧，300s 上限。
        # mode=audio 是纯音频（600s），我们要送图所以用 video。
        url = f"{scheme}://{self.config.gateway}/v1/realtime?mode=video"
        self.status = "connecting"
        LOG.info("[GW] connecting generation=%d %s (realtime)",
                 self.spec.generation, url)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(
                    url,
                    heartbeat=30,
                    ssl=self._ssl_context(),
                    max_msg_size=0,
                ) as ws:
                    self.ws = ws
                    await self._prepare(ws)
                    if not self._closing:
                        self.status = "running"
                        if self._ready is not None and not self._ready.done():
                            self._ready.set_result(None)
                    self._sender = asyncio.create_task(self._send_loop(ws))
                    receiver = asyncio.create_task(self._receive_loop(ws))
                    io_tasks = [self._sender, receiver]
                    try:
                        done, _ = await asyncio.wait(io_tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            if not task.cancelled():
                                task.result()
                        # stop() 先停止上行，接收者必须继续读关闭确认。
                        if self._closing and not receiver.done():
                            await receiver
                    finally:
                        for task in io_tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*io_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.status = "failed"
            self.last_error = str(exc)
            LOG.warning("[GW] session %s failed: %s", self.session_id, exc)
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(RuntimeError("gateway closed before session ready"))
            self._stopped.set()
            if self.status not in ("failed",):
                self.status = "closed"

    # ------------------------------------------------------------ 握手
    async def _prepare(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """等 session.queue_done → 发 session.init → 等 session.created。

        文档明确要求：**必须等 queue_done 再发 session.init**。
        没有排队时服务端会立刻下发 queue_done。
        """
        self.status = "queued"
        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise RuntimeError("gateway closed while queued")
                continue
            payload = json.loads(message.data)
            mtype = payload.get("type")
            if mtype == "session.closed":
                self._server_closed.set()
                raise RuntimeError("gateway session closed while queued")
            if mtype == "session.queue_done":
                break
            if mtype == "error":
                raise RuntimeError(
                    (payload.get("error") or {}).get("message")
                    if isinstance(payload.get("error"), dict)
                    else payload.get("error") or "gateway queue error"
                )
            if mtype in ("session.queued", "session.queue_update"):
                LOG.info("[GW] queue position=%s eta=%s",
                         payload.get("position"),
                         payload.get("estimated_wait_s"))

        self.status = "preparing"
        init_payload: dict[str, Any] = {
            "system_prompt": self.spec.system_prompt,
            "config": {
                "force_listen_count": self.config.force_listen_count,
                "chunk_ms": self.config.chunk_ms,
                "generate_audio": True,
                "max_new_speak_tokens_per_chunk": (
                    self.config.max_new_speak_tokens_per_chunk
                ),
                "length_penalty": self.config.length_penalty,
            },
        }
        await self._send_json({"type": "session.init", "payload": init_payload})

        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise RuntimeError("gateway closed while preparing")
                continue
            payload = json.loads(message.data)
            mtype = payload.get("type")
            if mtype == "session.closed":
                self._server_closed.set()
                raise RuntimeError("gateway session closed while preparing")
            if mtype == "session.created":
                # session_id 由服务端给，覆盖本地占位的那个
                sid = payload.get("session_id")
                if sid:
                    self.session_id = sid
                    self._backend_session_id = sid
                LOG.info("[GW] prepared session=%s generation=%d skill=%s mode=%s",
                         self.session_id, self.spec.generation,
                         self.spec.skill_id, payload.get("mode"))
                return
            if mtype == "error":
                raise RuntimeError(
                    (payload.get("error") or {}).get("message")
                    if isinstance(payload.get("error"), dict)
                    else payload.get("error") or "gateway prepare error"
                )

    # ------------------------------------------------------------ 上行
    async def _send_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """攒 1s 音频 + 带上最新帧，发 input.append。

        攒块、取帧、force_listen 的判断逻辑与父类完全一致，只换消息外壳。
        """
        carry = bytearray()
        last_frame_sequence = -1
        last_frame_sent = 0.0
        sent = 0
        while not self._closing:
            audio = await self._next_audio_chunk(carry)
            if self._closing:
                break
            if audio is None:
                continue
            inp: dict[str, Any] = {
                "audio": float32_to_base64(audio),
                "max_slice_nums": self.config.max_slice_nums,
            }
            if self.gate.speech_hold_active:
                inp["force_listen"] = True
            frame = self.latest_frame
            frame_age_ms = now_ms() - frame.timestamp_ms
            frame_due = time.monotonic() - last_frame_sent >= self.config.image_resend_s
            if (
                frame.jpeg
                and frame_age_ms <= self.config.image_max_age_s * 1000
                and (frame.sequence != last_frame_sequence or frame_due)
            ):
                inp["video_frames"] = [base64.b64encode(frame.jpeg).decode("ascii")]
                last_frame_sequence = frame.sequence
                last_frame_sent = time.monotonic()
            sent += 1
            LOG.debug("[GW-SEND] #%d audio=%.2fs lvl=%.4f frame=%s hold=%s",
                      sent, len(audio) / SAMPLE_RATE_IN,
                      float(np.abs(audio).mean()),
                      "video_frames" in inp, self.gate.speech_hold_active)
            await self._send_json({"type": "input.append", "input": inp})

    # ------------------------------------------------------------ 下行
    async def _receive_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """把 response.output.delta 翻译回 {is_listen, text, audio_data}。

        父类的 on_result 拿到的是 V1 那套字段，这里保持不变，
        所以 handle_result / TurnPrinter / speaker 那一侧一行都不用改。
        """
        cur_turn = None
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            payload = json.loads(message.data)
            mtype = payload.get("type")

            if mtype == "session.closed":
                self._server_closed.set()
                LOG.info("[GW-LIFECYCLE] transport close notification session=%s reason=%s",
                         self.session_id, payload.get("reason"))
                break
            if self._closing:
                continue  # 丢弃旧输出，但继续等 session.closed。

            if mtype == "response.output.delta":
                kind = payload.get("kind")
                turn = payload.get("turn_id")
                # full-duplex 没有 response.done，用 turn_id 变化当边界
                end_of_turn = (cur_turn is not None and turn is not None
                               and turn != cur_turn)
                if turn is not None:
                    cur_turn = turn
                result: dict[str, Any] = {
                    "type": "result",
                    "is_listen": kind == "listen",
                    "text": payload.get("text") or "",
                    "audio_data": payload.get("audio") or "",
                    "end_of_turn": end_of_turn,
                    "turn_id": turn,
                }
                await self.on_result(self, result)
                continue

            if mtype == "response.done":
                # 实测：full-duplex 下**也会**发 response.done（文档说只用于 chat，
                # 与实际不符），而且它的 text 是这一段的**完整文本** ——
                # 前面 delta 已经把这些字逐块传下去了，这里再传一次就是重复
                # （实测每段都被记两遍，只差 1~2ms）。
                # 所以这里只标记 turn 结束，**不再传 text**。
                await self.on_result(self, {
                    "type": "result", "is_listen": False,
                    "text": "", "audio_data": "",
                    "end_of_turn": True, "turn_id": cur_turn,
                })
                continue

            if mtype == "error":
                err = payload.get("error")
                msg = err.get("message") if isinstance(err, dict) else str(err)
                LOG.warning("[GW] error: %s", msg)
                self.last_error = msg or "gateway error"
                continue

            if mtype == "debug":
                # V2 自带的阶段追踪（llm.chunk / tts.chunk / t2w.chunk，带 ts）。
                # 平时不打，需要时 --log-level DEBUG 打开看服务端各阶段耗时。
                LOG.debug("[GW-DEBUG] %s", json.dumps(payload, ensure_ascii=False)[:200])
                continue

    # ------------------------------------------------------------ 结束
    async def _close_backend(self, reason: str) -> None:
        # Unlike the early WS notification, this HTTP response is emitted only
        # after the backend's cleanup mutex is released and the session removed.
        from urllib.parse import quote
        sid = self._backend_session_id
        if not sid:
            raise RuntimeError("backend session ID unavailable; cannot confirm cleanup")
        url = f"{self.config.backend_close_url.rstrip('/')}/sessions/{quote(sid, safe='')}/close"
        timeout = aiohttp.ClientTimeout(total=self.config.close_timeout_s)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
            async with client.post(url, json={"reason": reason}) as response:
                response.raise_for_status()
                result = await response.json()
                if (result.get("ok") is not True or result.get("closed") is not True
                        or result.get("session_id") != sid):
                    raise RuntimeError("backend did not confirm completed session cleanup")
        self._cleanup_confirmed = True
        LOG.info("[GW-LIFECYCLE] cleanup completed session=%s (backend HTTP)", sid)

    async def stop(self, cleanup_mode: str) -> None:
        async with self._stop_lock:
            first_close = not self._closing
            self._closing = True
            try:
                if self._sender and not self._sender.done():
                    self._sender.cancel()
                    await asyncio.gather(self._sender, return_exceptions=True)
                if self._init_sent and first_close and self.config.backend_close_url:
                    LOG.info("[GW-LIFECYCLE] waiting for backend cleanup session=%s", self.session_id)
                    # Do not also send session.close through worker: that would
                    # submit a second concurrent HTTP close for the same session.
                    await self._close_backend(cleanup_mode or "user_stop")
                elif self._init_sent and first_close and self.ws is not None and not self.ws.closed:
                    LOG.info("[GW-LIFECYCLE] close requested session=%s generation=%d",
                             self.session_id, self.spec.generation)
                    await asyncio.wait_for(
                        self._send_json({"type": "session.close",
                                         "reason": cleanup_mode or "user_stop"}),
                        timeout=self.config.close_timeout_s)
                if self._init_sent and self._task and not self._task.done():
                    await asyncio.wait_for(self._stopped.wait(),
                                           timeout=self.config.close_timeout_s)
            except Exception as exc:
                LOG.warning("[GW-LIFECYCLE] close incomplete session=%s: %r", self.session_id, exc)
            finally:
                # 排队尚未 init 时无需 close 消息；断开即可撤销排队。所有路径都回收任务。
                if self._task and not self._task.done():
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
                if self.ws is not None and not self.ws.closed:
                    await self.ws.close()
                self.close_uncertain = self._init_sent and not self._cleanup_confirmed
                if self.close_uncertain:
                    LOG.error("[GW-LIFECYCLE] close_unconfirmed session=%s; automatic replacement blocked",
                              self.session_id)
            if self.close_uncertain:
                self.last_error = "后端清理完成未获确认，已停止自动重建；请全部停止后重新启动"
                LOG.error("[GW-LIFECYCLE] %s session=%s", self.last_error, self.session_id)
                raise RuntimeError(self.last_error)

    # ------------------------------------------------------------ 文本注入
    async def inject_task(self, text: str) -> bool:
        """V2 的 input.append 没有 inject_text 字段，暂不支持。

        父类靠它实现"技能切换时把指令文本塞给模型"。V2 协议里没有对应字段，
        硬塞会被服务端忽略或报错，所以明确返回 False 让调用方走别的路径，
        而不是假装成功。
        """
        LOG.info("[GW] inject_task 在 /v1/realtime 协议下暂不支持，跳过: %s", text[:30])
        return False
