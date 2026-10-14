import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/avatar/avatar_setup.dart';
import 'package:wobot/avatar/frames.dart';

import 'fake_server.dart';

/// The avatar's manifest, with the first `ready` frames drawn under hashes `<key>-<n>`.
Map<String, Object?> avatarJson(
  String id, {
  String status = 'running',
  int ready = 0,
  int version = 1,
  Map<String, String> statuses = const {},
}) => {
  'avatar_id': id,
  'status': status,
  'error_code': null,
  'quality': 'medium',
  'created_at': '2026-10-14T10:00:00Z',
  'finished_at': null,
  'ready_frames': ready,
  'frames': [
    for (final (i, key) in frameKeys.indexed)
      {
        'key': key,
        'status': statuses[key] ?? (i < ready ? 'ready' : 'pending'),
        'sha256': (statuses[key] ?? (i < ready ? 'ready' : '')) == 'ready'
            ? '$key-$version'
            : null,
        'error_code': null,
      },
  ],
};

void main() {
  late FakeServer server;
  late MemoryFrameCache cache;
  late AvatarSetupController avatar;

  void serveFrames(String id, {int version = 1}) {
    for (final key in frameKeys) {
      server.replyBytes(
        'GET /v1/avatars/$id/frames/$key',
        utf8.encode('$key-$version'),
        headers: {'etag': '"$key-$version"'},
      );
    }
  }

  setUp(() {
    server = FakeServer();
    cache = MemoryFrameCache();
    // Polled by hand: the timer never fires within a test.
    avatar = AvatarSetupController(
      server.api(),
      cache,
      pollEvery: const Duration(hours: 1),
    );
  });

  tearDown(() => avatar.dispose());

  test('loads the avatar being made and keeps its drawn frames', () async {
    server
      ..reply(
        'GET /v1/me',
        200,
        meJson(
          avatar: {
            'active': null,
            'candidate': {'id': 'a1', 'status': 'running', 'error_code': null},
          },
        ),
      )
      ..reply(
        'GET /v1/avatars/a1',
        200,
        jsonEncode(avatarJson('a1', ready: 3)),
      );
    serveFrames('a1');

    await avatar.load();

    expect(avatar.candidate?.readyFrames, 3);
    expect(avatar.making, isTrue);
    expect(cache.kept['a1']?.keys, [
      'neutral_closed-1',
      'neutral_half-1',
      'neutral_wide-1',
    ]);
    final frame = avatar.candidate!.frame('neutral_half')!;
    expect(utf8.decode(avatar.png(frame)!), 'neutral_half-1');
  });

  test('a read asks only for the frames drawn since the last', () async {
    server
      ..reply(
        'GET /v1/me',
        200,
        meJson(
          avatar: {
            'active': null,
            'candidate': {'id': 'a1', 'status': 'running', 'error_code': null},
          },
        ),
      )
      ..reply(
        'GET /v1/avatars/a1',
        200,
        jsonEncode(avatarJson('a1', ready: 2)),
      );
    serveFrames('a1');
    await avatar.load();

    server.reply(
      'GET /v1/avatars/a1',
      200,
      jsonEncode(avatarJson('a1', ready: 5)),
    );
    await avatar.poll();

    expect(server.count('GET /v1/avatars/a1/frames/neutral_closed'), 1);
    expect(server.count('GET /v1/avatars/a1/frames/joy_half'), 1);
    expect(cache.kept['a1'], hasLength(5));
  });

  test('a frame on the device is not asked for again', () async {
    await cache.write(
      'a1',
      'neutral_closed-1',
      Uint8List.fromList(utf8.encode('kept')),
    );
    server
      ..reply(
        'GET /v1/me',
        200,
        meJson(
          avatar: {
            'active': {'id': 'a1', 'status': 'active', 'error_code': null},
            'candidate': null,
          },
        ),
      )
      ..reply(
        'GET /v1/avatars/a1',
        200,
        jsonEncode(avatarJson('a1', status: 'active', ready: 1)),
      );
    serveFrames('a1');

    await avatar.load();

    expect(server.count('GET /v1/avatars/a1/frames/neutral_closed'), 0);
    final frame = avatar.active!.frame('neutral_closed')!;
    expect(utf8.decode(avatar.png(frame)!), 'kept');
  });

  test('only an avatar with all 21 frames may be put to use', () async {
    server
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
        jsonEncode(avatarJson('a1', status: 'ready', ready: 20)),
      );
    serveFrames('a1');
    await avatar.load();
    expect(avatar.canActivate, isFalse);

    server.reply(
      'GET /v1/avatars/a1',
      200,
      jsonEncode(avatarJson('a1', status: 'ready', ready: 21)),
    );
    await avatar.load();
    expect(avatar.canActivate, isTrue);
  });

  test('a frame drawn again replaces the old one on the device', () async {
    server
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
        jsonEncode(avatarJson('a1', status: 'ready', ready: 21)),
      )
      ..reply(
        'POST /v1/avatars/a1/frames/joy_closed/redo',
        202,
        jsonEncode(
          avatarJson(
            'a1',
            status: 'queued',
            ready: 21,
            statuses: {
              'joy_closed': 'pending',
              'joy_half': 'pending',
              'joy_wide': 'pending',
            },
          ),
        ),
      );
    serveFrames('a1');
    await avatar.load();

    await avatar.redo('joy_closed');

    expect(avatar.candidate?.status, 'queued');
    expect(avatar.making, isTrue);
    expect(avatar.canActivate, isFalse);
    expect(cache.kept['a1'], hasLength(18));
    expect(cache.kept['a1']!.containsKey('joy_closed-1'), isFalse);

    server.reply(
      'GET /v1/avatars/a1',
      200,
      jsonEncode(avatarJson('a1', status: 'ready', ready: 21, version: 2)),
    );
    serveFrames('a1', version: 2);
    await avatar.poll();

    expect(cache.kept['a1']!.keys, everyElement(endsWith('-2')));
  });

  test('a failed avatar is tried again from where it stopped', () async {
    server
      ..reply(
        'GET /v1/me',
        200,
        meJson(
          avatar: {
            'active': null,
            'candidate': {'id': 'a1', 'status': 'failed', 'error_code': null},
          },
        ),
      )
      ..reply(
        'GET /v1/avatars/a1',
        200,
        jsonEncode(
          avatarJson(
            'a1',
            status: 'failed',
            ready: 18,
            statuses: {'joy_half': 'failed'},
          ),
        ),
      )
      ..reply(
        'POST /v1/avatars/a1/retry',
        202,
        jsonEncode(avatarJson('a1', status: 'queued', ready: 18)),
      );
    serveFrames('a1');
    await avatar.load();
    expect(avatar.editable, isTrue);

    await avatar.retry();

    expect(server.count('POST /v1/avatars/a1/retry'), 1);
    expect(avatar.candidate?.status, 'queued');
  });

  test('using an avatar names the one in use and drops its frames', () async {
    server
      ..reply(
        'GET /v1/me',
        200,
        meJson(
          avatar: {
            'active': {'id': 'old', 'status': 'active', 'error_code': null},
            'candidate': {'id': 'a1', 'status': 'ready', 'error_code': null},
          },
        ),
      )
      ..reply(
        'GET /v1/avatars/old',
        200,
        // Another avatar's frames hash differently.
        jsonEncode(avatarJson('old', status: 'active', ready: 21, version: 9)),
      )
      ..reply(
        'GET /v1/avatars/a1',
        200,
        jsonEncode(avatarJson('a1', status: 'ready', ready: 21)),
      )
      ..reply(
        'POST /v1/avatars/a1/activate',
        200,
        jsonEncode(avatarJson('a1', status: 'active', ready: 21)),
      );
    serveFrames('old', version: 9);
    serveFrames('a1');
    await avatar.load();

    await avatar.activate();

    expect(server.bodies['POST /v1/avatars/a1/activate'], {
      'expected_active': 'old',
    });
    expect(avatar.active?.id, 'a1');
    expect(avatar.candidate, isNull);
    expect(cache.kept.containsKey('old'), isFalse);
    expect(cache.kept['a1'], hasLength(21));
  });

  test('a changed avatar on another device is read again', () async {
    server
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
        jsonEncode(avatarJson('a1', status: 'ready', ready: 21)),
      )
      ..reply(
        'POST /v1/avatars/a1/activate',
        409,
        jsonEncode({
          'error': {
            'code': 'REVISION_CONFLICT',
            'message': 'The avatar in use was changed on another device.',
          },
        }),
      );
    serveFrames('a1');
    await avatar.load();

    await avatar.activate();

    expect(server.count('GET /v1/me'), 2);
    expect(avatar.error, contains('another device'));
  });

  test('a photo whose upload failed is sent again under its ID', () async {
    server.reply(
      'POST /v1/avatars',
      503,
      jsonEncode({
        'error': {'code': 'PROVIDER_UNAVAILABLE', 'message': 'Unavailable.'},
      }),
    );

    await avatar.upload(Uint8List.fromList([1, 2]));
    final first = server.bodies['POST /v1/avatars'] as String;
    expect(avatar.unsent, isNotNull);
    expect(avatar.error, 'Unavailable.');

    server.reply(
      'POST /v1/avatars',
      202,
      jsonEncode(avatarJson(avatar.unsent!.id, status: 'queued')),
    );
    await avatar.sendAgain();

    final id = RegExp(r'name="avatar_id"\r\n\r\n([0-9a-f-]+)');
    expect(
      id.firstMatch(server.bodies['POST /v1/avatars'] as String)![1],
      id.firstMatch(first)![1],
    );
    expect(avatar.unsent, isNull);
    expect(avatar.making, isTrue);
  });

  test('deleting the avatar in use removes its frames', () async {
    server
      ..reply(
        'GET /v1/me',
        200,
        meJson(
          avatar: {
            'active': {'id': 'a1', 'status': 'active', 'error_code': null},
            'candidate': null,
          },
        ),
      )
      ..reply(
        'GET /v1/avatars/a1',
        200,
        jsonEncode(avatarJson('a1', status: 'active', ready: 21)),
      )
      ..reply('DELETE /v1/avatars/a1', 204, '');
    serveFrames('a1');
    await avatar.load();

    await avatar.delete(avatar.active!);

    expect(avatar.active, isNull);
    expect(cache.kept.containsKey('a1'), isFalse);
  });
}
