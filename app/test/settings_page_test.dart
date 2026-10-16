import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/api.dart';
import 'package:wobot/pages/settings_page.dart';
import 'package:wobot/theme.dart';

import 'fake_server.dart';

void main() {
  group('deleting the account', () {
    late FakeServer server;
    late List<String> steps;

    Future<void> show(WidgetTester tester, {bool signsInAgain = true}) async {
      server = FakeServer()
        ..reply('GET /v1/me', 200, meJson())
        ..reply('DELETE /v1/me', 202, '');
      steps = [];
      await tester.pumpWidget(
        MaterialApp(
          theme: wobotTheme(),
          home: SettingsPage(
            api: server.api(),
            accountId: 'uid-1',
            email: 'member@example.com',
            idToken: () async => 'token',
            settings: const AssistantSettings(chatbotName: 'Wobot', version: 0),
            onSettings: (_) {},
            beforeSignOut: () async => steps.add('stopped'),
            frames: MemoryFrameCache(),
            reauthenticate: () async {
              steps.add('signed in again');
              return signsInAgain;
            },
            deleteUser: () async => steps.add('user deleted'),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(find.text('Delete account'), 200);
      await tester.tap(find.text('Delete account'));
      await tester.pumpAndSettle();
    }

    testWidgets('signs in again, deletes it, then deletes the user', (
      tester,
    ) async {
      await show(tester);
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();

      expect(server.count('DELETE /v1/me'), 1);
      expect(steps, ['signed in again', 'stopped', 'user deleted']);
    });

    testWidgets('asks first, and cancelling deletes nothing', (tester) async {
      await show(tester);
      expect(find.text('Delete account?'), findsOneWidget);
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(server.count('DELETE /v1/me'), 0);
      expect(steps, isEmpty);
    });

    testWidgets('a sign-in the user cancels deletes nothing', (tester) async {
      await show(tester, signsInAgain: false);
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();

      expect(server.count('DELETE /v1/me'), 0);
      expect(steps, ['signed in again']);
    });
  });
}
