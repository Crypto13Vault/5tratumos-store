package xyz.bitaxermt.dashboard

import android.webkit.WebResourceRequest

class WebResourceRequestBuilder(private val original: WebResourceRequest) {
    private val headers = mutableMapOf<String, String>()

    init {
        headers.putAll(original.requestHeaders)
    }

    fun addHeader(name: String, value: String) = apply {
        headers[name] = value
    }

    fun build(): WebResourceRequest {
        return object : WebResourceRequest {
            override fun getUrl() = original.url
            override fun isForMainFrame() = original.isForMainFrame
            override fun isRedirect() = original.isRedirect
            override fun hasGesture() = original.hasGesture()
            override fun getMethod() = original.method
            override fun getRequestHeaders() = headers.toMap()
        }
    }
}
