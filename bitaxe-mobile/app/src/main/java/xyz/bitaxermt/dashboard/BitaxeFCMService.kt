package xyz.bitaxermt.dashboard

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class BitaxeFCMService : FirebaseMessagingService() {

    override fun onMessageReceived(remoteMessage: RemoteMessage) {
        val title = remoteMessage.notification?.title ?: "Bitaxe Alert"
        val body = remoteMessage.notification?.body ?: ""
        val data = remoteMessage.data

        showNotification(title, body, data)
    }

    override fun onNewToken(token: String) {
        super.onNewToken(token)
        AuthManager.setFcmToken(token)
        val deviceName = Build.MODEL ?: "Android Device"
        CoroutineScope(Dispatchers.IO).launch {
            ApiClient.registerFcmToken(token, deviceName)
        }
    }

    private fun showNotification(title: String, body: String, data: Map<String, String>) {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
            data["ip"]?.let { putExtra("alert_ip", it) }
        }

        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val notification = NotificationCompat.Builder(this, BitaxeApp.CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle(title)
            .setContentText(body)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .build()

        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(System.currentTimeMillis().toInt(), notification)
    }
}
