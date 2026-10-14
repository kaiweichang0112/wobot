import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:wobot/api.dart';

String sse(List<(String, Map<String, Object>)> events) => [
  for (final (name, data) in events)
    'event: $name\ndata: ${jsonEncode(data)}\n\n',
].join();

const newConversation = {
  'conversation_id': 'c1',
  'title': null,
  'created_at': '2026-10-13T10:00:00Z',
  'updated_at': '2026-10-13T10:00:00Z',
};

/// An API whose routes answer as given; each request is kept.
class FakeServer {
  final requests = <http.BaseRequest>[];
  final bodies = <String, Object?>{};
  final routes = <String, Future<http.StreamedResponse> Function()>{};

  void reply(String route, int status, String body) {
    routes[route] = () async =>
        http.StreamedResponse(Stream.value(utf8.encode(body)), status);
  }

  WobotApi api() => WobotApi(
    baseUrl: 'https://api.test',
    token: ({bool refresh = false}) async => 'token',
    client: MockClient.streaming((request, body) async {
      requests.add(request);
      final text = await body.bytesToString();
      final route = '${request.method} ${request.url.path}';
      bodies[route] = text.isEmpty ? null : jsonDecode(text);
      final answer = routes[route];
      if (answer == null) throw StateError('no route for $route');
      return answer();
    }),
  );
}
