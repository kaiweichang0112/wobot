import 'dart:io';
import 'dart:typed_data';

import 'package:path_provider/path_provider.dart';

/// Frames kept on the device, by avatar and SHA-256. A frame drawn again has a new
/// hash, so a kept frame is never stale; it is only no longer wanted.
abstract class FrameCache {
  Future<Uint8List?> read(String avatarId, String sha256);
  Future<void> write(String avatarId, String sha256, Uint8List png);

  /// Removes the avatar's frames other than `keep`.
  Future<void> prune(String avatarId, Set<String> keep);

  /// Removes the avatar's frames, or every avatar's.
  Future<void> clear([String? avatarId]);
}

/// In the app's support directory, under the account: `frames/<account>/<avatar>/`.
class FileFrameCache implements FrameCache {
  FileFrameCache(this.accountId, {Future<Directory> Function()? root})
    : _root = root ?? getApplicationSupportDirectory;

  final String accountId;
  final Future<Directory> Function() _root;

  Future<Directory> _dir([String? avatarId]) async {
    final root = await _root();
    return Directory(
      [root.path, 'frames', accountId, ?avatarId].join(Platform.pathSeparator),
    );
  }

  Future<File> _file(String avatarId, String sha256) async => File(
    '${(await _dir(avatarId)).path}${Platform.pathSeparator}$sha256.png',
  );

  @override
  Future<Uint8List?> read(String avatarId, String sha256) async {
    final file = await _file(avatarId, sha256);
    try {
      return await file.readAsBytes();
    } on FileSystemException {
      return null;
    }
  }

  @override
  Future<void> write(String avatarId, String sha256, Uint8List png) async {
    final file = await _file(avatarId, sha256);
    await file.parent.create(recursive: true);
    // Written aside and renamed, so a frame half written is never read.
    final partial = File('${file.path}.part');
    await partial.writeAsBytes(png, flush: true);
    await partial.rename(file.path);
  }

  @override
  Future<void> prune(String avatarId, Set<String> keep) async {
    final dir = await _dir(avatarId);
    if (!await dir.exists()) return;
    await for (final entry in dir.list()) {
      final name = entry.uri.pathSegments.last;
      final sha256 = name.endsWith('.png')
          ? name.substring(0, name.length - 4)
          : null;
      if (entry is File && !keep.contains(sha256)) {
        await entry.delete();
      }
    }
  }

  @override
  Future<void> clear([String? avatarId]) async {
    final dir = await _dir(avatarId);
    if (await dir.exists()) await dir.delete(recursive: true);
  }
}
