package `in`.aditya.gold

/**
 * Deployment-specific values for the widget.
 *
 * The widget fetches independently of the Flutter app, so it needs its own copy
 * of these rather than reading them from Dart. Kept in step with lib/secrets.dart
 * by hand -- there are only three values.
 *
 * This is the template. Copy to Secrets.kt and fill in your values.
 */
object Secrets {
    const val RELAY_DEFAULT = "http://your-relay-host:8787"
    const val RELAY_FALLBACK = "http://100.x.y.z:8787"
    const val TOKEN_DEFAULT = ""
}
