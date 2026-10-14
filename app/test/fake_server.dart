import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:wobot/api.dart';
import 'package:wobot/avatar/frame_cache.dart';

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

  void replyBytes(
    String route,
    List<int> body, {
    Map<String, String> headers = const {},
  }) {
    routes[route] = () async =>
        http.StreamedResponse(Stream.value(body), 200, headers: headers);
  }

  /// The requests sent to a route, such as `GET /v1/me`.
  int count(String route) =>
      requests.where((r) => '${r.method} ${r.url.path}' == route).length;

  WobotApi api() => WobotApi(
    baseUrl: 'https://api.test',
    token: ({bool refresh = false}) async => 'token',
    client: MockClient.streaming((request, body) async {
      requests.add(request);
      final text = await body.bytesToString();
      final route = '${request.method} ${request.url.path}';
      bodies[route] = text.isEmpty ? null : _decoded(text);
      final answer = routes[route];
      if (answer == null) throw StateError('no route for $route');
      return answer();
    }),
  );
}

/// JSON as a value, anything else, such as multipart form data, as its text.
Object? _decoded(String text) {
  try {
    return jsonDecode(text);
  } on FormatException {
    return text;
  }
}

/// `/v1/me` with the voice and the avatar as given.
String meJson({Map<String, Object?>? voice, Map<String, Object?>? avatar}) =>
    jsonEncode({
      'account_id': 'uid-1',
      'email': 'member@example.com',
      'settings': {'chatbot_name': 'Wobot', 'version': 0},
      'voice': voice ?? {'active': null, 'candidate': null},
      'avatar': avatar ?? {'active': null, 'candidate': null},
      'avatar_mode': false,
    });

/// Frames kept in memory.
class MemoryFrameCache implements FrameCache {
  final kept = <String, Map<String, Uint8List>>{};

  @override
  Future<Uint8List?> read(String avatarId, String sha256) async =>
      kept[avatarId]?[sha256];

  @override
  Future<void> write(String avatarId, String sha256, Uint8List png) async =>
      (kept[avatarId] ??= {})[sha256] = png;

  @override
  Future<void> prune(String avatarId, Set<String> keep) async =>
      kept[avatarId]?.removeWhere((sha, _) => !keep.contains(sha));

  @override
  Future<void> clear([String? avatarId]) async =>
      avatarId == null ? kept.clear() : kept.remove(avatarId);
}
