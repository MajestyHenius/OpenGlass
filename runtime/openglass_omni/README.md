# OpenGlass Omni Runtime

[English setup](STARTUP_en.md) · [中文安装与使用](STARTUP_zh.md) · [Project overview](../../README.md)

This directory contains the panel, device startup helpers and local configuration. The panel launches llama.cpp-omni, MiniCPM-o-Demo worker/gateway, Harness and the selected device adapter.

## Launch

Prepare the shared backend and device using the setup guide, copy `runtime.example.json` to `runtime.local.json`, and enter local model/backend paths. From the OpenGlass root:

```powershell
python glasses_panel.py
```

| Mode | Behavior |
| --- | --- |
| ESP32基础对话 | Basic multimodal conversation |
| ESP32功能对话 | Harness controls and image selection, without rejection speech or automatic pausing |
| Rokid功能对话 | Rokid input and Harness controls |

Rokid Android source and build/install scripts are in [rokid_app](../../rokid_app/README.md). Install the app once; the panel supplies the configured PC address at launch. USB bootstraps wireless ADB on first use or after the glasses reset debugging/network state.

## Configuration

| File | Purpose |
| --- | --- |
| `runtime.local.json` | Backend/model paths, environment and Rokid device settings |
| `devices.json` | ESP32 IP, name and rotation |
| `voice_commands.local.yaml` | Harness activation phrases; copy `voice_commands.example.yaml` |
| `panel.py` / `CONFIG["presets"]` | Panel chat prompts |
| `../../extensions/assistive_harness/prompts/` | Skill prompts |

Restart the panel after local configuration changes. Reactivate a skill after editing its prompt. The selected Rokid panel prompt overrides the idle-chat prompt file.

## Runtime files

| File or module | Purpose |
| --- | --- |
| `panel.py`, `panel.html` | Process control and panel UI |
| `process_job.py` | Windows child-process ownership |
| `rokid_device.py` | ADB selection, Wi-Fi recovery and collector launch |
| `esp32_bridge.py` | ESP32 bridge entry |
| `extensions.assistive_harness.phase_b.rokid_panel_runtime` | Rokid sensor ingress, Harness and live view |
| `extensions.assistive_harness.phase_b.recorder_live` | Functional-route recording/export |

Default ports: backend 22500, worker 22400, gateway 8006, Harness 8021, live view 8080 and Rokid sensor input 18080.

## Recording and replay

Rokid supports `--record-live` with `--ui-port 8080`. It writes `live_sessions/rokid_<timestamp>/`; use `--live-record-dir` to change the parent directory. Normal stop saves separate user/model WAV files, shuts down services and exports `live_session.mp4`. FFmpeg must be on PATH for video export. The two audio channels carry user/model audio.

Use the stop button and wait for export. Audio remains buffered until stop, so force-killing the process can lose it. The live UI is `http://localhost:8080/`; the corresponding running UI serves `/replay` for its recording directory.

The legacy ESP32 recorder uses `sessions/`. Its replay-only server can be launched separately:

```powershell
python runtime/openglass_omni/bridge_ui.py --sessions sessions --port 8080
```

## Rerun mode (command line)

`rerun_source.py` feeds a previously recorded session back into the model instead of a live ESP32 — the same bridge, but audio/images come from disk. This is the most convenient way to re-test the model repeatedly against a fixed input, without the glasses. It is a command-line workflow, not a panel button.

Bring up the front stages first (via the panel, or manually: `llama-omni-server` → `worker.py` → `gateway.py`), then run the bridge in rerun mode:

```bash
python runtime/openglass_omni/esp32_bridge.py \
  --rerun-from sessions/<session-id> \
  --gateway localhost:8006 \
  --prompt "your prompt"
```

Notes:

- **Gateway TLS**: the V2 gateway (`8006`) accepts `wss`, and the bridge defaults to it — do **not** pass `--no-tls`.
- **No device flags**: rerun does not connect to glasses, so omit `--device` / `--device-config`.
- **Inputs**: the session directory must contain `user_raw.pcm` (or `live_user.wav`), `images/`, and `events.jsonl` — all produced by a normal recorded run.
- **Optional**: `--rerun-speed` (default `1.0`), `--rerun-drain-s` (default `5.0`), `--ui-port` (default `8080`, change it if a live session is already using 8080).
- **Requires `sounddevice`**: rerun plays the recorded user audio through a separate output stream. Any machine that can run a live session already has it.

Watch the rerun via the bridge's own live view at `http://localhost:<ui-port>/`.
