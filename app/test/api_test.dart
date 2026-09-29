import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:wobot/api.dart';

void main() {
  late http.Request sent;

  WobotApi apiReplying(int status, String body) {
    return WobotApi(
      baseUrl: 'https://api.test',
      client: MockClient((request) async {
        sent = request;
        return http.Response(body, status);
      }),
    );
  }

  test('sends the ID token and returns the account', () async {
    final api = apiReplying(
      200,
      jsonEncode({'account_id': 'uid-1', 'email': 'member@example.com'}),
    );

    final me = await api.me('token-1');

    expect(sent.url, Uri.parse('https://api.test/v1/me'));
    expect(sent.headers['Authorization'], 'Bearer token-1');
    expect(me.accountId, 'uid-1');
    expect(me.email, 'member@example.com');
  });

  test('surfaces the error envelope', () async {
    final api = apiReplying(
      403,
      jsonEncode({
        'error': {
          'code': 'ACCOUNT_NOT_ALLOWED',
          'message': 'This account is not authorized.',
        },
      }),
    );

    await expectLater(
      api.me('token-1'),
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
    final api = apiReplying(502, '<html>Bad Gateway</html>');

    await expectLater(
      api.me('token-1'),
      throwsA(
        isA<ApiException>().having(
          (e) => e.code,
          'code',
          'UNEXPECTED_RESPONSE',
        ),
      ),
    );
  });
}
