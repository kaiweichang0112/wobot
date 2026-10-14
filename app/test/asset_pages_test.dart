import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/api.dart';
import 'package:wobot/assets.dart';
import 'package:wobot/avatar/avatar_setup.dart';
import 'package:wobot/avatar/frames.dart';
import 'package:wobot/pages/avatar_setup_page.dart';
import 'package:wobot/pages/settings_page.dart';
import 'package:wobot/theme.dart';

import 'avatar_setup_test.dart' show avatarJson;
import 'fake_server.dart';

/// A 1×1 transparent PNG.
final _png = base64Decode(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
);

const _ref = AssetRef.new;

void main() {
  test('Settings says how a voice or an avatar stands', () {
    expect(assetStatus(const AssetSummary()), 'Not set up');
    expect(
      assetStatus(
        AssetSummary(
          candidate: _ref(id: 'a', status: 'running'),
        ),
      ),
      'Creating…',
    );
    expect(
      assetStatus(
        AssetSummary(
          candidate: _ref(id: 'a', status: 'ready'),
        ),
      ),
      'Ready to review',
    );
    expect(
      assetStatus(
        AssetSummary(
          candidate: _ref(id: 'a', status: 'failed'),
        ),
      ),
      'Failed',
    );
    expect(
      assetStatus(
        AssetSummary(
          active: _ref(id: 'a', status: 'active'),
        ),
      ),
      'In use',
    );
    expect(
      assetStatus(
        AssetSummary(
          active: _ref(id: 'a', status: 'active'),
          candidate: _ref(id: 'b', status: 'failed'),
        ),
      ),
      'In use · the new one failed',
    );
  });

  testWidgets('Settings shows the voice and the avatar as they stand', (
    tester,
  ) async {
    final server = FakeServer()
      ..reply(
        'GET /v1/me',
        200,
        meJson(
          voice: {
            'active': {'id': 'v1', 'status': 'active', 'error_code': null},
            'candidate': null,
          },
          avatar: {
            'active': null,
            'candidate': {'id': 'a1', 'status': 'running', 'error_code': null},
          },
        ),
      );
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
          beforeSignOut: () async {},
          frames: MemoryFrameCache(),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('In use'), findsOneWidget);
    expect(find.text('Creating…'), findsOneWidget);
  });

  group('the Avatar page', () {
    late FakeServer server;

    Future<void> show(WidgetTester tester, {required int ready}) async {
      // A phone's screen, 390 × 844 points.
      tester.view
        ..physicalSize = const Size(1170, 2532)
        ..devicePixelRatio = 3;
      addTearDown(tester.view.reset);
      server = FakeServer()
        ..reply(
          'GET /v1/me',
          200,
          meJson(
            avatar: {
              'active': null,
              'candidate': {'id': 'a1', 'status': 'ready', 'error_code': null},
            },
          ),
        )
        ..reply(
          'GET /v1/avatars/a1',
          200,
          jsonEncode(avatarJson('a1', status: 'ready', ready: ready)),
        );
      for (final key in frameKeys) {
        server.replyBytes(
          'GET /v1/avatars/a1/frames/$key',
          _png,
          headers: {'etag': '"$key-1"'},
        );
      }
      await tester.pumpWidget(
        MaterialApp(
          theme: wobotTheme(),
          home: AvatarSetupPage(
            controller: AvatarSetupController(
              server.api(),
              MemoryFrameCache(),
              pollEvery: const Duration(hours: 1),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
    }

    Future<FilledButton> useButton(WidgetTester tester) async {
      final button = find.widgetWithText(FilledButton, 'Use this avatar');
      await tester.scrollUntilVisible(button, 200);
      return tester.widget<FilledButton>(button);
    }

    testWidgets('cannot use an avatar missing a frame', (tester) async {
      await show(tester, ready: 20);

      expect(find.text('20/21'), findsOneWidget);
      expect((await useButton(tester)).onPressed, isNull);
    });

    testWidgets('uses an avatar with all 21 frames', (tester) async {
      await show(tester, ready: 21);
      server.reply(
        'POST /v1/avatars/a1/activate',
        200,
        jsonEncode(avatarJson('a1', status: 'active', ready: 21)),
      );

      expect(find.text('21/21'), findsOneWidget);
      expect((await useButton(tester)).onPressed, isNotNull);
      await tester.tap(find.text('Use this avatar'));
      await tester.pumpAndSettle();

      expect(server.count('POST /v1/avatars/a1/activate'), 1);
      expect(find.text('Delete avatar'), findsOneWidget);
    });

    testWidgets('a frame opens large, with the others a tap away', (
      tester,
    ) async {
      await show(tester, ready: 21);

      await tester.tap(find.bySemanticsLabel('joy_half ready'));
      await tester.pumpAndSettle();

      expect(find.text('Joy · half'), findsOneWidget);
      await tester.tap(find.text('Wide'));
      await tester.pumpAndSettle();
      expect(find.text('Joy · wide'), findsOneWidget);
      expect(find.text('Draw this frame again'), findsOneWidget);
    });
  });
}
