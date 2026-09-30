# Rokid 采集 App

此目录保存面板使用的 OpenGlass Rokid 采集 App 0.1.3：发送 JPEG 与 PCM 音频，接受面板传入的 `pc_base_url`。它不是眼镜系统固件。

## 文件布局

- `OpenGlassRokidSensor/`：唯一 Android 源码工程，包含 Gradle Wrapper。
- `scripts/build_install.ps1`：构建，可选安装；不启动模型或旧 bridge。
- `build_install.cmd`：同一脚本的 Windows 便捷入口。

安装完成后，使用仓库根的 `glasses_panel.py` 启动模型与采集服务。

## 手机设置

手机下载安装乐奇官方 App Rokid AI，连接眼镜后在设置中开启开发者模式和眼镜 ADB 调试授权。

## 首次准备

安装 JDK 17（或与 Gradle 8.7 / Android 插件 8.5.2 兼容的 JDK）、Android SDK（API 34、构建工具和 Platform Tools）。设置 `JAVA_HOME` 和 `ANDROID_HOME`，或通过下方 `-Sdk` 指定 SDK。首次构建需要下载 Gradle/Maven 依赖。

推荐在 OpenGlass 根目录运行整理后的脚本。**先只编译**：

```powershell
.\rokid_app\build_install.cmd -Sdk "<ANDROID_SDK_PATH>"
```

**编译并安装**：连接 USB、确认授权，用 `adb devices -l` 取得设备序列号，再运行：

```powershell
.\rokid_app\build_install.cmd -Sdk "<ANDROID_SDK_PATH>" -Install -Serial "<USB_SERIAL>"
```

需要指定 ADB 时增加 `-Adb "<ADB_EXE_PATH>"`。也可直接调用 `scripts/build_install.ps1`，参数相同。脚本确认编译成功后才安装；设备不唯一时要求指定序列号。USB 和无线 ADB 同时在线会显示两项。

APK 输出：`OpenGlassRokidSensor/app/build/outputs/apk/debug/app-debug.apk`。这是生成物，不提交 Git。签名不匹配时脚本会停止，**不会自动卸载**。只有确认可以清除旧采集 App 数据和权限后，才由设备所有者处理旧版本。安装后允许相机和麦克风权限。

### 等价手动命令

已配置好 SDK/JDK 时，从 Android 工程目录执行：

```powershell
.\gradlew.bat :app:assembleDebug
# 上一步成功后再安装
adb -s <USB_SERIAL> install -r .\app\build\outputs\apk\debug\app-debug.apk
```

## 地址配置与日常启动

**编译时不填写电脑 IP。** 工程默认地址是眼镜回环地址，仅用于已有 ADB reverse 的情况；面板每次启动通过 `pc_base_url` 覆盖该地址，采集 App 会保存它。

安装完成后按[中文启动指南](../runtime/openglass_omni/STARTUP_zh.md)设置 `runtime.local.json`：填写后端路径、`rokid_adb`、`rokid_mode: "wifi"` 和 `rokid_pc_url: "http://<PC_LAN_IP>:18080"`。电脑 IP 从 `ipconfig` 中选择与眼镜互通的网卡地址，不是眼镜 IP。

先在设备/手机端保存 Wi-Fi 网络。首次保持 USB 连接，面板建立无线 ADB，确认音视频传入后拔线；以后从面板启动即可。换电脑 IP 只改本地 JSON，无需再编译 APK。眼镜断电可能重置 Wi-Fi/无线调试，需要 USB 恢复，但不会卸载 App。

详细录制与 Harness 关键词/提示词配置见启动指南。眼镜采集 App 不承担 Harness、ASR 或模型推理。
