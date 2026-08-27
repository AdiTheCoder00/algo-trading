package `in`.aditya.gold

import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Intent
import android.os.Build
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {

    private val channel = "in.aditya.gold/widget"

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    // Asks the launcher to place the widget, rather than making the
                    // user find it in the widget drawer. Supported from API 26; on
                    // launchers that decline, isRequestPinAppWidgetSupported is false
                    // and the caller is told so instead of nothing appearing to happen.
                    "pinWidget" -> {
                        val manager = AppWidgetManager.getInstance(this)
                        val provider = ComponentName(this, GoldWidgetProvider::class.java)
                        val ok = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
                            manager.isRequestPinAppWidgetSupported
                        if (ok) manager.requestPinAppWidget(provider, null, null)
                        result.success(ok)
                    }
                    // Live mode. Reports back whether a widget is actually on the
                    // home screen: starting a ticker with nothing to tick would burn
                    // battery updating no one, and the user would just see a
                    // notification with no visible effect.
                    "startLive" -> {
                        if (!GoldWidgetData.hasBoundWidgets(this)) {
                            result.success(false)
                        } else {
                            val ms = (call.argument<Int>("intervalMs")
                                ?: GoldTickerService.DEFAULT_INTERVAL_MS.toInt()).toLong()
                            GoldTickerService.start(this, ms)
                            result.success(true)
                        }
                    }
                    "stopLive" -> {
                        GoldTickerService.stop(this)
                        result.success(true)
                    }
                    "refreshWidget" -> {
                        sendBroadcast(
                            Intent(this, GoldWidgetProvider::class.java)
                                .setAction(GoldWidgetProvider.ACTION_REFRESH)
                        )
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
            }
    }

    /**
     * Nudge the widget when the app is left, so a relay URL changed in settings
     * takes effect immediately rather than at the next 30-minute tick.
     */
    override fun onStop() {
        super.onStop()
        sendBroadcast(
            Intent(this, GoldWidgetProvider::class.java)
                .setAction(GoldWidgetProvider.ACTION_REFRESH)
        )
    }
}
