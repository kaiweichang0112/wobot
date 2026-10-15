import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:audio_session/audio_session.dart';
import 'package:flutter/foundation.dart';
import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';

/// One sentence of a spoken reply, as a `speech` event brings it (DEC-089).
class Sentence {
  const Sentence({
    required this.index,
    required this.text,
    required this.emotion,
    required this.audio,
  });

  factory Sentence.fromEvent(Map<String, dynamic> data) => Sentence(
    index: data['index'] as int,
    text: data['text'] as String,
    emotion: data['emotion'] as String? ?? 'neutral',
    audio: base64Decode(data['audio'] as String),
  );

  final int index;
  final String text;

  /// One of the avatar's seven expressions, shown while the sentence is heard.
  final String emotion;

  /// MP3.
  final Uint8List audio;
}

/// Plays one sentence to its end, or until stopped.
abstract class SentencePlayer {
  /// Sets the phone up to play speech; called before the first question is heard.
  Future<void> prepare();

  /// Completes when the sentence has been played, or was stopped.
  Future<void> play(Uint8List mp3);
  Future<void> stop();
  Future<void> dispose();
}

/// The phone's speaker. Each sentence is written to a file and played from it.
class DeviceSentencePlayer implements SentencePlayer {
  final _player = AudioPlayer();
  int _played = 0;

  @override
  Future<void> prepare() async {
    // Playback, set before speech recognition starts: the recognizer switches to
    // recording while it listens and puts back what it found, so replies are heard
    // from the speaker, not the earpiece.
    final session = await AudioSession.instance;
    await session.configure(const AudioSessionConfiguration.speech());
  }

  @override
  Future<void> play(Uint8List mp3) async {
    final dir = await getTemporaryDirectory();
    // Two files in turn: the one just played may still be open.
    final file = File('${dir.path}/speech-${_played++ % 2}.mp3');
    await file.writeAsBytes(mp3, flush: true);
    await _player.setFilePath(file.path);
    await _player.play(); // completes at the end, or when stopped
    // A player stays playing after the end, and its next play() would return at once:
    // the next sentence would cut in on the one after, and only the last be heard.
    await _player.pause();
  }

  @override
  Future<void> stop() => _player.stop();

  @override
  Future<void> dispose() => _player.dispose();
}

/// Plays the sentences of a reply in the order they come, one after another, and
/// says when each starts: the avatar shows its expression then.
class SpeechQueue {
  SpeechQueue(this._player);

  final SentencePlayer _player;
  final _waiting = <Sentence>[];

  /// Whether a sentence is being played: the avatar's mouth moves only then.
  final playing = ValueNotifier(false);

  /// Called as each sentence starts.
  void Function(Sentence sentence)? onStart;

  /// Sentences played to their end since the last stop.
  int heard = 0;

  int _generation = 0;
  bool _running = false;
  Completer<void>? _drained;

  Future<void> prepare() => _player.prepare();

  void add(Sentence sentence) {
    _waiting.add(sentence);
    if (!_running) unawaited(_run(_generation));
  }

  /// Completes once every sentence given has been played, or the queue was stopped.
  Future<void> get idle {
    if (!_running && _waiting.isEmpty) return Future.value();
    return (_drained ??= Completer<void>()).future;
  }

  /// Stops at once and forgets what was waiting: an interruption (spec 22).
  Future<void> stop() async {
    _generation++;
    _waiting.clear();
    _running = false;
    heard = 0;
    playing.value = false;
    _settle();
    await _player.stop();
  }

  Future<void> _run(int generation) async {
    _running = true;
    while (_waiting.isNotEmpty && generation == _generation) {
      final sentence = _waiting.removeAt(0);
      onStart?.call(sentence);
      playing.value = true;
      try {
        await _player.play(sentence.audio);
      } on Exception catch (e) {
        debugPrint('a sentence could not be played: $e');
      }
      if (generation != _generation) return; // stopped: stop() has said so
      heard++;
    }
    _running = false;
    playing.value = false;
    _settle();
  }

  void _settle() {
    final drained = _drained;
    _drained = null;
    if (drained != null && !drained.isCompleted) drained.complete();
  }

  Future<void> dispose() async {
    await stop();
    await _player.dispose();
    playing.dispose();
  }
}
