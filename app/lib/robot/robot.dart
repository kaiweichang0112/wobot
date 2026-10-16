import 'package:flutter/foundation.dart';

import 'director.dart';
import 'link.dart';

/// The robot in Avatar mode (spec 26): found and connected by itself when the mode
/// opens, sent back to neutral and let go when it closes or leaves sight, and moving
/// as Avatar mode goes in between. Avatar mode works the same without one
/// (AC-AVA-05): nothing here waits on the robot, and while none is connected its
/// commands go nowhere.
class Robot {
  Robot(this.link, {Director? director})
    : director = director ?? Director(link.send) {
    link.onConnected = this.director.connected;
  }

  /// The phone's own Bluetooth and the robot it remembers.
  factory Robot.onPhone() =>
      Robot(RobotLink(UniversalBleTransport(), PhoneRobotMemory()));

  final RobotLink link;
  final Director director;

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
    director.stop();
    return link.close();
  }

  Future<void> dispose() async {
    director.dispose();
    await link.close();
    link.dispose();
  }
}
