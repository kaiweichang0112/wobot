import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

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

/// A voice sample or a photo, up to 10 MB, may go out slowly over a phone network.
const _uploading = Duration(seconds: 120);

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
    this.voice = const AssetSummary(),
    this.avatar = const AssetSummary(),
    this.avatarMode = false,
  });

  final String accountId;
  final String email;
  final AssistantSettings settings;
  final AssetSummary voice;
  final AssetSummary avatar;

  /// Both the voice and the avatar in use: Avatar mode is open (MODE-004).
  final bool avatarMode;
}

/// One version of a voice or an avatar, as `/v1/me` names it.
class AssetRef {
  const AssetRef({required this.id, required this.status, this.errorCode});

  factory AssetRef.fromJson(Map<String, dynamic> json) => AssetRef(
    id: json['id'] as String,
    status: json['status'] as String,
    errorCode: json['error_code'] as String?,
  );

  final String id;

  /// queued, running, ready, failed or active.
  final String status;
  final String? errorCode;
}

/// The version in use and the newest one that is not: being made, waiting to be
/// activated, or failed. The one in use stays in use meanwhile (AVA-009, VOI-013).
class AssetSummary {
  const AssetSummary({this.active, this.candidate});

  factory AssetSummary.fromJson(Map<String, dynamic> json) => AssetSummary(
    active: _ref(json['active']),
    candidate: _ref(json['candidate']),
  );

  final AssetRef? active;
  final AssetRef? candidate;

  static AssetRef? _ref(Object? json) =>
      json is Map<String, dynamic> ? AssetRef.fromJson(json) : null;
}

/// A cloned voice: made in the cloud from a recorded sample.
class Voice {
  const Voice({
    required this.id,
    required this.status,
    this.errorCode,
    required this.sampleSeconds,
  });

  factory Voice.fromJson(Map<String, dynamic> json) => Voice(
    id: json['voice_id'] as String,
    status: json['status'] as String,
    errorCode: json['error_code'] as String?,
    sampleSeconds: (json['sample_seconds'] as num).toDouble(),
  );

  final String id;
  final String status;
  final String? errorCode;
  final double sampleSeconds;
}

/// One of an avatar's 21 frames.
class AvatarFrame {
  const AvatarFrame({
    required this.key,
    required this.status,
    this.sha256,
    this.errorCode,
  });

  factory AvatarFrame.fromJson(Map<String, dynamic> json) => AvatarFrame(
    key: json['key'] as String,
    status: json['status'] as String,
    sha256: json['sha256'] as String?,
    errorCode: json['error_code'] as String?,
  );

  final String key;

  /// pending, running, ready or failed.
  final String status;

  /// Set when ready; it changes when the frame is made again.
  final String? sha256;
  final String? errorCode;
}

/// An avatar: a photo drawn as 21 frames in the cloud.
class Avatar {
  const Avatar({
    required this.id,
    required this.status,
    this.errorCode,
    required this.frames,
  });

  factory Avatar.fromJson(Map<String, dynamic> json) => Avatar(
    id: json['avatar_id'] as String,
    status: json['status'] as String,
    errorCode: json['error_code'] as String?,
    frames: [
      for (final f in json['frames'] as List)
        AvatarFrame.fromJson(f as Map<String, dynamic>),
    ],
  );

  final String id;
  final String status;
  final String? errorCode;
  final List<AvatarFrame> frames;

  int get readyFrames => frames.where((f) => f.status == 'ready').length;

  AvatarFrame? frame(String key) {
    for (final f in frames) {
      if (f.key == key) return f;
    }
    return null;
  }
}

/// A file sent with a request, as multipart form data.
class _Upload {
  const _Upload(this.fields, this.field, this.filename, this.bytes);

  final Map<String, String> fields;
  final String field;
  final String filename;
  final List<int> bytes;
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
      voice: AssetSummary.fromJson(body['voice'] as Map<String, dynamic>),
      avatar: AssetSummary.fromJson(body['avatar'] as Map<String, dynamic>),
      avatarMode: body['avatar_mode'] as bool,
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

  /// Deletes the account and all it owns; the ID token must say the sign-in is recent.
  Future<void> deleteAccount() => _json('DELETE', '/v1/me');

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
  /// In Avatar mode the reply is also spoken, as `speech` events (DEC-089).
  Stream<SseEvent> sendTurn(
    String conversationId, {
    required String turnId,
    required String text,
    String mode = 'text',
    Future<void>? abort,
  }) => _stream('/v1/conversations/$conversationId/turns', {
    'turn_id': turnId,
    'text': text,
    if (mode != 'text') 'mode': mode,
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

  /// How far the reply's speech was heard, and whether the user stopped it: a stop
  /// ends only the speech, and the text is written all the same (DEC-089).
  Future<void> playback(
    String turnId, {
    required int attempt,
    int? heard,
    bool stopped = false,
  }) => _json(
    'POST',
    '/v1/turns/$turnId/playback',
    body: {'attempt': attempt, 'heard': ?heard, 'stopped': stopped},
  );

  /// The next items of a list reply, spoken: `speech` events, then `completed` with
  /// where the next page starts, null at the end (DEC-096).
  Stream<SseEvent> continueReading(
    String turnId, {
    required int attempt,
    required int from,
    Future<void>? abort,
  }) => _stream('/v1/turns/$turnId/continue-reading', {
    'attempt': attempt,
    'from': from,
  }, abort);

  /// Sends a recorded sample, an M4A file of 60 to 180 seconds, to be cloned. Sent
  /// again with the same ID, it returns the voice the first request made.
  Future<Voice> createVoice(String voiceId, List<int> sample) async =>
      Voice.fromJson(
        await _json(
          'POST',
          '/v1/voices',
          upload: _Upload(
            {'voice_id': voiceId},
            'sample',
            'sample.m4a',
            sample,
          ),
        ),
      );

  /// The voice's progress; reading it also restarts a start the cloud lost.
  Future<Voice> voice(String voiceId) async =>
      Voice.fromJson(await _json('GET', '/v1/voices/$voiceId'));

  /// A sentence in Chinese and one in English, spoken in the voice, as MP3.
  Future<Uint8List> previewVoice(String voiceId) async =>
      (await _bytes('POST', '/v1/voices/$voiceId/preview')).body;

  /// Puts a ready voice to use in place of `expectedActive`, the one last seen in use.
  Future<Voice> activateVoice(String voiceId, {String? expectedActive}) async =>
      Voice.fromJson(
        await _json(
          'POST',
          '/v1/voices/$voiceId/activate',
          body: {'expected_active': expectedActive},
        ),
      );

  Future<void> deleteVoice(String voiceId) =>
      _json('DELETE', '/v1/voices/$voiceId');

  /// Sends a photo to be drawn as 21 frames. Sent again with the same ID, it returns
  /// the avatar the first request made.
  Future<Avatar> createAvatar(String avatarId, List<int> photo) async =>
      Avatar.fromJson(
        await _json(
          'POST',
          '/v1/avatars',
          upload: _Upload({'avatar_id': avatarId}, 'photo', 'photo.jpg', photo),
        ),
      );

  /// The avatar's progress, frame by frame.
  Future<Avatar> avatar(String avatarId) async =>
      Avatar.fromJson(await _json('GET', '/v1/avatars/$avatarId'));

  /// One frame as PNG, with the SHA-256 the server knows it by.
  Future<({Uint8List png, String? sha256})> frame(
    String avatarId,
    String key,
  ) async {
    final response = await _bytes('GET', '/v1/avatars/$avatarId/frames/$key');
    final etag = response.headers['etag'];
    return (png: response.body, sha256: etag?.replaceAll('"', ''));
  }

  /// Draws the frames that failed again; the ready ones stay.
  Future<Avatar> retryAvatar(String avatarId) async =>
      Avatar.fromJson(await _json('POST', '/v1/avatars/$avatarId/retry'));

  /// Draws one frame again, with the frames drawn from it.
  Future<Avatar> redoFrame(String avatarId, String key) async =>
      Avatar.fromJson(
        await _json('POST', '/v1/avatars/$avatarId/frames/$key/redo'),
      );

  /// Puts an avatar with all 21 frames to use in place of `expectedActive`.
  Future<Avatar> activateAvatar(
    String avatarId, {
    String? expectedActive,
  }) async => Avatar.fromJson(
    await _json(
      'POST',
      '/v1/avatars/$avatarId/activate',
      body: {'expected_active': expectedActive},
    ),
  );

  Future<void> deleteAvatar(String avatarId) =>
      _json('DELETE', '/v1/avatars/$avatarId');

  /// A request with the ID token; a refused token is refreshed once and sent again.
  Future<http.StreamedResponse> _send(
    String method,
    String path, {
    Map<String, String>? query,
    Object? body,
    _Upload? upload,
    Future<void>? abort,
  }) async {
    for (final refresh in [false, true]) {
      // Built anew for each attempt: a request is sent only once.
      final http.BaseRequest request;
      if (upload != null) {
        request = http.MultipartRequest(method, _uri(path, query))
          ..fields.addAll(upload.fields)
          ..files.add(
            http.MultipartFile.fromBytes(
              upload.field,
              upload.bytes,
              filename: upload.filename,
            ),
          );
      } else {
        final plain = http.AbortableRequest(
          method,
          _uri(path, query),
          abortTrigger: abort,
        );
        if (body != null) {
          plain.headers['Content-Type'] = 'application/json';
          plain.body = jsonEncode(body);
        }
        request = plain;
      }
      request.headers['Authorization'] =
          'Bearer ${await token(refresh: refresh)}';
      final response = await _client
          .send(request)
          .timeout(upload == null ? _coldStart : _uploading);
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
    _Upload? upload,
  }) async {
    final response = await http.Response.fromStream(
      await _send(method, path, query: query, body: body, upload: upload),
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

  /// A reply that is not JSON, such as audio or an image; an error still is.
  Future<({Uint8List body, Map<String, String> headers})> _bytes(
    String method,
    String path,
  ) async {
    final response = await http.Response.fromStream(await _send(method, path));
    if (response.statusCode >= 200 && response.statusCode < 300) {
      return (body: response.bodyBytes, headers: response.headers);
    }
    throw _error(
      response.statusCode,
      _decode(utf8.decode(response.bodyBytes, allowMalformed: true)),
    );
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
