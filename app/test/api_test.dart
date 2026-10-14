import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:wobot/api.dart';

const _me = {
  'account_id': 'uid-1',
  'email': 'member@example.com',
  'settings': {'chatbot_name': 'Wobot', 'version': 0},
};

void main() {
  late List<http.Request> sent;
  late List<bool> refreshes;

  WobotApi apiWith(Future<http.Response> Function(http.Request) reply) {
    sent = [];
    refreshes = [];
    return WobotApi(
      baseUrl: 'https://api.test',
      token: ({bool refresh = false}) async {
        refreshes.add(refresh);
        return refresh ? 'token-2' : 'token-1';
      },
      client: MockClient((request) {
        sent.add(request);
        return reply(request);
      }),
    );
  }

  test('sends the ID token and reads the account and its settings', () async {
    final api = apiWith((_) async => http.Response(jsonEncode(_me), 200));

    final me = await api.me();

    expect(sent.single.url, Uri.parse('https://api.test/v1/me'));
    expect(sent.single.headers['Authorization'], 'Bearer token-1');
    expect(me.email, 'member@example.com');
    expect(me.settings.chatbotName, 'Wobot');
    expect(me.settings.version, 0);
  });

  test(
    'a refused token is refreshed once and the request sent again',
    () async {
      final api = apiWith(
        (request) async => request.headers['Authorization'] == 'Bearer token-2'
            ? http.Response(jsonEncode(_me), 200)
            : http.Response('{}', 401),
      );

      await api.me();

      expect(refreshes, [false, true]);
      expect(sent.map((r) => r.headers['Authorization']), [
        'Bearer token-1',
        'Bearer token-2',
      ]);
    },
  );

  test('reads JSON as UTF-8 though the reply names no charset', () async {
    final api = apiWith(
      (_) async => http.Response.bytes(
        utf8.encode(
          jsonEncode({
            ..._me,
            'settings': {'chatbot_name': '小沃', 'version': 1},
          }),
        ),
        200,
        headers: {'content-type': 'application/json'},
      ),
    );

    expect((await api.me()).settings.chatbotName, '小沃');
  });

  test('surfaces the error envelope', () async {
    final api = apiWith(
      (_) async => http.Response(
        jsonEncode({
          'error': {
            'code': 'ACCOUNT_NOT_ALLOWED',
            'message': 'This account is not authorized.',
          },
        }),
        403,
      ),
    );

    await expectLater(
      api.me(),
      throwsA(
        isA<ApiException>()
            .having((e) => e.statusCode, 'statusCode', 403)
            .having((e) => e.code, 'code', 'ACCOUNT_NOT_ALLOWED')
            .having(
              (e) => e.message,
              'message',
              'This account is not authorized.',
            ),
      ),
    );
  });

  test('reports a response without the envelope as unexpected', () async {
    final api = apiWith(
      (_) async => http.Response('<html>Bad Gateway</html>', 502),
    );

    await expectLater(
      api.me(),
      throwsA(
        isA<ApiException>().having(
          (e) => e.code,
          'code',
          'UNEXPECTED_RESPONSE',
        ),
      ),
    );
  });

  test('a turn streams back as events', () async {
    final api = apiWith(
      (_) async => http.Response(
        'event: accepted\ndata: {"turn_id": "t1", "attempt": 1}\n\n'
        ': ping\n\n'
        'event: text_delta\ndata: {"text": "你好"}\n\n'
        'event: completed\ndata: {}\n\n',
        200,
        headers: {'content-type': 'text/event-stream; charset=utf-8'},
      ),
    );

    final events = await api.sendTurn('c1', turnId: 't1', text: '嗨').toList();

    expect(events.map((e) => e.name), ['accepted', 'text_delta', 'completed']);
    expect(events[1].data['text'], '你好');
    expect(sent.single.url.path, '/v1/conversations/c1/turns');
    expect(jsonDecode(sent.single.body), {'turn_id': 't1', 'text': '嗨'});
  });

  test('a turn refused before its stream opens is an error', () async {
    final api = apiWith(
      (_) async => http.Response(
        jsonEncode({
          'error': {
            'code': 'TURN_IN_PROGRESS',
            'message': 'A reply is already being written in this chat.',
          },
        }),
        409,
      ),
    );

    await expectLater(
      api.sendTurn('c1', turnId: 't1', text: '嗨').toList(),
      throwsA(
        isA<ApiException>().having((e) => e.code, 'code', 'TURN_IN_PROGRESS'),
      ),
    );
  });

  test('a stop sends the attempt and the words shown', () async {
    final api = apiWith(
      (_) async => http.Response(
        jsonEncode({'turn_id': 't1', 'attempt': 2, 'status': 'cancelled'}),
        200,
      ),
    );

    final status = await api.cancelTurn('t1', attempt: 2, shownText: '已顯示的字');

    expect(status, 'cancelled');
    expect(sent.single.url.path, '/v1/turns/t1/cancel');
    expect(jsonDecode(sent.single.body), {'attempt': 2, 'shown_text': '已顯示的字'});
  });

  test('warming up needs no token and never fails', () async {
    final api = apiWith((_) async => throw http.ClientException('offline'));

    await api.warmUp();

    expect(sent.single.url.path, '/health');
    expect(sent.single.headers.containsKey('Authorization'), isFalse);
  });
}
