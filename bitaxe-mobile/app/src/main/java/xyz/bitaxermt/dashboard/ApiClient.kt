package xyz.bitaxermt.dashboard

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

object ApiClient {

    private val JSON = "application/json; charset=utf-8".toMediaType()

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .build()

    fun buildRequest(url: String): Request.Builder {
        val builder = Request.Builder().url(url)
        val sessionKey = AuthManager.getSessionKey()
        if (sessionKey != null) {
            builder.addHeader("Authorization", "Bearer $sessionKey")
        }
        return builder
    }

    suspend fun get(url: String): String? {
        return try {
            val request = buildRequest(url).build()
            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) response.body?.string() else null
            }
        } catch (e: Exception) {
            null
        }
    }

    suspend fun postJson(url: String, json: String): String? {
        return try {
            val request = buildRequest(url)
                .post(json.toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) response.body?.string() else null
            }
        } catch (e: Exception) {
            null
        }
    }

    suspend fun exchangeToken(token: String, deviceName: String): String? {
        val json = """{"token":"$token","device_name":"$deviceName"}"""
        val url = "${AuthManager.getBaseUrl()}/api/pair/exchange"
        return postJson(url, json)
    }

    suspend fun registerFcmToken(fcmToken: String, deviceName: String): Boolean {
        val json = """{"fcm_token":"$fcmToken","device_name":"$deviceName"}"""
        val url = "${AuthManager.getBaseUrl()}/api/fcm/register"
        val result = postJson(url, json)
        return result?.contains("\"ok\":true") == true
    }
}
