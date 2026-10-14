import 'dart:async';

import 'package:flutter/foundation.dart';

import 'api.dart';

/// How a voice or an avatar stands, in a word for Settings.
String assetStatus(AssetSummary asset) {
  final candidate = asset.candidate;
  if (candidate != null) {
    switch (candidate.status) {
      case 'queued' || 'running':
        return 'Creating…';
      case 'ready':
        return 'Ready to review';
      case 'failed':
        return asset.active == null ? 'Failed' : 'In use · the new one failed';
    }
  }
  return asset.active == null ? 'Not set up' : 'In use';
}

/// Why a voice or an avatar could not be made, for the reader.
String assetFailure(String? code) => switch (code) {
  'VOICE_REJECTED' =>
    'The recording could not be used. Record again in a quiet room.',
  'IMAGE_REJECTED' =>
    'The photo could not be used. Take another, of your face alone.',
  'INVALID_IMAGE' => 'A frame came out unusable. Try again.',
  'PROVIDER_UNAVAILABLE' ||
  'WORKER_LOST' => 'The service was unavailable. Try again.',
  'SAMPLE_MISSING' ||
  'PHOTO_MISSING' => 'The upload was lost. Start again with a new one.',
  _ => 'Something went wrong. Try again.',
};

bool isMaking(String? status) => status == 'queued' || status == 'running';

/// Reads a version being made every few seconds while its page is open and the app is
/// in sight (AVA-011, VOI-014): the cloud goes on without it, and the page catches up
/// when it is back.
mixin Polling on ChangeNotifier {
  Duration get pollEvery;

  /// Whether a version is being made, so worth reading again.
  bool get making;

  /// Reads the version being made.
  Future<void> poll();

  Timer? _timer;
  bool _paused = false;
  bool disposed = false;

  /// Counts the changes the page made, so a read sent before one is not shown after it.
  int changes = 0;

  void schedule() {
    _timer?.cancel();
    if (_paused || disposed || !making) return;
    _timer = Timer(pollEvery, () async {
      await poll();
      schedule();
    });
  }

  /// The app left the screen: stop reading, not the making.
  void pause() {
    _paused = true;
    _timer?.cancel();
  }

  /// Back in sight: read how it stands now, then go on.
  Future<void> resume() async {
    _paused = false;
    if (making) await poll();
    schedule();
  }

  void notify() {
    if (!disposed) notifyListeners();
  }

  @override
  void dispose() {
    disposed = true;
    _timer?.cancel();
    super.dispose();
  }
}
