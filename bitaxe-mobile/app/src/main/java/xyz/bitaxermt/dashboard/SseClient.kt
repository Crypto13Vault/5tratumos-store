package xyz.bitaxermt.dashboard

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import java.util.concurrent.TimeUnit

class SseClient {

    interface Listener {
        fun onEvent(type: String, data: String)
        fun onClosed()
        fun onError(error: String)
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private var eventSource: EventSource? = null
    private var listener: Listener? = null
    private var autoReconnect = true

    fun connect(listener: Listener) {
        this.listener = listener
        val baseUrl = AuthManager.getBaseUrl()
        if (baseUrl.isEmpty()) return

        val sessionKey = AuthManager.getSessionKey()
        val url = "$baseUrl/api/events?token=${sessionKey.orEmpty()}"

        val request = Request.Builder()
            .url(url)
            .addHeader("Accept", "text/event-stream")
            .addHeader("Cache-Control", "no-cache")
            .build()

        eventSource = EventSources.createFactory(client).newEventSource(request, object : EventSourceListener() {
            override fun onOpen(eventSource: EventSource, response: Response) {
                // Connected
            }

            override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                if (type != null && data.isNotEmpty()) {
                    listener?.onEvent(type, data)
                }
            }

            override fun onClosed(eventSource: EventSource) {
                listener?.onClosed()
                if (autoReconnect) {
                    android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
                        connect(listener!!)
                    }, 5000)
                }
            }

            override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) {
                listener?.onError(t?.message ?: "SSE connection failed")
                if (autoReconnect) {
                    android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
                        connect(listener!!)
                    }, 10000)
                }
            }
        })
    }

    fun disconnect() {
        autoReconnect = false
        eventSource?.cancel()
        eventSource = null
    }
}
