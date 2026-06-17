package xyz.bitaxermt.dashboard

import android.app.Application

class BitaxeApp : Application() {

    override fun onCreate() {
        super.onCreate()
        AuthManager.init(this)
    }
}
