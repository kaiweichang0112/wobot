import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/voice/audio.dart';
import 'package:wobot/voice/voice_setup.dart';

import 'fake_server.dart';

/// Writes a few bytes where the recording goes, as the microphone would.
class FakeRecorder implements SampleRecorder {
  bool allowed = true;
  String? path;
  bool cancelled = false;
  final _levels = StreamController<double>.broadcast();

  @override
  Future<bool> hasPermission() async => allowed;

  @override
  Future<void> start(String path) async {
    this.path = path;
    await File(path).writeAsBytes([0, 1, 2, 3]);
  }

  @override
  Future<void> stop() async {}

  @override
  Future<void> cancel() async {
    cancelled = true;
    await File(path!).delete();
  }

  @override
  Stream<double> get levels => _levels.stream;

  @override
  Future<void> dispose() => _levels.close();
}

class FakePlayer implements SamplePlayer {
  final played = <String>[];
  final _playing = ValueNotifier(false);

  @override
  ValueListenable<bool> get playing => _playing;

  @override
  Future<void> playFile(String path) async => played.add(path);

  @override
  Future<void> stop() async {}

  @override
  Future<void> dispose() async {}
}

Map<String, Object?> voiceJson(String id, String status) => {
  'voice_id': id,
  'status': status,
  'error_code': null,
  'sample_seconds': 75.0,
  'created_at': '2026-10-14T10:00:00Z',
  'finished_at': null,
};

void main() {
  late FakeServer server;
  late FakeRecorder recorder;
  late FakePlayer player;
  late Directory temporary;
  late DateTime now;
  late VoiceSetupController voice;

  setUp(() async {
    server = FakeServer();
    recorder = FakeRecorder();
    player = FakePlayer();
    temporary = await Directory.systemTemp.createTemp('voice-test');
    now = DateTime(2026, 10, 14, 10);
    voice = VoiceSetupController(
      server.api(),
      recorder: recorder,
      player: player,
      temporary: () async => temporary,
      now: () => now,
      pollEvery: const Duration(hours: 1),
    );
  });

  tearDown(() async {
    voice.dispose();
    await temporary.delete(recursive: true);
  });

  Future<void> record(Duration length) async {
    await voice.startRecording();
    now = now.add(length);
    await voice.stopRecording();
  }

  test('a sample shorter than a minute cannot be sent', () async {
    await record(const Duration(seconds: 59));

    expect(voice.recording, Recording.recorded);
    expect(voice.canSubmit, isFalse);
  });

  test('a sample is sent, then leaves the device', () async {
    server
      ..reply('POST /v1/voices', 202, jsonEncode(voiceJson('v2', 'queued')))
      ..reply('GET /v1/voices/v2', 200, jsonEncode(voiceJson('v2', 'ready')));
    await record(const Duration(seconds: 75));
    final path = recorder.path!;
    expect(voice.canSubmit, isTrue);

    await voice.submit();

    final body = server.bodies['POST /v1/voices'] as String;
    expect(body, contains('name="sample"; filename="sample.m4a"'));
    expect(File(path).existsSync(), isFalse);
    expect(voice.recording, Recording.idle);
    expect(voice.making, isTrue);

    await voice.poll();
    expect(voice.candidate?.status, 'ready');
  });

  test(
    'a sample whose upload failed stays, and goes again under its ID',
    () async {
      server.reply(
        'POST /v1/voices',
        503,
        jsonEncode({
          'error': {'code': 'PROVIDER_UNAVAILABLE', 'message': 'Unavailable.'},
        }),
      );
      await record(const Duration(seconds: 90));

      await voice.submit();
      final first = server.bodies['POST /v1/voices'] as String;

      expect(voice.error, 'Unavailable.');
      expect(voice.recording, Recording.recorded);
      expect(File(recorder.path!).existsSync(), isTrue);

      server.reply(
        'POST /v1/voices',
        202,
        jsonEncode(voiceJson('v2', 'queued')),
      );
      await voice.submit();

      final id = RegExp(r'name="voice_id"\r\n\r\n([0-9a-f-]+)');
      expect(
        id.firstMatch(server.bodies['POST /v1/voices'] as String)![1],
        id.firstMatch(first)![1],
      );
    },
  );

  test('a refused microphone is reported, not recorded', () async {
    recorder.allowed = false;

    await voice.startRecording();

    expect(voice.microphoneRefused, isTrue);
    expect(voice.recording, Recording.idle);
    expect(recorder.path, isNull);
  });

  test(
    'leaving the screen while recording throws the recording away',
    () async {
      await voice.startRecording();
      final path = recorder.path!;

      await voice.leave();

      expect(recorder.cancelled, isTrue);
      expect(File(path).existsSync(), isFalse);
      expect(voice.recording, Recording.idle);
      expect(voice.error, contains('Record again'));
    },
  );

  test('recording again replaces the sample', () async {
    await record(const Duration(seconds: 70));
    final first = recorder.path!;
    now = now.add(const Duration(seconds: 1));

    await record(const Duration(seconds: 65));

    expect(File(first).existsSync(), isFalse);
    expect(recorder.path, isNot(first));
  });

  test('a sample is played once, then from the device', () async {
    server
      ..reply(
        'GET /v1/me',
        200,
        meJson(
          voice: {
            'active': {'id': 'v1', 'status': 'active', 'error_code': null},
            'candidate': null,
          },
        ),
      )
      ..reply('GET /v1/voices/v1', 200, jsonEncode(voiceJson('v1', 'active')))
      ..replyBytes('POST /v1/voices/v1/preview', [0xff, 0xf3]);
    await voice.load();

    await voice.preview(voice.active!);
    await voice.preview(voice.active!);

    expect(server.count('POST /v1/voices/v1/preview'), 1);
    expect(player.played, hasLength(2));
    expect(File(player.played.first).readAsBytesSync(), [0xff, 0xf3]);
  });

  test('using a voice names the one in use', () async {
    server
      ..reply(
        'GET /v1/me',
        200,
        meJson(
          voice: {
            'active': {'id': 'v1', 'status': 'active', 'error_code': null},
            'candidate': {'id': 'v2', 'status': 'ready', 'error_code': null},
          },
        ),
      )
      ..reply('GET /v1/voices/v1', 200, jsonEncode(voiceJson('v1', 'active')))
      ..reply('GET /v1/voices/v2', 200, jsonEncode(voiceJson('v2', 'ready')))
      ..reply(
        'POST /v1/voices/v2/activate',
        200,
        jsonEncode(voiceJson('v2', 'active')),
      );
    await voice.load();

    await voice.activate();

    expect(server.bodies['POST /v1/voices/v2/activate'], {
      'expected_active': 'v1',
    });
    expect(voice.active?.id, 'v2');
    expect(voice.candidate, isNull);
  });
}
