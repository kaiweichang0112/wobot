import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:just_audio/just_audio.dart';
import 'package:record/record.dart';

/// Records a voice sample to a file.
abstract class SampleRecorder {
  /// Asks for the microphone the first time; false once refused.
  Future<bool> hasPermission();
  Future<void> start(String path);
  Future<void> stop();

  /// Stops and deletes what was recorded.
  Future<void> cancel();

  /// How loud the microphone is, from 0 (silence) to 1.
  Stream<double> get levels;
  Future<void> dispose();
}

/// Plays a recording or a preview from a file.
abstract class SamplePlayer {
  ValueListenable<bool> get playing;
  Future<void> playFile(String path);
  Future<void> stop();
  Future<void> dispose();
}

/// AAC in an M4A file, mono: what the API takes as a sample (60 to 180 s, ≤10 MB;
/// about 1 MB a minute at 128 kb/s).
class MicrophoneRecorder implements SampleRecorder {
  final _recorder = AudioRecorder();

  @override
  Future<bool> hasPermission() => _recorder.hasPermission();

  @override
  Future<void> start(String path) => _recorder.start(
    const RecordConfig(
      encoder: AudioEncoder.aacLc,
      bitRate: 128000,
      sampleRate: 44100,
      numChannels: 1,
    ),
    path: path,
  );

  @override
  Future<void> stop() => _recorder.stop();

  @override
  Future<void> cancel() => _recorder.cancel();

  @override
  Stream<double> get levels => _recorder
      .onAmplitudeChanged(const Duration(milliseconds: 100))
      // dBFS, from about -60 in a quiet room to 0 at the loudest.
      .map((a) => ((a.current + 60) / 60).clamp(0.0, 1.0));

  @override
  Future<void> dispose() => _recorder.dispose();
}

class SpeakerPlayer implements SamplePlayer {
  SpeakerPlayer() {
    _states = _player.playerStateStream.listen((state) {
      _playing.value =
          state.playing && state.processingState != ProcessingState.completed;
    });
  }

  final _player = AudioPlayer();
  final _playing = ValueNotifier(false);
  late final StreamSubscription<PlayerState> _states;

  @override
  ValueListenable<bool> get playing => _playing;

  @override
  Future<void> playFile(String path) async {
    await _player.setFilePath(path);
    // Completes only when playback ends; `playing` says when.
    unawaited(_player.play());
  }

  @override
  Future<void> stop() => _player.stop();

  @override
  Future<void> dispose() async {
    await _states.cancel();
    await _player.dispose();
    _playing.dispose();
  }
}

/// A level bar's height for a level, eased so speech shows above the noise floor.
double levelHeight(double level) => pow(level, 2).toDouble();
