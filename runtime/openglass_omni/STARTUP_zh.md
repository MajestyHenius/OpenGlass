# ESP32 / Rokid 安装与使用

以下操作从 OpenGlass 仓库根目录执行。Rokid 构建安装细节见 [App 说明](../../rokid_app/README_zh.md)。

## 安装与启动

完成首次环境和设备准备后，可通过面板一键启动。ESP32 与 Rokid 共用电脑上的推理后端，眼镜准备步骤分别进行，选择自己的设备分支即可。

### 1. 环境要求

以下以 Windows、Conda/Python 和 NVIDIA GPU 为例。编译后端需要 Visual Studio 2022 C++ Build Tools、CMake 及匹配的 CUDA 环境；Python 和依赖版本需与所用上游 checkout 一致。

- **ESP32 用户**：另需 Arduino IDE、ESP32 开发板支持和 USB 数据线。
- **Rokid 用户**：另需 Android Platform Tools（ADB）；首次编译采集 App 需要 Android SDK、JDK 17 和兼容的眼镜端工程。
- **功能对话**：需要本地 FunASR 模型，用于 Harness 语音控制。
- **MP4 录制**：需要 FFmpeg，并加入启动面板时的 PATH。

电脑与眼镜需要网络互通。模型权重、MiniCPM-o-Demo 和 llama.cpp-omni 均在本仓库之外准备。

### 2. 准备共用推理后端

将 OpenGlass、MiniCPM-o-Demo 和 llama.cpp-omni 分别放在独立目录中。面板通过本地配置找到各项目并启动服务。

V2 后端的启动顺序为：先启动独立的 `llama-omni-server`，再启动 `worker` 和 `gateway`。眼镜接入服务由 OpenGlass 面板另外启动。

```powershell
# 克隆 MiniCPM-o 所需的 llama.cpp-omni 后端
git clone --branch master https://github.com/tc-mb/llama.cpp-omni.git
# 进入目录并编译 CUDA 版本的推理服务
cd llama.cpp-omni
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build build --config Release --target llama-omni-server -j
cd ..

# 克隆 MiniCPM-o-Demo（包含 worker 和 gateway）
git clone --branch master https://github.com/OpenBMB/MiniCPM-o-Demo.git
cd MiniCPM-o-Demo
# 安装 MiniCPM-o-Demo 依赖
python -m pip install -r requirements.txt
cd ..

# 克隆 OpenGlass 项目
git clone https://github.com/OpenSQZ/OpenGlass.git
cd OpenGlass
# 安装 OpenGlass 运行时依赖
python -m pip install -r runtime/openglass_omni/requirements.txt
```

#### 模型文件

将 MiniCPM-o 4.5 GGUF 模块放在仓库之外的同一个目录中。当前启动器通过 `-m` 接收主模型路径；vision、audio、TTS 和 Token2Wav 文件应遵循当前 checkout 的 `llama.cpp-omni` 版本所要求的目录结构。

```text
MiniCPM-o-4_5-gguf/
├── MiniCPM-o-4_5-Q4_K_M.gguf
├── vision/
├── audio/
├── tts/
└── token2wav-gguf/
```

准确文件名和下载方法以 [`llama.cpp-omni` 的 prerequisites](https://github.com/tc-mb/llama.cpp-omni#prerequisites) 为准。

#### OpenGlass 功能依赖与 ASR

在运行 MiniCPM-o-Demo 的同一个 Conda 环境中，从 OpenGlass 仓库根安装面板及功能依赖：

```powershell
python -m pip install -r runtime/openglass_omni/requirements.txt
python -m pip install -r extensions/assistive_harness/phase_b/requirements-phase-b.txt
```

功能依赖文件同时包含 ESP32 图像筛选组件。下载配置示例中指定的 FunASR 模型，在后续配置中将模型目录填入 `asr_model`。

### 3. 准备眼镜（二选一）

#### ESP32：烧录固件并取得设备地址

1. 按[硬件教程](../../hardware/AI_GLASSES_OPEN_SOURCE_REPORT.md)准备眼镜。
2. 打开 [`CameraWebServer_PDM_Audio.ino`](../../CameraWebServer_PDM_Audio/CameraWebServer_PDM_Audio.ino)，在本机填写 Wi-Fi 名称和密码，选择匹配的 ESP32-S3 开发板并上传。
3. 用 `115200` 波特率打开串口监视器，记录设备连接 Wi-Fi 后取得的 IP；下一步将它填入设备表。

#### Rokid乐奇眼镜：安装采集 App 并准备 USB 调试

**手机端连接与开发者设置**

> 手机下载安装乐奇官方app Rokid AI，连接眼镜后在设置中开启开发者模式和眼镜 ADB 调试授权。

**编译与安装采集 App**

眼镜端源码位于 [`rokid_app/OpenGlassRokidSensor/`](../../rokid_app/README_zh.md)。安装 Android SDK 和 JDK 后，可使用提供的脚本编译；Gradle 依赖会在首次构建时下载。

采集 App **0.1.3 支持由面板在启动时传入电脑地址**。编译时无需填写 IP；安装后在 OpenGlass 本地配置中设置即可。

准备好 SDK 和 JDK 后，推荐在 OpenGlass 根使用构建安装脚本：

```powershell
# 连接 USB，先通过 adb devices -l 确认序列号
.\rokid_app\build_install.cmd -Sdk "<ANDROID_SDK_PATH>" -Install -Serial "<USB_SERIAL>"
```

省略 `-Install` 可只编译 APK。直接执行 Gradle/ADB 的等价步骤见 [Rokid App 说明](../../rokid_app/README_zh.md)。

安装后，允许采集 App 使用相机和麦克风，并通过设备或配套手机配置 Wi-Fi。安装报错的处理方法见下方“常见问题”。

首次启动时保持 USB 连接，待面板完成无线初始化后再拔线。

### 4. 配置 OpenGlass

以下命令均在 OpenGlass 仓库根执行。首次复制配置模板；已有本地配置时直接编辑，避免覆盖：

```powershell
Copy-Item runtime/openglass_omni/runtime.example.json runtime/openglass_omni/runtime.local.json
```

编辑 `runtime.local.json`，填写本机实际路径：

| 配置项 | 内容 |
| --- | --- |
| `minicpm_demo_root` | 包含 `worker.py`、`gateway.py` 的 MiniCPM-o-Demo 目录 |
| `llama_server` | 编译得到的 `llama-omni-server.exe` |
| `llama_model` | 主 GGUF 模型路径 |
| `asr_model` | Harness 使用的本地 FunASR 模型目录 |
| `conda_env` | 指定运行环境；留空则使用外部已激活的环境 |

面板启动时读取 `runtime.local.json`。修改配置后重开面板；首次使用沿用默认端口即可。

**ESP32 设备表**

```powershell
Copy-Item examples/configs/devices.example.json runtime/openglass_omni/devices.json
```

在设备表中填写名称、刚才取得的 `esp32_host`、HTTP 端口和摄像头顺时针旋转角 `rotate`（0、90、180 或 270）。

**Rokid 本地配置**

在完整 `runtime.local.json` 中补充或修改以下字段：

```json
{
  "rokid_mode": "wifi",
  "rokid_adb": "<ADB_EXE_ABSOLUTE_PATH>",
  "rokid_serial": "",
  "rokid_adb_addr": "",
  "rokid_pc_url": "http://<PC_LAN_IP>:18080"
}
```

通过电脑的 `ipconfig` 查找与眼镜网络互通的 IPv4，填入 `rokid_pc_url`。这是**电脑接收地址**；`rokid_adb_addr` 则是**眼镜无线调试地址**，首次由面板建立并保存。单副眼镜的 `rokid_serial` 可留空。

换网络后，先在眼镜端配置 Wi-Fi，再更新本地电脑地址并重开面板。面板会开启眼镜 Wi-Fi 并连接已保存的网络。

### 5. 启动与停止

激活准备好的 Conda 环境，在 OpenGlass 根目录运行：

```powershell
python glasses_panel.py
```

选择对应模式；ESP32 还需选择设备名称。点击“一键启动”，面板按顺序等待服务就绪：

```text
llama-omni-server :22500 → worker :22400 → gateway :8006
                                            ├─ ESP32 基础接入
                                            └─ Harness :8021 → ESP32 / Rokid 功能接入
第一视角页面 :8080；Rokid 采集输入 :18080
```

服务就绪后，打开第一视角页面，确认画面更新，再对眼镜说话测试回答。

**Rokid 首次无线初始化：** 保持 USB 接入，面板尝试开启 Wi-Fi、等待已保存网络取得地址，建立无线 ADB 并启动采集 App。日志确认无线 ADB 成功，且画面和声音正常后，才可拔 USB。如果日志提示“不能拔线”，保持 USB 连接并检查眼镜 Wi-Fi。

之后关闭面板或重启电脑，可复用保存的无线地址。眼镜重启后若无法连接，按下方“常见问题”恢复无线调试。

结束使用时点“停止所有”，等待会话清理和录制导出完成，再关闭窗口。设备启动、安装错误与网络排查详见[完整启动指南](../../runtime/openglass_omni/STARTUP_zh.md)。

## 使用与配置

### 面板模式

| 模式 | 用途 |
| --- | --- |
| ESP32基础对话 | ESP32 音视频输入与模型对话，不经过 Harness 和选图漏斗。 |
| ESP32功能对话 | 加入 Harness 语音控制与图像质量筛选；质量拒绝不自动暂停对话、不播放提示语。 |
| Rokid功能对话 | Rokid 音视频输入与 Harness 语音控制；当前不启用 ESP32 选图漏斗。 |

Harness 支持“暂停”“继续”“重新开始”等语音控制，以及找物、读文字和描述场景等任务。触发说法和任务提示词均可修改。

### 修改关键词和提示词

**语音触发词**决定什么说法会触发控制或技能。复制示例后编辑本地文件，再重启 Harness：

```powershell
Copy-Item runtime/openglass_omni/voice_commands.example.yaml runtime/openglass_omni/voice_commands.local.yaml
```

**技能提示词**决定模型如何执行任务，位于 [`extensions/assistive_harness/prompts/`](../../extensions/assistive_harness/prompts/)，如 `find_object_zh.txt`、`read_text_zh.txt`。修改后重新激活技能或创建新会话；现有会话不会自动更新。

**面板普通聊天提示词**来自 `panel.py` 的 `CONFIG["presets"]`。Rokid 启动时传入的 `--prompt` 会优先于 `idle_chat_zh.txt`，因此只修改 idle 文件可能不生效。

### 录音、MP4 与回放

Rokid 启动命令使用 `--record-live` 开启录制，配合 `--ui-port 8080`，保存到 `live_sessions/rokid_<时间戳>/`。`--live-record-dir` 可指定其他父目录。

正常停止后先保存用户和模型的 WAV，再清理服务并合成 `live_session.mp4`。电脑 PATH 中需要有 FFmpeg；缺少 FFmpeg 时仍保留 WAV 和图片。MP4 左右声道分别保存用户输入与模型输出音频。

第一视角与回放页面分别为 `http://localhost:8080/`、`http://localhost:8080/replay`；回放页需在对应 UI 服务运行时访问。ESP32 的录制路径和命令行重放参见[运行时说明](../../runtime/openglass_omni/README.md)。

录音在停止时写入文件。录制结束后请使用页面停止按钮或面板“停止所有”，等待导出完成。

