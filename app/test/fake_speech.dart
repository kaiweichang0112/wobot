import 'dart:async';
import 'dart:typed_data';

import 'package:wobot/voice/listener.dart';
import 'package:wobot/voice/speech_queue.dart';

/// A recognizer the test speaks through.
class FakeEngine implements SpeechEngine {
  bool available = true;
  void Function(String words, bool isFinal)? onResult;
  late void Function(String error) onError;
  late void Function(String status) onStatus;
  int stops = 0;
  int cancels = 0;

  /// What the recognizer settles on after a stop, or nothing.
  String? finalText;

  @override
  Future<bool> initialize({
    required void Function(String error) onError,
    required void Function(String status) onStatus,
  }) async {
    this.onError = onError;
    this.onStatus = onStatus;
    return available;
  }

  @override
  Future<void> listen({
    required String localeId,
    required void Function(String words, bool isFinal) onResult,
  }) async => this.onResult = onResult;

  void say(String words, {bool isFinal = false}) => onResult!(words, isFinal);

  @override
  Future<void> stop() async {
    stops++;
    final text = finalText;
    if (text != null) scheduleMicrotask(() => onResult!(text, true));
  }

  @override
  Future<void> cancel() async => cancels++;
}

/// A speaker that plays nothing: each sentence lasts [lasts], or until [finish] when
/// [held], or until stopped.
class FakePlayer implements SentencePlayer {
  FakePlayer({this.lasts = const Duration(milliseconds: 10)});

  final Duration lasts;
  bool held = false;
  final played = <Uint8List>[];
  int stops = 0;
  Completer<void>? _playing;

  /// The sentences played, by the text their audio holds.
  List<String> get said => [for (final a in played) String.fromCharCodes(a)];

  @override
  Future<void> prepare() async {}

  @override
  Future<void> play(Uint8List mp3) {
    played.add(mp3);
    final playing = _playing = Completer<void>();
    if (!held) Timer(lasts, finish);
    return playing.future;
  }

  void finish() {
    final playing = _playing;
    if (playing != null && !playing.isCompleted) playing.complete();
  }

  @override
  Future<void> stop() async {
    stops++;
    finish();
  }

  @override
  Future<void> dispose() async {}
}
