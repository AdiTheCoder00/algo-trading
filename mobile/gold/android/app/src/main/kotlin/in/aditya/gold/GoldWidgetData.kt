package `in`.aditya.gold

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.RemoteViews
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Fetching and rendering for the home-screen widget, shared by two callers.
 *
 * [GoldWidgetProvider] drives it on Android's own 30-minute cadence and on a
 * footer tap; [GoldTickerService] drives it on a short timer while the user has
 * live mode switched on. Both need identical output, so the logic lives here
 * rather than in either of them.
 *
 * The fetch is deliberately native rather than pushed from Dart: a widget that
 * only updates while the Flutter app is running is not really a widget.
 */
object GoldWidgetData {

    private const val PREFS = "FlutterSharedPreferences"
    private const val KEY_RELAY = "flutter.relay"
    private const val KEY_TOKEN = "flutter.token"

    private val DEFAULT_TOKEN = Secrets.TOKEN_DEFAULT
    private val DEFAULT_RELAY = Secrets.RELAY_DEFAULT
    private val RELAY_FALLBACK = Secrets.RELAY_FALLBACK
    private const val SPOT_URL = "https://api.gold-api.com/price/XAU"

    // Last-good values, so a reboot or a relay outage leaves the widget showing
    // the last price it saw -- dimmed and dated -- rather than four dots. The
    // widget's own store, separate from the Flutter prefs it reads config from.
    private const val CACHE = "gold_widget_cache"

    private const val GREEN = 0xFF2FBF71.toInt()
    private const val RED = 0xFFF0554B.toInt()
    private const val DIM = 0xFF8A8A94.toInt()
    private const val FG = 0xFFF5F5F7.toInt()

    data class Snapshot(
        val xauPrice: Double? = null,
        val xauPct: Double? = null,
        val gmPrice: Double? = null,
        val gmChange: Double? = null,
        val gmPct: Double? = null,
        val gmStale: Boolean = true,
        val note: String = ""
    )

    /** Fetch and push to every bound instance. Must be called off the main thread. */
    fun refreshBlocking(context: Context, live: Boolean = false) {
        val manager = AppWidgetManager.getInstance(context)
        val ids = manager.getAppWidgetIds(ComponentName(context, GoldWidgetProvider::class.java))
        if (ids.isEmpty()) return
        val snapshot = loadSnapshot(context)
        remember(context, snapshot)
        val views = render(context, snapshot, live)
        ids.forEach { manager.updateAppWidget(it, views) }
    }

    fun hasBoundWidgets(context: Context): Boolean =
        AppWidgetManager.getInstance(context)
            .getAppWidgetIds(ComponentName(context, GoldWidgetProvider::class.java))
            .isNotEmpty()

    // ------------------------------------------------------------------ data

    fun loadSnapshot(context: Context): Snapshot {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val relay = (prefs.getString(KEY_RELAY, null) ?: DEFAULT_RELAY).trim().trimEnd('/')
        val token = prefs.getString(KEY_TOKEN, null)?.trim()?.takeIf { it.isNotEmpty() }
            ?: DEFAULT_TOKEN

        val candidates = buildList {
            if (relay.isNotEmpty()) add(relay)
            if (relay.contains("ts.net") && relay != RELAY_FALLBACK) add(RELAY_FALLBACK)
        }
        for (base in candidates) {
            val url = buildString {
                append("$base/prices.json?t=${System.currentTimeMillis()}")
                if (token.isNotEmpty()) append("&k=${Uri.encode(token)}")
            }
            httpGet(url)?.let { body ->
                runCatching {
                    val root = JSONObject(body)
                    val old = root.optDouble("age_seconds", 0.0) > 150.0
                    val x = root.optJSONObject("xauusd")
                    val g = root.optJSONObject("goldm")
                    return Snapshot(
                        xauPrice = x?.optDoubleOrNull("price"),
                        xauPct = x?.optDoubleOrNull("per_change"),
                        gmPrice = g?.optDoubleOrNull("price"),
                        gmChange = g?.optDoubleOrNull("change"),
                        gmPct = g?.optDoubleOrNull("per_change"),
                        gmStale = g?.optDoubleOrNull("price") == null ||
                            g.optBoolean("stale", false) || old,
                        note = stamp()
                    )
                }
            }
        }

        // Relay unreachable. Spot is public, so XAUUSD survives; GOLDM cannot.
        val spot = httpGet(SPOT_URL)?.let {
            runCatching { JSONObject(it).optDoubleOrNull("price") }.getOrNull()
        }
        val cache = context.getSharedPreferences(CACHE, Context.MODE_PRIVATE)
        val cachedAt = cache.getLong("at", 0L)
        val age = if (cachedAt > 0L) ageLabel(cachedAt) else null
        return Snapshot(
            xauPrice = spot ?: cache.getFloat("xau", 0f).takeIf { it > 0f }?.toDouble(),
            xauPct = if (spot != null) null else cache.getFloat("xau_pct", 0f).toDouble(),
            // GOLDM has no public source at all, so the cache is the only thing
            // standing between a relay outage and a blank widget.
            gmPrice = cache.getFloat("gm", 0f).takeIf { it > 0f }?.toDouble(),
            gmChange = cache.getFloat("gm_chg", 0f).toDouble(),
            gmPct = cache.getFloat("gm_pct", 0f).toDouble(),
            gmStale = true,
            note = when {
                spot != null && age != null -> "relay offline · GOLDM $age"
                spot != null -> "relay offline · ${stamp()}"
                age != null -> "no feed · last seen $age"
                else -> "no feed"
            }
        )
    }

    private fun remember(context: Context, s: Snapshot) {
        if (s.gmStale || s.gmPrice == null) return
        context.getSharedPreferences(CACHE, Context.MODE_PRIVATE).edit().apply {
            putFloat("gm", s.gmPrice.toFloat())
            s.gmChange?.let { putFloat("gm_chg", it.toFloat()) }
            s.gmPct?.let { putFloat("gm_pct", it.toFloat()) }
            s.xauPrice?.let { putFloat("xau", it.toFloat()) }
            s.xauPct?.let { putFloat("xau_pct", it.toFloat()) }
            putLong("at", System.currentTimeMillis())
            apply()
        }
    }

    private fun ageLabel(atMillis: Long): String {
        val mins = (System.currentTimeMillis() - atMillis) / 60000
        return when {
            mins < 1 -> "moments ago"
            mins < 60 -> "${mins}m ago"
            mins < 1440 -> "${mins / 60}h ago"
            else -> "${mins / 1440}d ago"
        }
    }

    private fun JSONObject.optDoubleOrNull(key: String): Double? =
        if (isNull(key)) null else optDouble(key).takeIf { !it.isNaN() }

    private fun httpGet(url: String): String? = runCatching {
        (URL(url).openConnection() as HttpURLConnection).run {
            connectTimeout = 5000
            readTimeout = 5000
            requestMethod = "GET"
            setRequestProperty("User-Agent", "gold-widget/1")
            if (responseCode != 200) {
                disconnect()
                return null
            }
            val text = inputStream.bufferedReader().use { it.readText() }
            disconnect()
            text
        }
    }.getOrNull()

    private fun stamp(): String = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())

    // ---------------------------------------------------------------- render

    fun render(context: Context, s: Snapshot, live: Boolean = false): RemoteViews {
        val views = RemoteViews(context.packageName, R.layout.gold_widget)

        views.setTextViewText(R.id.xau_price, s.xauPrice?.let { "$" + group(it, 2) } ?: "····")
        views.setTextViewText(R.id.xau_chg, pct(s.xauPct))
        views.setTextColor(R.id.xau_chg, tint(s.xauPct))

        views.setTextViewText(R.id.gm_price, s.gmPrice?.let { "₹" + group(it, 0) } ?: "····")
        val gmChg = if (s.gmChange != null && s.gmPct != null) {
            (if (s.gmChange > 0) "+" else "-") + group(Math.abs(s.gmChange), 0) + "  " + pct(s.gmPct)
        } else pct(s.gmPct)
        views.setTextViewText(R.id.gm_chg, gmChg)
        views.setTextColor(R.id.gm_chg, tint(s.gmPct))

        // Staleness dims the value itself, which reads at a glance from a home
        // screen in a way a small badge does not.
        views.setTextColor(R.id.gm_price, if (s.gmStale) DIM else FG)
        val suffix = when {
            s.gmStale -> "GOLDM stale"
            live -> "live"
            else -> "tap to refresh"
        }
        views.setTextViewText(R.id.footer, "${s.note} · $suffix")

        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        views.setOnClickPendingIntent(
            R.id.footer,
            PendingIntent.getBroadcast(
                context, 0,
                Intent(context, GoldWidgetProvider::class.java)
                    .setAction(GoldWidgetProvider.ACTION_REFRESH),
                flags
            )
        )
        context.packageManager.getLaunchIntentForPackage(context.packageName)?.let {
            views.setOnClickPendingIntent(
                R.id.body, PendingIntent.getActivity(context, 1, it, flags)
            )
        }
        return views
    }

    private fun tint(pct: Double?): Int = when {
        pct == null -> DIM
        pct > 0 -> GREEN
        pct < 0 -> RED
        else -> DIM
    }

    private fun pct(v: Double?): String =
        if (v == null) "—" else String.format(Locale.US, "%s%.2f%%", if (v > 0) "+" else "", v)

    /** Indian digit grouping: last three digits, then pairs. 157724 -> 1,57,724. */
    private fun group(value: Double, decimals: Int): String {
        val fixed = String.format(Locale.US, "%.${decimals}f", Math.abs(value))
        val intPart = fixed.substringBefore('.')
        val frac = fixed.substringAfter('.', "")
        val grouped = if (intPart.length <= 3) intPart else {
            val head = intPart.dropLast(3)
            val tail = intPart.takeLast(3)
            val sb = StringBuilder()
            head.forEachIndexed { i, c ->
                if (i > 0 && (head.length - i) % 2 == 0) sb.append(',')
                sb.append(c)
            }
            "$sb,$tail"
        }
        val sign = if (value < 0) "-" else ""
        return if (frac.isEmpty()) "$sign$grouped" else "$sign$grouped.$frac"
    }
}
