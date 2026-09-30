# Rokid sensor app

[中文说明](README_zh.md)

This directory contains the adapted OpenGlass Rokid sensor app 0.1.3 and a single Android project. It captures JPEG/PCM and accepts a dynamic `pc_base_url`. Use the repository-root `glasses_panel.py` for daily startup.

## Companion setup

Install the official Rokid AI phone app and connect the glasses. Enable developer mode and authorize glasses ADB debugging in settings.

## Build and install

Prepare JDK 17 (or a JDK compatible with Gradle 8.7 / AGP 8.5.2), Android SDK API 34, build tools and Platform Tools. Set JAVA_HOME and ANDROID_HOME or pass `-Sdk`. Initial builds download Gradle/Maven dependencies. From the OpenGlass root:

```powershell
# Build only
.\rokid_app\build_install.cmd -Sdk "<ANDROID_SDK_PATH>"
# Build and install after connecting/authorizing USB
.\rokid_app\build_install.cmd -Sdk "<ANDROID_SDK_PATH>" -Install -Serial "<USB_SERIAL>"
```

Use `adb devices -l` to obtain the serial. Optional `-Adb` selects an executable. The CMD delegates to `scripts/build_install.ps1`, which can also be called directly with the same options. A failed build never triggers installation. Multiple online entries require a serial; USB and wireless connections appear separately. Signature mismatches stop installation; the script never uninstalls an existing app. Allow camera/microphone permissions on the device.

Generated APK: `OpenGlassRokidSensor/app/build/outputs/apk/debug/app-debug.apk` (ignored by Git). Equivalent manual commands from the Android project root, with SDK/JDK configured:

```powershell
.\gradlew.bat :app:assembleDebug
# Only after a successful build:
adb -s <USB_SERIAL> install -r .\app\build\outputs\apk\debug\app-debug.apk
```

## Configure and run

No PC IP is needed at build time. Loopback defaults require an ADB reverse tunnel. The panel supplies and the app persists `pc_base_url` at launch. Configure the shared backend and `rokid_adb`, `rokid_mode`, `rokid_pc_url` in ignored `runtime/openglass_omni/runtime.local.json`; use the PC LAN IPv4 reachable from the glasses. Then run `python glasses_panel.py`.

Save a working Wi-Fi network on the glasses first. Keep USB attached until the panel establishes wireless ADB and sensor input is confirmed. Later PC/panel restarts can reuse wireless ADB. Glasses power loss may require USB recovery, but does not uninstall the app. A changed PC IP requires a local JSON edit and panel restart, not an APK rebuild.

See [runtime setup](../runtime/openglass_omni/STARTUP_en.md) for recording and Harness prompts. 
