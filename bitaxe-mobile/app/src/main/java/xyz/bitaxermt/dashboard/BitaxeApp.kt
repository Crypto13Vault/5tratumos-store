package xyz.bitaxermt.dashboard

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import com.google.firebase.FirebaseApp
import com.google.firebase.messaging.FirebaseMessaging

class BitaxeApp : Application() {

    override fun onCreate() {
        super.onCreate()
        FirebaseApp.initializeApp(this)

        FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
            if (task.isSuccessful) {
                val token = task.result
                AuthManager.setFcmToken(token)
            }
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Bitaxe Alerts",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "Block found and overheat alerts"
                enableVibration(true)
                enableLights(true)
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
    }

    companion object {
        const val CHANNEL_ID = "bitaxe_alerts"
    }
}
