import 'dart:typed_data';

import 'package:flutter/painting.dart';

import '../api.dart';
import 'frame_cache.dart';
import 'frames.dart';

/// The avatar as Avatar mode shows it: its 21 frames, by key. Their background is
/// transparent, so the app's own dark color shows behind them (P24).
class AvatarLook {
  const AvatarLook(this.images);

  final Map<String, ImageProvider> images;
}

/// Decoded at about twice the width a phone shows the avatar: 21 frames at full size
/// would hold some 90 MB.
const _shownWidth = 720;

/// The avatar in use, its frames from the device or, once, from the API.
Future<AvatarLook> loadLook(
  WobotApi api,
  FrameCache cache,
  String avatarId,
) async {
  final avatar = await api.avatar(avatarId);
  final pngs = <String, Uint8List>{};
  final frames = [
    for (final key in frameKeys)
      avatar.frame(key) ?? (throw StateError('The avatar has no $key frame.')),
  ];
  // Four at a time: as many as the setup page fetches.
  for (var i = 0; i < frames.length; i += 4) {
    await Future.wait(
      frames.skip(i).take(4).map((frame) async {
        pngs[frame.key] = await _png(api, cache, avatarId, frame);
      }),
    );
  }
  return AvatarLook({
    for (final MapEntry(:key, :value) in pngs.entries)
      key: ResizeImage(MemoryImage(value), width: _shownWidth),
  });
}

Future<Uint8List> _png(
  WobotApi api,
  FrameCache cache,
  String avatarId,
  AvatarFrame frame,
) async {
  final sha = frame.sha256;
  if (sha == null) throw StateError('The ${frame.key} frame is not ready.');
  final kept = await cache.read(avatarId, sha);
  if (kept != null) return kept;
  final fetched = await api.frame(avatarId, frame.key);
  await cache.write(avatarId, sha, fetched.png);
  return fetched.png;
}
