# Assistive Voice Skill Harness (Phase A)

This directory is an optional, local sidecar for the MiniCPM-o browser Demo. It is
disabled by default and does not replace the native microphone/video path.

Start the sidecar explicitly:

```powershell
python -m extensions.assistive_harness.server --enabled `
  --model-path "C:\path\to\a\local\FunASR\model"
```

Then opt the browser tab in with `?assistive_harness=1`. Test-only transcript
injection additionally requires `--allow-test-injection`; it is never enabled by
the normal command above.

The browser hook is deliberately thin. Control, ASR, prompt registry, echo guard,
state machine, metrics, and CV shadow interfaces live under this directory so the
module can later be moved behind another transport (for example OpenGlass).

The first Rokid/OpenGlass transport is implemented in
[`phase_b/`](phase_b/README.md). It preserves this Core and the existing 8040
Gateway while replacing browser microphone, camera, playback, and Session calls
with a Python device adapter.

## User-editable Skills

System prompts live in `extensions/assistive_harness/prompts/`. Existing prompt
files are read on every activation, so users can replace a prompt and activate
the Skill again without restarting the sidecar. Voice phrases are configured in `runtime/openglass_omni/voice_commands.local.yaml` (copy its example). Skill IDs, enable flags and prompt mappings are in `config/skills.example.yaml`. Restart the sidecar after changing either YAML file.

Enabled by default:

- `帮我找<物体>` -> `find_object` -> hot Session restart with `{{target}}`
- `读一下` / `帮我识字` -> `read_text` -> hot Session restart
- `描述一下` / `看看周围` -> `describe_scene` -> hot Session restart
- `帮我避障` / `前面有障碍吗` -> `obstacle_avoidance` -> hot Session restart
- `回到聊天` / `恢复普通聊天` -> `idle_chat` -> hot Session restart

`obstacle_avoidance` contains the frozen AAAI_SI prompt and is enabled only for
stationary, supervised validation. Its mobility safety has not been accepted;
never treat this Demo as a navigation or safety device.

With the sidecar running, `GET http://127.0.0.1:8021/skills` reports the active
configuration and absolute prompt path for every registered Skill.

## CV V1 shadow pipeline

Browser JPEG mirrors enter a capacity-one latest-frame queue and are analyzed
on a dedicated single-thread worker. `cv_mode: disabled` drops frames before
inference; `cv_mode: shadow` writes `CVObservation` records without changing a
Skill or taking ownership of MiniCPM output. Slow, timed-out, or failed plugins
remain outside the audio/control receive path.

`find_object` uses the local `yolo_onnx` reference provider. Other Skills keep
the `noop` provider until a task-specific plugin is registered. Provider setup,
the observation schema, and an OCR implementation template are documented in
[`cv/README.md`](cv/README.md).

## Rokid panel integration / Rokid 面板接入

See [English setup](../../runtime/openglass_omni/STARTUP_en.md) and [中文说明](../../runtime/openglass_omni/STARTUP_zh.md) for APK installation, wireless startup, recording and prompt editing. The panel route uses gateway 8006 `/v1/realtime` and Harness 8021; the older 8040 example above is a separate entry.

技能提示词在 `prompts/`，触发词在 `runtime/openglass_omni/voice_commands.local.yaml`；两者不同。面板普通聊天的 `--prompt` 优先于 `idle_chat_zh.txt`。重新激活技能或创建会话后使用新提示词。
