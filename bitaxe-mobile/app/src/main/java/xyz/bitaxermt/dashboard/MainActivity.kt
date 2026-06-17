package xyz.bitaxermt.dashboard

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.gson.JsonParser
import kotlinx.coroutines.launch
import xyz.bitaxermt.dashboard.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val sseClient = SseClient()
    private val pairLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK) {
            loadDashboard()
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AuthManager.init(this)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.webView.settings.javaScriptEnabled = true
        binding.webView.settings.domStorageEnabled = true
        binding.webView.settings.loadWithOverviewMode = true
        binding.webView.settings.useWideViewPort = true
        binding.webView.settings.setSupportZoom(true)

        binding.webView.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(
                view: WebView, request: WebResourceRequest
            ): WebResourceResponse? {
                val sessionKey = AuthManager.getSessionKey()
                if (sessionKey != null && request.url.toString().contains("/api/")) {
                    val newRequest = WebResourceRequestBuilder(request)
                        .addHeader("Authorization", "Bearer $sessionKey")
                        .build()
                }
                return null
            }

            override fun onPageFinished(view: WebView, url: String) {
                binding.loadingBar.visibility = View.GONE
            }
        }

        binding.fabSettings.setOnClickListener {
            val intent = Intent(this, SettingsActivity::class.java)
            startActivity(intent)
        }

        binding.btnPairFromMain.setOnClickListener {
            pairLauncher.launch(Intent(this, PairActivity::class.java))
        }

        if (AuthManager.isPaired()) {
            loadDashboard()
            connectSse()
        } else {
            binding.notPairedOverlay.visibility = View.VISIBLE
            binding.webView.visibility = View.GONE
        }
    }

    private fun loadDashboard() {
        val url = AuthManager.getBaseUrl()
        if (url.isEmpty()) return

        binding.notPairedOverlay.visibility = View.GONE
        binding.webView.visibility = View.VISIBLE
        binding.loadingBar.visibility = View.VISIBLE

        binding.webView.loadUrl(url)
    }

    private fun connectSse() {
        sseClient.connect(object : SseClient.Listener {
            override fun onEvent(type: String, data: String) {
                lifecycleScope.launch {
                    try {
                        val json = JsonParser.parseString(data).asJsonObject
                        when (type) {
                            "block_found" -> {
                                val miner = json.get("miner")?.asString ?: "Unknown"
                                showBlockAlert(miner)
                            }
                            "overheat" -> {
                                val miner = json.get("miner")?.asString ?: "Unknown"
                                val temp = json.get("temp")?.asFloat ?: 0f
                                showOverheatAlert(miner, temp)
                            }
                        }
                    } catch (e: Exception) {
                        // Ignore parse errors
                    }
                }
            }

            override fun onClosed() {
                // Auto-reconnect handled by SseClient
            }

            override fun onError(error: String) {
                // Auto-reconnect handled by SseClient
            }
        })
    }

    private fun showBlockAlert(miner: String) {
        runOnUiThread {
            Toast.makeText(this, "BLOCK FOUND by $miner!", Toast.LENGTH_LONG).show()
        }
    }

    private fun showOverheatAlert(miner: String, temp: Float) {
        runOnUiThread {
            Toast.makeText(this, "OVERHEAT: $miner at ${temp.toInt()}°C", Toast.LENGTH_LONG).show()
        }
    }

    override fun onResume() {
        super.onResume()
        if (AuthManager.isPaired() && binding.webView.visibility == View.GONE) {
            loadDashboard()
            connectSse()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        sseClient.disconnect()
    }
}
