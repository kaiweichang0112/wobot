import 'package:flutter/foundation.dart';

import 'link.dart';

/// The robot in Avatar mode (spec 26): found and connected by itself when the mode
/// opens, sent back to neutral and let go when it closes or leaves sight. Avatar mode
/// works the same without one (AC-AVA-05): nothing here waits on the robot.
class Robot {
  Robot(this.link);

  /// The phone's own Bluetooth and the robot it remembers.
  factory Robot.onPhone() =>
      Robot(RobotLink(UniversalBleTransport(), PhoneRobotMemory()));

  final RobotLink link;

  ValueListenable<RobotStatus> get status => link.status;

  Future<void> open() => link.open();

  /// Looks again, after a tap on the robot's status.
  Future<void> retry() => link.retry();

  /// Out of sight, or leaving Avatar mode.
  Future<void> pause() => link.close();

  Future<void> dispose() async {
    await link.close();
    link.dispose();
  }
}
