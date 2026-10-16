import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/avatar/avatar_look.dart';
import 'package:wobot/avatar/avatar_session.dart';
import 'package:wobot/avatar/frames.dart';
import 'package:wobot/chat.dart';
import 'package:wobot/pages/avatar_page.dart';
import 'package:wobot/robot/link.dart';
import 'package:wobot/robot/robot.dart';
import 'package:wobot/theme.dart';
import 'package:wobot/voice/listener.dart';
import 'package:wobot/voice/speech_queue.dart';

import 'fake_robot.dart';
import 'fake_server.dart';
import 'fake_speech.dart';

/// A transparent PNG, one pixel square: every frame in these tests.
final _pixel = base64Decode(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
);

void main() {
  late FakeEngine engine;
  late AvatarSession session;

  Future<void> show(WidgetTester tester, {Robot? robot}) async {
    final server = FakeServer();
    final api = server.api();
    final chat = ChatController(api, HistoryController(api));
    engine = FakeEngine();
    session = AvatarSession(
      api: api,
      chat: chat,
      listener: QuestionListener(engine),
      queue: SpeechQueue(FakePlayer()),
      locale: () async => 'zh-TW',
      robot: robot,
    );
    await tester.pumpWidget(
      MaterialApp(
        theme: wobotTheme(),
        home: AvatarPage(
          look: Future.value(
            AvatarLook({for (final key in frameKeys) key: MemoryImage(_pixel)}),
          ),
          session: session,
          chat: chat,
        ),
      ),
    );
    await tester.pump();
  }

  /// Closes the page, which ends the session and its timers.
  Future<void> close(WidgetTester tester) async {
    await tester.pumpWidget(const SizedBox());
    await tester.pump(const Duration(seconds: 1));
  }

  /// The first tap; the controls then fade in.
  Future<void> reveal(WidgetTester tester) async {
    await tester.tap(find.byKey(AvatarPage.revealKey));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));
  }

  Finder control(String tooltip) => find.byTooltip(tooltip).hitTestable();

  testWidgets(
    'the first tap only shows the controls; they hide after a while',
    (tester) async {
      await show(tester);
      expect(control('Speak'), findsNothing);

      await reveal(tester);

      expect(control('Speak'), findsOneWidget);
      expect(control('Show transcript'), findsOneWidget);
      expect(engine.onResult, isNull); // the tap pressed nothing

      await tester.pump(AvatarPage.hideAfter);
      await tester.pump(const Duration(milliseconds: 200));
      expect(control('Speak'), findsNothing);
      await close(tester);
    },
  );

  testWidgets('the controls stay while a question is heard', (tester) async {
    await show(tester);
    await reveal(tester);

    await tester.tap(control('Speak'));
    await tester.pump();
    await tester.pump();
    expect(session.state, AvatarState.listening);
    await tester.pump(const Duration(seconds: 5)); // past the time to hide

    expect(control('Send'), findsOneWidget);
    expect(find.text('Listening…'), findsOneWidget);

    await session.pause();
    await tester.pump(AvatarPage.hideAfter);
    await tester.pump(const Duration(milliseconds: 200));
    expect(control('Speak'), findsNothing); // free to hide once it is over
    await close(tester);
  });

  testWidgets('hidden controls are not read out by a screen reader', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    await show(tester);
    SemanticsFinder speak() =>
        find.semantics.byPredicate((node) => node.tooltip == 'Speak');

    expect(speak(), findsNothing);
    expect(find.semantics.byLabel('Show controls'), findsOne);

    await reveal(tester);
    expect(speak(), findsOne);
    await close(tester);
    semantics.dispose();
  });

  testWidgets(
    'without Bluetooth the robot only says why; the avatar is the same',
    (tester) async {
      final ble = FakeBle()..state = Bluetooth.denied;
      final robot = Robot(RobotLink(ble, FakeMemory()));
      await show(tester, robot: robot);
      await tester.pump();
      expect(robot.status.value.state, RobotState.unavailable);

      await reveal(tester);
      const why = 'Wobot may not use Bluetooth. Allow it in Settings.';
      expect(control(why), findsOneWidget);
      expect(control('Speak'), findsOneWidget);

      await tester.tap(control(why));
      await tester.pump();
      expect(find.text('Looking for the robot…'), findsOneWidget);
      expect(ble.scans, 0);
      await close(tester);
    },
  );

  testWidgets('without a robot there is no robot button', (tester) async {
    await show(tester);
    await reveal(tester);
    expect(find.byIcon(Icons.smart_toy_outlined), findsNothing);
    await close(tester);
  });
}
