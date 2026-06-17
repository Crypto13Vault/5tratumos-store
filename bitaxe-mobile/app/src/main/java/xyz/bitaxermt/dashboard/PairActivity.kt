package xyz.bitaxermt.dashboard

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import xyz.bitaxermt.dashboard.databinding.ActivityPairBinding
import java.net.URI
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

class PairActivity : AppCompatActivity() {

    private lateinit var binding: ActivityPairBinding
    private lateinit var cameraExecutor: ExecutorService
    private val barcodeScanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .build()
    )
    private var isProcessing = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityPairBinding.inflate(layoutInflater)
        setContentView(binding.root)

        cameraExecutor = Executors.newSingleThreadExecutor()

        binding.btnBack.setOnClickListener { finish() }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startCamera()
        } else {
            requestPermissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) startCamera() else finish()
    }

    private fun startCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener({
            val cameraProvider = cameraProviderFuture.get()
            val preview = androidx.camera.core.Preview.Builder().build()
            preview.setSurfaceProvider(binding.previewView.surfaceProvider)

            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
            analysis.setAnalyzer(cameraExecutor) { imageProxy ->
                if (!isProcessing) {
                    isProcessing = true
                    val mediaImage = imageProxy.image
                    if (mediaImage != null) {
                        val image = InputImage.fromMediaImage(
                            mediaImage, imageProxy.imageInfo.rotationDegrees
                        )
                        barcodeScanner.process(image)
                            .addOnSuccessListener { barcodes ->
                                for (barcode in barcodes) {
                                    val raw = barcode.rawValue
                                    if (raw != null && raw.startsWith("bitaxe://pair")) {
                                        handlePairUri(raw)
                                        break
                                    }
                                }
                            }
                            .addOnCompleteListener {
                                imageProxy.close()
                                isProcessing = false
                            }
                    } else {
                        imageProxy.close()
                        isProcessing = false
                    }
                } else {
                    imageProxy.close()
                }
            }

            cameraProvider.unbindAll()
            cameraProvider.bindToLifecycle(
                this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis
            )
        }, ContextCompat.getMainExecutor(this))
    }

    private fun handlePairUri(uriString: String) {
        lifecycleScope.launch {
            runOnUiThread {
                binding.pairStatus.text = "Processing..."
                binding.pairProgress.visibility = View.VISIBLE
            }

            try {
                val uri = URI.create(uriString)
                val params = uri.query?.split("&")?.associate {
                    val (k, v) = it.split("=")
                    k to v
                } ?: emptyMap()

                val host = params["host"] ?: ""
                val port = params["port"]?.toIntOrNull() ?: 5050
                val token = params["token"] ?: ""

                if (host.isEmpty() || token.isEmpty()) {
                    withContext(Dispatchers.Main) {
                        binding.pairStatus.text = "Invalid QR code"
                        binding.pairProgress.visibility = View.GONE
                    }
                    return@launch
                }

                AuthManager.saveConnection(host, port, "")

                val deviceName = "${Build.MANUFACTURER} ${Build.MODEL}"
                val response = ApiClient.exchangeToken(token, deviceName)

                if (response != null) {
                    val json = com.google.gson.JsonParser.parseString(response).asJsonObject
                    if (json.get("ok")?.asBoolean == true) {
                        val sessionKey = json.get("session_key")?.asString ?: ""
                        AuthManager.saveConnection(host, port, sessionKey)

                        val fcmToken = AuthManager.getFcmToken()
                        if (fcmToken != null) {
                            ApiClient.registerFcmToken(fcmToken, deviceName)
                        }

                        withContext(Dispatchers.Main) {
                            Toast.makeText(this@PairActivity,
                                getString(R.string.pairing_success), Toast.LENGTH_SHORT).show()
                            setResult(RESULT_OK)
                            finish()
                        }
                    } else {
                        withContext(Dispatchers.Main) {
                            binding.pairStatus.text = "Token expired or invalid"
                            binding.pairProgress.visibility = View.GONE
                        }
                    }
                } else {
                    withContext(Dispatchers.Main) {
                        binding.pairStatus.text = "Connection failed"
                        binding.pairProgress.visibility = View.GONE
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    binding.pairStatus.text = "Error: ${e.message}"
                    binding.pairProgress.visibility = View.GONE
                }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        cameraExecutor.shutdown()
        barcodeScanner.close()
    }
}
