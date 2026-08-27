package `in`.aditya.gold

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder

/**
 * Updates the widget on a short timer, for as long as the user leaves it on.
 *
 * ## Why this is a foreground service, and what that costs
 *
 * Android will not refresh a home-screen widget faster than every 30 minutes.
 * `updatePeriodMillis` is clamped to that floor, and WorkManager's periodic
 * minimum is 15 minutes. A foreground service is the *only* supported way to
 * update more often than that, and Android requires it to display a permanent,
 * non-dismissable notification for exactly this reason: the user is meant to be
 * able to see that something is running continuously on their behalf.
 *
 * At the default five-second interval this is roughly 720 requests an hour and
 * a CPU wake every five seconds. The battery cost is real, which is why it is
 * off by default and switched on explicitly from the app.
 *
 * `specialUse` rather than `dataSync` is deliberate: from Android 15, `dataSync`
 * foreground services are capped at roughly six hours in any 24-hour period, and
 * a price ticker that silently dies mid-session would be worse than one that
 * never started.
 */
class GoldTickerService : Service() {

    @Volatile
    private var running = false
    private var worker: Thread? = null

    companion object {
        const val ACTION_START = "in.aditya.gold.TICKER_START"
        const val ACTION_STOP = "in.aditya.gold.TICKER_STOP"
        const val EXTRA_INTERVAL_MS = "interval_ms"

        private const val CHANNEL_ID = "gold_ticker"
        private const val NOTIFICATION_ID = 42

        // Below this the widget cannot keep up: each cycle does a network round
        // trip and a RemoteViews push, and the launcher coalesces updates that
        // arrive faster than it can draw them.
        private const val MIN_INTERVAL_MS = 1000L
        const val DEFAULT_INTERVAL_MS = 5000L

        fun start(context: Context, intervalMs: Long = DEFAULT_INTERVAL_MS) {
            val intent = Intent(context, GoldTickerService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_INTERVAL_MS, intervalMs)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, GoldTickerService::class.java).setAction(ACTION_STOP)
            )
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopTicker()
            return START_NOT_STICKY
        }

        val interval = (intent?.getLongExtra(EXTRA_INTERVAL_MS, DEFAULT_INTERVAL_MS)
            ?: DEFAULT_INTERVAL_MS).coerceAtLeast(MIN_INTERVAL_MS)

        startForegroundCompat(interval)
        if (!running) {
            running = true
            worker = Thread {
                while (running) {
                    // A failed poll must not kill the loop -- the relay going away
                    // for a moment is expected, and the widget shows it as stale.
                    runCatching { GoldWidgetData.refreshBlocking(this, live = true) }
                    try {
                        Thread.sleep(interval)
                    } catch (_: InterruptedException) {
                        break
                    }
                }
            }.also { it.start() }
        }
        // START_STICKY so the ticker comes back if Android reclaims the process
        // while the user still has it switched on.
        return START_STICKY
    }

    private fun stopTicker() {
        running = false
        worker?.interrupt()
        worker = null
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
        stopSelf()
    }

    override fun onDestroy() {
        running = false
        worker?.interrupt()
        super.onDestroy()
    }

    private fun startForegroundCompat(interval: Long) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Live prices",
                // MIN keeps it silent and collapsed: the notification is a legal
                // requirement of running continuously, not something to shout with.
                NotificationManager.IMPORTANCE_MIN
            ).apply { setShowBadge(false) }
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }

        val open = packageManager.getLaunchIntentForPackage(packageName)
        val tap = open?.let {
            PendingIntent.getActivity(
                this, 0, it,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
        }
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, GoldTickerService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val seconds = interval / 1000.0
        val notification: Notification =
            Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("Gold — live")
                .setContentText("Updating every ${trimSeconds(seconds)}s")
                .setSmallIcon(android.R.drawable.ic_menu_recent_history)
                .setOngoing(true)
                .setContentIntent(tap)
                .addAction(
                    Notification.Action.Builder(null, "Stop", stop).build()
                )
                .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                NOTIFICATION_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun trimSeconds(v: Double): String =
        if (v == v.toLong().toDouble()) v.toLong().toString() else v.toString()
}
