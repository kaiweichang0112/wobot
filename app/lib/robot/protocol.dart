import 'dart:convert';

/// The robot's wire format (`firmware/README.md`, "BLE protocol"): one ASCII command
/// per write. Every command made here fits in 20 bytes, the payload of a write at
/// the smallest ATT MTU, so it never depends on the MTU the phone negotiates; angles
/// go as whole degrees.

const robotService = '6b9a1000-8f3e-4b7a-9c2d-1f5e7a0b3c11';
const robotCommandCharacteristic = '6b9a1001-8f3e-4b7a-9c2d-1f5e7a0b3c11';
const robotStatusCharacteristic = '6b9a1002-8f3e-4b7a-9c2d-1f5e7a0b3c11';

/// Every robot advertises as `Wobot-Robot-XXXX`, from its board ID.
const robotNamePrefix = 'Wobot-Robot-';

/// The longest write a command may be.
const maxCommandBytes = 20;

/// One of the head's three axes, with the neutral angle and soft limits compiled
/// into the firmware (measured on the rig, 2026-07-23).
enum RobotAxis {
  pan('p', 90, 45, 135),
  tilt('t', 160, 90, 175),
  roll('r', 90, 45, 135);

  const RobotAxis(this.letter, this.neutral, this.min, this.max);

  final String letter;
  final double neutral;
  final double min;
  final double max;

  /// The angle for an offset from neutral, kept inside the soft limits.
  int angle(double offset) => (neutral + offset).clamp(min, max).round();
}

/// A head pose as offsets from neutral, in degrees: a larger pan turns the head to
/// the viewer's right, a larger tilt noses it down (only 15° of travel), a larger
/// roll turns the screen counter-clockwise as the viewer sees it.
class Pose {
  const Pose({this.pan = 0, this.tilt = 0, this.roll = 0});

  static const neutral = Pose();

  final double pan;
  final double tilt;
  final double roll;

  double operator [](RobotAxis axis) => switch (axis) {
    RobotAxis.pan => pan,
    RobotAxis.tilt => tilt,
    RobotAxis.roll => roll,
  };

  @override
  bool operator ==(Object other) =>
      other is Pose &&
      other.pan == pan &&
      other.tilt == tilt &&
      other.roll == roll;

  @override
  int get hashCode => Object.hash(pan, tilt, roll);

  @override
  String toString() => 'Pose($pan, $tilt, $roll)';
}

/// A command as the firmware reads it.
sealed class RobotCommand {
  const RobotCommand();

  /// The line written, without a line ending.
  String get line;

  /// Whether the write waits for the robot to accept it. Only the commands that
  /// start or end a session do: a pose that is lost is set again by the next one.
  bool get withResponse => false;

  List<int> get bytes => ascii.encode(line);

  @override
  String toString() => line;
}

/// Engages the servos where the firmware believes the head is.
class Attach extends RobotCommand {
  const Attach();

  @override
  String get line => 'attach';

  @override
  bool get withResponse => true;
}

/// `M`: eases the centre of all three axes to a pose, along an S-curve that starts
/// and ends at rest.
class MoveTo extends RobotCommand {
  const MoveTo(this.pose, this.ms);

  final Pose pose;
  final int ms;

  @override
  String get line =>
      'M ${RobotAxis.pan.angle(pose.pan)} ${RobotAxis.tilt.angle(pose.tilt)} '
      '${RobotAxis.roll.angle(pose.roll)} ${ms.clamp(1, 60000)}';
}

/// `O`: oscillates one axis around its centre; amplitude 0 eases it to rest. Each
/// one also sets the lease: without another command for [leaseMs], the robot goes
/// back to neutral by itself.
class Sway extends RobotCommand {
  const Sway(this.axis, this.amplitude, this.periodMs, this.leaseMs);

  final RobotAxis axis;
  final double amplitude;
  final int periodMs;
  final int leaseMs;

  @override
  String get line =>
      'O ${axis.letter} ${_tenths(amplitude.clamp(0, 20).toDouble())} '
      '${periodMs.clamp(1, 60000)} ${leaseMs.clamp(0, 60000)}';
}

/// An amplitude to a tenth of a degree, without a trailing `.0`: `2`, `0.8`.
String _tenths(double value) {
  final tenths = (value * 10).round();
  return tenths % 10 == 0 ? '${tenths ~/ 10}' : (tenths / 10).toString();
}

/// `L`: where the head looks, as pan and tilt offsets added on top of the centre and
/// followed without a stop at each update; for face tracking (firmware stage 3).
class LookAt extends RobotCommand {
  const LookAt(this.pan, this.tilt);

  static const panLimit = 40.0;
  static const tiltUp = -40.0;
  static const tiltDown = 12.0;

  final double pan;
  final double tilt;

  @override
  String get line =>
      'L ${pan.clamp(-panLimit, panLimit).round()} '
      '${tilt.clamp(tiltUp, tiltDown).round()}';
}

/// `X`: eases everything back to neutral and stops the lease; the servos keep
/// holding the phone.
class Rest extends RobotCommand {
  const Rest();

  @override
  String get line => 'X';

  @override
  bool get withResponse => true;
}

/// A line typed by hand in Robot lab, sent as it is.
class RawLine extends RobotCommand {
  const RawLine(this.line);

  @override
  final String line;

  @override
  bool get withResponse => true;
}
