/*
 * CameraWebServer_PDM_Audio — Phase A: Concurrent Mode
 *
 * Camera + PDM microphone run simultaneously (no time-division multiplexing).
 *
 * Core design: concurrent operation
 *   - Camera uses DVP interface, PDM uses I2S_NUM_0; both are hardware-independent
 *   - Both camera and PDM are initialized in setup()
 *   - /ws_audio: streaming starts on connect, stops on disconnect; no audio_begin/audio_end needed
 *   - /audio_begin /audio_end kept as no-ops (backward compatibility)
 *
 * Hardware: Seeed Studio XIAO ESP32S3 + OV5640
 * PDM microphone pins (per schematic XIAO_ESP32S3_V1.3_SCH_260115):
 *   - IO42 = PDM_CLK (clock)
 *   - IO41 = PDM_DATA (data)
 *
 * WS audio format: 16kHz, mono, PCM16 LE, 20ms/frame (640 bytes)
 * Audio processing: DC offset removal + software gain (done on ESP32)
 *
 * Endpoints:
 *   GET  /           - Web UI (Camera)
 *   GET  /capture    - JPEG capture (always available)
 *   GET  :81/stream  - MJPEG video stream (always available)
 *   WS   /ws_audio   - PCM16 audio stream (stream on connect)
 *   GET  /status     - System status JSON
 *   GET  /audio_begin, /audio_end - Backward compatibility (no-op)
 *
 * Arduino IDE settings:
 *   Board: XIAO_ESP32S3 | PSRAM: OPI PSRAM
 *   Partition: Huge APP (3MB No OTA)
 */

#include "esp_camera.h"
#include <WiFi.h>
#include "esp_wifi.h"  
#include "ESP32_OV5640_AF.h"   // ★ 新增：OV5640 自动变焦(VCM)库。AF 只走 SCCB 控制镜头马达，
                               //    不改变图像输出格式/传输协议，capture/stream/PC 端一律不用动。

// ★ 新增：TCP 图像通道（与 HTTP /capture 并存，运行期由 PC 端选择走哪条）。
//   设计：独立 FreeRTOS task + 独立端口 5000，请求-响应式（PC 发 1 字节 → 回一帧 HD JPEG）。
//   关键：task 绑 Core 0（靠 WiFi 栈）、优先级 4（低于 audio_sender 的 6），
//   让音频发送永远优先，避免传图饿死音频——这是 TCP 方案相对 HTTP 共用单线程 httpd 的真正优势。
WiFiServer g_image_tcp_server(5000);
// ===================
// Select camera model
// ===================
#define CAMERA_MODEL_XIAO_ESP32S3 // Has PSRAM
#include "camera_pins.h"

// ★ 新增：全局 AF 对象。setup() 里一次性初始化为连续自动对焦后即放手，
//    loop() 不再轮询/触发它，避免与抓图争用 SCCB(I2C)总线。
OV5640 ov5640 = OV5640();
// 对焦模式: false=由 PC 通过 /control?var=af 按需触发(默认);
//           true=固件自动定时对焦(仅调试/对比用)
static bool g_auto_af = false;
// ===========================
// Enter your WiFi credentials
// ===========================
const char* ssid     = "YOUR_WIFI_NAME";
const char* password = "YOUR_WIFI_PASSWORD";

// Forward declarations
void startCameraServer();
void setupLedFlash(int pin);
extern bool pdm_mic_init();  // defined in app_httpd.cpp

// ========================================
// Camera config (global, for reinit)
// ========================================
static camera_config_t cam_config;

static void setup_cam_config() {
  cam_config.ledc_channel = LEDC_CHANNEL_0;
  cam_config.ledc_timer = LEDC_TIMER_0;
  cam_config.pin_d0 = Y2_GPIO_NUM;
  cam_config.pin_d1 = Y3_GPIO_NUM;
  cam_config.pin_d2 = Y4_GPIO_NUM;
  cam_config.pin_d3 = Y5_GPIO_NUM;
  cam_config.pin_d4 = Y6_GPIO_NUM;
  cam_config.pin_d5 = Y7_GPIO_NUM;
  cam_config.pin_d6 = Y8_GPIO_NUM;
  cam_config.pin_d7 = Y9_GPIO_NUM;
  cam_config.pin_xclk = XCLK_GPIO_NUM;
  cam_config.pin_pclk = PCLK_GPIO_NUM;
  cam_config.pin_vsync = VSYNC_GPIO_NUM;
  cam_config.pin_href = HREF_GPIO_NUM;
  cam_config.pin_sccb_sda = SIOD_GPIO_NUM;
  cam_config.pin_sccb_scl = SIOC_GPIO_NUM;
  cam_config.pin_pwdn = PWDN_GPIO_NUM;
  cam_config.pin_reset = RESET_GPIO_NUM;
  cam_config.xclk_freq_hz = 20000000;   // 前端实测为20MHz
  //cam_config.frame_size = FRAMESIZE_QSXGA;  // 2592x1944 5MP 摸边界用最大分辨率
  //cam_config.frame_size = FRAMESIZE_QXGA;
  //cam_config.frame_size = FRAMESIZE_FHD;
  cam_config.frame_size = FRAMESIZE_HD;

  cam_config.pixel_format = PIXFORMAT_JPEG;
  cam_config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  cam_config.fb_location = CAMERA_FB_IN_PSRAM;
  cam_config.jpeg_quality = 12;
  cam_config.fb_count = 2;

  if (cam_config.pixel_format == PIXFORMAT_JPEG) {
    if (psramFound()) {
      cam_config.jpeg_quality = 10;
      cam_config.fb_count = 2;  // 5MP一帧很大,降到1避免PSRAM装不下
      cam_config.grab_mode = CAMERA_GRAB_LATEST;
    } else {
      cam_config.frame_size = FRAMESIZE_HD;
      cam_config.fb_location = CAMERA_FB_IN_DRAM;
    }
  } else {
    cam_config.frame_size = FRAMESIZE_240X240;
#if CONFIG_IDF_TARGET_ESP32S3
    cam_config.fb_count = 2;
#endif
  }
}



static void apply_sensor_settings() {
  sensor_t *s = esp_camera_sensor_get();
  if (!s) {
    Serial.println("[CAM] WARNING: sensor_get returned NULL");
    return;
  }

  // ========================================================================
  // 说明（AF 版精简）：
  //   原参数是为「定焦相机 + 固定室内场景看清」手调的。换 AF 后，光学清晰度
  //   交给自动对焦负责，这里只保留「成像质量基础」——AF 替代不了的部分：
  //     · 三个自动算法主开关(AEC/AGC/AWB)：光线适应的根本，关了图就废
  //     · saturation=0：之前踩坑修掉的偏色(粉色)补丁，和对焦无关，必须留
  //     · vflip/hmirror：镜头物理方向，和眼镜佩戴朝向绑定
  //     · lenc：镜头阴影校正(四角暗角，也影响白平衡)；AF 模组暗角特性可能
  //             与定焦不同，先保留，烧后看四角是否正常再决定去留。
  //   删除的是为定焦特定场景硬调的项(手动曝光区间 reg / ae_level /
  //   gainceiling / contrast / sharpness / denoise / quality)，交回自动/默认。
  // ========================================================================

  // ---- 方向（物理朝向，必须保留）----
  s->set_vflip(s, 0);
  s->set_hmirror(s, 1);

  // ---- 自动曝光主开关（保留，交给 ISP 自适应）----
  s->set_exposure_ctrl(s, 1);   // ★ AEC 主开关
  s->set_aec2(s, 1);            // DSP 端 AEC 辅助

  // ---- 自动增益主开关（保留）----
  s->set_gain_ctrl(s, 1);       // ★ AGC 主开关

  // ---- 自动白平衡主开关（保留）----
  s->set_whitebal(s, 1);        // ★ AWB 主开关
  s->set_awb_gain(s, 1);        // 允许 AWB 调 R/B 增益
  s->set_wb_mode(s, 0);         // 0 = Auto

  // ---- 防偏色补丁（必须保留，和对焦无关）----
  s->set_saturation(s, 0);      // ★ 原来是 2,粉色的头号元凶,务必降到 0

  // ---- 镜头阴影校正（建议保留，烧后看四角再定）----
  s->set_lenc(s, 1);            // 四角暗角修正,也影响白平衡

  s->set_framesize(s, FRAMESIZE_HD);
}

// ========================================
// Camera functions callable from app_httpd.cpp
// ========================================
bool init_camera() {
  setup_cam_config();
  esp_err_t err = esp_camera_init(&cam_config);
  if (err != ESP_OK) {
    Serial.printf("[CAM] init FAILED: 0x%x\n", err);
    return false;
  }
  apply_sensor_settings();

  // ========================================================================
  // ★ AF 初始化（低频对焦，不用连续模式）
  //   连续对焦(autoFocusMode)会让 AF 固件持续用 SCCB(I2C)驱动马达+读状态，
  //   而抓图 esp_camera_fb_get 也走 SCCB，两者持续争用 → 抓图出现数秒空档。
  //   改为：开机对一次；之后 loop() 每隔 AF_REFOCUS_MS 触发一次单次对焦。
  //   单次对焦只在触发那一下用 SCCB，总线大部分时间空给抓图。
  //   用通用寄存器 0x3022=0x03(single auto focus)，不依赖 AF 库封装版本。
  // ========================================================================
  {
    sensor_t *s = esp_camera_sensor_get();
    if (s) {
      ov5640.start(s);
      if (ov5640.focusInit() == 0) {
        Serial.println("[AF] focusInit OK");
      } else {
        Serial.println("[AF] focusInit FAILED (degrade to fixed focus, image still works)");
      }
      s->set_reg(s, 0x3022, 0xff, 0x03);   // 开机单次对焦
      Serial.println("[AF] low-frequency single-focus mode enabled");
    } else {
      Serial.println("[AF] sensor NULL, skip AF init");
    }
  }

  Serial.printf("[CAM] init OK, free heap: %u, PSRAM free: %u\n",
                (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
  return true;
}

void deinit_camera() {
  esp_err_t err = esp_camera_deinit();
  if (err != ESP_OK) {
    Serial.printf("[CAM] deinit failed: 0x%x, retrying...\n", err);
    delay(200);
    err = esp_camera_deinit();
  }
  Serial.printf("[CAM] deinit: %s, free heap: %u\n",
                err == ESP_OK ? "OK" : "FAIL",
                (unsigned)ESP.getFreeHeap());
}

// ========================================
// setup & loop
// ========================================
void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(true);
  Serial.println();
  Serial.println("========================================");
  Serial.println(" CameraWebServer + PDM Audio (XIAO S3)");
  Serial.println("========================================");

  // Camera init (DVP interface)
  if (!init_camera()) {
    Serial.println("[FATAL] Camera init failed!");
    return;
  }

  // PDM mic init (I2S_NUM_0, runs concurrently with camera)
  if (!pdm_mic_init()) {
    Serial.println("[WARNING] PDM mic init failed! Audio will not work.");
    // Continue — camera still works
  }

#if defined(CAMERA_MODEL_ESP_EYE)
  pinMode(13, INPUT_PULLUP);
  pinMode(14, INPUT_PULLUP);
#endif

#if defined(LED_GPIO_NUM)
  setupLedFlash(LED_GPIO_NUM);
#endif

  // 如果自带AP，可以用下面这一段
  // WiFi
  // WiFi — 静态 IP，避免会场 DHCP 拥堵/续租失败
  //IPAddress local_ip(192, 168, 1, 50);
  //IPAddress gateway (192, 168, 1, 1);
  //IPAddress subnet  (255, 255, 255, 0);
  //IPAddress dns1    (192, 168, 1, 1);
  //WiFi.config(local_ip, gateway, subnet, dns1);

  WiFi.begin(ssid, password);
  WiFi.setSleep(false);
  esp_wifi_set_ps(WIFI_PS_NONE);                         // 双保险：彻底关 PS
  WiFi.setTxPower(WIFI_POWER_19_5dBm);                   // 拉满发射功率
  esp_wifi_config_11b_rate(WIFI_IF_STA, true);           // 禁用 11b 低速率，减少被会场干扰拖慢
  // 可选：如果你自己带 AP 并且知道信道号，解开下一行并把 6 改成你 AP 的信道，TODO
  // esp_wifi_set_channel(6, WIFI_SECOND_CHAN_NONE);


  Serial.print("[WiFi] Connecting");
  int wifi_retry = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    wifi_retry++;
    if (wifi_retry > 40) { // 20 seconds timeout
      Serial.println("\n[WiFi] FAILED to connect! Restarting...");
      ESP.restart();
    }
  }
  Serial.println();
  Serial.printf("[WiFi] Connected! IP: %s\n", WiFi.localIP().toString().c_str());

  // Start web server
  startCameraServer();

  // ★ 启动 TCP 图像通道 task：Core 0(靠 WiFi 栈)、优先级 4(低于 audio_sender 的 6)，
  //   让音频发送永远优先。与 HTTP /capture 并存，PC 端运行期选择走哪条。
  {
    BaseType_t ok = xTaskCreatePinnedToCore(
      image_tcp_task, "img_tcp", 4096, NULL, 4, NULL, 0);
    if (ok != pdPASS) {
      Serial.println("[TCP-IMG] task create FAILED (TCP 图像通道不可用，HTTP /capture 仍可用)");
    }
  }

  Serial.println("========================================");
  Serial.printf("[READY] Web UI:    http://%s/\n", WiFi.localIP().toString().c_str());
  Serial.printf("[READY] Capture:   http://%s/capture\n", WiFi.localIP().toString().c_str());
  Serial.printf("[READY] Stream:    http://%s:81/stream\n", WiFi.localIP().toString().c_str());
  Serial.printf("[READY] Audio WS:  ws://%s/ws_audio\n", WiFi.localIP().toString().c_str());
  Serial.printf("[READY] Image TCP: %s:5000 (req-resp)\n", WiFi.localIP().toString().c_str());
  Serial.printf("[READY] Free heap: %u bytes\n", (unsigned)ESP.getFreeHeap());
  Serial.println("========================================");
}

// ========================================================================
// ★ 可选 AF 状态调试函数（只读，不触发对焦）
//   修正了原 AF demo 的判断 bug：原代码只认 FW_STATUS_S_FOCUSED(0x10)，
//   漏判了 0x20——而 0x20 同样是「对焦完成」状态(对焦后持续保持)。原代码下
//   0x20 会落进空 else 不打印，看起来像「没对上」。这里把 0x10 和 0x20 都
//   识别为已对焦，0x00 为对焦中，其余原样打印 16 进制。
//   注意：此函数仅用于串口观察，不参与抓图/发送逻辑——图像始终照常发，
//   不因对焦状态丢帧（模糊帧也发，模型会在后续清晰帧上读出内容）。
// ========================================================================
static void af_status_debug() {
  uint8_t rc = ov5640.getFWStatus();
  if (rc == (uint8_t)-1) {
    Serial.println("[AF] FW_STATUS=0xFF  Check your OV5640");
  } else if (rc == FW_STATUS_S_FOCUSING) {        // 0x00
    Serial.println("[AF] FW_STATUS=0x00  Focusing!");
  } else if (rc == FW_STATUS_S_FOCUSED || rc == 0x20) {  // 0x10 或 0x20 都是已对焦
    Serial.printf("[AF] FW_STATUS=0x%02x  Focused!\n", rc);
  } else {
    Serial.printf("[AF] FW_STATUS=0x%02x  (other)\n", rc);
  }
}

// ───────────────────────────────────────────────────────────────────────
// ★ TCP 图像通道任务（与 HTTP /capture 并存）
//  协议（请求-响应，对齐 PC 端 TCP 图像源）：
//    PC 连上后，每需要一帧就发 1 字节(任意值)作为"请求一帧"；
//    ESP32 收到后抓一张 JPEG，回裸帧：
//      magic(4,0x55AA55AA) | frame_id(4) | w(2) | h(2) | fmt(1=JPEG) |
//      reserved(3) | len(4) | jpeg_bytes(len)   全部小端。
//  独立 task + Core 0 + 优先级4(<audio_sender的6)：让音频发送永远优先。
// ───────────────────────────────────────────────────────────────────────
static void image_tcp_task(void *param) {
  (void)param;
  g_image_tcp_server.begin();
  Serial.println("[TCP-IMG] image TCP server started on port 5000");
  for (;;) {
    WiFiClient client = g_image_tcp_server.available();
    if (!client) {
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    client.setNoDelay(true);
    Serial.println("[TCP-IMG] client connected");
    static uint32_t frame_id = 0;
    while (client.connected()) {
      if (client.available() <= 0) {
        vTaskDelay(pdMS_TO_TICKS(2));
        continue;
      }
      uint8_t req;
      int rn = client.read(&req, 1);
      if (rn <= 0) { vTaskDelay(pdMS_TO_TICKS(2)); continue; }

      camera_fb_t *fb = esp_camera_fb_get();
      if (!fb || fb->len == 0) {
        if (fb) esp_camera_fb_return(fb);
        uint8_t hdr[20] = {0};
        uint32_t magic = 0x55AA55AA;
        memcpy(hdr, &magic, 4);
        client.write(hdr, 20);   // len=0：PC 端据此知道没抓到
        continue;
      }
      frame_id++;
      uint8_t hdr[20];
      uint32_t magic = 0x55AA55AA;
      uint16_t w = fb->width, h = fb->height;
      uint32_t len = fb->len;
      memcpy(hdr + 0,  &magic, 4);
      memcpy(hdr + 4,  &frame_id, 4);
      memcpy(hdr + 8,  &w, 2);
      memcpy(hdr + 10, &h, 2);
      hdr[12] = 0;                       // fmt = JPEG
      hdr[13] = hdr[14] = hdr[15] = 0;   // reserved
      memcpy(hdr + 16, &len, 4);
      // 关键：WiFiClient::write 不保证一次发完(LWIP 发送缓冲满时只发一部分)。
      // 必须循环补发，否则 PC 端读取字节数与实际不符 → TCP 流错位 → bad magic。
      auto write_all = [&](const uint8_t *buf, size_t n) -> bool {
        size_t sent = 0;
        while (sent < n && client.connected()) {
          size_t w = client.write(buf + sent, n - sent);
          if (w == 0) {
            vTaskDelay(pdMS_TO_TICKS(1));  // 缓冲满，让出后重试
            continue;
          }
          sent += w;
        }
        return sent == n;
      };
      write_all(hdr, 20);
      write_all(fb->buf, fb->len);
      esp_camera_fb_return(fb);
    }
    client.stop();
    Serial.println("[TCP-IMG] client disconnected");
  }
}

// AF 低频对焦周期(ms)。你的场景"看几秒",焦距变化不频繁,3s够跟上。
// 图还慢就调大(5000)更省SCCB;焦距跟不上就调小(别低于~1500)。
#define AF_REFOCUS_MS 1000

void loop() {
  static unsigned long last_af_ms = 0;
  static unsigned long last_health_ms = 0;
  unsigned long now = millis();

  // 低频单次对焦:只在触发那一下用SCCB,不持续占总线
  // ★ 临时测量: 触发对焦后密集轮询, 测 Focusing->Focused 真实耗时
  
  // 对焦: 默认交给 PC 按需 HTTP 触发(/control?var=af)。
  // 仅当 g_auto_af=true 时固件自动定时对焦(调试用, 非阻塞版)。
  /*if (g_auto_af && now - last_af_ms >= AF_REFOCUS_MS) {
    last_af_ms = now;
    sensor_t *s = esp_camera_sensor_get();
    if (s) {
      s->set_reg(s, 0x3022, 0xff, 0x03);   // 单次对焦, 不阻塞轮询
    }
  }*/
  if (g_auto_af && now - last_af_ms >= AF_REFOCUS_MS) {
    last_af_ms = now;
    sensor_t *s = esp_camera_sensor_get();
    if (s) {
        s->set_reg(s, 0x3022, 0xff, 0x03);
    }
  }
  // 健康日志:每10s一次
  if (now - last_health_ms >= 10000) {
    last_health_ms = now;
    Serial.printf("[HEALTH] heap=%u  PSRAM=%u  RSSI=%d  temp=%.1fC  uptime=%lus\n",
                  (unsigned)ESP.getFreeHeap(),
                  (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                  WiFi.RSSI(),
                  temperatureRead(),
                  millis() / 1000);
  }

  delay(50);  // 非阻塞轮询
}
