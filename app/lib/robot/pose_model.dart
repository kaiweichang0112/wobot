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

/// Where the firmware's gaze is, worked out from the `L` targets sent, with the
/// follower of the firmware's `updateAxis()`: a velocity of [gainPerSecond] per
/// degree of error, capped, reached at a capped acceleration, stepped at 50 Hz.
class LookModel {
  LookModel(this._now);

  static const gainPerSecond = 4.0;
  static const _step = Duration(milliseconds: 20);
  static const _maxSpeed = (pan: 90.0, tilt: 60.0);
  static const _maxAcceleration = (pan: 600.0, tilt: 400.0);

  final DateTime Function() _now;
  DateTime? _at;
  var _pan = (look: 0.0, speed: 0.0, target: 0.0);
  var _tilt = (look: 0.0, speed: 0.0, target: 0.0);

  /// The last second of gaze, a sample each step, to answer for a frame taken
  /// a moment ago.
  final _past = <(DateTime, double, double)>[];

  /// The target last aimed at.
  (double, double) get target => (_pan.target, _tilt.target);

  /// An `L` just sent.
  void aim(double pan, double tilt) {
    _advance();
    _pan = (look: _pan.look, speed: _pan.speed, target: pan);
    _tilt = (look: _tilt.look, speed: _tilt.speed, target: tilt);
  }

  /// Looking ahead at rest, as after `X` or a boot.
  void reset() {
    _pan = _tilt = (look: 0.0, speed: 0.0, target: 0.0);
    _past.clear();
    _at = _now();
  }

  /// The gaze, pan and tilt, at [time] (now by default; up to a second ago).
  (double, double) at([DateTime? time]) {
    _advance();
    if (time == null || _past.isEmpty || !time.isBefore(_past.last.$1)) {
      return (_pan.look, _tilt.look);
    }
    for (final (sampled, pan, tilt) in _past.reversed) {
      if (!sampled.isAfter(time)) return (pan, tilt);
    }
    return (_past.first.$2, _past.first.$3);
  }

  void _advance() {
    final now = _now();
    var at = _at ?? now;
    while (!at.add(_step).isAfter(now)) {
      at = at.add(_step);
      _pan = _follow(_pan, _maxSpeed.pan, _maxAcceleration.pan);
      _tilt = _follow(_tilt, _maxSpeed.tilt, _maxAcceleration.tilt);
      _past.add((at, _pan.look, _tilt.look));
    }
    _at = at;
    final keep = now.subtract(const Duration(seconds: 1));
    while (_past.isNotEmpty && _past.first.$1.isBefore(keep)) {
      _past.removeAt(0);
    }
  }

  static ({double look, double speed, double target}) _follow(
    ({double look, double speed, double target}) axis,
    double maxSpeed,
    double maxAcceleration,
  ) {
    const dt = 0.02;
    final wanted = (gainPerSecond * (axis.target - axis.look)).clamp(
      -maxSpeed,
      maxSpeed,
    );
    final speed =
        axis.speed +
        (wanted - axis.speed).clamp(
          -maxAcceleration * dt,
          maxAcceleration * dt,
        );
    return (look: axis.look + speed * dt, speed: speed, target: axis.target);
  }
}
