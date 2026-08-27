package `in`.aditya.gold

import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent

/**
 * Home-screen widget for XAUUSD + GOLDM.
 *
 * All fetching and rendering lives in [GoldWidgetData], because [GoldTickerService]
 * needs exactly the same output on its own timer. This class is only the Android
 * entry points.
 *
 * ## Refresh cadence
 *
 * Android clamps [android.appwidget.AppWidgetProviderInfo.updatePeriodMillis] to a
 * 30-minute floor and will not honour anything shorter, so the periodic update is
 * set at that floor. Tapping the footer forces an immediate refresh, and
 * [GoldTickerService] covers the case where the user wants a genuinely live tick —
 * at the cost of a permanent notification, which is Android's price for it.
 */
class GoldWidgetProvider : AppWidgetProvider() {

    companion object {
        const val ACTION_REFRESH = "in.aditya.gold.WIDGET_REFRESH"
    }

    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) = refresh(context)

    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        if (intent.action == ACTION_REFRESH) refresh(context)
    }

    /**
     * Network cannot run on the main thread, and a BroadcastReceiver is normally
     * killed the moment onReceive returns. goAsync() keeps the process alive for
     * the duration of the fetch; finish() must be called on every path or the
     * receiver leaks and Android eventually complains.
     */
    private fun refresh(context: Context) {
        val pending = goAsync()
        Thread {
            try {
                GoldWidgetData.refreshBlocking(context)
            } catch (_: Throwable) {
                // Never let a widget update crash the host launcher process.
            } finally {
                pending.finish()
            }
        }.start()
    }

    /** Last instance removed: nothing left to tick for, so stop the service. */
    override fun onDisabled(context: Context) {
        super.onDisabled(context)
        GoldTickerService.stop(context)
    }
}
