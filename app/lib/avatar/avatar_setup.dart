import 'dart:async';

import 'package:flutter/foundation.dart';

import '../api.dart';
import '../assets.dart';
import '../chat.dart';
import 'frame_cache.dart';
import 'frames.dart';

/// The account's avatar on the Avatar page: the one in use, the one being made or
/// reviewed, their frames, and what can be done with them.
class AvatarSetupController extends ChangeNotifier with Polling {
  AvatarSetupController(
    this._api,
    this._cache, {
    this.pollEvery = const Duration(milliseconds: 2500),
  });

  final WobotApi _api;
  final FrameCache _cache;

  @override
  final Duration pollEvery;

  Avatar? active;

  /// The newest avatar not in use: being made, ready to review, or failed.
  Avatar? candidate;

  bool loading = true;

  /// An action is on its way: buttons wait for it.
  bool busy = false;
  String? error;

  /// A photo whose upload failed, sent again under the same ID: the server makes one
  /// avatar of it however often it arrives.
  ({String id, Uint8List photo})? unsent;

  final Map<String, Uint8List> _pngs = {};
  final Set<String> _fetching = {};

  @override
  bool get making => isMaking(candidate?.status);

  /// All 21 frames drawn: the avatar may be put to use (AVA-010).
  bool get canActivate =>
      candidate?.status == 'ready' &&
      candidate!.readyFrames == frameKeys.length;

  /// Frames may be drawn again before the avatar is put to use.
  bool get editable =>
      candidate?.status == 'ready' || candidate?.status == 'failed';

  /// The frame's PNG, once it is on the device.
  Uint8List? png(AvatarFrame frame) =>
      frame.sha256 == null ? null : _pngs[frame.sha256];

  Future<void> load() async {
    loading = true;
    error = null;
    notify();
    final seen = ++changes;
    try {
      final me = await _api.me();
      final ids = [me.avatar.active?.id, me.avatar.candidate?.id];
      final read = await Future.wait([
        for (final id in ids)
          id == null ? Future<Avatar?>.value() : _api.avatar(id),
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
    await Future.wait([
      for (final avatar in [active, candidate])
        if (avatar != null) _fetchFrames(avatar),
    ]);
  }

  @override
  Future<void> poll() async {
    final current = candidate;
    if (current == null) return;
    final seen = changes;
    try {
      final fresh = await _api.avatar(current.id);
      if (seen != changes || disposed) return;
      candidate = fresh;
      notify();
      await _fetchFrames(fresh);
    } on ApiException catch (e) {
      // Removed or put to use on another device meanwhile.
      if (e.statusCode == 404 || e.statusCode == 409) await load();
    } on Exception {
      // Unreachable for now; the next read tries again.
    }
  }

  /// Sends a photo to be drawn, under a new ID.
  Future<void> upload(Uint8List photo) {
    unsent = (id: newTurnId(), photo: photo);
    return sendAgain();
  }

  /// Sends the photo whose upload failed again.
  Future<void> sendAgain() => _act(() async {
    final photo = unsent;
    if (photo == null) return;
    candidate = await _api.createAvatar(photo.id, photo.photo);
    unsent = null;
  });

  /// Draws the frames that failed again.
  Future<void> retry() => _act(() async {
    candidate = await _api.retryAvatar(candidate!.id);
  });

  /// Draws a frame again, with the frames drawn from it.
  Future<void> redo(String key) => _act(() async {
    candidate = await _api.redoFrame(candidate!.id, key);
  });

  Future<void> activate() => _act(() async {
    final chosen = candidate!;
    final replaced = active;
    active = await _api.activateAvatar(chosen.id, expectedActive: replaced?.id);
    candidate = null;
    if (replaced != null) await _cache.clear(replaced.id);
  });

  /// Removes an avatar: the one in use, which ends Avatar mode, or the new one.
  Future<void> delete(Avatar avatar) => _act(() async {
    await _api.deleteAvatar(avatar.id);
    if (identical(avatar, active)) active = null;
    if (identical(avatar, candidate)) candidate = null;
    await _cache.clear(avatar.id);
  });

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
    final shown = candidate ?? active;
    if (shown != null) await _fetchFrames(shown);
  }

  /// Brings the ready frames not yet on the device, from the cache or the API, and
  /// lets go of the frames the avatar no longer has.
  Future<void> _fetchFrames(Avatar avatar) async {
    final wanted = [
      for (final frame in avatar.frames)
        if (frame.sha256 case final sha?)
          if (!_pngs.containsKey(sha) && !_fetching.contains(sha)) frame,
    ];
    _fetching.addAll(wanted.map((f) => f.sha256!));
    try {
      // A few at a time: each is about 1.3 MB.
      for (var i = 0; i < wanted.length; i += 4) {
        await Future.wait(
          wanted.skip(i).take(4).map((frame) => _fetchFrame(avatar.id, frame)),
        );
        notify();
      }
    } finally {
      _fetching.removeAll(wanted.map((f) => f.sha256!));
    }
    await _cache.prune(avatar.id, {
      for (final frame in avatar.frames) ?frame.sha256,
    });
  }

  Future<void> _fetchFrame(String avatarId, AvatarFrame frame) async {
    final sha = frame.sha256!;
    try {
      final kept = await _cache.read(avatarId, sha);
      if (kept != null) {
        _pngs[sha] = kept;
        return;
      }
      final fetched = await _api.frame(avatarId, frame.key);
      // Drawn again since the manifest was read: kept under its own hash, and the
      // next read asks for it by that.
      final actual = fetched.sha256 ?? sha;
      await _cache.write(avatarId, actual, fetched.png);
      _pngs[actual] = fetched.png;
    } on Exception {
      // Shown as missing; the next read tries again.
    }
  }
}
