package org.opensqz.openglass.rokid

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.ImageFormat
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.ImageReader
import android.media.MediaRecorder
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.util.Log
import android.util.Range
import android.util.Size
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.Surface
import android.view.View
import android.view.WindowManager
import android.widget.TextView
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.nio.ByteBuffer
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.abs
import kotlin.math.pow
import kotlin.math.sqrt

private const val TAG = "OpenGlassRokid"
private const val SAMPLE_RATE = 16_000
private const val CHUNK_SAMPLES = 640
private const val CHUNK_BYTES = CHUNK_SAMPLES * 2
private const val IMAGE_INTERVAL_MS = 1_000L
private const val STILL_CAPTURE_TIMEOUT_MS = 5_000L
private const val JPEG_QUALITY = 95
private const val HIGH_RES_CAPTURE_FOR_DOWNSAMPLE = false
private const val UPLOAD_TARGET_WIDTH = 720
private const val UPLOAD_TARGET_HEIGHT = 1280
private const val AE_TARGET_EV = 2.0
private const val MANUAL_EXPOSURE_ENABLED = true
private const val MANUAL_EXPOSURE_NS = 22_222_222L
private const val MANUAL_FRAME_DURATION_NS = 44_444_444L
private const val MANUAL_ISO = 1600
private const val USB_REVERSE_BASE_URL = "http://127.0.0.1:18080"
private const val USB_REVERSE_WS_URL = "ws://127.0.0.1:18080/rokid/audio"
private const val ENHANCE_IMAGE_FOR_UPLOAD = false
private const val ENHANCE_MIN_LUMA = 55.0
private const val ENHANCE_TARGET_LUMA = 70.0

class MainActivity : Activity() {
    private var pcBaseUrl = BuildConfig.PC_BASE_URL
    private var pcWsUrl = BuildConfig.PC_WS_URL

    private fun configurePcAddress() {
        val prefs = getSharedPreferences("pc_connection", Context.MODE_PRIVATE)
        val supplied = intent.getStringExtra("pc_base_url")
        val candidate = supplied ?: prefs.getString("pc_base_url", BuildConfig.PC_BASE_URL)!!
        try {
            val uri = java.net.URI(candidate.trim().trimEnd('/'))
            require(uri.scheme == "http" && !uri.host.isNullOrBlank()
                && uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null
                && uri.rawPath.isNullOrEmpty() && (uri.port == -1 || uri.port in 1..65535))
            pcBaseUrl = uri.toString()
            pcWsUrl = "ws://${uri.rawAuthority}/rokid/audio"
            if (supplied != null) prefs.edit().putString("pc_base_url", pcBaseUrl).apply()
            Log.i(TAG, "PC_CONFIG applied pcBase=$pcBaseUrl pcWs=$pcWsUrl")
        } catch (e: Exception) {
            Log.e(TAG, "Invalid PC address; keeping built-in default", e)
        }
    }

    private lateinit var hud: TextView
    private val mainHandler = Handler(Looper.getMainLooper())

    @Volatile private var running = false
    @Volatile private var wsOpen = false
    @Volatile private var lastError = "none"
    @Volatile private var lastRms = 0.0
    @Volatile private var lastPeak = 0
    @Volatile private var lastNonZero = 0
    @Volatile private var audioSourceText = "none"
    @Volatile private var cameraStatus = "idle"
    @Volatile private var audioStatus = "idle"
    @Volatile private var imageSizeText = "none"
    @Volatile private var audioWsUrlText = "none"
    @Volatile private var imagePostBaseText = "none"
    @Volatile private var imageCaptureStartedAtMs = 0L
    @Volatile private var startMs = 0L

    private val audioPackets = AtomicInteger(0)
    private val audioDrops = AtomicInteger(0)
    private val imagePackets = AtomicInteger(0)
    private val imageFailures = AtomicInteger(0)
    private val imageInFlight = AtomicBoolean(false)
    private val imageUploadInFlight = AtomicBoolean(false)
    private val cameraRestartPending = AtomicBoolean(false)

    private var audioThread: Thread? = null
    private var audioWs: SimpleBinaryWebSocket? = null
    private var reconnectDelayMs = 1_000L
    private var audioUrlIndex = 0

    private var cameraThread: HandlerThread? = null
    private var cameraHandler: Handler? = null
    private var imageReader: ImageReader? = null
    private var previewTexture: SurfaceTexture? = null
    private var previewSurface: Surface? = null
    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var cameraCharacteristics: CameraCharacteristics? = null

    private val hudTick = object : Runnable {
        override fun run() {
            updateHud()
            mainHandler.postDelayed(this, 500)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        configurePcAddress()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.decorView.systemUiVisibility =
            View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY

        hud = TextView(this).apply {
            setBackgroundColor(Color.BLACK)
            setTextColor(Color.rgb(40, 255, 110))
            textSize = 15f
            typeface = android.graphics.Typeface.MONOSPACE
            setPadding(18, 18, 18, 18)
        }
        setContentView(hud)
        Log.i(TAG, "onCreate pcBase=$pcBaseUrl pcWs=$pcWsUrl")
        mainHandler.post(hudTick)

        if (hasPermissions()) {
            startAll()
        } else {
            requestPermissions(
                arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO),
                1001,
            )
        }
    }

    override fun onDestroy() {
        stopAll()
        mainHandler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    override fun onResume() {
        super.onResume()
        if (hasPermissions() && !running) {
            Log.i(TAG, "onResume auto-start")
            startAll()
        } else {
            scheduleCameraStart(800)
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) {
            scheduleCameraStart(500)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 1001 && hasPermissions()) {
            Log.i(TAG, "permissions granted; auto-start")
            startAll()
        } else {
            lastError = "permissions denied"
            Log.e(TAG, lastError)
        }
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        Log.i(
            TAG,
            "key action=${event.action} code=${event.keyCode} name=${KeyEvent.keyCodeToString(event.keyCode)}",
        )
        if (event.action == KeyEvent.ACTION_UP) {
            when (event.keyCode) {
                KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER -> {
                    toggleStartStop()
                    return true
                }
                KeyEvent.KEYCODE_BACK -> {
                    if (running) {
                        stopAll()
                        return true
                    }
                }
            }
        }
        return super.dispatchKeyEvent(event)
    }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        Log.i(TAG, "touch action=${ev.action} x=${ev.x} y=${ev.y}")
        if (ev.action == MotionEvent.ACTION_UP) {
            toggleStartStop()
            return true
        }
        return super.dispatchTouchEvent(ev)
    }

    private fun hasPermissions(): Boolean =
        checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun toggleStartStop() {
        if (running) stopAll() else startAll()
    }

    private fun startAll() {
        if (running) return
        running = true
        startMs = System.currentTimeMillis()
        lastError = "none"
        audioPackets.set(0)
        audioDrops.set(0)
        imagePackets.set(0)
        imageFailures.set(0)
        Log.i(TAG, "startAll")
        connectAudioWebSocket()
        startAudioCapture()
        scheduleCameraStart(1_200)
    }

    private fun scheduleCameraStart(delayMs: Long) {
        if (!running || cameraThread != null || cameraDevice != null || imageReader != null) return
        cameraStatus = "camera pending"
        mainHandler.postDelayed({
            if (running && cameraThread == null && cameraDevice == null && imageReader == null) {
                startCameraCapture()
            }
        }, delayMs)
    }

    private fun stopAll() {
        if (!running) return
        running = false
        Log.i(TAG, "stopAll")
        stopAudioCapture()
        closeAudioWebSocket()
        stopCameraCapture()
        audioStatus = "stopped"
        cameraStatus = "stopped"
    }

    private fun connectAudioWebSocket() {
        if (!running) return
        val urls = pcWsUrls()
        val url = urls[audioUrlIndex % urls.size]
        audioWsUrlText = url
        audioStatus = "ws connecting"
        Log.i(TAG, "audio WS connecting $url")
        val ws = SimpleBinaryWebSocket(
            url,
            object : SimpleBinaryWebSocket.Listener {
                override fun onOpen() {
                    wsOpen = true
                    reconnectDelayMs = 1_000L
                    audioStatus = "ws connected"
                    lastError = "none"
                    Log.i(TAG, "audio WS connected $url")
                }

                override fun onClosed(reason: String) {
                    wsOpen = false
                    audioStatus = "ws closed"
                    Log.w(TAG, "audio WS closed url=$url reason=$reason")
                    scheduleReconnect()
                }

                override fun onFailure(error: Throwable) {
                    wsOpen = false
                    audioStatus = "ws failed"
                    audioUrlIndex = (audioUrlIndex + 1) % urls.size
                    lastError = "audio ws ${error.javaClass.simpleName}: ${error.message}"
                    Log.e(TAG, "audio WS failed url=$url; next=${urls[audioUrlIndex % urls.size]}", error)
                    scheduleReconnect()
                }
            },
        )
        audioWs = ws
        ws.start()
    }

    private fun pcBaseUrls(): List<String> =
        listOf(pcBaseUrl, USB_REVERSE_BASE_URL).distinct()

    private fun pcWsUrls(): List<String> =
        listOf(pcWsUrl, USB_REVERSE_WS_URL).distinct()

    private fun scheduleReconnect() {
        if (!running) return
        val delay = reconnectDelayMs
        reconnectDelayMs = (reconnectDelayMs * 2).coerceAtMost(5_000L)
        mainHandler.postDelayed({ connectAudioWebSocket() }, delay)
    }

    private fun closeAudioWebSocket() {
        wsOpen = false
        audioWs?.close()
        audioWs = null
    }

    private fun startAudioCapture() {
        audioThread = Thread({
            val recorder = createAudioRecord()
            if (recorder == null) {
                audioStatus = "AudioRecord failed"
                return@Thread
            }
            val buf = ByteArray(CHUNK_BYTES)
            var lastLog = System.currentTimeMillis()
            try {
                recorder.startRecording()
                audioStatus = "recording"
                Log.i(TAG, "AudioRecord started")
                while (running) {
                    val n = recorder.read(buf, 0, buf.size)
                    if (n <= 0) {
                        audioDrops.incrementAndGet()
                        continue
                    }
                    val stats = stats16(buf, n)
                    lastRms = stats.rms
                    lastPeak = stats.peak
                    lastNonZero = stats.nonZero
                    val ws = audioWs
                    if (wsOpen && ws != null && ws.queuedBytes() < 512_000L) {
                        if (ws.sendBinary(buf, 0, n)) {
                            audioPackets.incrementAndGet()
                        } else {
                            audioDrops.incrementAndGet()
                        }
                    } else {
                        audioDrops.incrementAndGet()
                    }
                    val now = System.currentTimeMillis()
                    if (now - lastLog >= 1_000L) {
                        Log.i(
                            TAG,
                            "audio source=$audioSourceText pkt=${audioPackets.get()} drop=${audioDrops.get()} rms=${fmt(lastRms)} peak=$lastPeak nonzero=$lastNonZero wsOpen=$wsOpen",
                        )
                        lastLog = now
                    }
                }
            } catch (t: Throwable) {
                lastError = "audio ${t.javaClass.simpleName}: ${t.message}"
                Log.e(TAG, "AudioRecord loop failed", t)
            } finally {
                try {
                    recorder.stop()
                } catch (_: Throwable) {
                }
                recorder.release()
                Log.i(TAG, "AudioRecord stopped")
            }
        }, "rokid-audio").also { it.start() }
    }

    private fun createAudioRecord(): AudioRecord? {
        val minBuffer = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val bufferSize = maxOf(minBuffer, CHUNK_BYTES * 4)
        val sources = intArrayOf(
            MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            MediaRecorder.AudioSource.CAMCORDER,
            MediaRecorder.AudioSource.UNPROCESSED,
            MediaRecorder.AudioSource.VOICE_PERFORMANCE,
            MediaRecorder.AudioSource.DEFAULT,
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
        )
        for (source in sources) {
            try {
                val record = AudioRecord(
                    source,
                    SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufferSize,
                )
                if (record.state == AudioRecord.STATE_INITIALIZED) {
                    audioSourceText = audioSourceName(source)
                    Log.i(TAG, "AudioRecord initialized source=$audioSourceText buffer=$bufferSize min=$minBuffer")
                    return record
                }
                record.release()
            } catch (t: Throwable) {
                Log.w(TAG, "AudioRecord source=${audioSourceName(source)} failed", t)
            }
        }
        lastError = "AudioRecord init failed"
        Log.e(TAG, lastError)
        return null
    }

    private fun audioSourceName(source: Int): String =
        when (source) {
            MediaRecorder.AudioSource.DEFAULT -> "DEFAULT"
            MediaRecorder.AudioSource.MIC -> "MIC"
            MediaRecorder.AudioSource.CAMCORDER -> "CAMCORDER"
            MediaRecorder.AudioSource.VOICE_RECOGNITION -> "VOICE_RECOGNITION"
            MediaRecorder.AudioSource.VOICE_COMMUNICATION -> "VOICE_COMMUNICATION"
            MediaRecorder.AudioSource.UNPROCESSED -> "UNPROCESSED"
            MediaRecorder.AudioSource.VOICE_PERFORMANCE -> "VOICE_PERFORMANCE"
            else -> "source-$source"
        }

    private fun stopAudioCapture() {
        audioThread?.join(700)
        audioThread = null
    }

    private fun startCameraCapture() {
        if (cameraThread != null || cameraDevice != null || imageReader != null) return
        cameraThread = HandlerThread("rokid-camera").also { it.start() }
        cameraHandler = Handler(cameraThread!!.looper)
        val manager = getSystemService(Context.CAMERA_SERVICE) as CameraManager
        try {
            val selection = chooseCamera(manager)
            if (selection == null) {
                cameraStatus = "no camera"
                lastError = "no camera"
                Log.e(TAG, lastError)
                return
            }
            cameraCharacteristics = selection.characteristics
            imageSizeText = "${selection.size.width}x${selection.size.height}"
            imageReader = ImageReader.newInstance(
                selection.size.width,
                selection.size.height,
                ImageFormat.JPEG,
                2,
            ).also { reader ->
                reader.setOnImageAvailableListener({ r ->
                    val image = r.acquireLatestImage() ?: return@setOnImageAvailableListener
                    try {
                        val buffer: ByteBuffer = image.planes[0].buffer
                        val bytes = ByteArray(buffer.remaining())
                        buffer.get(bytes)
                        imageInFlight.set(false)
                        uploadJpeg(bytes)
                    } catch (t: Throwable) {
                        imageInFlight.set(false)
                        imageFailures.incrementAndGet()
                        lastError = "image ${t.javaClass.simpleName}: ${t.message}"
                        Log.e(TAG, "read JPEG failed", t)
                    } finally {
                        image.close()
                    }
                }, cameraHandler)
            }

            if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                lastError = "camera permission missing"
                return
            }
            cameraStatus = "opening ${selection.cameraId}"
            Log.i(TAG, "opening camera id=${selection.cameraId} size=$imageSizeText")
            manager.openCamera(
                selection.cameraId,
                object : CameraDevice.StateCallback() {
                    override fun onOpened(camera: CameraDevice) {
                        cameraDevice = camera
                        createSession(camera)
                    }

                    override fun onDisconnected(camera: CameraDevice) {
                        cameraStatus = "camera disconnected"
                        lastError = cameraStatus
                        Log.w(TAG, cameraStatus)
                        camera.close()
                        restartCameraCapture("camera disconnected")
                    }

                    override fun onError(camera: CameraDevice, error: Int) {
                        cameraStatus = "camera error $error"
                        lastError = cameraStatus
                        Log.e(TAG, cameraStatus)
                        camera.close()
                        restartCameraCapture(cameraStatus)
                    }
                },
                cameraHandler,
            )
        } catch (t: Throwable) {
            cameraStatus = "camera failed"
            lastError = "camera ${t.javaClass.simpleName}: ${t.message}"
            Log.e(TAG, "startCameraCapture failed", t)
        }
    }

    private fun createSession(camera: CameraDevice) {
        val jpegSurface = imageReader?.surface ?: return
        val texture = SurfaceTexture(0).also {
            it.setDefaultBufferSize(640, 480)
        }
        val preview = Surface(texture)
        previewTexture = texture
        previewSurface = preview
        try {
            camera.createCaptureSession(
                listOf(preview, jpegSurface),
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(session: CameraCaptureSession) {
                        captureSession = session
                        cameraStatus = "camera ready $imageSizeText"
                        Log.i(TAG, cameraStatus)
                        startRepeatingPreview(camera, session, preview)
                        scheduleStillCapture(1_200)
                    }

                    override fun onConfigureFailed(session: CameraCaptureSession) {
                        cameraStatus = "session configure failed"
                        lastError = cameraStatus
                        Log.e(TAG, cameraStatus)
                    }
                },
                cameraHandler,
            )
        } catch (t: Throwable) {
            lastError = "session ${t.javaClass.simpleName}: ${t.message}"
            Log.e(TAG, "createCaptureSession failed", t)
        }
    }

    private fun startRepeatingPreview(
        camera: CameraDevice,
        session: CameraCaptureSession,
        surface: Surface,
    ) {
        try {
            val req = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                addTarget(surface)
                applyCameraQuality(this, still = false)
            }
            session.setRepeatingRequest(req.build(), null, cameraHandler)
            Log.i(TAG, "preview repeating started for 3A convergence")
        } catch (t: Throwable) {
            Log.w(TAG, "preview repeating failed; still capture will continue", t)
        }
    }

    private fun scheduleStillCapture(delayMs: Long) {
        cameraHandler?.postDelayed({
            if (!running) return@postDelayed
            captureStill()
            scheduleStillCapture(IMAGE_INTERVAL_MS)
        }, delayMs)
    }

    private fun captureStill() {
        val camera = cameraDevice
        val session = captureSession
        val surface = imageReader?.surface
        if (camera == null || session == null || surface == null) {
            restartCameraCapture("camera component missing")
            return
        }
        if (!imageInFlight.compareAndSet(false, true)) {
            Log.w(TAG, "skip image capture: previous still in flight")
            return
        }
        val captureStartedAt = System.currentTimeMillis()
        imageCaptureStartedAtMs = captureStartedAt
        cameraHandler?.postDelayed({
            if (imageInFlight.get() && imageCaptureStartedAtMs == captureStartedAt) {
                imageInFlight.set(false)
                imageFailures.incrementAndGet()
                lastError = "still capture timeout $imageSizeText"
                cameraStatus = "still timeout $imageSizeText"
                Log.w(TAG, "still capture timeout; resetting in-flight for $imageSizeText")
                restartCameraCapture("still capture timeout")
            }
        }, STILL_CAPTURE_TIMEOUT_MS)
        try {
            val req = camera.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE).apply {
                addTarget(surface)
                applyCameraQuality(this, still = true)
                set(
                    CaptureRequest.JPEG_QUALITY,
                    JPEG_QUALITY.toByte(),
                )
            }
            session.capture(
                req.build(),
                object : CameraCaptureSession.CaptureCallback() {},
                cameraHandler,
            )
        } catch (t: Throwable) {
            imageInFlight.set(false)
            imageFailures.incrementAndGet()
            lastError = "capture ${t.javaClass.simpleName}: ${t.message}"
            Log.e(TAG, "captureStill failed", t)
            restartCameraCapture(lastError)
        }
    }

    private fun restartCameraCapture(reason: String, delayMs: Long = 800L) {
        if (!running) return
        if (!cameraRestartPending.compareAndSet(false, true)) return
        cameraStatus = "camera restarting"
        lastError = "camera restart: $reason"
        Log.w(TAG, "camera restart requested: $reason")
        mainHandler.post {
            try {
                stopCameraCapture()
            } catch (t: Throwable) {
                Log.w(TAG, "camera cleanup during restart failed", t)
            }
            mainHandler.postDelayed({
                cameraRestartPending.set(false)
                if (running) {
                    Log.i(TAG, "camera restart now")
                    startCameraCapture()
                }
            }, delayMs)
        }
    }

    private fun applyCameraQuality(req: CaptureRequest.Builder, still: Boolean) {
        val chars = cameraCharacteristics
        req.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
        req.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON)
        req.set(CaptureRequest.CONTROL_AE_LOCK, false)
        req.set(CaptureRequest.CONTROL_AWB_MODE, CaptureRequest.CONTROL_AWB_MODE_AUTO)
        req.set(
            CaptureRequest.CONTROL_CAPTURE_INTENT,
            if (still) CaptureRequest.CONTROL_CAPTURE_INTENT_STILL_CAPTURE
            else CaptureRequest.CONTROL_CAPTURE_INTENT_PREVIEW,
        )
        if (chars == null) {
            req.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_OFF)
            return
        }

        if (applyManualExposure(req, chars)) {
            applyFastProcessing(req, chars)
            return
        }

        chooseAeFpsRange(chars)?.let { req.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
        applyFastProcessing(req, chars)
        val range = chars.get(CameraCharacteristics.CONTROL_AE_COMPENSATION_RANGE)
        val step = chars.get(CameraCharacteristics.CONTROL_AE_COMPENSATION_STEP)
        if (range != null && step != null && step.toDouble() > 0.0) {
            val compensation = Math.round(AE_TARGET_EV / step.toDouble()).toInt()
                .coerceIn(range.lower, range.upper)
            req.set(CaptureRequest.CONTROL_AE_EXPOSURE_COMPENSATION, compensation)
        }
    }

    private fun applyManualExposure(
        req: CaptureRequest.Builder,
        chars: CameraCharacteristics,
    ): Boolean {
        if (!MANUAL_EXPOSURE_ENABLED) return false
        val caps = chars.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES)
        if (caps == null || !caps.contains(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_SENSOR)) {
            return false
        }
        val exposureRange = chars.get(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE)
        val sensitivityRange = chars.get(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)
        val maxFrameDuration = chars.get(CameraCharacteristics.SENSOR_INFO_MAX_FRAME_DURATION)
        val exposureNs = exposureRange?.clamp(MANUAL_EXPOSURE_NS) ?: MANUAL_EXPOSURE_NS
        val iso = sensitivityRange?.clamp(MANUAL_ISO) ?: MANUAL_ISO
        val frameDurationNs = maxOf(MANUAL_FRAME_DURATION_NS, exposureNs)
            .let { if (maxFrameDuration != null) minOf(it, maxFrameDuration) else it }
        req.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_OFF)
        req.set(CaptureRequest.SENSOR_EXPOSURE_TIME, exposureNs)
        req.set(CaptureRequest.SENSOR_SENSITIVITY, iso)
        req.set(CaptureRequest.SENSOR_FRAME_DURATION, frameDurationNs)
        return true
    }

    private fun applyFastProcessing(req: CaptureRequest.Builder, chars: CameraCharacteristics) {
        chooseMode(
            chars.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES),
            CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE,
            CaptureRequest.CONTROL_AF_MODE_OFF,
        )?.let { req.set(CaptureRequest.CONTROL_AF_MODE, it) }
        chooseMode(
            chars.get(CameraCharacteristics.NOISE_REDUCTION_AVAILABLE_NOISE_REDUCTION_MODES),
            CaptureRequest.NOISE_REDUCTION_MODE_FAST,
            CaptureRequest.NOISE_REDUCTION_MODE_HIGH_QUALITY,
        )?.let { req.set(CaptureRequest.NOISE_REDUCTION_MODE, it) }
        chooseMode(
            chars.get(CameraCharacteristics.EDGE_AVAILABLE_EDGE_MODES),
            CaptureRequest.EDGE_MODE_FAST,
            CaptureRequest.EDGE_MODE_HIGH_QUALITY,
        )?.let { req.set(CaptureRequest.EDGE_MODE, it) }
        chooseMode(
            chars.get(CameraCharacteristics.HOT_PIXEL_AVAILABLE_HOT_PIXEL_MODES),
            CaptureRequest.HOT_PIXEL_MODE_FAST,
            CaptureRequest.HOT_PIXEL_MODE_HIGH_QUALITY,
        )?.let { req.set(CaptureRequest.HOT_PIXEL_MODE, it) }
        chooseMode(
            chars.get(CameraCharacteristics.COLOR_CORRECTION_AVAILABLE_ABERRATION_MODES),
            CaptureRequest.COLOR_CORRECTION_ABERRATION_MODE_FAST,
            CaptureRequest.COLOR_CORRECTION_ABERRATION_MODE_HIGH_QUALITY,
        )?.let { req.set(CaptureRequest.COLOR_CORRECTION_ABERRATION_MODE, it) }
    }

    private fun chooseAeFpsRange(chars: CameraCharacteristics): Range<Int>? {
        val ranges = chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
            ?: return null
        return ranges.firstOrNull { it.lower == 15 && it.upper == 15 }
            ?: ranges.firstOrNull { it.lower <= 15 && it.upper <= 30 }
            ?: ranges.minWithOrNull { a, b ->
                val scoreA = abs(a.lower - 15) + abs(a.upper - 15)
                val scoreB = abs(b.lower - 15) + abs(b.upper - 15)
                scoreA - scoreB
            }
    }

    private fun chooseMode(modes: IntArray?, preferred: Int, fallback: Int): Int? {
        if (modes == null || modes.isEmpty()) return null
        return when {
            modes.contains(preferred) -> preferred
            modes.contains(fallback) -> fallback
            else -> null
        }
    }

    private fun uploadJpeg(jpeg: ByteArray) {
        if (!running) return
        if (!imageUploadInFlight.compareAndSet(false, true)) {
            imageFailures.incrementAndGet()
            Log.w(TAG, "drop image upload: previous POST still in flight")
            return
        }
        Thread({
            val prepareStart = System.currentTimeMillis()
            val downsampledJpeg = maybeDownsampleJpegForUpload(jpeg)
            val postJpeg = if (ENHANCE_IMAGE_FOR_UPLOAD) enhanceJpegForModel(downsampledJpeg) else downsampledJpeg
            val prepareMs = System.currentTimeMillis() - prepareStart
            var posted = false
            var lastFailure: String? = null
            for (baseUrl in pcBaseUrls()) {
                imagePostBaseText = baseUrl
                try {
                val postStart = System.currentTimeMillis()
                val conn = (URL("$baseUrl/rokid/image").openConnection() as HttpURLConnection).apply {
                    requestMethod = "POST"
                    connectTimeout = 1_500
                    readTimeout = 4_000
                    doOutput = true
                    setRequestProperty("Content-Type", "image/jpeg")
                    setRequestProperty("Content-Length", postJpeg.size.toString())
                }
                conn.outputStream.use { it.write(postJpeg) }
                val code = conn.responseCode
                conn.disconnect()
                if (code in 200..299) {
                    val count = imagePackets.incrementAndGet()
                    cameraStatus = "image ok $imageSizeText"
                    Log.i(
                        TAG,
                        "image POST ok base=$baseUrl count=$count bytes=${postJpeg.size} prepareMs=$prepareMs postMs=${System.currentTimeMillis() - postStart}",
                    )
                    posted = true
                    break
                } else {
                    lastFailure = "HTTP $code at $baseUrl"
                    Log.e(TAG, "image POST HTTP $code base=$baseUrl")
                }
                } catch (t: Throwable) {
                    lastFailure = "${t.javaClass.simpleName}: ${t.message} at $baseUrl"
                    Log.w(TAG, "image POST failed base=$baseUrl bytes=${postJpeg.size}", t)
                }
            }
            if (!posted) {
                imageFailures.incrementAndGet()
                lastError = "image post failed: $lastFailure"
                cameraStatus = "image post failed"
            }
            imageUploadInFlight.set(false)
        }, "rokid-image-post").start()
    }

    private fun maybeDownsampleJpegForUpload(jpeg: ByteArray): ByteArray {
        if (!HIGH_RES_CAPTURE_FOR_DOWNSAMPLE) return jpeg
        return try {
            val src = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size) ?: return jpeg
            val width = src.width
            val height = src.height
            if (width <= UPLOAD_TARGET_WIDTH && height <= UPLOAD_TARGET_HEIGHT) {
                src.recycle()
                return jpeg
            }
            val scale = minOf(
                UPLOAD_TARGET_WIDTH.toDouble() / width.toDouble(),
                UPLOAD_TARGET_HEIGHT.toDouble() / height.toDouble(),
            )
            val outWidth = (width * scale).toInt().coerceAtLeast(1)
            val outHeight = (height * scale).toInt().coerceAtLeast(1)
            val scaled = Bitmap.createScaledBitmap(src, outWidth, outHeight, true)
            val baos = ByteArrayOutputStream()
            scaled.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, baos)
            val out = baos.toByteArray()
            Log.i(TAG, "image downsampled ${width}x$height -> ${outWidth}x$outHeight bytes=${jpeg.size}->${out.size}")
            if (scaled != src) scaled.recycle()
            src.recycle()
            out
        } catch (t: Throwable) {
            Log.w(TAG, "image downsample failed; using original", t)
            jpeg
        }
    }

    private fun enhanceJpegForModel(jpeg: ByteArray): ByteArray {
        return try {
            val src = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size) ?: return jpeg
            val width = src.width
            val height = src.height
            val pixels = IntArray(width * height)
            src.getPixels(pixels, 0, width, 0, 0, width, height)

            var sum = 0.0
            var count = 0
            val stride = maxOf(1, pixels.size / 20_000)
            var i = 0
            while (i < pixels.size) {
                val c = pixels[i]
                sum += lumaOf(c)
                count += 1
                i += stride
            }
            val meanLuma = if (count == 0) 0.0 else sum / count
            if (meanLuma >= ENHANCE_MIN_LUMA) return jpeg

            val gain = (ENHANCE_TARGET_LUMA / meanLuma.coerceAtLeast(1.0)).coerceIn(1.0, 1.8)
            val gamma = when {
                meanLuma < 24.0 -> 0.70
                meanLuma < 40.0 -> 0.78
                else -> 0.86
            }

            for (p in pixels.indices) {
                val c = pixels[p]
                val r = enhanceChannel(Color.red(c), gain, gamma)
                val g = enhanceChannel(Color.green(c), gain, gamma)
                val b = enhanceChannel(Color.blue(c), gain, gamma)
                pixels[p] = Color.rgb(r, g, b)
            }

            val out = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
            out.setPixels(pixels, 0, width, 0, 0, width, height)
            val baos = ByteArrayOutputStream()
            out.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, baos)
            val enhanced = baos.toByteArray()
            Log.i(TAG, "image enhanced meanLuma=${fmt(meanLuma)} gain=${fmt(gain)} gamma=${fmt(gamma)} bytes=${jpeg.size}->${enhanced.size}")
            enhanced
        } catch (t: Throwable) {
            Log.w(TAG, "image enhance failed; using original", t)
            jpeg
        }
    }

    private fun lumaOf(color: Int): Double =
        0.299 * Color.red(color) + 0.587 * Color.green(color) + 0.114 * Color.blue(color)

    private fun enhanceChannel(value: Int, gain: Double, gamma: Double): Int {
        val normalized = (value / 255.0).coerceIn(0.0, 1.0)
        val enhanced = 255.0 * normalized.pow(gamma) * gain
        return enhanced.toInt().coerceIn(0, 255)
    }

    private fun stopCameraCapture() {
        cameraHandler?.removeCallbacksAndMessages(null)
        try {
            captureSession?.close()
        } catch (_: Throwable) {
        }
        try {
            cameraDevice?.close()
        } catch (_: Throwable) {
        }
        try {
            imageReader?.close()
        } catch (_: Throwable) {
        }
        captureSession = null
        cameraDevice = null
        imageReader = null
        try {
            previewSurface?.release()
        } catch (_: Throwable) {
        }
        try {
            previewTexture?.release()
        } catch (_: Throwable) {
        }
        previewSurface = null
        previewTexture = null
        cameraCharacteristics = null
        cameraThread?.quitSafely()
        cameraThread = null
        cameraHandler = null
        imageInFlight.set(false)
        imageCaptureStartedAtMs = 0L
    }

    private data class CameraSelection(
        val cameraId: String,
        val size: Size,
        val characteristics: CameraCharacteristics,
    )

    private fun chooseCamera(manager: CameraManager): CameraSelection? {
        var fallback: CameraSelection? = null
        for (id in manager.cameraIdList) {
            val chars = manager.getCameraCharacteristics(id)
            val facing = chars.get(CameraCharacteristics.LENS_FACING)
            val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            val sizes = map?.getOutputSizes(ImageFormat.JPEG)?.toList().orEmpty()
            Log.i(TAG, "camera id=$id facing=$facing jpegSizes=${sizes.joinToString(limit = 12)}")
            if (sizes.isEmpty()) continue
            val chosen = chooseSize(sizes)
            val selection = CameraSelection(id, chosen, chars)
            if (fallback == null) fallback = selection
            if (facing == CameraCharacteristics.LENS_FACING_BACK ||
                facing == CameraCharacteristics.LENS_FACING_EXTERNAL
            ) {
                return selection
            }
        }
        return fallback
    }

    private fun chooseSize(sizes: List<Size>): Size {
        val preferred = if (HIGH_RES_CAPTURE_FOR_DOWNSAMPLE) {
            listOf(
                Size(1600, 1200),
                Size(1920, 1440),
                Size(1080, 1440),
                Size(1440, 1080),
                Size(2400, 1800),
                Size(2688, 2016),
                Size(3072, 2304),
                Size(3264, 2448),
                Size(1280, 720),
            )
        } else {
            listOf(
                Size(1280, 720),
                Size(720, 1280),
                Size(1080, 1440),
                Size(1440, 1080),
                Size(1280, 960),
                Size(1024, 768),
                Size(800, 600),
                Size(640, 480),
            )
        }
        for (target in preferred) {
            sizes.firstOrNull { it.width == target.width && it.height == target.height }?.let { return it }
        }
        return sizes.minWith { a, b ->
            val scoreA = sizeScore(a)
            val scoreB = sizeScore(b)
            scoreA - scoreB
        }
    }

    private fun sizeScore(size: Size): Int {
        val target = Size(720, 1280)
        val tooLargePenalty = if (size.width > 1920 || size.height > 1440) 6000 else 0
        val tooSmallPenalty = if (size.width < 640 || size.height < 480) 2000 else 0
        return abs(size.width - target.width) + abs(size.height - target.height) + tooLargePenalty + tooSmallPenalty
    }

    private fun updateHud() {
        val up = if (running) (System.currentTimeMillis() - startMs) / 1000 else 0
        val mode = if (running) "RUNNING" else "STOPPED"
        hud.text = """
            OpenGlass Rokid Sensor
            MODE: $mode
            PC: img=$imagePostBaseText audio=$audioWsUrlText
            AUDIO: $audioStatus source=$audioSourceText wsOpen=$wsOpen pkt=${audioPackets.get()} drop=${audioDrops.get()} rms=${fmt(lastRms)} peak=$lastPeak nz=$lastNonZero
            IMAGE: $cameraStatus pkt=${imagePackets.get()} fail=${imageFailures.get()} size=$imageSizeText hiResDownsample=$HIGH_RES_CAPTURE_FOR_DOWNSAMPLE
            UP: ${up}s
            LAST ERROR: $lastError

            Enter/DPAD: start-stop
            Back: stop / exit
        """.trimIndent()
    }

    private data class AudioStats(val rms: Double, val peak: Int, val nonZero: Int)

    private fun stats16(buf: ByteArray, n: Int): AudioStats {
        var sum = 0.0
        var count = 0
        var peak = 0
        var nonZero = 0
        var i = 0
        while (i + 1 < n) {
            val lo = buf[i].toInt() and 0xff
            val hi = buf[i + 1].toInt()
            val sampleInt = ((hi shl 8) or lo).toShort().toInt()
            val sample = sampleInt / 32768.0
            val absSample = abs(sampleInt)
            if (absSample > peak) peak = absSample
            if (sampleInt != 0) nonZero += 1
            sum += sample * sample
            count += 1
            i += 2
        }
        val rms = if (count == 0) 0.0 else sqrt(sum / count)
        return AudioStats(rms, peak, nonZero)
    }

    private fun fmt(v: Double): String = String.format(Locale.US, "%.4f", v)
}
