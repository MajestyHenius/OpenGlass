// Copyright 2015-2016 Espressif Systems (Shanghai) PTE LTD
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
#include <Arduino.h>
#include "esp_http_server.h"
#include "esp_timer.h"
#include "esp_camera.h"
#include "img_converters.h"
#include "fb_gfx.h"
#include "esp32-hal-ledc.h"
#include "sdkconfig.h"
#include "camera_index.h"

// ---- PDM Audio ----
#include "driver/i2s_pdm.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
// 新增：用于对 WS socket 做 TCP 层调优（TCP_NODELAY / SO_SNDBUF / keepalive）
#include "lwip/sockets.h"
#include "lwip/tcp.h"
// 前向声明：定义在后面，但 ws_audio_handler / ws_audio_v2_handler 会先调用它
static void ws_tune_socket(int fd);

#if defined(ARDUINO_ARCH_ESP32) && defined(CONFIG_ARDUHAL_ESP_LOG)
#include "esp32-hal-log.h"
#endif

// ============================================================
// WebSocket support check
// ESP32 Arduino 3.x enables CONFIG_HTTPD_WS_SUPPORT by default
// If build fails, enable WS support in Arduino IDE sdkconfig
// ============================================================
#ifndef CONFIG_HTTPD_WS_SUPPORT
#error "WebSocket support required! Use ESP32 Arduino Core 3.x or enable CONFIG_HTTPD_WS_SUPPORT manually"
#endif

// Enable LED FLASH setting
#define CONFIG_LED_ILLUMINATOR_ENABLED 1

// LED FLASH setup
#if CONFIG_LED_ILLUMINATOR_ENABLED

#define LED_LEDC_GPIO            22  //configure LED pin
#define CONFIG_LED_MAX_INTENSITY 255

int led_duty = 0;
bool isStreaming = false;

#endif

// ============================================================
// PDM microphone config (XIAO ESP32S3)
// Per schematic XIAO_ESP32S3_V1.3_SCH_260115:
//   IO42 = PDM_CLK (MTMS)
//   IO41 = PDM_DATA (MTDI)
// ============================================================
#define PDM_CLK_IO        42
#define PDM_DATA_IO       41
#define PDM_SAMPLE_RATE   16000   // 16kHz, suitable for ASR
#define PDM_DMA_DESC_NUM  6       // Number of DMA descriptors
#define PDM_DMA_FRAME_NUM 1024    // Samples per DMA buffer
#define AUDIO_READ_BYTES  640     // 20ms @ 16kHz mono 16bit = 320 samples * 2 bytes
#define AUDIO_GAIN        3       // Software gain (1=no gain, 2-4 suitable for PDM mic)
#define AUDIO_DISCARD_FRAMES 5    // Discard first N frames after connect (flush stale DMA data)

// ============================================================
// v5 buffered: timestamped ring buffer (PSRAM-backed)
// Producer (PDM capture) writes packets; consumer (WS sender) drains them.
// Decouples audio capture from WiFi send blocking (camera uploads).
// ============================================================
#define AUDIO_PKT_SAMPLES      320                 // 20ms @ 16kHz
#define AUDIO_PKT_BYTES        (AUDIO_PKT_SAMPLES * 2)  // 640 bytes PCM
#define AUDIO_RING_CAPACITY    400                 // 160 * 20ms = 3.2 s buffer  160改成了400抗抖动，代价是PSRAM 多吃约 160 KB，8MB PSRAM 毫无压力。
#define AUDIO_WIRE_HDR_BYTES   12                  // seq(4) + ts_ms(4) + n_samples(2) + drops(2)
#define AUDIO_WIRE_PKT_BYTES   (AUDIO_WIRE_HDR_BYTES + AUDIO_PKT_BYTES)

#pragma pack(push, 1)
typedef struct {
  uint32_t seq;                        // monotonic packet sequence number
  uint32_t ts_ms;                      // ESP32 timestamp (esp_timer ms) of first sample
  uint16_t n_samples;                  // number of PCM16 samples in this packet
  uint16_t reserved;                   // pad / future use
  int16_t  pcm[AUDIO_PKT_SAMPLES];     // PCM16 little-endian data
} audio_pkt_t;
#pragma pack(pop)

// ============================================================
// Audio state (cross-task, volatile)
// ============================================================
// g_audio_mode removed — Phase A concurrent mode, camera + PDM run simultaneously
static volatile bool g_audio_streaming = false;   // Whether audio streaming task is running
static i2s_chan_handle_t g_pdm_rx_handle = NULL;  // I2S PDM RX channel handle
static volatile int g_ws_audio_fd = -1;           // WS client socket fd
static volatile TaskHandle_t g_audio_task = NULL; // v1 single-task (legacy) handle

// v5: split producer/consumer task handles
static volatile TaskHandle_t g_audio_capture_task_h = NULL;
static volatile TaskHandle_t g_audio_sender_task_h  = NULL;

// v5: ring buffer
static audio_pkt_t *g_audio_ring = NULL;
static volatile uint32_t g_ring_head  = 0;  // producer writes here (next write slot)
static volatile uint32_t g_ring_tail  = 0;  // consumer reads here (next read slot)
static volatile uint32_t g_ring_seq   = 0;  // monotonic seq counter
static volatile uint32_t g_ring_drops = 0;  // total dropped-oldest count since init
static portMUX_TYPE g_ring_mux = portMUX_INITIALIZER_UNLOCKED;

// External: camera init/deinit (defined in .ino)
extern bool init_camera();
extern void deinit_camera();

// ============================================================
// Original camera types & variables
// ============================================================
typedef struct {
  httpd_req_t *req;
  size_t len;
} jpg_chunking_t;

#define PART_BOUNDARY "123456789000000000000987654321"
static const char *_STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char *_STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char *_STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\nX-Timestamp: %d.%06d\r\n\r\n";

httpd_handle_t stream_httpd = NULL;
httpd_handle_t camera_httpd = NULL;

typedef struct {
  size_t size;   //number of values used for filtering
  size_t index;  //current value index
  size_t count;  //value count
  int sum;
  int *values;  //array to be filled with values
} ra_filter_t;

static ra_filter_t ra_filter;

static ra_filter_t *ra_filter_init(ra_filter_t *filter, size_t sample_size) {
  memset(filter, 0, sizeof(ra_filter_t));

  filter->values = (int *)malloc(sample_size * sizeof(int));
  if (!filter->values) {
    return NULL;
  }
  memset(filter->values, 0, sample_size * sizeof(int));

  filter->size = sample_size;
  return filter;
}

#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
static int ra_filter_run(ra_filter_t *filter, int value) {
  if (!filter->values) {
    return value;
  }
  filter->sum -= filter->values[filter->index];
  filter->values[filter->index] = value;
  filter->sum += filter->values[filter->index];
  filter->index++;
  filter->index = filter->index % filter->size;
  if (filter->count < filter->size) {
    filter->count++;
  }
  return filter->sum / filter->count;
}
#endif

#if CONFIG_LED_ILLUMINATOR_ENABLED
void enable_led(bool en) {  // Turn LED On or Off
  int duty = en ? led_duty : 0;
  if (en && isStreaming && (led_duty > CONFIG_LED_MAX_INTENSITY)) {
    duty = CONFIG_LED_MAX_INTENSITY;
  }
  ledcWrite(LED_LEDC_GPIO, duty);
  log_i("Set LED intensity to %d", duty);
}
#endif

// ============================================================
// PDM Microphone Init / Deinit
// ============================================================

/**
 * Initialize PDM microphone (I2S0, RX only)
 * Must be called after camera deinit to avoid resource conflicts
 */
bool pdm_mic_init() {
  Serial.printf("[PDM] Init: CLK=IO%d, DATA=IO%d, rate=%dHz\n",
                PDM_CLK_IO, PDM_DATA_IO, PDM_SAMPLE_RATE);
  Serial.printf("[PDM] Heap before: free=%u, PSRAM=%u\n",
                (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

  // 1. Create I2S channel (RX only, I2S_NUM_0)
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  chan_cfg.dma_desc_num = PDM_DMA_DESC_NUM;
  chan_cfg.dma_frame_num = PDM_DMA_FRAME_NUM;

  esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &g_pdm_rx_handle);
  if (err != ESP_OK) {
    Serial.printf("[PDM] i2s_new_channel FAILED: 0x%x (%s)\n", err, esp_err_to_name(err));
    g_pdm_rx_handle = NULL;
    return false;
  }

  // 2. Configure PDM RX mode
  i2s_pdm_rx_config_t pdm_cfg = {
    .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(PDM_SAMPLE_RATE),
    .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
    .gpio_cfg = {
      .clk = (gpio_num_t)PDM_CLK_IO,
      .din = (gpio_num_t)PDM_DATA_IO,
      .invert_flags = {
        .clk_inv = false,
      },
    },
  };

  err = i2s_channel_init_pdm_rx_mode(g_pdm_rx_handle, &pdm_cfg);
  if (err != ESP_OK) {
    Serial.printf("[PDM] init_pdm_rx_mode FAILED: 0x%x (%s)\n", err, esp_err_to_name(err));
    i2s_del_channel(g_pdm_rx_handle);
    g_pdm_rx_handle = NULL;
    return false;
  }

  // 3. Enable channel
  err = i2s_channel_enable(g_pdm_rx_handle);
  if (err != ESP_OK) {
    Serial.printf("[PDM] channel_enable FAILED: 0x%x (%s)\n", err, esp_err_to_name(err));
    i2s_del_channel(g_pdm_rx_handle);
    g_pdm_rx_handle = NULL;
    return false;
  }

  Serial.printf("[PDM] Init OK! Heap after: free=%u, PSRAM=%u\n",
                (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
  return true;
}

/**
 * Stop and release PDM microphone resources
 */
static void pdm_mic_deinit() {
  if (g_pdm_rx_handle) {
    i2s_channel_disable(g_pdm_rx_handle);
    i2s_del_channel(g_pdm_rx_handle);
    g_pdm_rx_handle = NULL;
    Serial.printf("[PDM] Deinit OK, free heap: %u\n", (unsigned)ESP.getFreeHeap());
  }
}

// ============================================================
// Audio WebSocket streaming task
// Runs in dedicated FreeRTOS task, reads audio from PDM and pushes via WS
// ============================================================
static void audio_stream_task(void *param) {
  // Allocate read buffer
  uint8_t *buf = (uint8_t *)malloc(AUDIO_READ_BYTES);
  if (!buf) {
    Serial.printf("[WS_AUDIO] OOM! Cannot allocate %d bytes, heap=%u\n",
                  AUDIO_READ_BYTES, (unsigned)ESP.getFreeHeap());
    g_audio_streaming = false;
    g_audio_task = NULL;
    vTaskDelete(NULL);
    return;
  }

  Serial.println("[WS_AUDIO] Streaming task started (with DC-offset + gain)");
  uint32_t total_bytes = 0;
  uint32_t start_ms = millis();
  uint32_t last_log_ms = start_ms;
  uint32_t send_errors = 0;
  uint32_t frame_count = 0;
  int32_t dc_acc = 0;  // DC offset EMA accumulator

  while (g_audio_streaming && g_pdm_rx_handle != NULL && g_ws_audio_fd >= 0) {
    size_t bytes_read = 0;
    esp_err_t err = i2s_channel_read(g_pdm_rx_handle, buf, AUDIO_READ_BYTES,
                                      &bytes_read, 200); // 200ms timeout

    if (err != ESP_OK) {
      if (err == ESP_ERR_TIMEOUT) {
        continue; // Timeout is normal, continue
      }
      Serial.printf("[WS_AUDIO] i2s_channel_read error: 0x%x\n", err);
      break;
    }
    if (bytes_read == 0) {
      continue;
    }

    frame_count++;
    // Discard initial frames (flush stale DMA buffer data)
    if (frame_count <= AUDIO_DISCARD_FRAMES) continue;

    // Audio processing: DC offset removal + software gain
    {
      int16_t *samples = (int16_t *)buf;
      int n = bytes_read / 2;
      for (int i = 0; i < n; i++) {
        // DC offset removal via exponential moving average
        dc_acc = dc_acc - (dc_acc >> 8) + (int32_t)samples[i];
        int32_t val = (int32_t)samples[i] - (dc_acc >> 8);
        // Software gain
        val *= AUDIO_GAIN;
        // Clamp to int16 range
        if (val > 32767) val = 32767;
        if (val < -32768) val = -32768;
        samples[i] = (int16_t)val;
      }
    }

    // Send binary frame via WebSocket
    httpd_ws_frame_t ws_frame;
    memset(&ws_frame, 0, sizeof(ws_frame));
    ws_frame.type = HTTPD_WS_TYPE_BINARY;
    ws_frame.payload = buf;
    ws_frame.len = bytes_read;
    ws_frame.final = true;

    err = httpd_ws_send_frame_async(camera_httpd, g_ws_audio_fd, &ws_frame);
    if (err != ESP_OK) {
      send_errors++;
      if (send_errors >= 3) {
        Serial.printf("[WS_AUDIO] WS send failed %u times (last: 0x%x), stopping\n",
                      send_errors, err);
        break;
      }
      vTaskDelay(pdMS_TO_TICKS(10)); // Brief wait before retry
      continue;
    }

    send_errors = 0; // Reset error count
    total_bytes += bytes_read;

    // Print stats every 5 seconds
    uint32_t now_ms = millis();
    if (now_ms - last_log_ms >= 5000) {
      float elapsed_s = (now_ms - start_ms) / 1000.0f;
      Serial.printf("[WS_AUDIO] Streaming: %u bytes (%.1f KB/s), heap=%u\n",
                    total_bytes,
                    elapsed_s > 0 ? (total_bytes / 1024.0f / elapsed_s) : 0,
                    (unsigned)ESP.getFreeHeap());
      last_log_ms = now_ms;
    }
  }

  uint32_t elapsed_ms = millis() - start_ms;
  Serial.printf("[WS_AUDIO] Stream ended: %u bytes in %u ms (%.1f KB/s)\n",
                total_bytes, elapsed_ms,
                elapsed_ms > 0 ? (total_bytes / 1024.0f / (elapsed_ms / 1000.0f)) : 0);

  free(buf);
  g_audio_streaming = false;
  g_audio_task = NULL;
  vTaskDelete(NULL);
}

// ============================================================
// v5 BUFFERED: Ring buffer helpers
// ============================================================

static bool audio_ring_init() {
  if (g_audio_ring) {
    // already allocated — just reset pointers
    taskENTER_CRITICAL(&g_ring_mux);
    g_ring_head = g_ring_tail = g_ring_seq = g_ring_drops = 0;
    taskEXIT_CRITICAL(&g_ring_mux);
    return true;
  }
  size_t total = (size_t)AUDIO_RING_CAPACITY * sizeof(audio_pkt_t);
  g_audio_ring = (audio_pkt_t *)heap_caps_malloc(total, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!g_audio_ring) {
    Serial.printf("[RING] OOM: cannot alloc %u bytes in PSRAM (free PSRAM=%u)\n",
                  (unsigned)total,
                  (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    return false;
  }
  memset(g_audio_ring, 0, total);
  g_ring_head = g_ring_tail = g_ring_seq = g_ring_drops = 0;
  Serial.printf("[RING] Alloc %u bytes PSRAM for %d packets (%.1fs buffer)\n",
                (unsigned)total, AUDIO_RING_CAPACITY,
                AUDIO_RING_CAPACITY * 20.0f / 1000.0f);
  return true;
}

static void audio_ring_deinit() {
  if (g_audio_ring) {
    heap_caps_free(g_audio_ring);
    g_audio_ring = NULL;
  }
  g_ring_head = g_ring_tail = g_ring_seq = g_ring_drops = 0;
}

// ============================================================
// v5 BUFFERED: Audio capture task (PRODUCER)
// - Reads 20ms chunks from PDM
// - Applies DC-offset removal + software gain (same as legacy path)
// - Tags each packet with seq + ts_ms and writes to ring buffer
// - Drops OLDEST packet if ring full (consumer fell behind due to WiFi stall)
// ============================================================
static void audio_capture_task(void *param) {
  uint8_t *tmp = (uint8_t *)malloc(AUDIO_PKT_BYTES);
  if (!tmp) {
    Serial.printf("[CAP] OOM! Cannot allocate %d bytes\n", AUDIO_PKT_BYTES);
    g_audio_capture_task_h = NULL;
    vTaskDelete(NULL);
    return;
  }

  Serial.println("[CAP] Capture task started (ring-buffered)");
  int32_t  dc_acc      = 0;
  uint32_t frame_count = 0;
  uint32_t last_log_ms = millis();

  while (g_audio_streaming && g_pdm_rx_handle != NULL) {
    size_t bytes_read = 0;
    esp_err_t err = i2s_channel_read(g_pdm_rx_handle, tmp, AUDIO_PKT_BYTES,
                                      &bytes_read, 200);
    if (err != ESP_OK) {
      if (err == ESP_ERR_TIMEOUT) continue;
      Serial.printf("[CAP] i2s_channel_read error: 0x%x\n", err);
      break;
    }
    if (bytes_read != AUDIO_PKT_BYTES) continue;  // partial reads skipped

    frame_count++;
    if (frame_count <= AUDIO_DISCARD_FRAMES) continue;

    // DC offset removal (EMA) + software gain + clipping
    {
      int16_t *samples = (int16_t *)tmp;
      int n = bytes_read / 2;
      for (int i = 0; i < n; i++) {
        dc_acc = dc_acc - (dc_acc >> 8) + (int32_t)samples[i];
        int32_t val = (int32_t)samples[i] - (dc_acc >> 8);
        val *= AUDIO_GAIN;
        if (val >  32767) val =  32767;
        if (val < -32768) val = -32768;
        samples[i] = (int16_t)val;
      }
    }

    uint32_t ts_ms = (uint32_t)(esp_timer_get_time() / 1000ULL);

    taskENTER_CRITICAL(&g_ring_mux);
    uint32_t head      = g_ring_head;
    uint32_t next_head = (head + 1) % AUDIO_RING_CAPACITY;
    if (next_head == g_ring_tail) {
      // Ring full — drop oldest (advance tail)
      g_ring_tail = (g_ring_tail + 1) % AUDIO_RING_CAPACITY;
      g_ring_drops++;
    }
    audio_pkt_t *slot = &g_audio_ring[head];
    slot->seq       = ++g_ring_seq;
    slot->ts_ms     = ts_ms;
    slot->n_samples = AUDIO_PKT_SAMPLES;
    slot->reserved  = 0;
    memcpy(slot->pcm, tmp, AUDIO_PKT_BYTES);
    g_ring_head = next_head;
    taskEXIT_CRITICAL(&g_ring_mux);

    uint32_t now_ms = millis();
    if (now_ms - last_log_ms >= 10000) {
      uint32_t h, t, drops, seq;
      taskENTER_CRITICAL(&g_ring_mux);
      h = g_ring_head; t = g_ring_tail; drops = g_ring_drops; seq = g_ring_seq;
      taskEXIT_CRITICAL(&g_ring_mux);
      uint32_t pending = (h + AUDIO_RING_CAPACITY - t) % AUDIO_RING_CAPACITY;
      Serial.printf("[CAP] seq=%u ring=%u/%d drops=%u heap=%u\n",
                    seq, pending, AUDIO_RING_CAPACITY, drops,
                    (unsigned)ESP.getFreeHeap());
      last_log_ms = now_ms;
    }
  }

  free(tmp);
  Serial.println("[CAP] Capture task exit");
  g_audio_capture_task_h = NULL;
  vTaskDelete(NULL);
}

// ============================================================
// v5 BUFFERED: Audio sender task (CONSUMER)
// - Peeks one packet from ring tail
// - Sends [header(12) | PCM16 data(640)] as a single WS BINARY frame
// - On send success: advances tail
// - On send error (typically WiFi congested while camera HTTP is uploading):
//     retries same packet after a brief delay; does NOT advance tail.
//     This is the core mechanism that gives "timestamp retransmit" semantics.
// - After persistent failure (~ N retries = WS dead), stops streaming.
// ============================================================
static void audio_sender_task(void *param) {
  Serial.println("[SND] Sender task started (Core 0)");
  uint32_t total_bytes   = 0;
  uint32_t total_packets = 0;
  uint32_t send_errors   = 0;
  uint32_t start_ms      = millis();
  uint32_t last_log_ms   = start_ms;

  // Wire buffer is allocated once (stack too small for ~652 bytes); in DRAM to avoid PSRAM latency for TCP
  uint8_t *wire = (uint8_t *)malloc(AUDIO_WIRE_PKT_BYTES);
  if (!wire) {
    Serial.printf("[SND] OOM wire buf %d\n", AUDIO_WIRE_PKT_BYTES);
    g_audio_sender_task_h = NULL;
    vTaskDelete(NULL);
    return;
  }

  while (g_audio_streaming && g_ws_audio_fd >= 0) {
    // Peek from ring tail (copy to avoid race with producer drop-oldest)
    audio_pkt_t pkt;
    uint32_t read_pos = 0;
    bool     have_pkt = false;

    taskENTER_CRITICAL(&g_ring_mux);
    if (g_ring_tail != g_ring_head) {
      read_pos = g_ring_tail;
      pkt      = g_audio_ring[read_pos];
      have_pkt = true;
    }
    taskEXIT_CRITICAL(&g_ring_mux);

    if (!have_pkt) {
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }

    // Build wire packet: [seq(4) | ts_ms(4) | n_samples(2) | drops(2) | pcm(640)]
    uint32_t seq_le    = pkt.seq;
    uint32_t ts_le     = pkt.ts_ms;
    uint16_t n_le      = pkt.n_samples;
    uint32_t drops_cur;
    taskENTER_CRITICAL(&g_ring_mux);
    drops_cur = g_ring_drops;
    taskEXIT_CRITICAL(&g_ring_mux);
    uint16_t drops_le  = (drops_cur > 0xFFFFu) ? 0xFFFFu : (uint16_t)drops_cur;

    memcpy(wire + 0,  &seq_le,   4);
    memcpy(wire + 4,  &ts_le,    4);
    memcpy(wire + 8,  &n_le,     2);
    memcpy(wire + 10, &drops_le, 2);
    memcpy(wire + 12, pkt.pcm,   AUDIO_PKT_BYTES);

    httpd_ws_frame_t ws_frame;
    memset(&ws_frame, 0, sizeof(ws_frame));
    ws_frame.type    = HTTPD_WS_TYPE_BINARY;
    ws_frame.payload = wire;
    ws_frame.len     = AUDIO_WIRE_PKT_BYTES;
    ws_frame.final   = true;

    esp_err_t err = httpd_ws_send_frame_async(camera_httpd, g_ws_audio_fd, &ws_frame);
    if (err != ESP_OK) {
      // Keep the packet in the ring; retry later. Camera HTTP upload typically
      // blocks 100-500ms; we back off and try again — this is the whole point.
      send_errors++;
      if (send_errors >= 125) {  // ~1 sec of continuous failures → give up  20260509，50改为125，2.5s
        Serial.printf("[SND] WS send failed %u times (last 0x%x), closing\n",
                      send_errors, err);
        break;
      }
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }

    // Success: advance tail. Guard against producer having already advanced it
    // past read_pos (ring-full drop case). Only advance if tail is still where
    // we read from.
    taskENTER_CRITICAL(&g_ring_mux);
    if (g_ring_tail == read_pos) {
      g_ring_tail = (read_pos + 1) % AUDIO_RING_CAPACITY;
    }
    taskEXIT_CRITICAL(&g_ring_mux);

    send_errors    = 0;
    total_bytes   += AUDIO_WIRE_PKT_BYTES;
    total_packets++;

    uint32_t now_ms = millis();
    if (now_ms - last_log_ms >= 5000) {
      float elapsed_s = (now_ms - start_ms) / 1000.0f;
      uint32_t h, t, drops;
      taskENTER_CRITICAL(&g_ring_mux);
      h = g_ring_head; t = g_ring_tail; drops = g_ring_drops;
      taskEXIT_CRITICAL(&g_ring_mux);
      uint32_t pending = (h + AUDIO_RING_CAPACITY - t) % AUDIO_RING_CAPACITY;
      Serial.printf("[SND] sent=%u pkts, %.1f KB/s, ring_pending=%u/%d drops=%u\n",
                    total_packets,
                    elapsed_s > 0 ? (total_bytes / 1024.0f / elapsed_s) : 0,
                    pending, AUDIO_RING_CAPACITY, drops);
      last_log_ms = now_ms;
    }
  }

  free(wire);
  Serial.printf("[SND] Sender task exit: sent=%u pkts\n", total_packets);

  // Signal shutdown to capture task
  g_audio_streaming = false;
  g_ws_audio_fd     = -1;
  g_audio_sender_task_h = NULL;
  vTaskDelete(NULL);
}

// ============================================================
// HTTP Handlers: Original camera handlers (with audio mode guard)
// ============================================================

static esp_err_t bmp_handler(httpd_req_t *req) {
  camera_fb_t *fb = NULL;
  esp_err_t res = ESP_OK;
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  uint64_t fr_start = esp_timer_get_time();
#endif
  fb = esp_camera_fb_get();
  if (!fb) {
    log_e("Camera capture failed");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  httpd_resp_set_type(req, "image/x-windows-bmp");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.bmp");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  char ts[32];
  snprintf(ts, 32, "%lld.%06ld", fb->timestamp.tv_sec, fb->timestamp.tv_usec);
  httpd_resp_set_hdr(req, "X-Timestamp", (const char *)ts);

  uint8_t *buf = NULL;
  size_t buf_len = 0;
  bool converted = frame2bmp(fb, &buf, &buf_len);
  esp_camera_fb_return(fb);
  if (!converted) {
    log_e("BMP Conversion failed");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  res = httpd_resp_send(req, (const char *)buf, buf_len);
  free(buf);
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  uint64_t fr_end = esp_timer_get_time();
#endif
  log_i("BMP: %llums, %uB", (uint64_t)((fr_end - fr_start) / 1000), buf_len);
  return res;
}

static size_t jpg_encode_stream(void *arg, size_t index, const void *data, size_t len) {
  jpg_chunking_t *j = (jpg_chunking_t *)arg;
  if (!index) {
    j->len = 0;
  }
  if (httpd_resp_send_chunk(j->req, (const char *)data, len) != ESP_OK) {
    return 0;
  }
  j->len += len;
  return len;
}

static esp_err_t capture_handler(httpd_req_t *req) {
  camera_fb_t *fb = NULL;
  esp_err_t res = ESP_OK;
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  int64_t fr_start = esp_timer_get_time();
#endif

#if CONFIG_LED_ILLUMINATOR_ENABLED
  enable_led(true);
  vTaskDelay(150 / portTICK_PERIOD_MS);
  fb = esp_camera_fb_get();
  enable_led(false);
#else
  fb = esp_camera_fb_get();
#endif

  if (!fb) {
    log_e("Camera capture failed");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  char ts[32];
  snprintf(ts, 32, "%lld.%06ld", fb->timestamp.tv_sec, fb->timestamp.tv_usec);
  httpd_resp_set_hdr(req, "X-Timestamp", (const char *)ts);

#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  size_t fb_len = 0;
#endif
  if (fb->format == PIXFORMAT_JPEG) {
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
    fb_len = fb->len;
#endif
    res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  } else {
    jpg_chunking_t jchunk = {req, 0};
    res = frame2jpg_cb(fb, 80, jpg_encode_stream, &jchunk) ? ESP_OK : ESP_FAIL;
    httpd_resp_send_chunk(req, NULL, 0);
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
    fb_len = jchunk.len;
#endif
  }
  esp_camera_fb_return(fb);
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  int64_t fr_end = esp_timer_get_time();
#endif
  log_i("JPG: %uB %ums", (uint32_t)(fb_len), (uint32_t)((fr_end - fr_start) / 1000));
  return res;
}

static esp_err_t stream_handler(httpd_req_t *req) {
  camera_fb_t *fb = NULL;
  struct timeval _timestamp;
  esp_err_t res = ESP_OK;
  size_t _jpg_buf_len = 0;
  uint8_t *_jpg_buf = NULL;
  char *part_buf[128];

  static int64_t last_frame = 0;
  if (!last_frame) {
    last_frame = esp_timer_get_time();
  }

  res = httpd_resp_set_type(req, _STREAM_CONTENT_TYPE);
  if (res != ESP_OK) {
    return res;
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "X-Framerate", "60");

#if CONFIG_LED_ILLUMINATOR_ENABLED
  isStreaming = true;
  enable_led(true);
#endif

  while (true) {
    fb = esp_camera_fb_get();
    if (!fb) {
      log_e("Camera capture failed");
      res = ESP_FAIL;
    } else {
      _timestamp.tv_sec = fb->timestamp.tv_sec;
      _timestamp.tv_usec = fb->timestamp.tv_usec;
      if (fb->format != PIXFORMAT_JPEG) {
        bool jpeg_converted = frame2jpg(fb, 80, &_jpg_buf, &_jpg_buf_len);
        esp_camera_fb_return(fb);
        fb = NULL;
        if (!jpeg_converted) {
          log_e("JPEG compression failed");
          res = ESP_FAIL;
        }
      } else {
        _jpg_buf_len = fb->len;
        _jpg_buf = fb->buf;
      }
    }
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, _STREAM_BOUNDARY, strlen(_STREAM_BOUNDARY));
    }
    if (res == ESP_OK) {
      size_t hlen = snprintf((char *)part_buf, 128, _STREAM_PART, _jpg_buf_len, _timestamp.tv_sec, _timestamp.tv_usec);
      res = httpd_resp_send_chunk(req, (const char *)part_buf, hlen);
    }
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, (const char *)_jpg_buf, _jpg_buf_len);
    }
    if (fb) {
      esp_camera_fb_return(fb);
      fb = NULL;
      _jpg_buf = NULL;
    } else if (_jpg_buf) {
      free(_jpg_buf);
      _jpg_buf = NULL;
    }
    if (res != ESP_OK) {
      log_e("Send frame failed");
      break;
    }
    int64_t fr_end = esp_timer_get_time();

    int64_t frame_time = fr_end - last_frame;
    last_frame = fr_end;

    frame_time /= 1000;
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
    uint32_t avg_frame_time = ra_filter_run(&ra_filter, frame_time);
#endif
    log_i(
      "MJPG: %uB %ums (%.1ffps), AVG: %ums (%.1ffps)", (uint32_t)(_jpg_buf_len), (uint32_t)frame_time, 1000.0 / (uint32_t)frame_time, avg_frame_time,
      1000.0 / avg_frame_time
    );
  }

#if CONFIG_LED_ILLUMINATOR_ENABLED
  isStreaming = false;
  enable_led(false);
#endif

  return res;
}

// ============================================================
// Original control/status/register handlers (unchanged)
// ============================================================

static esp_err_t parse_get(httpd_req_t *req, char **obuf) {
  char *buf = NULL;
  size_t buf_len = 0;

  buf_len = httpd_req_get_url_query_len(req) + 1;
  if (buf_len > 1) {
    buf = (char *)malloc(buf_len);
    if (!buf) {
      httpd_resp_send_500(req);
      return ESP_FAIL;
    }
    if (httpd_req_get_url_query_str(req, buf, buf_len) == ESP_OK) {
      *obuf = buf;
      return ESP_OK;
    }
    free(buf);
  }
  httpd_resp_send_404(req);
  return ESP_FAIL;
}

static esp_err_t cmd_handler(httpd_req_t *req) {
  char *buf = NULL;
  char variable[32];
  char value[32];

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }
  if (httpd_query_key_value(buf, "var", variable, sizeof(variable)) != ESP_OK || httpd_query_key_value(buf, "val", value, sizeof(value)) != ESP_OK) {
    free(buf);
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  free(buf);

  int val = atoi(value);
  log_i("%s = %d", variable, val);
  sensor_t *s = esp_camera_sensor_get();
  int res = 0;

  if (!strcmp(variable, "framesize")) {
    if (s->pixformat == PIXFORMAT_JPEG) {
      res = s->set_framesize(s, (framesize_t)val);
    }
  } else if (!strcmp(variable, "quality")) {
    res = s->set_quality(s, val);
  } else if (!strcmp(variable, "contrast")) {
    res = s->set_contrast(s, val);
  } else if (!strcmp(variable, "brightness")) {
    res = s->set_brightness(s, val);
  } else if (!strcmp(variable, "saturation")) {
    res = s->set_saturation(s, val);
  } else if (!strcmp(variable, "gainceiling")) {
    res = s->set_gainceiling(s, (gainceiling_t)val);
  } else if (!strcmp(variable, "colorbar")) {
    res = s->set_colorbar(s, val);
  } else if (!strcmp(variable, "awb")) {
    res = s->set_whitebal(s, val);
  } else if (!strcmp(variable, "agc")) {
    res = s->set_gain_ctrl(s, val);
  } else if (!strcmp(variable, "aec")) {
    res = s->set_exposure_ctrl(s, val);
  } else if (!strcmp(variable, "hmirror")) {
    res = s->set_hmirror(s, val);
  } else if (!strcmp(variable, "vflip")) {
    res = s->set_vflip(s, val);
  } else if (!strcmp(variable, "awb_gain")) {
    res = s->set_awb_gain(s, val);
  } else if (!strcmp(variable, "agc_gain")) {
    res = s->set_agc_gain(s, val);
  } else if (!strcmp(variable, "aec_value")) {
    res = s->set_aec_value(s, val);
  } else if (!strcmp(variable, "aec2")) {
    res = s->set_aec2(s, val);
  } else if (!strcmp(variable, "dcw")) {
    res = s->set_dcw(s, val);
  } else if (!strcmp(variable, "bpc")) {
    res = s->set_bpc(s, val);
  } else if (!strcmp(variable, "wpc")) {
    res = s->set_wpc(s, val);
  } else if (!strcmp(variable, "raw_gma")) {
    res = s->set_raw_gma(s, val);
  } else if (!strcmp(variable, "lenc")) {
    res = s->set_lenc(s, val);
  } else if (!strcmp(variable, "special_effect")) {
    res = s->set_special_effect(s, val);
  } else if (!strcmp(variable, "wb_mode")) {
    res = s->set_wb_mode(s, val);
  } else if (!strcmp(variable, "ae_level")) {
    res = s->set_ae_level(s, val);
  }
  else if (!strcmp(variable, "af")) {
      // 单次自动对焦: 写 0x3022=0x03, 和原 loop 触发动作一致。
      // AF 走 SCCB, 与抓图共用总线, PC 端已限流 >1s, 勿高频调。
      res = s->set_reg(s, 0x3022, 0xff, 0x03);

}
 
#if CONFIG_LED_ILLUMINATOR_ENABLED
  else if (!strcmp(variable, "led_intensity")) {
    led_duty = val;
    if (isStreaming) {
      enable_led(true);
    }
  }
#endif
  else {
    log_i("Unknown command: %s", variable);
    res = -1;
  }

  if (res < 0) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static int print_reg(char *p, sensor_t *s, uint16_t reg, uint32_t mask) {
  return sprintf(p, "\"0x%x\":%u,", reg, s->get_reg(s, reg, mask));
}

static esp_err_t status_handler(httpd_req_t *req) {
  static char json_response[1024];

  sensor_t *s = esp_camera_sensor_get();
  char *p = json_response;
  *p++ = '{';

  // Audio status (Phase A: audio_mode removed, always concurrent)
  p += sprintf(p, "\"audio_streaming\":%s,", g_audio_streaming ? "true" : "false");

  if (s != NULL) {
    if (s->id.PID == OV5640_PID || s->id.PID == OV3660_PID) {
      for (int reg = 0x3400; reg < 0x3406; reg += 2) {
        p += print_reg(p, s, reg, 0xFFF);
      }
      p += print_reg(p, s, 0x3406, 0xFF);

      p += print_reg(p, s, 0x3500, 0xFFFF0);
      p += print_reg(p, s, 0x3503, 0xFF);
      p += print_reg(p, s, 0x350a, 0x3FF);
      p += print_reg(p, s, 0x350c, 0xFFFF);

      for (int reg = 0x5480; reg <= 0x5490; reg++) {
        p += print_reg(p, s, reg, 0xFF);
      }

      for (int reg = 0x5380; reg <= 0x538b; reg++) {
        p += print_reg(p, s, reg, 0xFF);
      }

      for (int reg = 0x5580; reg < 0x558a; reg++) {
        p += print_reg(p, s, reg, 0xFF);
      }
      p += print_reg(p, s, 0x558a, 0x1FF);
    } else if (s->id.PID == OV2640_PID) {
      p += print_reg(p, s, 0xd3, 0xFF);
      p += print_reg(p, s, 0x111, 0xFF);
      p += print_reg(p, s, 0x132, 0xFF);
    }

    p += sprintf(p, "\"xclk\":%u,", s->xclk_freq_hz / 1000000);
    p += sprintf(p, "\"pixformat\":%u,", s->pixformat);
    p += sprintf(p, "\"framesize\":%u,", s->status.framesize);
    p += sprintf(p, "\"quality\":%u,", s->status.quality);
    p += sprintf(p, "\"brightness\":%d,", s->status.brightness);
    p += sprintf(p, "\"contrast\":%d,", s->status.contrast);
    p += sprintf(p, "\"saturation\":%d,", s->status.saturation);
    p += sprintf(p, "\"sharpness\":%d,", s->status.sharpness);
    p += sprintf(p, "\"special_effect\":%u,", s->status.special_effect);
    p += sprintf(p, "\"wb_mode\":%u,", s->status.wb_mode);
    p += sprintf(p, "\"awb\":%u,", s->status.awb);
    p += sprintf(p, "\"awb_gain\":%u,", s->status.awb_gain);
    p += sprintf(p, "\"aec\":%u,", s->status.aec);
    p += sprintf(p, "\"aec2\":%u,", s->status.aec2);
    p += sprintf(p, "\"ae_level\":%d,", s->status.ae_level);
    p += sprintf(p, "\"aec_value\":%u,", s->status.aec_value);
    p += sprintf(p, "\"agc\":%u,", s->status.agc);
    p += sprintf(p, "\"agc_gain\":%u,", s->status.agc_gain);
    p += sprintf(p, "\"gainceiling\":%u,", s->status.gainceiling);
    p += sprintf(p, "\"bpc\":%u,", s->status.bpc);
    p += sprintf(p, "\"wpc\":%u,", s->status.wpc);
    p += sprintf(p, "\"raw_gma\":%u,", s->status.raw_gma);
    p += sprintf(p, "\"lenc\":%u,", s->status.lenc);
    p += sprintf(p, "\"hmirror\":%u,", s->status.hmirror);
    p += sprintf(p, "\"dcw\":%u,", s->status.dcw);
    p += sprintf(p, "\"colorbar\":%u", s->status.colorbar);
  } else {
    // Camera not available (audio mode)
    p += sprintf(p, "\"camera\":\"unavailable\"");
  }

#if CONFIG_LED_ILLUMINATOR_ENABLED
  p += sprintf(p, ",\"led_intensity\":%u", led_duty);
#else
  p += sprintf(p, ",\"led_intensity\":%d", -1);
#endif
  *p++ = '}';
  *p++ = 0;
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json_response, strlen(json_response));
}

static esp_err_t xclk_handler(httpd_req_t *req) {
  char *buf = NULL;
  char _xclk[32];

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }
  if (httpd_query_key_value(buf, "xclk", _xclk, sizeof(_xclk)) != ESP_OK) {
    free(buf);
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  free(buf);

  int xclk = atoi(_xclk);
  log_i("Set XCLK: %d MHz", xclk);

  sensor_t *s = esp_camera_sensor_get();
  int res = s->set_xclk(s, LEDC_TIMER_0, xclk);
  if (res) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static esp_err_t reg_handler(httpd_req_t *req) {
  char *buf = NULL;
  char _reg[32];
  char _mask[32];
  char _val[32];

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }
  if (httpd_query_key_value(buf, "reg", _reg, sizeof(_reg)) != ESP_OK || httpd_query_key_value(buf, "mask", _mask, sizeof(_mask)) != ESP_OK
      || httpd_query_key_value(buf, "val", _val, sizeof(_val)) != ESP_OK) {
    free(buf);
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  free(buf);

  int reg = atoi(_reg);
  int mask = atoi(_mask);
  int val = atoi(_val);
  log_i("Set Register: reg: 0x%02x, mask: 0x%02x, value: 0x%02x", reg, mask, val);

  sensor_t *s = esp_camera_sensor_get();
  int res = s->set_reg(s, reg, mask, val);
  if (res) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static esp_err_t greg_handler(httpd_req_t *req) {
  char *buf = NULL;
  char _reg[32];
  char _mask[32];

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }
  if (httpd_query_key_value(buf, "reg", _reg, sizeof(_reg)) != ESP_OK || httpd_query_key_value(buf, "mask", _mask, sizeof(_mask)) != ESP_OK) {
    free(buf);
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  free(buf);

  int reg = atoi(_reg);
  int mask = atoi(_mask);
  sensor_t *s = esp_camera_sensor_get();
  int res = s->get_reg(s, reg, mask);
  if (res < 0) {
    return httpd_resp_send_500(req);
  }
  log_i("Get Register: reg: 0x%02x, mask: 0x%02x, value: 0x%02x", reg, mask, res);

  char buffer[20];
  const char *val = itoa(res, buffer, 10);
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, val, strlen(val));
}

static int parse_get_var(char *buf, const char *key, int def) {
  char _int[16];
  if (httpd_query_key_value(buf, key, _int, sizeof(_int)) != ESP_OK) {
    return def;
  }
  return atoi(_int);
}

static esp_err_t pll_handler(httpd_req_t *req) {
  char *buf = NULL;

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }

  int bypass = parse_get_var(buf, "bypass", 0);
  int mul = parse_get_var(buf, "mul", 0);
  int sys = parse_get_var(buf, "sys", 0);
  int root = parse_get_var(buf, "root", 0);
  int pre = parse_get_var(buf, "pre", 0);
  int seld5 = parse_get_var(buf, "seld5", 0);
  int pclken = parse_get_var(buf, "pclken", 0);
  int pclk = parse_get_var(buf, "pclk", 0);
  free(buf);

  log_i("Set Pll: bypass: %d, mul: %d, sys: %d, root: %d, pre: %d, seld5: %d, pclken: %d, pclk: %d", bypass, mul, sys, root, pre, seld5, pclken, pclk);
  sensor_t *s = esp_camera_sensor_get();
  int res = s->set_pll(s, bypass, mul, sys, root, pre, seld5, pclken, pclk);
  if (res) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static esp_err_t win_handler(httpd_req_t *req) {
  char *buf = NULL;

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }

  int startX = parse_get_var(buf, "sx", 0);
  int startY = parse_get_var(buf, "sy", 0);
  int endX = parse_get_var(buf, "ex", 0);
  int endY = parse_get_var(buf, "ey", 0);
  int offsetX = parse_get_var(buf, "offx", 0);
  int offsetY = parse_get_var(buf, "offy", 0);
  int totalX = parse_get_var(buf, "tx", 0);
  int totalY = parse_get_var(buf, "ty", 0);
  int outputX = parse_get_var(buf, "ox", 0);
  int outputY = parse_get_var(buf, "oy", 0);
  bool scale = parse_get_var(buf, "scale", 0) == 1;
  bool binning = parse_get_var(buf, "binning", 0) == 1;
  free(buf);

  log_i(
    "Set Window: Start: %d %d, End: %d %d, Offset: %d %d, Total: %d %d, Output: %d %d, Scale: %u, Binning: %u", startX, startY, endX, endY, offsetX, offsetY,
    totalX, totalY, outputX, outputY, scale, binning
  );
  sensor_t *s = esp_camera_sensor_get();
  int res = s->set_res_raw(s, startX, startY, endX, endY, offsetX, offsetY, totalX, totalY, outputX, outputY, scale, binning);
  if (res) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static esp_err_t index_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html");
  httpd_resp_set_hdr(req, "Content-Encoding", "gzip");
  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    if (s->id.PID == OV3660_PID) {
      return httpd_resp_send(req, (const char *)index_ov3660_html_gz, index_ov3660_html_gz_len);
    } else if (s->id.PID == OV5640_PID) {
      return httpd_resp_send(req, (const char *)index_ov5640_html_gz, index_ov5640_html_gz_len);
    } else {
      return httpd_resp_send(req, (const char *)index_ov2640_html_gz, index_ov2640_html_gz_len);
    }
  } else {
    httpd_resp_set_type(req, "text/html");
    const char *html = "<html><body><h1>Camera initializing</h1>"
                       "<p>Sensor not ready yet. Please wait.</p></body></html>";
    return httpd_resp_send(req, html, HTTPD_RESP_USE_STRLEN);
  }
}

// ============================================================
// NEW: Audio control handlers
// ============================================================

/**
 * GET /audio_begin
 * Phase A: Deprecated (backward compatibility). Camera + PDM now run concurrently, no switching needed.
 */
static esp_err_t audio_begin_handler(httpd_req_t *req) {
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_type(req, "text/plain");
  Serial.println("[AUDIO_BEGIN] (no-op in Phase A concurrent mode)");
  return httpd_resp_send(req, "ok (concurrent mode, no action needed)", HTTPD_RESP_USE_STRLEN);
}

/**
 * GET /audio_end
 * Phase A: Deprecated (backward compatibility). Camera + PDM now run concurrently, no switching needed.
 */
static esp_err_t audio_end_handler(httpd_req_t *req) {
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_type(req, "text/plain");
  Serial.println("[AUDIO_END] (no-op in Phase A concurrent mode)");
  return httpd_resp_send(req, "ok (concurrent mode, no action needed)", HTTPD_RESP_USE_STRLEN);
}

/**
 * WebSocket handler for /ws_audio
 * When PC connects to this endpoint, ESP32 starts pushing PCM16 audio data
 *
 * Data format: 16kHz, mono, PCM16 little-endian, binary frames
 */
static esp_err_t ws_audio_handler(httpd_req_t *req) {
  // WebSocket handshake (first call, method == HTTP_GET)
  if (req->method == HTTP_GET) {
    // Reject new connection if client already connected
    if (g_ws_audio_fd >= 0 || g_audio_streaming) {
      Serial.println("[WS_AUDIO] Rejected: another client already connected");
      httpd_resp_set_status(req, "409 Conflict");
      httpd_resp_set_type(req, "text/plain");
      return httpd_resp_send(req, "Another audio client already connected", HTTPD_RESP_USE_STRLEN);
    }

    g_ws_audio_fd = httpd_req_to_sockfd(req);
    Serial.printf("[WS_AUDIO] Client connected, fd=%d\n", g_ws_audio_fd);

    ws_tune_socket(g_ws_audio_fd);   // ← 新增

    // Start audio streaming task (Core 1, priority 5)
    g_audio_streaming = true;
    BaseType_t ok = xTaskCreatePinnedToCore(
      audio_stream_task,  // Task function
      "audio_ws",         // Name
      8192,               // Stack size
      NULL,               // Parameters
      5,                  // Priority
      (TaskHandle_t *)&g_audio_task, // Handle (cast to remove volatile)
      1                   // Pin to Core 1 (Core 0 for WiFi/network)
    );

    if (ok != pdPASS) {
      Serial.printf("[WS_AUDIO] Failed to create task! err=%d, heap=%u\n",
                    ok, (unsigned)ESP.getFreeHeap());
      g_audio_streaming = false;
      g_ws_audio_fd = -1;
      return ESP_FAIL;
    }

    Serial.println("[WS_AUDIO] Streaming task launched");
    return ESP_OK;
  }

  // Handle WS frames from client (we don't expect to receive data, but need to handle close/ping)
  httpd_ws_frame_t pkt;
  memset(&pkt, 0, sizeof(pkt));
  pkt.type = HTTPD_WS_TYPE_BINARY;

  // Read header first
  esp_err_t ret = httpd_ws_recv_frame(req, &pkt, 0);
  if (ret != ESP_OK) {
    Serial.printf("[WS_AUDIO] recv_frame error: 0x%x, client may have disconnected\n", ret);
    g_audio_streaming = false;
    g_ws_audio_fd = -1;
    return ret;
  }

  // CLOSE frame
  if (pkt.type == HTTPD_WS_TYPE_CLOSE) {
    Serial.println("[WS_AUDIO] Client sent CLOSE frame");
    g_audio_streaming = false;
    g_ws_audio_fd = -1;
  }

  // If client sent text/data, we don't process it, just discard
  if (pkt.len > 0) {
    uint8_t *temp = (uint8_t *)malloc(pkt.len);
    if (temp) {
      pkt.payload = temp;
      httpd_ws_recv_frame(req, &pkt, pkt.len);
      free(temp);
    }
  }

  return ESP_OK;
}

// ============================================================
// WebSocket handler for /ws_audio_v2 (v5 BUFFERED)
// Wire format per BINARY frame:
//   [seq:u32 LE][ts_ms:u32 LE][n_samples:u16 LE][drops:u16 LE][PCM16 x n_samples]
// Packet cadence: 1 frame per 20ms (50 Hz)
// ============================================================
static esp_err_t ws_audio_v2_handler(httpd_req_t *req) {
  if (req->method == HTTP_GET) {
    if (g_ws_audio_fd >= 0 || g_audio_streaming) {
      Serial.println("[WS_AUDIO_V2] Rejected: another audio client already connected");
      httpd_resp_set_status(req, "409 Conflict");
      httpd_resp_set_type(req, "text/plain");
      return httpd_resp_send(req, "Another audio client already connected", HTTPD_RESP_USE_STRLEN);
    }

    if (!audio_ring_init()) {
      Serial.println("[WS_AUDIO_V2] Ring buffer init failed");
      httpd_resp_set_status(req, "500 Internal Server Error");
      return httpd_resp_send(req, "Ring buffer alloc failed", HTTPD_RESP_USE_STRLEN);
    }

    g_ws_audio_fd     = httpd_req_to_sockfd(req);
    g_audio_streaming = true;
    Serial.printf("[WS_AUDIO_V2] Client connected, fd=%d\n", g_ws_audio_fd);

    ws_tune_socket(g_ws_audio_fd);   // ← 新增：TCP_NODELAY / SNDBUF / KEEPALIVE

    // Producer on Core 1 (near PDM)
    BaseType_t ok1 = xTaskCreatePinnedToCore(
      audio_capture_task, "aud_cap", 4096, NULL, 5,
      (TaskHandle_t *)&g_audio_capture_task_h, 1);

    if (ok1 != pdPASS) {
      Serial.printf("[WS_AUDIO_V2] Capture task create failed err=%d heap=%u\n",
                    ok1, (unsigned)ESP.getFreeHeap());
      g_audio_streaming = false;
      g_ws_audio_fd     = -1;
      audio_ring_deinit();
      return ESP_FAIL;
    }

  



    // Consumer on Core 0 (near WiFi stack → WS send blocks don't stall PDM)
    BaseType_t ok2 = xTaskCreatePinnedToCore(
      audio_sender_task, "aud_snd", 6144, NULL, 6,   //优先级4改成了6
      (TaskHandle_t *)&g_audio_sender_task_h, 0);

    if (ok2 != pdPASS) {
      Serial.printf("[WS_AUDIO_V2] Sender task create failed err=%d heap=%u\n",
                    ok2, (unsigned)ESP.getFreeHeap());
      g_audio_streaming = false;
      g_ws_audio_fd     = -1;
      // capture task will self-exit on flag
      return ESP_FAIL;
    }

    Serial.println("[WS_AUDIO_V2] Capture+Sender tasks launched (v5 buffered)");
    return ESP_OK;
  }

  // Non-handshake: handle incoming frames (expect only CLOSE)
  httpd_ws_frame_t pkt;
  memset(&pkt, 0, sizeof(pkt));
  pkt.type = HTTPD_WS_TYPE_BINARY;

  esp_err_t ret = httpd_ws_recv_frame(req, &pkt, 0);
  if (ret != ESP_OK) {
    Serial.printf("[WS_AUDIO_V2] recv_frame error: 0x%x, client disconnected\n", ret);
    g_audio_streaming = false;
    g_ws_audio_fd     = -1;
    return ret;
  }

  if (pkt.type == HTTPD_WS_TYPE_CLOSE) {
    Serial.println("[WS_AUDIO_V2] Client sent CLOSE frame");
    g_audio_streaming = false;
    g_ws_audio_fd     = -1;
  }

  if (pkt.len > 0) {
    uint8_t *temp = (uint8_t *)malloc(pkt.len);
    if (temp) {
      pkt.payload = temp;
      httpd_ws_recv_frame(req, &pkt, pkt.len);
      free(temp);
    }
  }
  return ESP_OK;
}


// ============================================================
  // WS socket 调优：降低小包延迟、加大在途窗口、启用 keepalive
  // 对 WAIC 这种高抖动 WiFi 环境尤其重要。
  // ============================================================
  static void ws_tune_socket(int fd) {
    if (fd < 0) return;

    int one = 1;
    // 关闭 Nagle：音频 652B 小包不再等合并，直接发
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    // 加大发送缓冲，允许更多在途数据（默认一般 5744B）
    int sndbuf = 32 * 1024;
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    // TCP keepalive：5s 无流量开始探测，每 2s 一次，3 次失败判死(≈11s)
    int ka = 1, idle = 5, intvl = 2, cnt = 3;
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &ka,    sizeof(ka));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE,  &idle,  sizeof(idle));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT,   &cnt,   sizeof(cnt));

    Serial.printf("[WS] socket tuned: fd=%d NODELAY=1 SNDBUF=%d KA=%d/%d/%d\n",
                  fd, sndbuf, idle, intvl, cnt);
  }



// ============================================================
// Server startup
// ============================================================
void startCameraServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.max_uri_handlers   = 20;    // Increased to accommodate new endpoints
  config.task_priority      = 6;     // 默认 5，略高于 sender，避免互相阻塞
  config.stack_size         = 8192;  // 默认 4096，防 WS 处理爆栈
  config.lru_purge_enable   = true;  // 连接过多时自动淘汰最老
  config.recv_wait_timeout  = 10;    // 秒
  config.send_wait_timeout  = 10;    // 秒

  // ---- URI definitions ----

  httpd_uri_t index_uri = {
    .uri = "/",
    .method = HTTP_GET,
    .handler = index_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t status_uri = {
    .uri = "/status",
    .method = HTTP_GET,
    .handler = status_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t cmd_uri = {
    .uri = "/control",
    .method = HTTP_GET,
    .handler = cmd_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t capture_uri = {
    .uri = "/capture",
    .method = HTTP_GET,
    .handler = capture_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t stream_uri = {
    .uri = "/stream",
    .method = HTTP_GET,
    .handler = stream_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t bmp_uri = {
    .uri = "/bmp",
    .method = HTTP_GET,
    .handler = bmp_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t xclk_uri = {
    .uri = "/xclk",
    .method = HTTP_GET,
    .handler = xclk_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t reg_uri = {
    .uri = "/reg",
    .method = HTTP_GET,
    .handler = reg_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t greg_uri = {
    .uri = "/greg",
    .method = HTTP_GET,
    .handler = greg_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t pll_uri = {
    .uri = "/pll",
    .method = HTTP_GET,
    .handler = pll_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t win_uri = {
    .uri = "/resolution",
    .method = HTTP_GET,
    .handler = win_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  // ---- Audio endpoints ----

  httpd_uri_t audio_begin_uri = {
    .uri = "/audio_begin",
    .method = HTTP_GET,
    .handler = audio_begin_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t audio_end_uri = {
    .uri = "/audio_end",
    .method = HTTP_GET,
    .handler = audio_end_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t ws_audio_uri = {
    .uri = "/ws_audio",
    .method = HTTP_GET,
    .handler = ws_audio_handler,
    .user_ctx = NULL,
    .is_websocket = true,             // Critical: enable WebSocket
    .handle_ws_control_frames = false, // Let httpd handle ping/pong automatically
    .supported_subprotocol = NULL
  };

  // v5 BUFFERED: ring-buffered + timestamped audio (for duplex_v5 bridge)
  httpd_uri_t ws_audio_v2_uri = {
    .uri = "/ws_audio_v2",
    .method = HTTP_GET,
    .handler = ws_audio_v2_handler,
    .user_ctx = NULL,
    .is_websocket = true,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  // ---- Start server ----

  ra_filter_init(&ra_filter, 20);

  log_i("Starting web server on port: '%d'", config.server_port);
  if (httpd_start(&camera_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(camera_httpd, &index_uri);
    httpd_register_uri_handler(camera_httpd, &cmd_uri);
    httpd_register_uri_handler(camera_httpd, &status_uri);
    httpd_register_uri_handler(camera_httpd, &capture_uri);
    httpd_register_uri_handler(camera_httpd, &bmp_uri);

    httpd_register_uri_handler(camera_httpd, &xclk_uri);
    httpd_register_uri_handler(camera_httpd, &reg_uri);
    httpd_register_uri_handler(camera_httpd, &greg_uri);
    httpd_register_uri_handler(camera_httpd, &pll_uri);
    httpd_register_uri_handler(camera_httpd, &win_uri);

    // Register audio endpoints
    httpd_register_uri_handler(camera_httpd, &audio_begin_uri);
    httpd_register_uri_handler(camera_httpd, &audio_end_uri);
    httpd_register_uri_handler(camera_httpd, &ws_audio_uri);
    httpd_register_uri_handler(camera_httpd, &ws_audio_v2_uri);

    Serial.println("[HTTP] Registered: / /capture /bmp /control /status + /audio_begin /audio_end /ws_audio /ws_audio_v2");
  }

  config.server_port += 1;
  config.ctrl_port += 1;
  log_i("Starting stream server on port: '%d'", config.server_port);
  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
    Serial.println("[HTTP] Registered stream on port 81: /stream");
  }
}

void setupLedFlash(int pin) {
#if CONFIG_LED_ILLUMINATOR_ENABLED
  ledcAttach(pin, 5000, 8);
#else
  log_i("LED flash is disabled -> CONFIG_LED_ILLUMINATOR_ENABLED = 0");
#endif
}
