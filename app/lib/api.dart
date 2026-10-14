import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'sse.dart';

/// Set at build time: `--dart-define=API_BASE_URL=https://...`.
const apiBaseUrl = String.fromEnvironment('API_BASE_URL');

/// A Firebase ID token; `refresh` asks for a new one, after the API refused the last.
typedef TokenSource = Future<String> Function({bool refresh});

/// Long enough for the first request to a service scaled to zero (about 20 s).
const _coldStart = Duration(seconds: 40);

/// The server sends a keepalive every 15 s: a stream silent this long is lost.
const _silence = Duration(seconds: 45);

/// An error from the API, read from its `{"error": {"code", "message"}}` envelope.
class ApiException implements Exception {
  const ApiException(this.statusCode, this.code, this.message);

  final int statusCode;
  final String code;
  final String message;

  @override
  String toString() => 'ApiException($statusCode, $code): $message';
}

class AssistantSettings {
  const AssistantSettings({required this.chatbotName, required this.version});

  factory AssistantSettings.fromJson(Map<String, dynamic> json) =>
      AssistantSettings(
        chatbotName: json['chatbot_name'] as String,
        version: json['version'] as int,
      );

  final String chatbotName;

  /// 0 until the name is first changed; sent back so two devices never overwrite
  /// each other unseen.
  final int version;
}

/// The signed-in account as the API sees it.
class Me {
  const Me({
    required this.accountId,
    required this.email,
    required this.settings,
  });

  final String accountId;
  final String email;
  final AssistantSettings settings;
}

class Conversation {
  const Conversation({required this.id, this.title, required this.updatedAt});

  factory Conversation.fromJson(Map<String, dynamic> json) => Conversation(
    id: json['conversation_id'] as String,
    title: json['title'] as String?,
    updatedAt: DateTime.parse(json['updated_at'] as String),
  );

  final String id;
  final String? title;
  final DateTime updatedAt;

  Conversation copyWith({String? title, DateTime? updatedAt}) => Conversation(
    id: id,
    title: title ?? this.title,
    updatedAt: updatedAt ?? this.updatedAt,
  );
}

class Page<T> {
  const Page(this.items, this.nextCursor);

  final List<T> items;
  final String? nextCursor;
}

/// A question and its reply as saved, with how the attempt to answer it ended.
class TurnRecord {
  const TurnRecord({
    required this.turnId,
    required this.userText,
    required this.status,
    required this.attempt,
    this.replyText,
    this.errorCode,
  });

  factory TurnRecord.fromJson(Map<String, dynamic> json) => TurnRecord(
    turnId: json['turn_id'] as String,
    userText: json['user_text'] as String,
    status: json['status'] as String,
    attempt: json['attempt'] as int,
    replyText: json['reply_text'] as String?,
    errorCode: json['error_code'] as String?,
  );

  final String turnId;
  final String userText;
  final String status;
  final int attempt;
  final String? replyText;
  final String? errorCode;
}

class WobotApi {
  WobotApi({
    required this.token,
    http.Client? client,
    this.baseUrl = apiBaseUrl,
  }) : _client = client ?? http.Client();

  final TokenSource token;
  final http.Client _client;
  final String baseUrl;

  Uri _uri(String path, [Map<String, String>? query]) =>
      Uri.parse('$baseUrl$path').replace(queryParameters: query);

  /// Starts an instance while the user reads or types (DEC-084). Needs no token, and
  /// a failure only means the first message waits instead.
  Future<void> warmUp() async {
    try {
      await _client.get(_uri('/health')).timeout(_coldStart);
    } on Object {
      // The service may be starting; the next request will tell.
    }
  }

  Future<Me> me() async {
    final body = await _json('GET', '/v1/me');
    return Me(
      accountId: body['account_id'] as String,
      email: body['email'] as String,
      settings: AssistantSettings.fromJson(
        body['settings'] as Map<String, dynamic>,
      ),
    );
  }

  Future<AssistantSettings> renameAssistant(
    String name, {
    required int expectedVersion,
  }) async {
    final body = await _json(
      'PATCH',
      '/v1/me/settings',
      body: {'chatbot_name': name, 'expected_version': expectedVersion},
    );
    return AssistantSettings.fromJson(body);
  }

  Future<Conversation> startConversation() async =>
      Conversation.fromJson(await _json('POST', '/v1/conversations'));

  Future<Page<Conversation>> conversations({String? cursor}) async {
    final body = await _json(
      'GET',
      '/v1/conversations',
      query: {'cursor': ?cursor},
    );
    return Page([
      for (final c in body['conversations'] as List)
        Conversation.fromJson(c as Map<String, dynamic>),
    ], body['next_cursor'] as String?);
  }

  /// The conversation's latest turns, oldest first; the cursor reads earlier ones.
  Future<Page<TurnRecord>> turns(
    String conversationId, {
    String? cursor,
  }) async {
    final body = await _json(
      'GET',
      '/v1/conversations/$conversationId/turns',
      query: {'cursor': ?cursor},
    );
    return Page([
      for (final t in body['turns'] as List)
        TurnRecord.fromJson(t as Map<String, dynamic>),
    ], body['next_cursor'] as String?);
  }

  Future<void> deleteConversation(String conversationId) =>
      _json('DELETE', '/v1/conversations/$conversationId');

  /// Sends a message; its reply streams back. Completing `abort` closes the stream.
  Stream<SseEvent> sendTurn(
    String conversationId, {
    required String turnId,
    required String text,
    Future<void>? abort,
  }) => _stream('/v1/conversations/$conversationId/turns', {
    'turn_id': turnId,
    'text': text,
  }, abort);

  /// Writes a stopped or failed reply again, as the turn's next attempt.
  Stream<SseEvent> retryTurn(
    String turnId, {
    required int expectedAttempt,
    Future<void>? abort,
  }) => _stream('/v1/turns/$turnId/retry', {
    'expected_attempt': expectedAttempt,
  }, abort);

  /// Stops a reply, keeping the words shown. Returns how the turn stands: a reply
  /// that completed first stays completed.
  Future<String> cancelTurn(
    String turnId, {
    required int attempt,
    required String shownText,
  }) async {
    final body = await _json(
      'POST',
      '/v1/turns/$turnId/cancel',
      body: {'attempt': attempt, 'shown_text': shownText},
    );
    return body['status'] as String;
  }

  /// A request with the ID token; a refused token is refreshed once and sent again.
  Future<http.StreamedResponse> _send(
    String method,
    String path, {
    Map<String, String>? query,
    Object? body,
    Future<void>? abort,
  }) async {
    for (final refresh in [false, true]) {
      final request = http.AbortableRequest(
        method,
        _uri(path, query),
        abortTrigger: abort,
      );
      request.headers['Authorization'] =
          'Bearer ${await token(refresh: refresh)}';
      if (body != null) {
        request.headers['Content-Type'] = 'application/json';
        request.body = jsonEncode(body);
      }
      final response = await _client.send(request).timeout(_coldStart);
      if (response.statusCode != 401 || refresh) return response;
      await response.stream.drain<void>();
    }
    throw StateError('unreachable');
  }

  Future<Map<String, dynamic>> _json(
    String method,
    String path, {
    Map<String, String>? query,
    Object? body,
  }) async {
    final response = await http.Response.fromStream(
      await _send(method, path, query: query, body: body),
    );
    // JSON is UTF-8 (RFC 8259); `body` would read a reply without a charset as Latin-1.
    final decoded = _decode(
      utf8.decode(response.bodyBytes, allowMalformed: true),
    );
    if (response.statusCode >= 200 && response.statusCode < 300) {
      return decoded ?? const {};
    }
    throw _error(response.statusCode, decoded);
  }

  Stream<SseEvent> _stream(
    String path,
    Map<String, Object> body,
    Future<void>? abort,
  ) async* {
    final response = await _send('POST', path, body: body, abort: abort);
    if (response.statusCode != 200) {
      final text = await response.stream.bytesToString(utf8);
      throw _error(response.statusCode, _decode(text));
    }
    yield* parseSse(response.stream.timeout(_silence));
  }

  static ApiException _error(int status, Map<String, dynamic>? body) {
    final error = body?['error'];
    if (error is Map<String, dynamic>) {
      return ApiException(
        status,
        error['code'] as String,
        error['message'] as String,
      );
    }
    return ApiException(
      status,
      'UNEXPECTED_RESPONSE',
      'Wobot answered unexpectedly (HTTP $status). Try again.',
    );
  }

  static Map<String, dynamic>? _decode(String body) {
    try {
      final value = jsonDecode(body);
      return value is Map<String, dynamic> ? value : null;
    } on FormatException {
      return null;
    }
  }
}
