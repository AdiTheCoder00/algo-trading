// Smoke test: the app builds its shell and shows both instruments before any
// network call resolves. Guards the placeholder path -- a widget that throws
// while the feed is still in flight would show a blank screen on launch, which
// is exactly when a user is most likely to be looking at it.

import 'package:flutter_test/flutter_test.dart';

import 'package:gold/main.dart';

void main() {
  testWidgets('renders both instruments with placeholders before data arrives',
      (WidgetTester tester) async {
    await tester.pumpWidget(const GoldApp());
    await tester.pump();

    expect(find.text('XAU / USD'), findsOneWidget);
    expect(find.text('GOLDM · MCX'), findsOneWidget);

    // Both prices start as the placeholder, not as a stale or zeroed number.
    expect(find.text('····'), findsNWidgets(2));
  });
}
