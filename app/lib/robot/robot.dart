import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:wobot_faces/wobot_faces.dart';

import 'director.dart';
import 'link.dart';
import 'pose_model.dart';
import 'tracker.dart';

/// The robot in Avatar mode (spec 26): found and connected by itself when the mode
/// opens, sent back to neutral and let go when it closes or leaves sight, moving as
/// Avatar mode goes, and keeping the phone turned toward the user. Avatar mode works
/// the same without one (AC-AVA-05): nothing here waits on the robot, and while none
/// is connected its commands go nowhere and the camera is off.
class Robot {
  Robot(this.link, {Director? director, this.faces})
    : director = director ?? Director(link.send) {
    tracker = GazeTracker(
      send: link.send,
      centre: this.director.model,
      look: LookModel(DateTime.now),
    );
    link.onConnected = _connected;
    link.status.addListener(_followStatus);
    this.director.holding.addListener(_hold);
  }

  /// The phone's own Bluetooth, the robot it remembers, and its front camera.
  factory Robot.onPhone() => Robot(
    RobotLink(UniversalBleTransport(), PhoneRobotMemory()),
    faces: PhoneFaceSource(),
  );

  final RobotLink link;
  final Director director;

  /// Where faces come from; none, and the robot does not follow the user.
  final FaceSource? faces;
  late final GazeTracker tracker;

  /// Why the user is not followed, when the camera cannot be used.
  final camera = ValueNotifier<String?>(null);

  StreamSubscription<FaceFrame>? _watching;

  ValueListenable<RobotStatus> get status => link.status;

  Future<void> open() => link.open();

  /// Looks again, after a tap on the robot's status.
  Future<void> retry() => link.retry();

  /// The user speaks.
  void listening() => director.listening();

  /// The reply is awaited.
  void waiting() => director.waiting();

  /// A sentence starts to be heard.
  void speaking(Spoken line) => director.speaking(line);

  /// The reply was heard to its end.
  void rested() => director.rested();

  /// Nothing goes on, after a failure when [error].
  void idle({bool error = false}) => director.idle(error: error);

  /// Out of sight, or leaving Avatar mode.
  Future<void> pause() {
    _unwatch();
    director.stop();
    return link.close();
  }

  Future<void> dispose() async {
    _unwatch();
    link.status.removeListener(_followStatus);
    director.holding.removeListener(_hold);
    director.dispose();
    camera.dispose();
    await link.close();
    link.dispose();
  }

  /// Just attached at neutral: greet, and start watching for the user.
  void _connected() {
    director.connected();
    tracker.reset();
    _watch();
  }

  /// The camera is on only while a robot is there to turn.
  void _followStatus() {
    if (!link.status.value.connected) _unwatch();
  }

  void _hold() => tracker.hold = director.holding.value;

  void _watch() {
    final source = faces;
    if (source == null || _watching != null) return;
    camera.value = null;
    _watching = source.frames().listen(
      tracker.see,
      onError: (Object error) {
        camera.value = error is PlatformException ? error.message : '$error';
        _unwatch();
      },
    );
  }

  void _unwatch() {
    unawaited(_watching?.cancel());
    _watching = null;
  }
}
