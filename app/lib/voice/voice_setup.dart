import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../api.dart';
import '../assets.dart';
import '../chat.dart';
import 'audio.dart';

enum Recording { idle, starting, recording, recorded }

/// The account's voice on the Voice page: the one in use, the one being made or
/// reviewed, and recording the sample a new one is made from.
class VoiceSetupController extends ChangeNotifier with Polling {
  VoiceSetupController(
    this._api, {
    required this.recorder,
    required this.player,
    required this.temporary,
    DateTime Function()? now,
    this.pollEvery = const Duration(milliseconds: 2500),
  }) : _now = now ?? DateTime.now;

  /// What ElevenLabs asks for, and the API accepts.
  static const shortest = Duration(seconds: 60);
  static const longest = Duration(seconds: 180);

  final WobotApi _api;
  final SampleRecorder recorder;
  final SamplePlayer player;
  final Future<Directory> Function() temporary;
  final DateTime Function() _now;

  @override
  final Duration pollEvery;

  Voice? active;

  /// The newest voice not in use: being made, ready to try, or failed.
  Voice? candidate;

  bool loading = true;
  bool busy = false;
  String? error;

  Recording recording = Recording.idle;
  Duration elapsed = Duration.zero;
  double level = 0;
  bool microphoneRefused = false;

  /// The sample recorded, and the ID it is sent under: the same ID if sent again after
  /// a failed upload, so the server makes one voice of it.
  String? _samplePath;
  String? _sampleId;
  DateTime? _startedAt;
  Timer? _ticker;
  StreamSubscription<double>? _levels;
  final Map<String, String> _previews = {};

  @override
  bool get making => isMaking(candidate?.status);

  bool get canSubmit =>
      recording == Recording.recorded && elapsed >= shortest && !busy;

  Future<void> load() async {
    loading = true;
    error = null;
    notify();
    final seen = ++changes;
    try {
      final me = await _api.me();
      final ids = [me.voice.active?.id, me.voice.candidate?.id];
      final read = await Future.wait([
        for (final id in ids)
          id == null ? Future<Voice?>.value() : _api.voice(id),
      ]);
      if (seen != changes) return;
      active = read[0];
      candidate = read[1];
    } on Exception catch (e) {
      error = describe(e);
    } finally {
      loading = false;
      notify();
    }
    schedule();
  }

  @override
  Future<void> poll() async {
    final current = candidate;
    if (current == null) return;
    final seen = changes;
    try {
      final fresh = await _api.voice(current.id);
      if (seen != changes || disposed) return;
      candidate = fresh;
      notify();
    } on ApiException catch (e) {
      if (e.statusCode == 404 || e.statusCode == 409) await load();
    } on Exception {
      // Unreachable for now; the next read tries again.
    }
  }

  /// Starts recording, in place of a sample recorded before.
  Future<void> startRecording() async {
    if (recording == Recording.starting || recording == Recording.recording) {
      return;
    }
    await player.stop();
    await _dropSample();
    recording = Recording.starting;
    error = null;
    notify();
    try {
      if (!await recorder.hasPermission()) {
        microphoneRefused = true;
        recording = Recording.idle;
        notify();
        return;
      }
      microphoneRefused = false;
      final dir = await temporary();
      final path =
          '${dir.path}/voice-sample-${_now().millisecondsSinceEpoch}.m4a';
      _samplePath = path;
      await recorder.start(path);
      if (disposed) return;
      _startedAt = _now();
      elapsed = Duration.zero;
      recording = Recording.recording;
      _levels = recorder.levels.listen((value) {
        level = value;
        notify();
      });
      _ticker = Timer.periodic(
        const Duration(milliseconds: 200),
        (_) => _tick(),
      );
    } on Exception {
      await _dropSample();
      recording = Recording.idle;
      error = 'The recording could not start. Try again.';
    }
    notify();
  }

  void _tick() {
    elapsed = _now().difference(_startedAt!);
    if (elapsed >= longest) {
      elapsed = longest;
      unawaited(stopRecording());
    }
    notify();
  }

  Future<void> stopRecording() async {
    if (recording != Recording.recording) return;
    _stopTicking();
    elapsed = _now().difference(_startedAt!);
    if (elapsed > longest) elapsed = longest;
    try {
      await recorder.stop();
      _sampleId = newTurnId();
      recording = Recording.recorded;
    } on Exception {
      await _dropSample();
      recording = Recording.idle;
      error = 'The recording could not be finished. Record again.';
    }
    notify();
  }

  /// Throws the recording away.
  Future<void> discardRecording() async {
    await player.stop();
    await _dropSample();
    recording = Recording.idle;
    elapsed = Duration.zero;
    notify();
  }

  Future<void> playRecording() async {
    final path = _samplePath;
    if (recording != Recording.recorded || path == null) return;
    await player.playFile(path);
  }

  Future<void> stopPlaying() => player.stop();

  /// Sends the sample to be cloned. It leaves the device once the server has it.
  Future<void> submit() async {
    if (!canSubmit) return;
    await player.stop();
    await _act(() async {
      final sample = await File(_samplePath!).readAsBytes();
      candidate = await _api.createVoice(_sampleId!, sample);
      await _dropSample();
      recording = Recording.idle;
      elapsed = Duration.zero;
    });
  }

  /// Speaks a sentence in Chinese and one in English in the voice.
  Future<void> preview(Voice voice) async {
    if (busy) return;
    var path = _previews[voice.id];
    if (path == null) {
      busy = true;
      error = null;
      notify();
      try {
        final audio = await _api.previewVoice(voice.id);
        final dir = await temporary();
        path = '${dir.path}/voice-preview-${voice.id}.mp3';
        await File(path).writeAsBytes(audio, flush: true);
        _previews[voice.id] = path;
      } on ApiException catch (e) {
        error = e.message;
      } on Exception catch (e) {
        error = describe(e);
      } finally {
        busy = false;
        notify();
      }
    }
    if (path != null && !disposed) await player.playFile(path);
  }

  Future<void> activate() => _act(() async {
    active = await _api.activateVoice(
      candidate!.id,
      expectedActive: active?.id,
    );
    candidate = null;
  });

  /// Removes a voice: the one in use, which ends Avatar mode, or the new one.
  Future<void> delete(Voice voice) => _act(() async {
    await player.stop();
    await _api.deleteVoice(voice.id);
    if (identical(voice, active)) active = null;
    if (identical(voice, candidate)) candidate = null;
  });

  /// The app left the screen: nothing records or plays out of sight (VOI-010).
  Future<void> leave() async {
    pause();
    await player.stop();
    if (recording == Recording.recording || recording == Recording.starting) {
      _stopTicking();
      try {
        await recorder.cancel();
      } on Exception {
        // Gone with the file below.
      }
      await _dropSample();
      recording = Recording.idle;
      elapsed = Duration.zero;
      error = 'Recording stopped when Wobot left the screen. Record again.';
      notify();
    }
  }

  Future<void> _act(Future<void> Function() action) async {
    if (busy) return;
    busy = true;
    error = null;
    changes++;
    notify();
    try {
      await action();
    } on ApiException catch (e) {
      error = e.message;
      if (e.code == 'REVISION_CONFLICT' || e.statusCode == 404) {
        busy = false;
        await load();
        error = e.message;
      }
    } on Exception catch (e) {
      error = describe(e);
    } finally {
      busy = false;
      notify();
    }
    schedule();
  }

  void _stopTicking() {
    _ticker?.cancel();
    _ticker = null;
    unawaited(_levels?.cancel());
    _levels = null;
    level = 0;
  }

  Future<void> _dropSample() async {
    final path = _samplePath;
    _samplePath = null;
    _sampleId = null;
    if (path != null) await _deleteFile(path);
  }

  static Future<void> _deleteFile(String path) async {
    try {
      await File(path).delete();
    } on FileSystemException {
      // Never written, or already gone.
    }
  }

  @override
  void dispose() {
    _stopTicking();
    final sample = _samplePath;
    final recorded = recording;
    unawaited(() async {
      if (recorded == Recording.recording) {
        try {
          await recorder.cancel();
        } on Exception {
          // The file goes below.
        }
      }
      await recorder.dispose();
      await player.dispose();
      for (final path in [?sample, ..._previews.values]) {
        await _deleteFile(path);
      }
    }());
    super.dispose();
  }
}
