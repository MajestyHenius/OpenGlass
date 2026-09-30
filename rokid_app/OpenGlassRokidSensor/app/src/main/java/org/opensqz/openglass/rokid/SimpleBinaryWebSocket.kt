package org.opensqz.openglass.rokid

import android.util.Base64
import android.util.Log
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.EOFException
import java.net.Socket
import java.net.URI
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Locale
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean

private const val WS_TAG = "OpenGlassRokid"
private const val WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

class SimpleBinaryWebSocket(
    private val url: String,
    private val listener: Listener,
    private val queueCapacity: Int = 8,
) {
    interface Listener {
        fun onOpen()
        fun onClosed(reason: String)
        fun onFailure(error: Throwable)
    }

    private val random = SecureRandom()
    private val open = AtomicBoolean(false)
    private val stopped = AtomicBoolean(false)
    private val queue = ArrayBlockingQueue<ByteArray>(queueCapacity)
    private val writeLock = Object()
    @Volatile private var socket: Socket? = null
    @Volatile private var output: BufferedOutputStream? = null
    private var readerThread: Thread? = null
    private var writerThread: Thread? = null

    fun start() {
        readerThread = Thread({ runReader() }, "simple-ws-reader").also { it.start() }
    }

    fun close() {
        stopped.set(true)
        open.set(false)
        try {
            writeFrame(0x8, ByteArray(0))
        } catch (_: Throwable) {
        }
        try {
            socket?.close()
        } catch (_: Throwable) {
        }
        socket = null
    }

    fun isOpen(): Boolean = open.get()

    fun queuedBytes(): Long = queue.size.toLong() * 1280L

    fun sendBinary(data: ByteArray, offset: Int, length: Int): Boolean {
        if (!open.get() || stopped.get() || length <= 0) return false
        val copy = data.copyOfRange(offset, offset + length)
        return queue.offer(copy)
    }

    private fun runReader() {
        try {
            val uri = URI(url)
            require(uri.scheme == "ws") { "only ws:// is supported for MVP: $url" }
            val port = if (uri.port > 0) uri.port else 80
            val path = buildString {
                append(if (uri.rawPath.isNullOrEmpty()) "/" else uri.rawPath)
                if (!uri.rawQuery.isNullOrEmpty()) append('?').append(uri.rawQuery)
            }
            val sock = Socket()
            socket = sock
            sock.connect(java.net.InetSocketAddress(uri.host, port), 3_000)
            val input = BufferedInputStream(sock.getInputStream())
            val out = BufferedOutputStream(sock.getOutputStream())
            output = out

            val keyBytes = ByteArray(16)
            random.nextBytes(keyBytes)
            val key = Base64.encodeToString(keyBytes, Base64.NO_WRAP)
            val request = (
                "GET $path HTTP/1.1\r\n" +
                    "Host: ${uri.host}:$port\r\n" +
                    "Upgrade: websocket\r\n" +
                    "Connection: Upgrade\r\n" +
                    "Sec-WebSocket-Key: $key\r\n" +
                    "Sec-WebSocket-Version: 13\r\n" +
                    "\r\n"
                ).toByteArray(Charsets.US_ASCII)
            out.write(request)
            out.flush()

            val header = readHttpHeader(input)
            if (!header.startsWith("HTTP/1.1 101") && !header.startsWith("HTTP/1.0 101")) {
                throw IOExceptionCompat("bad websocket handshake: ${header.lineSequence().firstOrNull()}")
            }
            val accept = header.lineSequence()
                .firstOrNull { it.lowercase(Locale.US).startsWith("sec-websocket-accept:") }
                ?.substringAfter(':')
                ?.trim()
            val expected = Base64.encodeToString(
                MessageDigest.getInstance("SHA-1").digest((key + WS_GUID).toByteArray(Charsets.US_ASCII)),
                Base64.NO_WRAP,
            )
            if (accept != expected) {
                throw IOExceptionCompat("bad websocket accept")
            }

            open.set(true)
            listener.onOpen()
            writerThread = Thread({ runWriter() }, "simple-ws-writer").also { it.start() }

            while (!stopped.get()) {
                val opcode = readFrame(input)
                if (opcode == 0x8) break
            }
            open.set(false)
            listener.onClosed("closed")
        } catch (t: Throwable) {
            open.set(false)
            if (!stopped.get()) listener.onFailure(t)
        } finally {
            stopped.set(true)
            open.set(false)
            try {
                socket?.close()
            } catch (_: Throwable) {
            }
        }
    }

    private fun runWriter() {
        try {
            while (!stopped.get()) {
                val data = queue.take()
                writeFrame(0x2, data)
            }
        } catch (t: Throwable) {
            open.set(false)
            if (!stopped.get()) listener.onFailure(t)
        }
    }

    private fun readFrame(input: BufferedInputStream): Int {
        val b0 = input.read()
        val b1 = input.read()
        if (b0 < 0 || b1 < 0) throw EOFException("websocket EOF")
        val opcode = b0 and 0x0f
        var len = b1 and 0x7f
        val masked = (b1 and 0x80) != 0
        if (len == 126) {
            len = (readByte(input) shl 8) or readByte(input)
        } else if (len == 127) {
            var longLen = 0L
            repeat(8) { longLen = (longLen shl 8) or readByte(input).toLong() }
            if (longLen > Int.MAX_VALUE) throw IOExceptionCompat("websocket frame too large")
            len = longLen.toInt()
        }
        val mask = if (masked) ByteArray(4) { readByte(input).toByte() } else null
        val payload = ByteArray(len)
        var off = 0
        while (off < len) {
            val n = input.read(payload, off, len - off)
            if (n < 0) throw EOFException("websocket payload EOF")
            off += n
        }
        if (mask != null) {
            for (i in payload.indices) payload[i] = (payload[i].toInt() xor mask[i % 4].toInt()).toByte()
        }
        when (opcode) {
            0x8 -> return opcode
            0x9 -> writeFrame(0xA, payload)
            0xA -> Log.d(WS_TAG, "websocket pong")
        }
        return opcode
    }

    private fun writeFrame(opcode: Int, payload: ByteArray) {
        val out = output ?: return
        synchronized(writeLock) {
            out.write(0x80 or (opcode and 0x0f))
            val len = payload.size
            when {
                len < 126 -> out.write(0x80 or len)
                len <= 0xffff -> {
                    out.write(0x80 or 126)
                    out.write((len ushr 8) and 0xff)
                    out.write(len and 0xff)
                }
                else -> {
                    out.write(0x80 or 127)
                    val l = len.toLong()
                    for (shift in 56 downTo 0 step 8) out.write(((l ushr shift) and 0xff).toInt())
                }
            }
            val mask = ByteArray(4)
            random.nextBytes(mask)
            out.write(mask)
            for (i in payload.indices) out.write(payload[i].toInt() xor mask[i % 4].toInt())
            out.flush()
        }
    }

    private fun readHttpHeader(input: BufferedInputStream): String {
        val bytes = ArrayList<Byte>(1024)
        var last4 = 0
        while (bytes.size < 16_384) {
            val b = input.read()
            if (b < 0) throw EOFException("handshake EOF")
            bytes.add(b.toByte())
            last4 = ((last4 shl 8) or b) and 0xffffffff.toInt()
            if (last4 == 0x0d0a0d0a) break
        }
        return bytes.toByteArray().toString(Charsets.US_ASCII)
    }

    private fun readByte(input: BufferedInputStream): Int {
        val b = input.read()
        if (b < 0) throw EOFException("websocket EOF")
        return b and 0xff
    }
}

private class IOExceptionCompat(message: String) : java.io.IOException(message)
