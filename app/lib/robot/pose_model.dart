import 'dart:math';

import 'protocol.dart';

/// Where the firmware has the head's centre, worked out from the moves sent: the
/// robot reports no position, and reading its status takes a round trip. Like the
/// firmware's `moveCenter()`, each move eases from where the centre is to its target
/// along `(1 - cos(πu)) / 2`.
class CentreModel {
  CentreModel(this._now);

  final DateTime Function() _now;

  Pose _from = Pose.neutral;
  Pose _to = Pose.neutral;
  DateTime _start = DateTime.fromMillisecondsSinceEpoch(0);
  int _ms = 1;

  /// When the move under way ends.
  DateTime get endsAt => _start.add(Duration(milliseconds: _ms));

  /// Whether a move is under way: the next one must not cut into it, since a move
  /// starts from rest and would stop the head where it is.
  bool get moving => _now().isBefore(endsAt);

  /// A move just sent.
  void moved(Pose to, int ms) {
    _from = at();
    _to = to;
    _start = _now();
    _ms = max(1, ms);
  }

  /// Back at neutral at once, as when the robot starts over.
  void reset() {
    _from = _to = Pose.neutral;
    _start = DateTime.fromMillisecondsSinceEpoch(0);
    _ms = 1;
  }

  /// The centre at [time], now by default.
  Pose at([DateTime? time]) {
    final elapsed = (time ?? _now()).difference(_start).inMicroseconds / 1000;
    if (elapsed >= _ms) return _to;
    if (elapsed <= 0) return _from;
    final eased = (1 - cos(pi * elapsed / _ms)) / 2;
    double mix(double a, double b) => a + (b - a) * eased;
    return Pose(
      pan: mix(_from.pan, _to.pan),
      tilt: mix(_from.tilt, _to.tilt),
      roll: mix(_from.roll, _to.roll),
    );
  }
}
