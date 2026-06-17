package xyz.bitaxermt.dashboard

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

object AuthManager {

    private const val PREFS_NAME = "bitaxe_auth"
    private const val KEY_HOST = "host"
    private const val KEY_PORT = "port"
    private const val KEY_SESSION = "session_key"

    private lateinit var prefs: SharedPreferences

    fun init(context: Context) {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        prefs = EncryptedSharedPreferences.create(
            context,
            PREFS_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    fun isPaired(): Boolean = prefs.contains(KEY_SESSION)

    fun getHost(): String? = prefs.getString(KEY_HOST, null)
    fun getPort(): Int = prefs.getInt(KEY_PORT, 5050)
    fun getSessionKey(): String? = prefs.getString(KEY_SESSION, null)

    fun saveConnection(host: String, port: Int, sessionKey: String) {
        prefs.edit()
            .putString(KEY_HOST, host)
            .putInt(KEY_PORT, port)
            .putString(KEY_SESSION, sessionKey)
            .apply()
    }

    fun clear() {
        prefs.edit().clear().apply()
    }

    fun getBaseUrl(): String {
        val host = getHost() ?: return ""
        val port = getPort()
        return "http://$host:$port"
    }
}
