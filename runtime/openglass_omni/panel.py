#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SmartGlasses 现场演示控制面板 —— ALL（双链路版）
==============================================
在 glasses_panel_new.py 基础上，把 Rokid 链路也并进来。两条链路二选一：

  ESP32 链: llama -> worker -> gateway -> demo_esp32_duplex_0703.py
  Rokid 链: llama -> worker -> gateway -> rokid_minicpm_v8.py

前三级共用，只有第四级不同。顶部「链路」下拉切换，切换即换状态灯/日志页签/
第一视角地址；Rokid 分支自动隐藏「眼镜」下拉（它是 APK 反向连进来，没有选镜这回事）。

★ Rokid 必须用 v8：v7 是旧协议(/ws/duplex + prepare)，连不上新 gateway(8006, V2)。
★ run_rokid.ps1 / run_rokid_wifi.cmd 已不需要——建目录、设环境变量、拼参数、
  USB 下 adb reverse + 拉起 APK，全部内联进本面板（见 _rokid_pre_launch /
  _rokid_env / _rokid_post_launch）。把 rokid_minicpm_v8.py 放在本面板同级目录即可。

旧的 glasses_panel_new.py 保持原样不动：这条路线万一现场坏了，ESP32 链路还有退路。

运行：  python glasses_panel_all.py
依赖：  pip install pywebview
只需修改下面的 CONFIG 区块即可，其余无需改动。
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
import re
import webview  # pip install pywebview

# ============================================================================
#  CONFIG —— 只改这一块
# ============================================================================
CONFIG = {
    # 环境：推荐先在终端 `conda activate SmartGlasses` 再跑本面板，此项填 None，
    # 面板会用当前激活环境的 python 起子进程（等同你手动 python worker.py）。
    # 只有在没预先激活、想让面板自己进环境时才填环境名（Windows 下可能因
    # subprocess 找不到 conda 而报 WinError 2，不推荐）。
    "conda_env": None,

    # 工作目录（仅对 llama/demo/rokid 生效；这几个已用绝对路径，一般留空即可）。
    "cwd": "",

    # ★ 必填：MiniCPM-o-Demo 目录（worker.py / gateway.py 所在处）★
    # worker 和 gateway 是上游 MiniCPM-o-Demo 的文件，不在本仓库里，面板需要知道去哪
    # 启动它们。填成你 clone 的 MiniCPM-o-Demo 的绝对路径，例如：
    #   r"D:\MiniCPM-o-Demo"
    # 留空则 worker/gateway 会在面板目录找 worker.py（找不到，启动失败）。
    #"minicpm_demo_dir": r"<PATH_TO>\MiniCPM-o-Demo",
    #e.g.
    "minicpm_demo_dir": r"D:\MiniCPM-o-Demo_0710\MiniCPM-o-Demo",

    # 注意：眼镜 IP 不在这里配。固件走 DHCP，IP 会变，统一由 devices.json 管理、
    # 面板顶部下拉选择（见下方 devices）。此处不再放任何眼镜 IP。

    # ── 新架构（llama.cpp-omni）端口 ──
    # 启动链：llama-omni-server(22500) → worker(22400) → gateway(8006) → demo
    "gateway_port": 8006,          # gateway 就绪探测端口（demo 连它，新架构默认 8006）

    # llama-omni-server 健康检查：panel 轮询 http://127.0.0.1:<port>/health，
    # 返回 200 才放行 worker（对应你手动的 curl .../health）。这是新架构的第一个进程。
    "llama_health_url": "http://127.0.0.1:22500/health",
    "llama_ready_timeout_s": 300.0,   # llama 加载大模型可能很慢，给足 5 分钟

    # worker 就绪：日志关键字命中 "Uvicorn running on ...:22400" 即放行（见 _ready_pat）。
    "worker_ready_port": 22400,
    "worker_health_port": None,     # 关键字就绪为主，不用 HTTP health（None=关闭，避免端口配错卡顿）
    "health_probe_s": 15.0,
    "stable_alive_s": 8.0,

    # 启动就绪与重试
    "ready_timeout_s": 120.0,   # 单进程就绪等待上限（worker 连 backend + 加载可能几十秒）
    "start_retry": 3,
    "start_retry_gap_s": 4.0,

    # 第一视角地址 = demo 的 bridge_ui（--ui-port 默认 8080），跑在本机 PC 上，
    # 与眼镜 IP 无关，用 localhost 一劳永逸。可开关。
    "fpv_url": "http://localhost:8080",

    # ── Rokid 分支专用 ──
    # Rokid 是"反向"链路：bridge 在 PC 上监听，眼镜里的 APK 主动连进来。
    # 所以 bridge 没有 --device / 眼镜 IP，只有自己的监听端口。
    "rokid_port": 18080,                       # bridge 监听端口(APK 硬编码连这个)
    "rokid_health_url": "http://127.0.0.1:18080/health",
    # Rokid 的第一视角 = live.html（v8 已接入 bridge_ui，和 ESP32 同一套前端）。
    # 端口沿用 8080：两条链互斥，一台机不会同时接两副眼镜，不会撞端口。
    # bridge 自带的诊断页仍在 http://127.0.0.1:18080/（要看原始帧时手动开）。
    "rokid_fpv_url": "http://localhost:8080",
    "rokid_save_root": "sessions",
    "rokid_log_dir": "logs",
    # USB 模式才需要 adb reverse + 拉起 APK；WiFi 模式不需要 adb。
    "rokid_mode": "wifi",                      # "wifi" | "usb"
    "rokid_enable_funasr": False,              # True = 加 --enable-funasr（需装 funasr 包）
    "rokid_adb": "adb",                        # USB 模式下的 adb 路径
    "rokid_package": "org.opensqz.openglass.rokid.debug",
    "rokid_activity": "org.opensqz.openglass.rokid.debug/org.opensqz.openglass.rokid.MainActivity",

    # 三个进程的启动命令（数组形式，避免 shell 引号地狱）。
    # {prompt} 占位符会在启动 demo 时替换成当前选中的 prompt。
    "procs": {
        # ① llama-omni-server（C++ 后端，必须最先起）。
        #    ★ 开源用户必改：把下面两个路径改成你机器上的实际位置★
        #      - llama-omni-server.exe：llama.cpp-omni 编译产物
        #        (build/bin/Release/llama-omni-server.exe)
        #      - -m 后的 .gguf：MiniCPM-o 主模型权重（vision/audio/tts 子模型放同级目录，
        #        见 README 的模型目录结构）
        #    panel 会 curl llama_health_url 返回 200 后才起 worker。
        "llama": [
            #r"<PATH_TO>\llama.cpp-omni\build\bin\Release\llama-omni-server.exe",
            #e.g.
            r"D:\New llama\llama.cpp-omni\build\bin\Release\llama-omni-server.exe",
            #"-m", r"<PATH_TO>\MiniCPM-o-gguf\MiniCPM-o-4_5-Q4_K_M.gguf",
            # e.g.
            "-m", r"C:\SmartGlasses\MiniCPM-o-4-5-gguf\MiniCPM-o-4_5-Q4_K_M.gguf",
            "-ngl", "99",
            "--host", "127.0.0.1",
            "--port", "22500",
            "--ctx-size", "8192",
        ],
        # ② worker：通过 --backend-server-url 连已起好的 llama-omni-server
        "worker": [
            "python", "worker.py",
            "--host", "0.0.0.0",
            "--port", "22400",
            "--gpu-id", "0",
            "--backend-server-url", "http://127.0.0.1:22500",
        ],
        # ③ gateway
        "gateway": ["python", "gateway.py"],
        # ④ demo
        "demo": [
            "python", "{here}/esp32_bridge.py",
            # 眼镜 IP 从配置文件读取；{device} 会被面板选中的眼镜名替换
            "--device-config", "{here}/devices.json",
            "--device", "{device}",
            "--image-min-interval-s", "0.8",
            # 旋转角度不再写死：由 devices.json 里每个设备的 rotate 决定
            "--gateway", "localhost:8006",
            # 新架构 gateway(8006) 是明文 ws，必须加 --no-tls（否则 demo 默认走 wss 连不上）
            #"--no-tls",
            # barge 暂时关闭（force_listen 实测不即时生效，且底噪易误触发吞音）。
            # 需要时把下一行取消注释即可重新启用。
            # "--barge-enable",
            "--barge-hold-ms", "200",
            "--image-transport", "http",
            # barge 触发阈值：0.002 太低、埋在环境底噪(实测 0.0016~0.0046)里导致误触发、
            # 大量 flush 吞掉 AI 音频。提到 0.01：真打断实测都 >0.0098，底噪都 <0.005，干净分界。
            "--barge-user-rms", "0.01",
            "--echo-mode", "off",
            # 播放器起播预缓冲(对应 gateway 前端 200ms)；调吞音/起播延迟时改这里
            "--player-prebuffer-ms", "200",
            # 声卡播放采样率(0=自动查询设备原生率，对齐 gateway)；异常时可手动指定如 44100/48000
            "--player-device-sr", "0",
            # 音频输出走 WASAPI(现代低延迟，避开默认 MME 的实时丢帧)；异常可改 mme / default
            "--player-hostapi", "wasapi",
            # 诊断丢音用：取消注释后录 callback 实际送声卡的音频到 live_dac.wav
            # "--player-dac-probe",
            # 诊断丢音用：取消注释后把上游原始块连续拼接录到 live_raw.wav(无归位无补零)
            # "--player-raw-probe",
            "--prompt", "{prompt}",
            # 连接韧性：眼镜忘开/没电也不退出，持续等待（面板状态条显示"连接中"）
            "--connect-wait-s", "120",
            "--connect-retry",
        ],

        # ⑤ 8021 harness —— 语音控制腿（停一下/重新开始/找物），也是 ASR 来源。
        #    只有 ②③④ 带 harness 的链路才起它。
        #    ★ 开源用户必改：--model-path 指向你机器上的 FunASR 流式模型目录 ★
        #    证书：自签即可，只为过 wss 握手，不绑机器；客户端全是 CERT_NONE 不校验。
        #    ★ 它在 extensions/ 下，用 -m 启动，因此 cwd 必须是 MiniCPM-o-Demo 目录
        #      （见 _spawn 里的 name in (...) 白名单）。
        "harness": [
            "python", "-m", "extensions.assistive_harness.server", "--enabled",
            #"--model-path", r"<PATH_TO>\LocalASRmodel\speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online",
            # e.g.
            "--model-path", (r"D:\OpenGlass\OmniDeployment\OpenGlass\LocalASRmodel"
                             r"\speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"),
            "--port", "8021",
            # 绝对路径：harness 的 cwd 是 OpenGlass 仓库根，而证书由 _ensure_certs
            # 生成在 minicpm_demo_dir/certs 下（gateway 也用同一对）。
            "--certfile", "{certfile}",
            "--keyfile", "{keyfile}",
        ],

        # ⑥ demo_funnel —— esp32_runtime（harness + CV 漏斗）。与 ④ demo 互斥：
        #    两者都要独占 ESP32 的音频 WS 和图像 TCP，不能同时跑。
        #    这里只放各档位共用的参数；--esp32-host/--esp32-port/--rotate 由
        #    devices.json 填（_device_args），漏斗档位参数由 _funnel_extra_args 追加。
        #    ★ 同样在 extensions/ 下，cwd 必须是 MiniCPM-o-Demo 目录。
        #    ★ 8006 是 TLS（https:// 能取到 openapi.json，http:// 是 Empty reply），
        #      走默认 wss，**不要**加 --no-tls；而 harness 8021 是自签 wss，两者都是 wss
        #      但证书来源不同。
        "demo_funnel": [
            "python", "-m", "extensions.assistive_harness.phase_b.esp32_runtime",
            "--gateway", "localhost:8006",
            "--harness-url", "wss://127.0.0.1:8021/ws/control",
            "--image-tcp-port", "5000",
            "--web-ui-port", "8080",
            "--record-live",
            "--prompt", "{prompt}",
        ],

        # ⑦ rokid bridge —— 与 demo 平行、二选一的“第四个进程”。
        #    注意：它不是客户端去连眼镜，而是在 PC 上开 18080 端口等 APK 连进来。
        #    ★ 用 v8（API V2）。v7 是旧协议(/ws/duplex)，连不上新 gateway:8006。
        #    ★ run_rokid.ps1 / run_rokid_wifi.cmd 已不再需要——它们做的事
        #      （建目录、设环境变量、拼参数、USB 下 adb reverse + 拉起 APK）
        #      全部内联到本面板里了，见 _rokid_pre_launch() 与 _spawn()。
        "rokid": [
            "python", "{here}/rokid_minicpm_v8.py",
            "--host", "0.0.0.0",
            "--port", "18080",
            # v8 默认已是 ws + 8006，这里显式写出，方便现场改
            "--gateway", "localhost:8006",
            #"--no-gateway-tls",
            "--gateway-tls",
            "--save-session",
            "--save-root", "sessions",
            "--image-enhance", "auto",
            "--image-rotate-cw", "270",
            "--log-level", "INFO",
            # live.html 观测页（与 ESP32 demo 同一套 bridge_ui 前端/模板）
            "--ui-port", "8080",
            "--prompt", "{prompt}",
            # ★ 眼镜连的 WiFi。**不要把真实密码提交进仓库** ——
            #   在 panel.py 同目录放一个 panel.local.json（已在 .gitignore）：
            #     { "glasses_ssid": "你的WiFi", "glasses_psk": "你的密码" }
            #   没有该文件时用下面的占位值，Rokid 链路会连不上 WiFi 但其余功能正常。
            "--glasses-ssid", "{glasses_ssid}",
            "--glasses-psk", "{glasses_psk}",
        ],
    },

    # ── 链路分支：panel 同时只跑一条 ──
    # 共用前三级(llama→worker→gateway)，只有第四级不同：
    #   esp32 → demo_esp32_duplex_0703.py（PC 主动连眼镜，需要选 device）
    #   rokid → rokid_minicpm_v7.py      （PC 开端口等 APK 连进来，无 device）
    "chains": {
        "esp32": {
            "label": "① 基础对话",
            "tail": "demo",                      # 第四级进程名
            "start_order": ["llama", "worker", "gateway", "demo"],
            "stop_order":  ["demo", "gateway", "worker", "llama"],
            "need_device": True,                 # 显示眼镜下拉
            "fpv_key": "fpv_url",                # 第一视角地址
        },
        # ── 以下三条走 esp32_runtime（harness + 漏斗），四档递进演示 ──
        #   ① 基础对话 = 上面的 "esp32"（esp32_bridge.py，无 harness 无漏斗）
        #   ② 语音控制 = harness 开、漏斗关
        #   ③ 质量筛选 = ② + 每秒多帧里挑最清晰的一张
        #   ④ 完整防幻觉 = ③ + 坏图直接拦下并语音提示
        #   每档只比上一档多一件事：看到什么说什么 → 能听懂指令 → 图会挑 → 坏图会拦
        #   （没有"只对焦"这一档：对焦后要等 settle 再重抓，1s chunk 时序固定，
        #     对焦后那张未必赶得上这一轮，等于花了时间没用上。）
        "esp32_voice": {
            "label": "② 语音控制",
            "tail": "demo_funnel",
            "funnel": 0,                         # 漏斗档位：0 关 / 2 选图 / 3 选图+拒绝
            "start_order": ["llama", "worker", "gateway", "harness", "demo_funnel"],
            "stop_order":  ["demo_funnel", "harness", "gateway", "worker", "llama"],
            "need_device": True,
            "fpv_key": "fpv_url",
        },
        "esp32_select": {
            "label": "③ 质量筛选",
            "tail": "demo_funnel",
            "funnel": 2,
            "start_order": ["llama", "worker", "gateway", "harness", "demo_funnel"],
            "stop_order":  ["demo_funnel", "harness", "gateway", "worker", "llama"],
            "need_device": True,
            "fpv_key": "fpv_url",
        },
        "esp32_full": {
            "label": "④ 完整防幻觉",
            "tail": "demo_funnel",
            "funnel": 3,
            "start_order": ["llama", "worker", "gateway", "harness", "demo_funnel"],
            "stop_order":  ["demo_funnel", "harness", "gateway", "worker", "llama"],
            "need_device": True,
            "fpv_key": "fpv_url",
        },
        "rokid": {
            "label": "Rokid 眼镜",
            "tail": "rokid",
            "start_order": ["llama", "worker", "gateway", "rokid"],
            "stop_order":  ["rokid", "gateway", "worker", "llama"],
            "need_device": False,                # bridge 不需要眼镜 IP
            "fpv_key": "rokid_fpv_url",
        },
    },
    "default_chain": "esp32",

    # 判据档位：漏斗的两套标定参数。对外用场景名，不暴露内部值。
    #   严格 = medicine  （药盒小字高危，worst_x0=6.5 ACCEPT_Q=0.55）
    #   日常 = stationery（生活用品，worst_x0=5.0 ACCEPT_Q=0.48）
    "scenes": {"严格判据": "medicine", "日常判据": "stationery"},
    "default_scene": "严格判据",

    # 档位④的提示音目录。**跟着 extensions 包走**（和 bridge_ui 的 templates/ 同思路），
    # 不落在上游 MiniCPM-o-Demo 里，保持三仓库独立。这里是相对 OpenGlass 仓库根，
    # panel 会拼成绝对路径传给 esp32_runtime（它的 cwd 是上游，相对路径会指错地方）。
    # 启动前检查，缺了就报错 —— 而不是跑起来才发现没声音，那时 duplex 已在跑，
    # 没法当场用 gen_reject_wavs.py 生成。
    "reject_wav_dir": "extensions/assistive_harness/phase_b/assets/reject_wav",

    # 进程的界面显示名（灯泡/日志页签用）。内部名不变，只影响 UI。
    "proc_labels": {
        "llama": "推理后端",
        "worker": "worker",
        "gateway": "gateway",
        "harness": "语音控制(8021)",
        "demo": "眼镜",
        "demo_funnel": "眼镜+漏斗",
        "rokid": "Rokid",
    },

    # 可选眼镜（对应 devices.json 里的 name）。面板顶部下拉选择。仅 ESP32 分支用。
    "devices": ["左镜", "右镜", "备用镜"],

    # 预设场景 prompt（顶部若干按钮）。文本可在界面里再编辑。
    #"presets": {
    #    "通用助手": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面,听到用户的声音。 你能做的事: - 帮用户找东西。用户说要找什么,在画面里看到了就告诉用户位置,没看到就说没看到。 - 念画面里的字。用户让你念,就照原样念出来,看不清就让用户拿近一点,严禁编造信息。 - 描述用户看到的场景。用户让你描述,用三到五句话说清楚画面里有什么。 - 在室内帮用户找路。用户说要去哪,根据画面里的通道、门、走廊指方向。 工作方式: - 用户问问题或提要求,简短直接地回应。 - 用户没问问题、没提要求,保持安静,不要主动说话。 - 不要主动描述画面、不要主动评论、不要主动询问。 - 只说当前画面里确实看到的,不要凭印象补全。看不清就告诉用户看不清。 每次开口都要简短,一两句话说清楚,不要长篇大论。",
    #    "寻物助手": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面。任务:帮用户找东西。用户告诉你他要找什么后,留意画面:一旦画面里出现要找的东西,主动告诉用户它在哪、它的形状颜色、附近有什么、什么样子,一句话说完。用户没有指定要找什么之前,保持安静。",
    #    "文字识别": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面。任务:把画面里的文字念给用户听。用户让你念字后,留意画面:画面里能看清文字就念出来,从上到下、从左到右,数字、单位、符号都念。用户没有让你念字之前,保持安静。",
    #    "室内领路": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面。任务:帮用户在室内找路。用户告诉你想去哪里后,留意画面:根据画面里实际能看到的可通行方向,告诉用户该往哪边走,以及大概多远。用户没说要去哪里之前,保持安静。",
    #    "声音提醒": "你是智能眼镜助手。你能看到画面,也能听到各种声音,不只是说话。用户和你约定留意某种声音，就特别注意，当听到这种声音时立即提醒用。其余时候保持安静。每次开口一两句话。",
    #    "描述场景": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面。任务:帮用户描述他看到的场景。用户让你描述场景后,留意画面:用三到五句话讲清楚画面里的主要内容,包括场所、主要物品、人物动作。用户没让你描述之前,保持安静。",
    #},
    "presets": {
        #"通用助手":"你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面,也能听到环境里的声音,包括用户说话和其他各种声音。你只能通过眼镜的画面和声音来帮用户。每次用户提出要求,你先判断这件事能不能通过看画面或听声音来完成。能,就去做。不能,第一句话就告诉用户目前没有这个功能,不要假装完成。用户问什么就答什么,用你所有的能力去回应。默认状态是沉默。用户提问后应该立即回答，用户没有开口的时候,你保持安静,不主动描述画面、不主动评论。",
        #"通用助手":"你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面,也能听到环境里的声音,包括用户说话和其他各种声音。你只能通过眼镜的画面和声音来帮用户。每次用户提出要求,你先判断这件事能不能通过看画面或听声音来完成。能,就去做。不能,第一句话就告诉用户目前没有这个功能,不要假装完成。用户问什么就答什么,用你所有的能力去回应。默认状态是沉默。只有在用户向你提问或提出要求时才开口。用户没有开口的时候,你保持安静,不主动描述画面、不主动评论。",
        "通用助手": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面,听到用户的声音。\n\n你有四项能力:\n1. 找东西——用户说要找什么,在画面里看到就说出位置,没看到就说没看到。\n2. 念字——用户让你念,照原样念出画面里的文字,看不清就让用户拿近一点,不编造。\n3. 描述场景——用户让你描述,用三到五句话说清画面里有什么。\n4. 室内领路——用户说要去哪,根据画面里的通道、门、走廊指方向。\n\n用户问你能做什么时,把四项都说出来。\n\n默认状态是沉默。只有在用户向你提问或提出要求时才开口。用户没有开口的时候,你保持安静。\n\n执行任务时回应简短,一两句话说清。只说画面里确实看到的,看不清就说看不清。",
        "寻物助手": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面。\n\n你的任务是帮用户找东西。\n\n默认状态是沉默。等用户说出要找什么之后,你才开始留意画面。\n\n用户指定要找的东西之后,持续观察画面。当要找的东西出现在画面里时,主动告诉用户它在哪里、什么形状颜色、附近有什么。一句话说完。\n\n要找的东西没有出现在画面里时,保持安静,继续观察。",
        "文字识别": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面。\n\n你的任务是把画面里的文字念给用户听。\n\n默认状态是沉默。等用户让你念字之后,你才开始念。\n\n用户让你念字之后,念出画面里能看清的文字,从上到下、从左到右,数字、单位、符号都念。看不清就让用户拿近一点。不编造看不清的内容。\n\n用户没有让你念字之前,保持安静。",
        "室内领路": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面。\n\n你的任务是帮用户在室内找路。\n\n默认状态是沉默。等用户说出想去哪里之后,你才开始指路。\n\n用户说出目的地之后,根据画面里实际能看到的可通行方向,告诉用户往哪边走、大概多远。一两句话说清。只根据画面里确实看到的通道、门、走廊来判断,看不清就说看不清。\n\n用户没说要去哪里之前,保持安静。",
        "声音提醒": "你是智能眼镜助手。你能看到画面,也能听到环境里的各种声音,不只是人说话。\n\n你的任务是替用户留意他指定的声音。\n\n默认状态是沉默。等用户说出要留意什么声音之后,你才开始监听。\n\n用户指定之后,持续留意环境音。听到用户指定的那种声音时,立即提醒用户。一两句话说清。\n\n没有听到用户指定的声音时,保持安静。用户没有指定之前,也保持安静。",
        "描述场景": "你是智能眼镜助手。用户通过眼镜与你对话,你能看到用户视角的画面。\n\n你的任务是帮用户描述他看到的场景。\n\n默认状态是沉默。等用户让你描述之后,你才开始描述。\n\n用户让你描述之后,用三到五句话讲清画面里的主要内容:场所、主要物品、人物动作。只说画面里确实看到的,看不清就说看不清。\n\n用户没让你描述之前,保持安静。",
    },


    # 启动/停止顺序已按链路拆分，见上面的 "chains"。
    # （llama→worker→gateway 三级共用，第四级 demo / rokid 二选一。）

    "is_windows": os.name == "nt",
}

# —— 让眼镜下拉自动跟随 devices.json，避免两处手动同步 ——
def _load_devices_from_json(path="devices.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        names = [d["name"] for d in data.get("devices", []) if d.get("name")]
        if names:
            return names
    except FileNotFoundError:
        print(f"[panel] 未找到 {path}，沿用 CONFIG 里的 devices 列表")
    except Exception as e:
        print(f"[panel] 读取 {path} 失败({e})，沿用 CONFIG 里的 devices 列表")
    return None

_HERE = os.path.dirname(os.path.abspath(__file__))
# devices.json 优先找 panel.py 同目录（不依赖 cwd）：从仓库根用 shim 启动时 cwd 是根，
# 相对路径会找不到；用 __file__ 定位保证任何 cwd 下都能读到。cwd 显式配置时以它为准。
if CONFIG.get("cwd"):
    _dev_path = os.path.join(CONFIG["cwd"], "devices.json")
else:
    _dev_path = os.path.join(_HERE, "devices.json")
_names = _load_devices_from_json(_dev_path)
if _names:
    CONFIG["devices"] = _names
    print(f"[panel] 眼镜列表来自 devices.json: {_names}")


# ============================================================================
def _load_device_map(path="devices.json"):
    """读整份设备表：name -> {host, port, rotate}。

    原来只取 name（下拉用）。但 esp32_bridge 自己读 json（--device-config），
    而 esp32_runtime 不读，它只认 --esp32-host/--esp32-port/--rotate，
    所以由 panel 把这几项取出来填。rotate 尤其不能漏：摄像头物理侧装，
    不转正的话漏斗的方向检测会把"相机侧装"误判成"用户把盒子拿反了"。
    utf-8-sig：记事本/VSCode 存的 json 可能带 BOM，用 utf-8 读会炸在第一个字符。
    """
    out = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        for d in data.get("devices", []):
            n = d.get("name")
            if not n:
                continue
            out[n] = {
                "host": d.get("esp32_host", ""),
                "port": int(d.get("esp32_port", 80)),
                "rotate": int(d.get("rotate", 0)) % 360,
            }
    except FileNotFoundError:
        print(f"[panel] 未找到 {path}，带漏斗的链路(②③④)将无法自动填 IP")
    except Exception as e:
        print(f"[panel] 读取 {path} 失败({e})")
    return out


CONFIG["device_map"] = _load_device_map(_dev_path)
if CONFIG["device_map"]:
    print("[panel] 设备详情: " + ", ".join(
        f"{k}({v['host']} rot={v['rotate']})" for k, v in CONFIG["device_map"].items()))


def _load_runtime_local():
    """读 runtime.local.json —— 本机路径与私密配置，**不进仓库**。

    为什么要它：panel.py 里原本写死了五处本机绝对路径
    （conda 环境、MiniCPM-o-Demo 目录、llama-omni-server.exe、主 gguf、FunASR 模型），
    还有眼镜 WiFi 的明文密码。开源后每个人都要改源码才能跑，密码也会进 git 历史。
    改成从这个文件读，clone 下来只需 `cp runtime.example.json runtime.local.json` 再填。

    键名沿用仓库里已有的 runtime.example.json 风格。
    找不到文件时保留 CONFIG 里的默认值（也就是原来的写死值），行为不变。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    f = os.path.join(here, "runtime.local.json")
    if not os.path.isfile(f):
        print(f"[panel] 未找到 {f}")
        print("[panel]   请复制 runtime.example.json 为 runtime.local.json 并填写本机路径；")
        print("[panel]   否则将沿用 panel.py 里的默认值（多半不是你的路径）。")
        return {}
    try:
        with open(f, "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except Exception as e:
        print(f"[panel] 读取 runtime.local.json 失败({e})，沿用默认值")
        return {}

    def _expand(v):
        return os.path.expandvars(os.path.expanduser(v)) if isinstance(v, str) else v

    n = 0
    # ① 简单键 -> CONFIG 顶层
    for src_key, dst_key in (("conda_env", "conda_env"),
                             ("minicpm_demo_root", "minicpm_demo_dir")):
        v = _expand(cfg.get(src_key))
        if v:
            CONFIG[dst_key] = v
            n += 1
    # ② 需要替换到命令行数组里的
    def _sub(proc, old_pred, new_val):
        """把 procs[proc] 里满足 old_pred 的那一项换成 new_val。"""
        arr = CONFIG["procs"].get(proc)
        if not arr or not new_val:
            return 0
        for i, x in enumerate(arr):
            if isinstance(x, str) and old_pred(x):
                arr[i] = new_val
                return 1
        return 0

    n += _sub("llama", lambda x: x.lower().endswith("llama-omni-server.exe")
              or x.lower().endswith("llama-omni-server"),
              _expand(cfg.get("llama_server")))
    n += _sub("llama", lambda x: x.lower().endswith(".gguf"),
              _expand(cfg.get("llama_model")))
    n += _sub("harness", lambda x: "speech_paraformer" in x or "LocalASRmodel" in x,
              _expand(cfg.get("asr_model")))
    print(f"[panel] 已读入 runtime.local.json（生效 {n} 项）")
    return cfg


def _load_local_secrets(rt):
    """眼镜 WiFi（Rokid 链路传给 APK）。同样来自 runtime.local.json，
    写死在 panel.py 里就等于明文密码进公开仓库。"""
    out = {"glasses_ssid": "<YOUR_WIFI_SSID>", "glasses_psk": "<YOUR_WIFI_PASSWORD>"}
    g = (rt or {}).get("glasses") or {}
    for k in ("glasses_ssid", "glasses_psk"):
        v = g.get(k.replace("glasses_", "")) or (rt or {}).get(k)
        if v:
            out[k] = v
    return out


_RTLOCAL = _load_runtime_local()
CONFIG["local"] = _load_local_secrets(_RTLOCAL)


def _ensure_certs(base_dir):
    """确保 certs/cert.pem + key.pem 存在，没有就自签一对。

    gateway(8006) 和 harness(8021) 都用这一对（gateway.py 的默认值就是
    certs/cert.pem，缺了会直接报错退出），所以四条链路都需要它。
    自签证书**不绑机器**，里面没有硬件信息；客户端全是 CERT_NONE 不校验，
    只是为了让 wss 握手能过。所以本地生成一份即可，不必也不该提交进仓库。

    优先用 cryptography 库；没装就退回调 openssl；都不行就打出手动命令。
    """
    if not base_dir or not os.path.isdir(base_dir):
        return False
    d = os.path.join(base_dir, "certs")
    cert = os.path.join(d, "cert.pem")
    key = os.path.join(d, "key.pem")
    if os.path.isfile(cert) and os.path.isfile(key):
        return True
    os.makedirs(d, exist_ok=True)
    print(f"[panel] 未找到证书，正在生成自签证书 -> {d}")

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime as _dt

        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = _dt.datetime.now(_dt.timezone.utc)
        crt = (x509.CertificateBuilder()
               .subject_name(name).issuer_name(name)
               .public_key(k.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - _dt.timedelta(days=1))
               .not_valid_after(now + _dt.timedelta(days=3650))
               .add_extension(x509.SubjectAlternativeName([
                   x509.DNSName("localhost"),
                   x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1")),
               ]), critical=False)
               .sign(k, hashes.SHA256()))
        with open(key, "wb") as f:
            f.write(k.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
        with open(cert, "wb") as f:
            f.write(crt.public_bytes(serialization.Encoding.PEM))
        print("[panel] 证书已生成（cryptography，有效期 10 年）")
        return True
    except ImportError:
        pass
    except Exception as e:
        print(f"[panel] cryptography 生成失败({e})，改试 openssl")

    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048",
             "-keyout", key, "-out", cert, "-days", "3650",
             "-nodes", "-subj", "/CN=localhost"],
            check=True, capture_output=True, timeout=60)
        print("[panel] 证书已生成（openssl，有效期 10 年）")
        return True
    except Exception as e:
        print(f"[panel] !! 自动生成证书失败: {e}")
        print(f"[panel]    请手动执行（在 {base_dir} 下）：")
        print("[panel]    openssl req -x509 -newkey rsa:2048 "
              "-keyout certs/key.pem -out certs/cert.pem "
              "-days 3650 -nodes -subj \"/CN=localhost\"")
        return False


_ensure_certs(CONFIG.get("minicpm_demo_dir") or CONFIG.get("cwd"))


class ProcManager:
    """管理三个子进程：起停、状态轮询、日志收集、端口/就绪探测。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.procs = {}          # name -> Popen
        self.logs = {n: deque(maxlen=400) for n in cfg["procs"]}
        # —— 关键字就绪：命中即放行，不再 poll /health ——
        _wp = cfg.get("worker_ready_port", 22400)
        self._ready_pat = {
            "worker": re.compile(rf"Uvicorn running on http://[\d.]+:{_wp}"),
        }
        self._ready_events = {n: threading.Event() for n in cfg["procs"]}
        self.status = {n: "stopped" for n in cfg["procs"]}  # stopped/starting/running/crashed
        self.current_prompt = next(iter(cfg["presets"].values()))
        self.current_device = (cfg.get("devices") or ["默认"])[0]
        self.current_scene = cfg.get("default_scene", "严格判据")
        # 尾进程是"为哪套配置"起的。②③④ 共用 demo_funnel 这一个进程名，
        # 只看进程活着就跳过启动的话，切链路后新的漏斗参数永远不会生效 ——
        # UI 显示④，实际还在跑②。所以记指纹，变了就重启。
        self._tail_started_for = {}      # tail 进程名 -> 指纹
        # —— 当前链路：esp32 / rokid，决定第四级起哪个进程 ——
        self.current_chain = cfg.get("default_chain", "esp32")
        self._lock = threading.Lock()
        # —— 新增：串行化所有启停操作，杜绝多开 ——
        self._op_lock = threading.Lock()
        # —— 新增：急停信号，打断正在进行的启动/重试等待 ——
        self._cancel = threading.Event()
        # —— 新增：正被主动停止的进程集合，避免轮询把它误判成 crashed ——
        self._stopping = set()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ---- 新增：可被急停打断的睡眠 ----
    def _sleep(self, secs):
        """睡 secs 秒；正常睡满返回 True，被 _cancel 打断返回 False。"""
        return not self._cancel.wait(timeout=secs)

    # ---- 命令构造 ----
    def _wrap_conda(self, cmd):
        env = self.cfg["conda_env"]
        if env:
            return ["conda", "run", "-n", env] + cmd
        return [sys.executable if x == "python" else x for x in cmd]

    # ---- 链路 ----
    def chain(self):
        """当前链路的配置字典。"""
        return self.cfg["chains"][self.current_chain]

    def tail(self):
        """当前链路的第四级进程名（demo 或 rokid）。"""
        return self.chain()["tail"]

    def chain_procs(self):
        """当前链路涉及的进程（另一条链的尾巴不算）。"""
        return self.chain()["start_order"]

    def _build_cmd(self, name):
        cmd = list(self.cfg["procs"][name])
        # {here} → panel.py 同目录绝对路径（esp32_bridge.py / rokid / devices.json 定位，
        # 不依赖 cwd，从仓库根用 shim 启动也能找到）。
        import os as _os
        _here = _os.path.dirname(_os.path.abspath(__file__))
        cmd = [x.replace("{here}", _here) if isinstance(x, str) else x for x in cmd]
        # demo 要 prompt + device；rokid bridge 只要 prompt（它没有 device 概念）
        if name == "demo":
            subst = {"{prompt}": self.current_prompt, "{device}": self.current_device}
            cmd = [subst.get(x, x) for x in cmd]
        elif name == "harness":
            # 证书用绝对路径：harness 的 cwd 是 OpenGlass 仓库根，
            # 而证书由 _ensure_certs 生成在 minicpm_demo_dir/certs 下（gateway 共用）。
            _base = self.cfg.get("minicpm_demo_dir") or "."
            subst = {
                "{certfile}": os.path.join(_base, "certs", "cert.pem"),
                "{keyfile}": os.path.join(_base, "certs", "key.pem"),
            }
            cmd = [subst.get(x, x) for x in cmd]
        elif name == "demo_funnel":
            # esp32_runtime：prompt + 设备参数(IP/端口/旋转) + 漏斗档位
            subst = {"{prompt}": self.current_prompt}
            cmd = [subst.get(x, x) for x in cmd]
            cmd += self._device_args()
            cmd += self._funnel_extra_args()
        elif name == "rokid":
            _loc = self.cfg.get("local", {})
            subst = {
                "{prompt}": self.current_prompt,
                "{glasses_ssid}": _loc.get("glasses_ssid", ""),
                "{glasses_psk}": _loc.get("glasses_psk", ""),
            }
            cmd = [subst.get(x, x) for x in cmd]
            cmd += self._rokid_extra_args()   # 内联 ps1 的 --enable-funasr 分支
        return self._wrap_conda(cmd)

    # ---- 日志 ----
    def _log(self, name, line):
        ts = time.strftime("%H:%M:%S")
        self.logs[name].append(f"[{ts}] {line.rstrip()}")

    def _open_proc_logfile(self, name):
        """为进程 name 打开一个落盘日志文件（logs/<name>_<stamp>.log）。

        失败不影响运行，仅在 UI 日志里提示一句、返回 None。
        """
        log_dir = self.cfg.get("proc_log_dir", "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(log_dir, f"{name}_{stamp}.log")
            fh = open(path, "a", encoding="utf-8", errors="replace")
            self._log(name, f"日志落盘: {path}")
            return fh
        except Exception as e:
            self._log(name, f"!! 日志落盘失败: {e}")
            return None

    def _pump(self, name, proc):
        pat = self._ready_pat.get(name)
        fh = self._open_proc_logfile(name)
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                self._log(name, line)
                if fh is not None:
                    try:
                        fh.write(line)
                        fh.flush()
                    except Exception:
                        pass
                # 命中就绪关键字：立即置位，_wait_ready_or_die 会马上放行
                if pat and not self._ready_events[name].is_set() and pat.search(line):
                    self._ready_events[name].set()
                    self._log(name, "✅ 就绪关键字命中(端口已监听)，直接进入下一步")
        except Exception:
            pass
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass

    # ---- 就绪探测 ----
    def _port_open(self, port, host="127.0.0.1", timeout=0.5):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _wait_port(self, port, up=True, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._port_open(port) == up:
                return True
            time.sleep(0.4)
        return False

    # ---- Rokid：内联 run_rokid.ps1 / run_rokid_wifi.cmd 的全部职责 ----
    # 原来那两层脚本做的事，这里一步到位，不再多一层 shell：
    #   1) 建 sessions/ logs/ 目录
    #   2) 设 ROKID_V7_LOG_FILE / PYTHONUNBUFFERED / PYTHONIOENCODING
    #   3) 按需追加 --enable-funasr
    #   4) USB 模式：adb reverse tcp:18080 + 唤醒并拉起眼镜 APK
    #   5) WiFi 模式：不碰 adb，只打印本机 IPv4 供你核对 APK 里编进去的 IP
    def _rokid_env(self):
        """Rokid 专属环境变量（对应 ps1 里的 $env: 那几行）。"""
        env = {}
        log_dir = self.cfg.get("rokid_log_dir", "logs")
        save_root = self.cfg.get("rokid_save_root", "sessions")
        for d in (log_dir, save_root):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as e:
                self._log("rokid", f"!! 建目录失败 {d}: {e}")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        mode = self.cfg.get("rokid_mode", "wifi")
        env["ROKID_V7_LOG_FILE"] = os.path.join(log_dir, f"rokid_{mode}_{stamp}.log")
        env["PYTHONUNBUFFERED"] = "1"
        self._log("rokid", f"日志: {env['ROKID_V7_LOG_FILE']}")
        return env

    def _device_args(self):
        """从 devices.json 取当前眼镜的 IP / 端口 / 旋转角，填给 esp32_runtime。"""
        d = (self.cfg.get("device_map") or {}).get(self.current_device)
        if not d or not d.get("host"):
            self._log("demo_funnel",
                      f"!! devices.json 里找不到「{self.current_device}」的 esp32_host")
            return []
        args = ["--esp32-host", d["host"], "--esp32-port", str(d.get("port", 80))]
        if d.get("rotate"):
            args += ["--rotate", str(d["rotate"])]
        return args

    def _funnel_extra_args(self):
        """按链路的档位追加漏斗参数。

            0  语音控制    不加 → 走 no_funnel 分支，每轮取一帧直发
            2  质量筛选    --funnel --no-reject → 选 best 但永远放行
            3  完整防幻觉  --funnel --force-measure → 选 best + 拒绝 + 语音提示
        """
        lv = int(self.chain().get("funnel", 0))
        if lv <= 0:
            return []
        scene = self.cfg["scenes"].get(self.current_scene, "medicine")
        args = ["--funnel", "--scene", scene]
        if lv == 2:
            args += ["--no-reject"]
        else:
            args += ["--force-measure", "--reject-wav-dir", self._reject_wav_dir()]
        return args

    def _check_deps(self):
        """②③④ 启动前检查关键依赖，缺了就说清楚缺什么、怎么装。

        为什么值得单独查：paddleocr 装不上时，方向分类器会**静默回退**到
        早期的简易判据，表现是画面明明是正的却一直报"画面好像反了"、
        接着播报把帧间隔拉长又误报"晃动" —— 全程不报错，极难定位。
        实测踩过：panel 在没装好 paddle 的环境里启动，一上来就全是 orient_flipped。
        """
        lv = int(self.chain().get("funnel", 0))
        need = [("aiohttp", "aiohttp"), ("numpy", "numpy"),
                ("PIL", "Pillow"), ("sounddevice", "sounddevice")]
        if self.chain().get("tail") == "demo_funnel":
            need += [("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                     ("yaml", "PyYAML"), ("funasr", "funasr")]
        if lv >= 2:
            need += [("cv2", "opencv-python")]
        if lv >= 3 or lv == 2:
            need += [("paddle", "paddlepaddle"), ("paddleocr", "paddleocr")]
        import importlib.util as _iu
        missing = [pip for mod, pip in need if _iu.find_spec(mod) is None]
        if missing:
            tail = self.chain().get("tail") or "demo"
            self._log(tail, "!! 缺少依赖: " + ", ".join(missing))
            self._log(tail, "   请在**启动 panel 的那个 conda 环境**里安装：")
            self._log(tail, "     pip install " + " ".join(missing))
            self._log(tail, "   （完整清单见 extensions/requirements-phase-b.txt）")
            return False
        return True

    def _check_extensions(self):
        """②③④ 需要 OpenGlass 仓库内的 extensions/ 包完整。

        不需要复制到别处 —— _spawn 把 cwd 设成 OpenGlass 仓库根。
        缺文件时子进程会起来立刻死、日志里一行 No module named extensions，
        面板上只看到灯变红，所以提前拦住并说清楚。
        """
        if self.chain().get("tail") != "demo_funnel":
            return True
        f = os.path.join(self._repo_root(), "extensions", "assistive_harness",
                         "phase_b", "esp32_runtime.py")
        if not os.path.isfile(f):
            self._log("demo_funnel",
                      f"!! 未找到 {f}\n"
                      f"   ②③④ 需要仓库内的 extensions/ 包，请确认它没有被删除或移动。")
            return False
        return True

    def _repo_root(self):
        """OpenGlass 仓库根（panel.py 在 runtime/openglass_omni/ 下，往上三级）。"""
        return os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))

    def _reject_wav_dir(self):
        d = self.cfg.get("reject_wav_dir",
                         "extensions/assistive_harness/phase_b/assets/reject_wav")
        return d if os.path.isabs(d) else os.path.join(self._repo_root(), d)

    def _check_reject_wav(self):
        """档位④要播提示音，wav 必须事先用 gen_reject_wavs.py 生成好
        （且要在 duplex 没跑的时候生成）。这里只检查，不在面板里现场生成。"""
        if int(self.chain().get("funnel", 0)) < 3:
            return True
        full = self._reject_wav_dir()
        try:
            n = len([x for x in os.listdir(full) if x.lower().endswith(".wav")])
        except Exception:
            n = 0
        if n == 0:
            self._log("demo_funnel",
                      f"!! {full} 里没有 wav。档位④要播提示音，"
                      f"请先在 duplex 未运行时跑 gen_reject_wavs.py 生成。")
            return False
        return True

    def _rokid_extra_args(self):
        """按配置追加参数（对应 ps1 里的 $bridgeArgs += ...）。"""
        extra = []
        if self.cfg.get("rokid_enable_funasr"):
            extra += ["--enable-funasr", "--funasr-wait-ready-s", "0"]
        return extra

    def _local_ipv4s(self):
        out = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127.") and ip not in out:
                    out.append(ip)
        except Exception:
            pass
        return out

    def _rokid_pre_launch(self):
        """起 bridge 之前该做的事。USB 才用 adb；WiFi 只提示 IP。"""
        mode = self.cfg.get("rokid_mode", "wifi")
        port = int(self.cfg.get("rokid_port", 18080))
        if mode == "usb":
            adb = self.cfg.get("rokid_adb", "adb")
            self._log("rokid", f"USB 模式：adb reverse tcp:{port}")
            try:
                subprocess.run([adb, "reverse", "--remove", f"tcp:{port}"],
                               capture_output=True, timeout=10)
                r = subprocess.run([adb, "reverse", f"tcp:{port}", f"tcp:{port}"],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode != 0:
                    self._log("rokid", f"!! adb reverse 失败: {r.stderr.strip()}")
            except FileNotFoundError:
                self._log("rokid", f"!! 找不到 adb（{adb}），USB 模式无法转发端口")
            except Exception as e:
                self._log("rokid", f"!! adb reverse 异常: {e}")
        else:
            ips = self._local_ipv4s()
            self._log("rokid", f"WiFi 模式：本机 IPv4 = {', '.join(ips) if ips else '未探测到'}")
            self._log("rokid", "注意：眼镜 APK 里的 PC IP 是编译期常量，必须与上面某个 IP 一致")

    def _rokid_post_launch(self):
        """bridge 就绪后拉起眼镜 APK（仅 USB；WiFi 下眼镜自己会连进来）。
        对应 ps1 里的 Start-RokidLaunchJob。"""
        if self.cfg.get("rokid_mode", "wifi") != "usb":
            return
        adb = self.cfg.get("rokid_adb", "adb")
        pkg = self.cfg.get("rokid_package", "")
        act = self.cfg.get("rokid_activity", "")

        def run():
            try:
                subprocess.run([adb, "shell", "am", "force-stop", pkg],
                               capture_output=True, timeout=10)
                subprocess.run([adb, "shell", "input", "keyevent", "KEYCODE_WAKEUP"],
                               capture_output=True, timeout=10)
                subprocess.run([adb, "shell", "wm", "dismiss-keyguard"],
                               capture_output=True, timeout=10)
                time.sleep(1.0)
                self._log("rokid", "拉起眼镜采集 APK…")
                r = subprocess.run([adb, "shell", "am", "start", "-S", "-W", "-n", act],
                                   capture_output=True, text=True, timeout=20)
                if r.returncode != 0:
                    subprocess.run([adb, "shell", "monkey", "-p", pkg, "-c",
                                    "android.intent.category.LAUNCHER", "1"],
                                   capture_output=True, timeout=20)
                self._log("rokid", "APK 已拉起，等待它连入 18080")
            except FileNotFoundError:
                self._log("rokid", f"!! 找不到 adb（{adb}），无法自动拉起 APK")
            except Exception as e:
                self._log("rokid", f"!! 拉起 APK 失败: {e}")

        threading.Thread(target=run, daemon=True).start()

    # ---- 起停单个进程 ----
    def _spawn(self, name):
        self._ready_events[name].clear()  # ← 新增:重试/重启前复位就绪标记
        extra_env = {}
        if name == "rokid":
            self._rokid_pre_launch()          # 内联 ps1：建目录/adb reverse/提示 IP
            extra_env = self._rokid_env()
        cmd = self._build_cmd(name)
        self._log(name, f"$ {' '.join(cmd)}")
        creationflags = 0
        if self.cfg["is_windows"]:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        # worker / gateway 是上游 MiniCPM-o-Demo 的文件（worker.py / gateway.py），
        # 必须在那个目录下启动才能找到。llama / demo / rokid 用的是绝对路径，不依赖 cwd。
        # harness / demo_funnel 用 `python -m extensions...` 启动，
        # **cwd 必须是 OpenGlass 仓库根** —— extensions/ 就在它下面。
        #   ★ 不要用 PYTHONPATH 代替：`-m` 时 sys.path[0] 是 cwd，cwd 优先于
        #     PYTHONPATH。如果 cwd 设成 MiniCPM-o-Demo 而那边**也有**一份
        #     extensions/，OpenGlass 这份会被完全遮蔽（改了不生效）；
        #     更糟的是 PYTHONPATH 把 OpenGlass 根塞进 sys.path，实测导致
        #     paddle 导入失败（partially initialized module 'paddle' ...
        #     circular import）→ 方向分类器加载失败 → 回退到错误的早期 CV 判据
        #     → 第一轮就误报 orient_flipped，然后播报拉长帧间隔又误报 severe_shake。
        if name in ("harness", "demo_funnel"):
            _cwd = self._repo_root()
        elif name in ("worker", "gateway"):
            _cwd = self.cfg.get("minicpm_demo_dir") or self.cfg.get("cwd") or None
            if not _cwd:
                self._log(name, "!! 未配置 minicpm_demo_dir（MiniCPM-o-Demo 目录），"
                                "worker.py/gateway.py 将无法定位，请在 CONFIG 里设置")
            elif not os.path.isdir(_cwd):
                self._log(name, f"!! minicpm_demo_dir 不是有效目录: {_cwd}")
                _cwd = None
        else:
            _cwd = self.cfg.get("cwd") or None
            if _cwd and not os.path.isdir(_cwd):
                self._log(name, f"!! cwd 无效，忽略并用当前目录: {_cwd}")
                _cwd = None
        _env = dict(os.environ)
        _env["PYTHONIOENCODING"] = "utf-8"
        _env["PYTHONUTF8"] = "1"
        _env.update(extra_env)   # rokid: ROKID_V7_LOG_FILE / PYTHONUNBUFFERED
        proc = subprocess.Popen(
            cmd,
            cwd=_cwd,
            env=_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1024 * 1024,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        self.procs[name] = proc
        threading.Thread(target=self._pump, args=(name, proc), daemon=True).start()
        return proc

    def _kill(self, name, graceful=False, grace_timeout=20):
        self._forget_tail_fp(name)   # 进程要没了，指纹一并作废
        proc = self.procs.get(name)
        if not proc or proc.poll() is not None:
            self.status[name] = "stopped"
            return
        self._stopping.add(name)   # 标记：这是主动停止，别被轮询判成 crashed
        try:
            if self.cfg["is_windows"]:
                if graceful:
                    self._log(name, f"发送 CTRL_BREAK，等待 demo 落盘(最多 {grace_timeout}s)…")
                    ok = True
                    try:
                        import signal as _sig
                        proc.send_signal(_sig.CTRL_BREAK_EVENT)
                    except Exception as e:
                        self._log(name, f"CTRL_BREAK 失败({e})，改强杀")
                        ok = False
                    if ok:
                        try:
                            proc.wait(timeout=grace_timeout)
                            self._log(name, "demo 已优雅退出（落盘完成）")
                        except subprocess.TimeoutExpired:
                            self._log(name, f"{grace_timeout}s 未退出，强杀兜底")
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                           capture_output=True)
                    else:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                       capture_output=True)
                else:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True)
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=grace_timeout if graceful else 5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as e:
            self._log(name, f"kill error: {e}")
        finally:
            self._stopping.discard(name)
        self.status[name] = "stopped"

    def _force_kill(self, proc):
        try:
            if self.cfg["is_windows"]:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                proc.kill()
        except Exception:
            pass

    def _kill_by_port(self, port):
        if not self.cfg["is_windows"]:
            return
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                  capture_output=True, text=True).stdout
            pids = set()
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        pids.add(parts[-1])
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
                self._log("gateway", f"按端口 {port} 强杀 PID {pid}")
        except Exception as e:
            self._log("gateway", f"kill_by_port err: {e}")

    # ---- 对外动作 ----
    def _alive(self, name):
        p = self.procs.get(name)
        return p is not None and p.poll() is None

    def _http_get_json(self, url, timeout=2.0):
        import urllib.request, json as _json
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return _json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def _http_ok(self, url, timeout=2.0):
        """GET 返回 HTTP 200 即 True（llama-omni-server /health 用，不解析 body）。"""
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return 200 <= getattr(r, "status", r.getcode()) < 300
        except Exception:
            return False

    def _wait_ready_or_die(self, name, timeout):
        """等就绪，绝不永久卡住；被急停(_cancel)时立即返回 False。"""
        t_start = time.time()
        # llama backend 模型加载慢，用更长的专属超时
        if name == "llama":
            timeout = float(self.cfg.get("llama_ready_timeout_s", 300.0))
        deadline = t_start + timeout
        wport = self.cfg.get("worker_health_port")
        health_probe_s = float(self.cfg.get("health_probe_s", 15.0))
        stable_s = float(self.cfg.get("stable_alive_s", 8.0))
        last_log = 0.0
        health_ever_reachable = False
        llama_url = self.cfg.get("llama_health_url", "http://127.0.0.1:22500/health")
        while time.time() < deadline:
            if self._cancel.is_set():          # 急停：立刻放弃就绪等待
                self._log(name, "就绪等待被急停打断")
                return False
            p = self.procs.get(name)
            if p is None or p.poll() is not None:
                return False
            # 关键字就绪优先：一旦泵线程命中(如 worker 的 22400)立刻放行
            if self._ready_events.get(name) is not None and self._ready_events[name].is_set():
                self._log(name, "就绪(关键字命中)，放行")
                return True
            elapsed = time.time() - t_start
            if name == "harness":
                # 8021 是自签 wss，不能用 HTTP health，只能探 TCP 端口。
                # 而且端口开了之后 /ws/control 还要一小会儿才绑好，
                # 不等的话 esp32_runtime 连过去会失败。
                if self._port_open(8021):
                    self._log("harness", "8021 端口已开，再等 2s 让 /ws/control 绑好")
                    time.sleep(2.0)
                    self._log("harness", "harness 就绪，放行")
                    return True
                _now = time.time()
                if _now - last_log > 5:
                    last_log = _now
                    self._log("harness", f"等待 8021 (FunASR 模型加载中) {elapsed:.0f}s")
                time.sleep(1.0)
                continue
            if name == "llama":
                # llama-omni-server：轮询 /health 返回 200 才放行（= 你手动的 curl .../health）
                if self._http_ok(llama_url):
                    self._log("llama", f"/health 200，backend 就绪，放行")
                    return True
                now = time.time()
                if now - last_log > 5:
                    self._log("llama", f"等待 backend 加载模型… ({elapsed:.0f}s) {llama_url}")
                    last_log = now
            elif name == "worker":
                if wport:
                    h = self._http_get_json(f"http://localhost:{wport}/health")
                    if h is not None:
                        health_ever_reachable = True
                        if h.get("status") == "healthy":
                            self._log("worker", "/health = healthy")
                            return True
                    now = time.time()
                    if now - last_log > 3:
                        st = h.get("status") if h else "无响应"
                        self._log("worker", f"等待就绪… /health status={st}")
                        last_log = now
                    if (not health_ever_reachable) and elapsed >= health_probe_s and elapsed >= stable_s:
                        self._log("worker", f"!! {health_probe_s:.0f}s 内 /health 无响应"
                                            f"(检查 worker_health_port，当前={wport})，"
                                            f"已按进程稳定存活放行")
                        return True
                else:
                    if elapsed >= stable_s:
                        return True
            elif name == "gateway":
                if self._port_open(self.cfg["gateway_port"]):
                    return True
                if elapsed >= max(health_probe_s, stable_s):
                    self._log("gateway", f"!! {self.cfg['gateway_port']} 未探到，已按稳定存活放行")
                    return True
            elif name == "rokid":
                # bridge 起来了 = 18080 /health 返回 200（真探测，不是 sleep）。
                # 就绪只代表"PC 侧在等眼镜连"，眼镜连没连要看 /health 里的 clients。
                if self._http_ok(self.cfg.get("rokid_health_url",
                                              "http://127.0.0.1:18080/health")):
                    self._log("rokid", "/health 200，bridge 就绪，等待眼镜 APK 连入")
                    self._rokid_post_launch()   # USB：此时才拉起 APK（ps1 里也是等 health 再拉）
                    return True
                now = time.time()
                if now - last_log > 3:
                    self._log("rokid", f"等待 bridge 监听 {self.cfg.get('rokid_port', 18080)}… ({elapsed:.0f}s)")
                    last_log = now
            else:  # demo
                if elapsed >= 3:
                    return True
            if not self._sleep(0.4):            # 可被急停打断的间隔
                return False
        p = self.procs.get(name)
        if p is not None and p.poll() is None and not self._cancel.is_set():
            self._log(name, "就绪探测超时，但进程存活，放行")
            return True
        return False

    def _start_one(self, name):
        """启动单个进程：spawn → 等就绪 → 早退则自动重试。被急停立即返回 False。"""
        retries = int(self.cfg.get("start_retry", 3))
        ready_timeout = float(self.cfg.get("ready_timeout_s", 90.0))
        gap = float(self.cfg.get("start_retry_gap_s", 4.0))
        for attempt in range(1, retries + 2):
            if self._cancel.is_set():
                return False
            self.status[name] = "starting"
            self._spawn(name)
            if self._wait_ready_or_die(name, ready_timeout):
                self.status[name] = "running"
                if name == self.tail():
                    # 记下这次是按哪套配置起的，下次切链路时用来判断要不要重启
                    self._tail_started_for[name] = self._tail_fingerprint()
                return True
            # 未就绪/退出/被急停：先把这次 spawn 的进程杀干净，绝不留残余
            p = self.procs.get(name)
            code = p.poll() if p is not None else None
            if p:
                self._force_kill(p)
            if self._cancel.is_set():           # 急停：不再重试
                self.status[name] = "stopped"
                return False
            if attempt <= retries:
                wait_s = gap * attempt
                self._log(name, f"!! 未就绪/退出(code={code})，{wait_s:.0f}s 后自动重试 "
                                f"({attempt}/{retries})，通常是下游 llama-server 尚未就绪")
                if not self._sleep(wait_s):     # 重试间隔可被急停打断
                    self.status[name] = "stopped"
                    return False
            else:
                self.status[name] = "crashed"
                self._log(name, f"!! 启动失败，已重试 {retries} 次仍未就绪(code={code})")
                return False
        return False

    def _forget_tail_fp(self, name):
        """进程不在了就清掉指纹，否则下次会误判成"配置没变、跳过启动"。"""
        self._tail_started_for.pop(name, None)

    def _other_tails(self):
        """除当前链路外，其它链路的第四级进程名。"""
        cur = self.tail()
        return [c["tail"] for k, c in self.cfg["chains"].items() if c["tail"] != cur]

    def _tail_fingerprint(self):
        """当前链路下，尾进程真正依赖的那几个量。

        任何一项变了，已在跑的尾进程就是按旧配置起的，必须重启：
          chain   决定漏斗档位（--funnel / --no-reject / --force-measure）
          scene   决定 --scene（严格/日常判据）
          device  决定 --esp32-host / --esp32-port / --rotate
          prompt  决定 --prompt
        """
        c = self.chain()
        return (self.current_chain, c.get("funnel", 0), self.current_scene,
                self.current_device, self.current_prompt)

    def _do_start_all(self):
        # ②③④ 的两个前置检查：extensions 复制了没、档位④的 wav 有没有。
        # 提前拦住比跑起来才发现好 —— 后者时 duplex 已在跑，没法当场补。
        if (not self._check_extensions() or not self._check_deps()
                or not self._check_reject_wav()):
            return
        """按序补齐：只起没在跑的，已在跑的跳过；被急停立即停下。"""
        # 切链后若另一条链的尾巴还活着，先杀掉——两个客户端同时占一个 gateway
        # 会互相抢 session，必须互斥。
        for other in self._other_tails():
            if self._alive(other):
                self._log(other, "!! 另一条链路的客户端仍在运行，先停掉（同一 gateway 不能双占）")
                self._kill(other, graceful=True, grace_timeout=120)

        for name in self.chain_procs():
            if self._cancel.is_set():
                return
            if self._alive(name):
                # 尾进程还要比配置指纹：同一个 demo_funnel 进程，
                # ②③④ 传的参数完全不同，光看"活着"会漏掉重启。
                if name == self.tail():
                    want = self._tail_fingerprint()
                    got = self._tail_started_for.get(name)
                    if got is not None and got != want:
                        self._log(name, "配置已变（链路/判据/眼镜/Prompt），重启该进程")
                        self._kill(name, graceful=True, grace_timeout=120)
                    else:
                        self.status[name] = "running"
                        self._log(name, "已在运行，跳过启动")
                        continue
                else:
                    self.status[name] = "running"
                    self._log(name, "已在运行，跳过启动")
                    continue
            if not self._start_one(name):
                if self._cancel.is_set():
                    self._log(name, "!! 启动被急停中止")
                else:
                    self._log(name, "!! 未能就绪，中止后续启动")
                return

    def start_all(self):
        """一键启动：串行独占，重复点击直接忽略（杜绝多开）。"""
        def run():
            if not self._op_lock.acquire(blocking=False):
                return  # 已有启停操作在进行，忽略这次点击
            try:
                self._cancel.clear()
                self._do_start_all()
            finally:
                self._op_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def _stop_all_sync(self):
        # 先停另一条链可能残留的尾巴，再按当前链路逆序停。
        # 尾巴(demo/rokid)都要 graceful：它们都要落盘 session。
        tails = {c["tail"] for c in self.cfg["chains"].values()}
        for other in self._other_tails():
            if self._alive(other):
                self._kill(other, graceful=True)
                time.sleep(0.3)
        for name in self.chain()["stop_order"]:
            self._kill(name, graceful=(name in tails))
            time.sleep(0.3)
        for name in self.cfg["procs"]:
            proc = self.procs.get(name)
            if proc is not None and proc.poll() is not None:
                continue
            if proc is not None:
                self._log(name, "!! 仍在运行，强杀兜底")
                self._force_kill(proc)
        if self._wait_port(self.cfg["gateway_port"], up=False, timeout=8):
            self._log("gateway", f"端口 {self.cfg['gateway_port']} 已释放，全部已停止")
        else:
            self._log("gateway", f"!! {self.cfg['gateway_port']} 仍被占用，尝试按端口强杀")
            self._kill_by_port(self.cfg["gateway_port"])
        for name in self.cfg["procs"]:
            self.status[name] = "stopped"

    def stop_all(self):
        """全部停止：等同三个 Ctrl+C。先发急停打断任何正在进行的启动，再彻底杀干净。"""
        def run():
            self._cancel.set()          # 立刻让正在跑的启动线程退出
            with self._op_lock:         # 等它退出并释放锁后再动手
                self._cancel.clear()
                self._stop_all_sync()
        threading.Thread(target=run, daemon=True).start()

    def restart_all(self):
        """全部重启：先急停+全停，再全部重开。"""
        def run():
            self._cancel.set()
            with self._op_lock:
                self._cancel.clear()
                self._stop_all_sync()
                if not self._sleep(1.5):
                    return
                self._do_start_all()
        threading.Thread(target=run, daemon=True).start()

    def restart_demo(self, prompt, device=None):
        """切 prompt = 用新 prompt 重启当前链路的第四级（demo 或 rokid bridge）。
        worker/gateway 不动。忙则忽略防多开。"""
        def run():
            if not self._op_lock.acquire(blocking=False):
                return
            try:
                if prompt:
                    self.current_prompt = prompt
                if device:
                    self.current_device = device
                t = self.tail()                      # ← demo 或 rokid
                self._kill(t, graceful=True, grace_timeout=120)
                if not self._sleep(1.0):
                    return
                self.status[t] = "starting"
                self._spawn(t)
                if not self._sleep(1.0):
                    return
                self.status[t] = "running" if self.procs[t].poll() is None else "crashed"
            finally:
                self._op_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def stop_demo(self):
        """停止：只停当前链路的第四级（优雅落盘），保住 worker/gateway。忙则忽略。"""
        def run():
            if not self._op_lock.acquire(blocking=False):
                return
            try:
                self._kill(self.tail(), graceful=True, grace_timeout=120)
            finally:
                self._op_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def set_chain(self, chain):
        """切链路。若已有进程在跑，停掉另一条链的尾巴由下次 start_all 处理。"""
        if chain in self.cfg["chains"]:
            self.current_chain = chain
        return self.current_chain

    def shutdown(self):
        """关窗时同步清理，阻塞直到全停（比原来的异步 stop_all 更可靠）。"""
        self._cancel.set()
        with self._op_lock:
            self._stop_all_sync()

    # ---- 崩溃检测轮询 ----
    def _poll_loop(self):
        while True:
            for name, proc in list(self.procs.items()):
                if (proc and proc.poll() is not None
                        and self.status[name] == "running"
                        and name not in self._stopping):   # 主动停止的不算崩溃
                    self.status[name] = "crashed"
                    self._log(name, f"!! 进程退出 code={proc.returncode}")
            time.sleep(1.0)

    def snapshot(self):
        return {
            "status": dict(self.status),
            "logs": {n: list(self.logs[n]) for n in self.logs},
            "current_prompt": self.current_prompt,
            "chain": self.current_chain,
            "procs": self.chain_procs(),   # 当前链路的四个进程，UI 只点这四盏灯
        }


class Api:
    """JS <-> Python 桥。前端调用这些方法。"""

    def __init__(self, mgr, cfg):
        self.mgr = mgr
        self.cfg = cfg

    def get_config(self):
        # 每条链路把自己的 fpv 地址/是否要选眼镜一并给前端，前端切链即切界面
        chains = {}
        for k, c in self.cfg["chains"].items():
            chains[k] = {
                "label": c["label"],
                "need_device": c["need_device"],
                "fpv_url": self.cfg.get(c["fpv_key"], ""),
                "procs": c["start_order"],
                # 只有开了漏斗的链路(③④)才需要选判据档位；①②选了也不起作用
                "need_scene": int(c.get("funnel", 0)) > 0,
            }
        return {
            "presets": self.cfg["presets"],
            "fpv_url": self.cfg["fpv_url"],
            "devices": self.cfg.get("devices", []),
            "chains": chains,
            "scenes": list(self.cfg.get("scenes", {}).keys()),
            "default_scene": self.cfg.get("default_scene", ""),
            "proc_labels": self.cfg.get("proc_labels", {}),
            "default_chain": self.cfg.get("default_chain", "esp32"),
        }

    def set_chain(self, chain):
        return self.mgr.set_chain(chain)

    def set_scene(self, scene):
        if scene:
            self.mgr.current_scene = scene
        return "ok"

    def set_device(self, device):
        if device:
            self.mgr.current_device = device
        return "ok"

    def start_all(self, prompt=None, device=None, chain=None, scene=None):
        # prompt 来自前端文本框（用户选的 preset 或编辑后的内容）。在起进程前更新
        # current_prompt，否则一键启动只会用初始的第一个 preset。
        if prompt:
            self.mgr.current_prompt = prompt
        if chain:
            self.mgr.set_chain(chain)
        if device:
            self.mgr.current_device = device
        if scene:
            self.mgr.current_scene = scene
        self.mgr.start_all(); return "ok"

    def stop_all(self):
        self.mgr.stop_all(); return "ok"

    def stop_demo(self):
        self.mgr.stop_demo(); return "ok"

    def restart_all(self):
        self.mgr.restart_all(); return "ok"

    def restart_demo(self, prompt, device=None):
        self.mgr.restart_demo(prompt, device); return "ok"

    def poll(self):
        return self.mgr.snapshot()


# HTML 从同目录的 panel.html 读取（UI 单独成文件，与 live.html 一致，便于维护）。
from pathlib import Path as _Path
_PANEL_HTML_PATH = _Path(__file__).with_name("panel.html")
try:
    HTML = _PANEL_HTML_PATH.read_text(encoding="utf-8")
except FileNotFoundError:
    raise SystemExit(f"[panel] 缺少 UI 文件: {_PANEL_HTML_PATH}（需与 panel 同目录）")


def main():
    mgr = ProcManager(CONFIG)
    api = Api(mgr, CONFIG)
    window = webview.create_window(
        "SmartGlasses 演示控制台 (ESP32 + Rokid)",
        html=HTML,
        js_api=api,
        width=1180,
        height=780,
        min_size=(900, 640),
    )
    webview.start()
    # 关窗时兜底：确保所有子进程被清掉
    mgr.shutdown()
    #mgr.stop_all()
    #time.sleep(1)


if __name__ == "__main__":
    main()
