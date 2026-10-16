import 'dart:math';

import 'package:wobot_faces/wobot_faces.dart';

import 'pose_model.dart';
import 'protocol.dart';

/// Keeps the phone turned toward the user (ROB-D01, DEC-104): where the face is,
/// as the front camera saw it, becomes where the gaze (`L`) aims.
///
/// The camera rides on the head, so a face off-centre is off by the head's own
/// direction at the moment the frame was taken: the centre the motions set and the
/// gaze already sent, both worked out by models of the firmware. Adding the face's
/// angle in the picture gives where the user is, whatever the head is doing; aiming
/// there, not adding the error to the last aim, is what keeps a frame that is late
/// from being counted twice, which would overshoot.
///
/// Small moves of the face are let be (a deadband), and nothing is aimed while the
/// head turns away on purpose ([hold]). Without a face it keeps its aim a while,
/// then looks ahead.
class GazeTracker {
  GazeTracker({
    required this.send,
    required this.centre,
    required this.look,
    DateTime Function()? now,
  }) : _now = now ?? DateTime.now;

  /// Whether a face to the user's right is seen to the right of the picture, and
  /// one above the eyes' line above it; checked on the rig in Robot lab.
  static const panSign = 1.0;
  static const tiltSign = 1.0;

  /// How far the user may move before the head follows, and how close it comes.
  static const deadband = 3.0;
  static const settle = 1.0;

  /// How quickly the estimate of where the user is follows the frames.
  static const smoothing = Duration(milliseconds: 300);

  /// The least change worth an `L`, and the least time between two.
  static const leastChange = 1.0;
  static const leastGap = Duration(milliseconds: 100);

  /// With no face, the aim is kept this long, then the head looks ahead.
  static const forget = Duration(seconds: 5);

  final void Function(LookAt command) send;
  final CentreModel centre;
  final LookModel look;
  final DateTime Function() _now;

  /// Set while the head turns away on purpose: frames still count, nothing is aimed.
  bool hold = false;

  FaceBox? _face;
  double? _pan;
  double? _tilt;
  DateTime? _filtered;
  DateTime? _seen;
  DateTime? _sent;
  bool _following = false;
  double _aimPan = 0;
  double _aimTilt = 0;

  /// The user's direction as last worked out, for Robot lab.
  (double, double)? get estimate => _pan == null ? null : (_pan!, _tilt!);

  /// The face being followed, for Robot lab.
  FaceBox? get face => _face;

  /// Starts over: the robot was just attached and looks ahead.
  void reset() {
    _face = null;
    _pan = _tilt = null;
    _filtered = _seen = _sent = null;
    _following = false;
    _aimPan = _aimTilt = 0;
    look.reset();
  }

  /// A frame from the camera.
  void see(FaceFrame frame) {
    final now = _now();
    final face = _choose(frame.faces);
    _face = face;
    if (face == null) return _missing(now);
    _seen = now;

    // The face's angle from the middle of the picture: right and up are positive.
    final right = _degrees(
      atan((face.centreX - 0.5) * 2 * tan(_radians(frame.hfov) / 2)),
    );
    final up = _degrees(
      atan((0.5 - face.eyesY) * 2 * tan(_radians(frame.vfov) / 2)),
    );
    final taken = now.subtract(frame.age);
    final head = centre.at(taken);
    final (gazePan, gazeTilt) = look.at(taken);
    // A larger pan turns to the viewer's right; a larger tilt noses down.
    final pan = head.pan + gazePan + panSign * right;
    final tilt = head.tilt + gazeTilt - tiltSign * up;

    final last = _filtered;
    if (last == null || _pan == null) {
      _pan = pan;
      _tilt = tilt;
    } else {
      final dt = now.difference(last).inMicroseconds / 1e6;
      final k = 1 - exp(-dt / (smoothing.inMicroseconds / 1e6));
      _pan = _pan! + k * (pan - _pan!);
      _tilt = _tilt! + k * (tilt - _tilt!);
    }
    _filtered = now;
    if (!hold) _follow(now);
  }

  void _follow(DateTime now) {
    final pan = _pan!.clamp(-LookAt.panLimit, LookAt.panLimit);
    final tilt = _tilt!.clamp(LookAt.tiltUp, LookAt.tiltDown);
    final off = max((pan - _aimPan).abs(), (tilt - _aimTilt).abs());
    if (!_following && off <= deadband) return;
    _following = off > settle;
    _aim(now, pan, tilt);
  }

  void _missing(DateTime now) {
    final seen = _seen;
    if (seen == null || now.difference(seen) < forget) return;
    _pan = _tilt = null;
    _filtered = null;
    _following = false;
    if (_aimPan != 0 || _aimTilt != 0) _aim(now, 0, 0, always: true);
  }

  void _aim(DateTime now, double pan, double tilt, {bool always = false}) {
    final sent = _sent;
    final changed =
        (pan - _aimPan).abs() >= leastChange ||
        (tilt - _aimTilt).abs() >= leastChange;
    if (!always && !changed) return;
    if (!always && sent != null && now.difference(sent) < leastGap) return;
    _aimPan = pan;
    _aimTilt = tilt;
    _sent = now;
    look.aim(pan, tilt);
    send(LookAt(pan, tilt));
  }

  /// The largest face, unless the one followed is still there and not much smaller:
  /// the head does not flick between two people.
  FaceBox? _choose(List<FaceBox> faces) {
    if (faces.isEmpty) return null;
    final largest = faces.reduce((a, b) => a.area >= b.area ? a : b);
    final before = _face;
    if (before == null) return largest;
    FaceBox? same;
    for (final face in faces) {
      final near =
          (face.centreX - before.centreX).abs() < 0.15 &&
          (face.eyesY - before.eyesY).abs() < 0.15;
      if (near && (same == null || face.area > same.area)) same = face;
    }
    if (same != null && largest.area < same.area * 1.5) return same;
    return largest;
  }

  static double _radians(double degrees) => degrees * pi / 180;
  static double _degrees(double radians) => radians * 180 / pi;
}
