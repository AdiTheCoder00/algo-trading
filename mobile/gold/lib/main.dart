// Gold — XAUUSD + GOLDM, big and bold.
//
// Two feeds, deliberately asymmetric:
//
//   XAUUSD  public spot API, reachable from anywhere. Needs nothing from home.
//   GOLDM   MCX has no free public quote API, so it comes from the relay running
//           on the PC (scripts/price_publisher.py). No broker credential is ever
//           in this APK — an APK is decompilable, and those credentials place
//           orders. The relay publishes prices and nothing else.
//
// So the app degrades honestly: off the home network XAUUSD stays live and GOLDM
// greys out as STALE rather than showing an old number in a big bold font.

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'secrets.dart';

const kSpotUrl = 'https://api.gold-api.com/price/XAU';
const kRefresh = Duration(seconds: 5);
const kWidgetChannel = MethodChannel('in.aditya.gold/widget');

// Live-mode tick for the home-screen widget. Android's own widget floor is 30
// minutes; anything faster needs the foreground service, which is what this
// drives. Five seconds rather than two: it still reads as live on a glance-at-it
// widget, while cutting CPU wakes and requests by 60%. Gold does not move enough
// in three seconds to be worth the battery.
const kLiveIntervalMs = 5000;


// Beyond this the relay's number is not "live" any more. MCX ticks continuously
// in session, so a gap this long means the poll died or the market is shut.
const kStaleAfter = Duration(seconds: 150);

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  runApp(const GoldApp());
}

class GoldApp extends StatelessWidget {
  const GoldApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Gold',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          brightness: Brightness.dark,
          scaffoldBackgroundColor: const Color(0xFF0B0B0D),
          fontFamily: 'Roboto',
        ),
        home: const PriceScreen(),
      );
}

/// One instrument's last state.
class Leg {
  final double? price;
  final double? change;
  final double? perChange;
  final String? label;
  final String? note;
  final bool stale;

  const Leg({
    this.price,
    this.change,
    this.perChange,
    this.label,
    this.note,
    this.stale = true,
  });

  static const empty = Leg();
}

class PriceScreen extends StatefulWidget {
  const PriceScreen({super.key});
  @override
  State<PriceScreen> createState() => _PriceScreenState();
}

class _PriceScreenState extends State<PriceScreen> with WidgetsBindingObserver {
  Timer? _timer;
  String _relay = kRelayDefault;
  String _token = '';
  Leg _xau = Leg.empty;
  Leg _gm = Leg.empty;
  DateTime? _cachedAt;
  String _status = 'connecting';
  bool _healthy = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _load();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _stop();
    super.dispose();
  }

  // Polling only while the app is actually on screen. A five-second timer running
  // in the background would be a battery complaint, not a feature.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _start();
    } else {
      _stop();
    }
  }

  Future<void> _load() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _relay = prefs.getString('relay') ?? kRelayDefault;
      _token = prefs.getString('token') ?? kTokenDefault;
      _restore(prefs);
    });
    _start();
  }

  /// Rehydrate the last values seen, marked stale.
  ///
  /// Without this, killing the app and reopening it with the relay unreachable
  /// shows two blank dashes -- the process lost the only copy. A price from an
  /// hour ago, labelled as such, is more use than nothing at all.
  void _restore(SharedPreferences prefs) {
    final at = prefs.getInt('cache_at');
    if (at == null) return;
    _cachedAt = DateTime.fromMillisecondsSinceEpoch(at);
    final age = _ageLabel(_cachedAt!);
    if (prefs.containsKey('cache_xau')) {
      _xau = Leg(
        price: prefs.getDouble('cache_xau'),
        perChange: prefs.getDouble('cache_xau_pct'),
        label: 'last seen',
        note: age,
        stale: true,
      );
    }
    if (prefs.containsKey('cache_gm')) {
      _gm = Leg(
        price: prefs.getDouble('cache_gm'),
        change: prefs.getDouble('cache_gm_chg'),
        perChange: prefs.getDouble('cache_gm_pct'),
        label: prefs.getString('cache_gm_sym'),
        note: age,
        stale: true,
      );
    }
    _status = 'last seen $age';
  }

  static String _ageLabel(DateTime t) {
    final d = DateTime.now().difference(t);
    if (d.inMinutes < 1) return 'moments ago';
    if (d.inMinutes < 60) return '${d.inMinutes}m ago';
    if (d.inHours < 24) return '${d.inHours}h ago';
    return '${d.inDays}d ago';
  }

  Future<void> _cache() async {
    final prefs = await SharedPreferences.getInstance();
    if (_xau.price != null && !_xau.stale) {
      await prefs.setDouble('cache_xau', _xau.price!);
      if (_xau.perChange != null) await prefs.setDouble('cache_xau_pct', _xau.perChange!);
    }
    if (_gm.price != null && !_gm.stale) {
      await prefs.setDouble('cache_gm', _gm.price!);
      if (_gm.change != null) await prefs.setDouble('cache_gm_chg', _gm.change!);
      if (_gm.perChange != null) await prefs.setDouble('cache_gm_pct', _gm.perChange!);
      if (_gm.label != null) await prefs.setString('cache_gm_sym', _gm.label!);
    }
    await prefs.setInt('cache_at', DateTime.now().millisecondsSinceEpoch);
  }

  void _start() {
    _timer ??= Timer.periodic(kRefresh, (_) => _tick());
    _tick();
  }

  void _stop() {
    _timer?.cancel();
    _timer = null;
  }

  Future<void> _tick() async {
    final relayOk = await _fromRelay();
    // The relay carries both legs. When it is unreachable, spot is still public,
    // so XAUUSD is refetched directly rather than left to rot alongside GOLDM.
    if (!relayOk) await _spotOnly();
    unawaited(_cache());
    if (mounted) setState(() {});
  }

  Future<bool> _fromRelay() async {
    if (await _tryRelay(_relay)) return true;
    // Always worth a second attempt at the known-good address. Gating this on the
    // configured URL being a ts.net name meant a stale saved URL -- an old LAN
    // address, say -- could never fall back, which is precisely the case that
    // needs rescuing.
    if (_relay.trim() != kRelayFallback && kRelayFallback.isNotEmpty) {
      if (await _tryRelay(kRelayFallback)) {
        _status = 'via fallback address';
        return true;
      }
    }
    return false;
  }

  Future<bool> _tryRelay(String base) async {
    if (base.trim().isEmpty) return false;
    try {
      final uri = Uri.parse('${base.trim()}/prices.json'
          '?t=${DateTime.now().millisecondsSinceEpoch}'
          '${_token.isEmpty ? '' : '&k=$_token'}');
      final r = await http.get(uri).timeout(const Duration(seconds: 6));
      if (r.statusCode == 401) {
        _status = 'relay rejected the token';
        return false;
      }
      if (r.statusCode != 200) throw Exception('HTTP ${r.statusCode}');
      final d = jsonDecode(r.body) as Map<String, dynamic>;

      final age = (d['age_seconds'] as num?)?.toDouble() ?? 0;
      final old = age > kStaleAfter.inSeconds;

      final x = (d['xauusd'] ?? {}) as Map<String, dynamic>;
      final g = (d['goldm'] ?? {}) as Map<String, dynamic>;

      _xau = Leg(
        price: (x['price'] as num?)?.toDouble(),
        perChange: (x['per_change'] as num?)?.toDouble(),
        label: (x['source'] as String?)?.split(' ').first,
        note: _range(x),
        stale: x['price'] == null || (x['stale'] == true),
      );
      _gm = Leg(
        price: (g['price'] as num?)?.toDouble(),
        change: (g['change'] as num?)?.toDouble(),
        perChange: (g['per_change'] as num?)?.toDouble(),
        label: g['tradingsymbol'] as String?,
        note: g['open_interest'] == null ? null : 'OI ${_grp(g['open_interest'])}',
        stale: g['price'] == null || (g['stale'] == true) || old,
      );
      _healthy = !old;
      _status = 'updated ${(d['generated_at_ist'] as String? ?? '').padRight(19).substring(11).trim()}';
      return true;
    } catch (_) {
      return false;
    }
  }

  String? _range(Map<String, dynamic> x) {
    final lo = (x['day_low'] as num?)?.toDouble();
    final hi = (x['day_high'] as num?)?.toDouble();
    if (lo == null || hi == null) return null;
    // Was labelled "CMX" back when the range came straight from COMEX. The
    // publisher now tracks spot's own session high/low (see _SpotSession in
    // price_publisher.py), so the range is the same instrument as the price --
    // the old label would have been actively misleading, claiming a different
    // source than what is actually shown.
    return '${_usd(lo)}–${_usd(hi)}';
  }

  Future<void> _spotOnly() async {
    _healthy = false;
    _gm = Leg(
      price: _gm.price,
      change: _gm.change,
      perChange: _gm.perChange,
      label: _gm.label,
      note: _cachedAt == null ? _gm.note : _ageLabel(_cachedAt!),
      stale: true,
    );
    try {
      final r = await http.get(Uri.parse(kSpotUrl)).timeout(const Duration(seconds: 6));
      if (r.statusCode != 200) throw Exception('HTTP ${r.statusCode}');
      final d = jsonDecode(r.body) as Map<String, dynamic>;
      _xau = Leg(
        price: (d['price'] as num?)?.toDouble(),
        label: 'gold-api.com',
        note: 'relay offline',
        stale: false,
      );
      _status = 'spot only — relay unreachable';
    } catch (e) {
      _xau = Leg(
        price: _xau.price,
        change: _xau.change,
        perChange: _xau.perChange,
        label: _xau.label,
        note: _cachedAt == null ? _xau.note : _ageLabel(_cachedAt!),
        stale: true,
      );
      // Both legs are down, which almost always means the phone -- not the relay.
      // Naming the likely cause beats a bare "no feed" the user cannot act on.
      final why = e.toString().toLowerCase();
      final hint = why.contains('failed host lookup') || why.contains('nodename')
          ? 'DNS failing'
          : why.contains('timed out') || why.contains('timeout')
              ? 'no route'
              : 'offline?';
      _status = 'relay + internet unreachable · $hint';
    }
  }

  Future<void> _settings() async {
    // A full screen rather than an AlertDialog: four actions in a dialog's button
    // row wrapped into a ragged vertical column, and "Live mode" was a button for
    // what is really a persisted on/off state -- a switch showing its current
    // value is the honest control for that.
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => SettingsScreen(relay: _relay, token: _token),
      ),
    );
    if (changed != true) return;
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _relay = prefs.getString('relay') ?? kRelayDefault;
      _token = prefs.getString('token') ?? '';
    });
    _tick();
  }

  // ---- formatting

  static String _usd(double v) => v.toStringAsFixed(2).replaceAllMapped(
      RegExp(r'\B(?=(\d{3})+(?!\d))'), (m) => ',');

  /// Indian digit grouping: last three, then pairs. 157434 -> 1,57,434
  static String _grp(Object? v) {
    if (v == null) return '';
    final s = (v is num ? v.round() : int.tryParse('$v') ?? 0).abs().toString();
    if (s.length <= 3) return s;
    final head = s.substring(0, s.length - 3);
    final tail = s.substring(s.length - 3);
    final buf = StringBuffer();
    for (var i = 0; i < head.length; i++) {
      if (i > 0 && (head.length - i) % 2 == 0) buf.write(',');
      buf.write(head[i]);
    }
    return '$buf,$tail';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 14, 14, 8),
          child: Column(
            children: [
            const Spacer(),
            _Card(
                symbol: 'XAU / USD',
                value: _xau.price == null ? '····' : '\$${_usd(_xau.price!)}',
                leg: _xau,
                changeText: _xau.perChange == null
                    ? '—'
                    : '${_xau.perChange! > 0 ? '+' : ''}${_xau.perChange!.toStringAsFixed(2)}%',
            ),
            const SizedBox(height: 14),
            _Card(
                symbol: 'GOLDM · MCX',
                value: _gm.price == null ? '····' : '₹${_grp(_gm.price)}',
                leg: _gm,
                changeText: _gm.perChange == null
                    ? '—'
                    : '${_gm.change! > 0 ? '+' : '-'}${_grp(_gm.change!.abs())}   '
                        '${_gm.perChange! > 0 ? '+' : ''}${_gm.perChange!.toStringAsFixed(2)}%',
            ),
            const Spacer(),
            Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
              Row(children: [
                Container(
                  width: 7,
                  height: 7,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: _healthy ? const Color(0xFF2FBF71) : const Color(0xFFF0554B),
                  ),
                ),
                const SizedBox(width: 7),
                Text(_status, style: const TextStyle(fontSize: 11.5, color: Color(0xFF8A8A94))),
              ]),
              GestureDetector(
                onTap: _settings,
                behavior: HitTestBehavior.opaque,
                child: const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 6, vertical: 4),
                  child: Icon(Icons.settings, size: 17, color: Color(0xFF8A8A94)),
                ),
              ),
            ]),
          ]),
        ),
      ),
    );
  }
}

class _Card extends StatelessWidget {
  final String symbol;
  final String value;
  final String changeText;
  final Leg leg;

  const _Card({
    required this.symbol,
    required this.value,
    required this.changeText,
    required this.leg,
  });

  @override
  Widget build(BuildContext context) {
    final pc = leg.perChange ?? 0;
    final tint = pc > 0
        ? const Color(0xFF2FBF71)
        : pc < 0
            ? const Color(0xFFF0554B)
            : const Color(0xFF8A8A94);

    return Opacity(
      opacity: leg.stale ? 0.42 : 1,
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.fromLTRB(22, 18, 22, 18),
        decoration: BoxDecoration(
          color: const Color(0xFF141418),
          borderRadius: BorderRadius.circular(20),
          border: Border.all(color: const Color(0xFF26262C)),
        ),
        child: Stack(children: [
          if (leg.stale)
            Positioned(
              top: 0,
              right: 0,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
                decoration: BoxDecoration(
                  border: Border.all(color: const Color(0xFFF0554B)),
                  borderRadius: BorderRadius.circular(99),
                ),
                child: const Text('STALE',
                    style: TextStyle(
                        fontSize: 9,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 1.1,
                        color: Color(0xFFF0554B))),
              ),
            ),
          Column(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
                Text(symbol,
                    style: const TextStyle(
                        fontSize: 14,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 1.9,
                        color: Color(0xFF8A8A94))),
                if (leg.label != null)
                  Flexible(
                    child: Padding(
                      padding: EdgeInsets.only(left: 24, right: leg.stale ? 68 : 4),
                      child: Text(leg.label!,
                          overflow: TextOverflow.ellipsis,
                          textAlign: TextAlign.right,
                          style: const TextStyle(fontSize: 11, color: Color(0xFF8A8A94))),
                    ),
                  ),
              ]),
              const SizedBox(height: 6),
              FittedBox(
                fit: BoxFit.scaleDown,
                alignment: Alignment.centerLeft,
                child: Text(
                  value,
                  maxLines: 1,
                  style: const TextStyle(
                    fontSize: 86,
                    fontWeight: FontWeight.w800,
                    height: 1.02,
                    letterSpacing: -2.4,
                    fontFeatures: [FontFeature.tabularFigures()],
                    color: Color(0xFFF5F5F7),
                  ),
                ),
              ),
              const SizedBox(height: 6),
              Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
                Text(changeText,
                    style: TextStyle(
                      fontSize: 21,
                      fontWeight: FontWeight.w700,
                      fontFeatures: const [FontFeature.tabularFigures()],
                      color: tint,
                    )),
                if (leg.note != null)
                  Flexible(
                    child: Text(leg.note!,
                        overflow: TextOverflow.ellipsis,
                        textAlign: TextAlign.right,
                        style: const TextStyle(fontSize: 11.5, color: Color(0xFF8A8A94))),
                  ),
              ]),
            ],
          ),
        ]),
      ),
    );
  }
}


/// Settings, as a screen rather than a dialog.
///
/// The dialog it replaces had four actions competing for one button row, which
/// Material wraps into a ragged column once they no longer fit. Two of those
/// actions were not dialog actions at all: adding the widget and toggling live
/// updates are things you *do*, not ways to dismiss a form.
class SettingsScreen extends StatefulWidget {
  final String relay;
  final String token;
  const SettingsScreen({super.key, required this.relay, required this.token});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _relay =
      TextEditingController(text: widget.relay);
  late final TextEditingController _token =
      TextEditingController(text: widget.token);
  bool _live = false;
  bool _busy = false;
  bool _dirty = false;

  static const _dim = Color(0xFF8A8A94);
  static const _card = Color(0xFF141418);
  static const _line = Color(0xFF26262C);
  static const _gold = Color(0xFFE8B53A);

  @override
  void initState() {
    super.initState();
    // Reflect the real state of the ticker rather than assuming it is off.
    SharedPreferences.getInstance().then((p) {
      if (mounted) setState(() => _live = p.getBool('live') ?? false);
    });
  }

  @override
  void dispose() {
    _relay.dispose();
    _token.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('relay', _relay.text.trim());
    await prefs.setString('token', _token.text.trim());
    if (mounted) Navigator.pop(context, true);
  }

  Future<void> _toggleLive(bool on) async {
    if (_busy) return;
    setState(() => _busy = true);
    final messenger = ScaffoldMessenger.of(context);
    bool ok;
    if (on) {
      ok = await kWidgetChannel
              .invokeMethod<bool>('startLive', {'intervalMs': kLiveIntervalMs})
              .catchError((_) => false) ??
          false;
    } else {
      await kWidgetChannel.invokeMethod<bool>('stopLive').catchError((_) => false);
      ok = true;
    }
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('live', on && ok);
    if (!mounted) return;
    setState(() {
      _live = on && ok;
      _busy = false;
    });
    if (on && !ok) {
      // The native side refuses to start a ticker with no widget bound: it would
      // hold a notification and drain battery updating nothing.
      messenger.showSnackBar(const SnackBar(
        content: Text('Add the widget to your home screen first'),
      ));
    }
  }

  Future<void> _pin() async {
    final messenger = ScaffoldMessenger.of(context);
    final ok =
        await kWidgetChannel.invokeMethod<bool>('pinWidget').catchError((_) => false);
    messenger.showSnackBar(SnackBar(
      content: Text(ok == true
          ? 'Check your home screen'
          : 'Your launcher will not add it this way \u2014 use the widget drawer'),
    ));
  }

  Widget _label(String text) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 26, 4, 10),
        child: Text(
          text.toUpperCase(),
          style: const TextStyle(
            fontSize: 11,
            fontWeight: FontWeight.w700,
            letterSpacing: 1.6,
            color: _dim,
          ),
        ),
      );

  Widget _note(String text) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 10, 4, 0),
        child: Text(text,
            style: const TextStyle(fontSize: 12, color: _dim, height: 1.45)),
      );

  Widget _group(List<Widget> children) => Container(
        decoration: BoxDecoration(
          color: _card,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: _line),
        ),
        child: Column(children: children),
      );

  @override
  Widget build(BuildContext context) {
    final seconds = (kLiveIntervalMs / 1000).toStringAsFixed(0);
    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF0B0B0D),
        surfaceTintColor: Colors.transparent,
        title: const Text('Settings', style: TextStyle(fontSize: 18)),
        actions: [
          TextButton(
            // Disabled until something actually changes, so the affordance says
            // whether there is anything to save.
            onPressed: _dirty ? _save : null,
            child: Text(
              'Save',
              style: TextStyle(
                fontWeight: FontWeight.w700,
                color: _dirty ? _gold : _dim,
              ),
            ),
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 32),
        children: [
          _label('Relay'),
          _group([
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 16, 4),
              child: TextField(
                controller: _relay,
                autocorrect: false,
                keyboardType: TextInputType.url,
                onChanged: (_) => setState(() => _dirty = true),
                decoration: const InputDecoration(
                  labelText: 'URL',
                  border: InputBorder.none,
                  isDense: true,
                ),
              ),
            ),
            const Divider(height: 1, color: _line),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 14),
              child: TextField(
                controller: _token,
                autocorrect: false,
                onChanged: (_) => setState(() => _dirty = true),
                decoration: const InputDecoration(
                  labelText: 'Token',
                  border: InputBorder.none,
                  isDense: true,
                ),
              ),
            ),
          ]),
          _note('GOLDM comes from the relay. XAUUSD is public and keeps working '
              'without it.'),
          _label('Home screen'),
          _group([
            ListTile(
              contentPadding: const EdgeInsets.symmetric(horizontal: 16),
              leading: const Icon(Icons.add_to_home_screen, color: _dim),
              title: const Text('Add widget', style: TextStyle(fontSize: 15)),
              subtitle: const Text('Ask the launcher to place it',
                  style: TextStyle(fontSize: 12, color: _dim)),
              onTap: _pin,
            ),
            const Divider(height: 1, color: _line),
            SwitchListTile(
              contentPadding: const EdgeInsets.symmetric(horizontal: 16),
              secondary: const Icon(Icons.bolt, color: _dim),
              title: const Text('Live updates', style: TextStyle(fontSize: 15)),
              subtitle: Text(
                _live
                    ? 'Widget updating every ${seconds}s'
                    : 'Widget updates every 30 min',
                style: const TextStyle(fontSize: 12, color: _dim),
              ),
              value: _live,
              onChanged: _busy ? null : _toggleLive,
            ),
          ]),
          _note('Live updates keep a permanent notification and use noticeably '
              'more battery. Android has no way to refresh a widget faster '
              'without one.'),
        ],
      ),
    );
  }
}
