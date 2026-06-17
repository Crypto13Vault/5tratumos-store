package xyz.bitaxermt.dashboard

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import xyz.bitaxermt.dashboard.databinding.ActivitySettingsBinding

class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        title = "Settings"

        val host = AuthManager.getHost() ?: ""
        val port = AuthManager.getPort()
        binding.tvConnectionInfo.text = if (host.isNotEmpty()) {
            "Connected to $host:$port"
        } else {
            "Not paired"
        }

        binding.btnPair.setOnClickListener {
            startActivityForResult(Intent(this, PairActivity::class.java), 1)
        }

        binding.btnRevoke.setOnClickListener {
            AuthManager.clear()
            binding.tvConnectionInfo.text = "Not paired"
            Toast.makeText(this, "Device unpaired", Toast.LENGTH_SHORT).show()
        }

        binding.btnReconnect.setOnClickListener {
            if (AuthManager.isPaired()) {
                lifecycleScope.launch {
                    val fcmToken = AuthManager.getFcmToken()
                    if (fcmToken != null) {
                        val deviceName = "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}"
                        val success = ApiClient.registerFcmToken(fcmToken, deviceName)
                        if (success) {
                            Toast.makeText(this@SettingsActivity, "FCM registered", Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            }
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 1 && resultCode == RESULT_OK) {
            val host = AuthManager.getHost() ?: ""
            val port = AuthManager.getPort()
            binding.tvConnectionInfo.text = "Connected to $host:$port"
            Toast.makeText(this, getString(R.string.pairing_success), Toast.LENGTH_SHORT).show()
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}
