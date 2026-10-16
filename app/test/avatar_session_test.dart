import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:wobot/avatar/avatar_session.dart';
import 'package:wobot/chat.dart';
import 'package:wobot/robot/director.dart';
import 'package:wobot/robot/link.dart';
import 'package:wobot/robot/robot.dart';
import 'package:wobot/voice/listener.dart';
import 'package:wobot/voice/speech_queue.dart';

import 'fake_robot.dart';
import 'fake_server.dart';
import 'fake_speech.dart';

/// A `speech` event; its audio holds `audio<index>`, so what was played can be read.
Map<String, Object> speech(
  int index,
  String text, {
  String emotion = 'neutral',
}) => {
  'turn_id': 't',
  'attempt': 1,
  'index': index,
  'text': text,
  'emotion': emotion,
  'audio': base64Encode('audio$index'.codeUnits),
};

const _state = {
  'turn_id': 't',
  'attempt': 1,
  'status': 'cancelled',
  'heard_upto': null,
  'speech_stopped': true,
};

/// A robot that only notes what Avatar mode tells it.
class RecordingRobot extends Robot {
  RecordingRobot()
    : super(RobotLink(FakeBle()..state = Bluetooth.denied, FakeMemory()));

  final cues = <String>[];

  @override
  void listening() => cues.add('listening');

  @override
  void waiting() => cues.add('waiting');

  @override
  void speaking(Spoken line) => cues.add(
    'speaking ${line.index} ${line.emotion} ${line.route} reading=${line.reading}',
  );

  @override
  void rested() => cues.add('rested');

  @override
  void idle({bool error = false}) => cues.add(error ? 'error' : 'idle');
}

void main() {
  late FakeServer server;
  late ChatController chat;
  late FakeEngine engine;
  late FakePlayer player;
  late AvatarSession session;
  late List<String> faces;

  setUp(() {
    server = FakeServer()
      ..reply('POST /v1/conversations', 201, jsonEncode(newConversation))
      ..reply('POST /v1/turns/*/playback', 200, jsonEncode(_state))
      ..reply('POST /v1/turns/*/cancel', 200, jsonEncode(_state));
    final api = server.api();
    chat = ChatController(api, HistoryController(api));
    engine = FakeEngine();
    player = FakePlayer();
    session = AvatarSession(
      api: api,
      chat: chat,
      listener: QuestionListener(
        engine,
        pause: const Duration(milliseconds: 100),
        silence: const Duration(milliseconds: 300),
        limit: const Duration(seconds: 2),
        finalWait: const Duration(milliseconds: 20),
      ),
      queue: SpeechQueue(player),
      locale: () async => 'zh-TW',
      previousWait: const Duration(milliseconds: 300),
      hold: const Duration(milliseconds: 50),
    );
    faces = [];
    session.addListener(() {
      if (faces.isEmpty || faces.last != session.expression) {
        faces.add(session.expression);
      }
    });
  });

  tearDown(() => session.dispose());

  /// Taps the button and says the words; the question ends at the pause after them.
  Future<void> ask(String words) async {
    final done = session.talk();
    await pumpEventQueue();
    engine.say(words);
    await done;
  }

  test(
    'a question is sent in Avatar mode and its reply spoken, each sentence with its face',
    () async {
      server.reply(
        'POST /v1/conversations/c1/turns',
        200,
        sse([
          ('accepted', {'turn_id': 't', 'attempt': 1}),
          ('reply', {'text': '太好了，我知道！GRC 在桃園。'}),
          ('speech', speech(0, '太好了，我知道！', emotion: 'joy')),
          ('speech', speech(1, 'GRC 在桃園。')),
          ('completed', {'next': null}),
        ]),
      );

      await ask('GRC 在哪裡');
      await pumpEventQueue();

      final turn = chat.turns.single;
      expect(server.bodies['POST /v1/conversations/c1/turns'], {
        'turn_id': turn.id,
        'text': 'GRC 在哪裡',
        'mode': 'avatar',
      });
      expect(player.said, ['audio0', 'audio1']);
      expect(faces, containsAllInOrder(['thinking', 'joy', 'neutral']));
      expect(session.state, AvatarState.idle);
      expect(server.bodies['POST /v1/turns/${turn.id}/playback'], {
        'attempt': 1,
        'heard': 2,
        'stopped': false,
      });
    },
  );

  test(
    'a tap while it speaks stops the voice at once and listens; the text goes on',
    () async {
      final reply = StreamController<List<int>>();
      server.routes['POST /v1/conversations/c1/turns'] = () async =>
          http.StreamedResponse(reply.stream, 200);
      player.held = true;
      final first = session.talk();
      await pumpEventQueue();
      engine.say('說個故事');
      await Future<void>.delayed(const Duration(milliseconds: 200));
      final turn = chat.turns.single;
      reply.add(
        utf8.encode(
          sse([
            ('accepted', {'turn_id': turn.id, 'attempt': 1}),
            ('speech', speech(0, '從前從前有一隻熊。')),
          ]),
        ),
      );
      await pumpEventQueue();
      expect(session.state, AvatarState.speaking);

      final second = session.talk();
      await pumpEventQueue();

      expect(player.stops, greaterThan(0));
      expect(session.state, AvatarState.listening);
      expect(server.bodies['POST /v1/turns/${turn.id}/playback'], {
        'attempt': 1,
        'heard': 0,
        'stopped': true,
      });
      reply.add(
        utf8.encode(
          sse([
            ('speech', speech(1, '牠住在森林裡。')),
            ('reply', {'text': '從前從前有一隻熊。牠住在森林裡。'}),
            ('completed', {'next': null}),
          ]),
        ),
      );
      await reply.close();
      await first;
      expect(player.said, ['audio0']); // the late sentence is never played
      expect(turn.state, TurnState.completed);
      expect(turn.text, '從前從前有一隻熊。牠住在森林裡。');
      await session.pause();
      await second;
    },
  );

  test(
    'a question waits for the reply before it, and stops one stuck too long',
    () async {
      var sent = 0;
      final stuck = StreamController<List<int>>();
      server.routes['POST /v1/conversations/c1/turns'] = () async => sent++ == 0
          ? http.StreamedResponse(stuck.stream, 200)
          : http.StreamedResponse(
              Stream.value(
                utf8.encode(
                  sse([
                    ('accepted', {'turn_id': 't2', 'attempt': 1}),
                    ('reply', {'text': '好。'}),
                    ('completed', {'next': null}),
                  ]),
                ),
              ),
              200,
            );
      unawaited(chat.send('先寫的問題'));
      await pumpEventQueue();
      final before = chat.turns.single;
      stuck.add(
        utf8.encode(
          sse([
            ('accepted', {'turn_id': before.id, 'attempt': 1}),
            ('text_delta', {'text': '寫到一半'}),
          ]),
        ),
      );
      await pumpEventQueue();

      await ask('下一個問題');

      expect(server.count('POST /v1/turns/${before.id}/cancel'), 1);
      expect(before.state, TurnState.cancelled);
      expect(before.text, '寫到一半');
      expect(chat.turns.last.question, '下一個問題');
      expect(chat.turns.last.state, TurnState.completed);
      await stuck.close();
    },
  );

  test('nothing heard says so and sends nothing', () async {
    final done = session.talk();
    await pumpEventQueue();
    await done; // the silence limit ends it

    expect(session.error, 'No speech detected. Try again.');
    expect(session.state, AvatarState.idle);
    expect(server.count('POST /v1/conversations/c1/turns'), 0);
  });

  test('a list is read on from where its turn left off', () async {
    server
      ..reply(
        'POST /v1/conversations/c1/turns',
        200,
        sse([
          ('accepted', {'turn_id': 't', 'attempt': 1}),
          ('reply', {'text': '共 5 場演講。'}),
          ('speech', speech(0, '共 5 場演講。')),
          ('completed', {'next': 3}),
        ]),
      )
      ..reply(
        'POST /v1/turns/*/continue-reading',
        200,
        sse([
          ('speech', speech(0, '2024/01/04，演講 4')),
          ('speech', speech(1, '2024/01/05，演講 5')),
          ('completed', {'next': null}),
        ]),
      );
    await ask('列出演講');
    expect(session.canReadOn, isTrue);

    await session.readOn();

    final turn = chat.turns.single;
    expect(server.bodies['POST /v1/turns/${turn.id}/continue-reading'], {
      'attempt': 1,
      'from': 3,
    });
    expect(player.said, ['audio0', 'audio0', 'audio1']);
    expect(session.canReadOn, isFalse);
  });

  test('a refused question shows why', () async {
    server.reply(
      'POST /v1/conversations/c1/turns',
      409,
      jsonEncode({
        'error': {
          'code': 'VOICE_NOT_READY',
          'message': 'Set up your voice to use Avatar mode.',
        },
      }),
    );

    await ask('你好');

    expect(session.error, 'Set up your voice to use Avatar mode.');
    expect(session.state, AvatarState.idle);
    expect(session.expression, 'neutral');
  });

  test(
    'the robot is told each step: listening, waiting, each sentence, the end',
    () async {
      final robot = RecordingRobot();
      final api = server.api();
      final withRobot = AvatarSession(
        api: api,
        chat: chat,
        listener: QuestionListener(
          engine,
          pause: const Duration(milliseconds: 100),
          silence: const Duration(milliseconds: 300),
          limit: const Duration(seconds: 2),
          finalWait: const Duration(milliseconds: 20),
        ),
        queue: SpeechQueue(player),
        locale: () async => 'zh-TW',
        robot: robot,
        hold: const Duration(milliseconds: 50),
      );
      server.reply(
        'POST /v1/conversations/c1/turns',
        200,
        sse([
          ('accepted', {'turn_id': 't', 'attempt': 1}),
          ('status', {'stage': 'writing', 'route': 'chat'}),
          ('speech', speech(0, '太好了，我知道！', emotion: 'joy')),
          ('speech', speech(1, 'GRC 在桃園。')),
          ('reply', {'text': '太好了，我知道！GRC 在桃園。'}),
          ('completed', {'next': null}),
        ]),
      );

      final done = withRobot.talk();
      await pumpEventQueue();
      engine.say('GRC 在哪裡');
      await done;
      await pumpEventQueue();

      expect(chat.turns.single.route, 'chat');
      expect(robot.cues, [
        'listening',
        'waiting',
        'speaking 0 joy chat reading=false',
        'speaking 1 neutral chat reading=false',
        'rested',
      ]);
      withRobot.dispose();
    },
  );
}
