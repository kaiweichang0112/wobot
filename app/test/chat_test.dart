import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:wobot/chat.dart';

import 'fake_server.dart';

void main() {
  late FakeServer server;
  late HistoryController history;
  late ChatController chat;

  setUp(() {
    server = FakeServer()
      ..reply('POST /v1/conversations', 201, jsonEncode(newConversation));
    final api = server.api();
    history = HistoryController(api);
    chat = ChatController(api, history);
  });

  test(
    'a first message starts the conversation, streams and names it',
    () async {
      server.reply(
        'POST /v1/conversations/c1/turns',
        200,
        sse([
          ('accepted', {'turn_id': 't', 'attempt': 1}),
          ('status', {'stage': 'writing'}),
          ('text_delta', {'text': '你好'}),
          ('text_delta', {'text': '，我是 Wobot'}),
          ('reply', {'text': '你好，我是 Wobot。'}),
          ('title', {'title': '打招呼'}),
          ('completed', {}),
        ]),
      );

      await chat.send('  你好  ');

      final turn = chat.turns.single;
      expect(turn.question, '你好');
      expect(turn.state, TurnState.completed);
      expect(turn.text, '你好，我是 Wobot。'); // the reply replaces the deltas
      expect(chat.busy, isFalse);
      expect(chat.conversation?.title, '打招呼');
      expect(history.conversations.single.title, '打招呼');
      expect(server.bodies['POST /v1/conversations/c1/turns'], {
        'turn_id': turn.id,
        'text': '你好',
      });
    },
  );

  test('a failed reply can be tried again, as its next attempt', () async {
    server.reply(
      'POST /v1/conversations/c1/turns',
      200,
      sse([
        ('accepted', {'turn_id': 't', 'attempt': 1}),
        ('reply', {'text': '抱歉，暫時無法回答。'}),
        (
          'error',
          {
            'code': 'PROVIDER_UNAVAILABLE',
            'message': 'Try again.',
            'retryable': true,
          },
        ),
      ]),
    );
    await chat.send('問題');
    final turn = chat.turns.single;
    expect((turn.state, turn.error), (TurnState.failed, 'Try again.'));
    expect(chat.retryable, same(turn));

    server.reply(
      'POST /v1/turns/${turn.id}/retry',
      200,
      sse([
        ('accepted', {'turn_id': turn.id, 'attempt': 2}),
        ('reply', {'text': '回答'}),
        ('completed', {}),
      ]),
    );
    await chat.retry();

    expect(
      (turn.state, turn.attempt, turn.text),
      (TurnState.completed, 2, '回答'),
    );
    expect(server.bodies['POST /v1/turns/${turn.id}/retry'], {
      'expected_attempt': 1,
    });
    expect(chat.turns, hasLength(1)); // the question is not asked twice
  });

  test('a stop keeps the words shown and tells the server', () async {
    final stream = StreamController<List<int>>();
    server.routes['POST /v1/conversations/c1/turns'] = () async =>
        http.StreamedResponse(stream.stream, 200);
    server.reply(
      'POST /v1/turns/__/cancel',
      200,
      '{}',
    ); // replaced below, once the turn's ID is known

    unawaited(chat.send('問題'));
    await pumpEventQueue();
    final turn = chat.turns.single;
    server.reply(
      'POST /v1/turns/${turn.id}/cancel',
      200,
      jsonEncode({'turn_id': turn.id, 'attempt': 1, 'status': 'cancelled'}),
    );
    stream.add(
      utf8.encode(
        sse([
          ('accepted', {'turn_id': turn.id, 'attempt': 1}),
          ('text_delta', {'text': '已經寫出'}),
        ]),
      ),
    );
    await pumpEventQueue();
    expect(chat.busy, isTrue);

    await chat.stop();
    stream.add(
      utf8.encode(
        sse([
          ('text_delta', {'text': '晚到的字'}),
        ]),
      ),
    );
    await stream.close();
    await pumpEventQueue();

    expect((turn.state, turn.text), (TurnState.cancelled, '已經寫出'));
    expect(chat.busy, isFalse);
    expect(server.bodies['POST /v1/turns/${turn.id}/cancel'], {
      'attempt': 1,
      'shown_text': '已經寫出',
    });
  });

  test('a stream that ends without saying how is read back', () async {
    server.reply(
      'POST /v1/conversations/c1/turns',
      200,
      sse([
        ('accepted', {'turn_id': 't', 'attempt': 1}),
      ]),
    );
    server.routes['GET /v1/conversations/c1/turns'] = () async {
      final id = chat.turns.single.id;
      return http.StreamedResponse(
        Stream.value(
          utf8.encode(
            jsonEncode({
              'turns': [
                {
                  'turn_id': id,
                  'user_text': '問題',
                  'status': 'cancelled',
                  'attempt': 1,
                  'reply_text': '另一台裝置停止前的字',
                  'error_code': null,
                },
              ],
              'next_cursor': null,
            }),
          ),
        ),
        200,
      );
    };

    await chat.send('問題');

    final turn = chat.turns.single;
    expect((turn.state, turn.text), (TurnState.cancelled, '另一台裝置停止前的字'));
  });

  test(
    'a message refused before its stream opens fails with the reason',
    () async {
      server.reply(
        'POST /v1/conversations/c1/turns',
        409,
        jsonEncode({
          'error': {
            'code': 'TURN_IN_PROGRESS',
            'message': 'A reply is already being written in this chat.',
          },
        }),
      );

      await chat.send('問題');

      final turn = chat.turns.single;
      expect(turn.state, TurnState.failed);
      expect(turn.error, 'A reply is already being written in this chat.');
    },
  );

  test('turn IDs are random version 4 UUIDs', () {
    final ids = {for (var i = 0; i < 100; i++) newTurnId()};

    expect(ids, hasLength(100));
    for (final id in ids) {
      expect(
        id,
        matches(
          RegExp(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
          ),
        ),
      );
    }
  });
}
