import 'dart:math';

import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/robot/pose_model.dart';
import 'package:wobot/robot/protocol.dart';
import 'package:wobot/robot/tracker.dart';
import 'package:wobot_faces/wobot_faces.dart';

const hfov = 50.0;
const vfov = 64.0;

double rad(double degrees) => degrees * pi / 180;

/// A rig on a fake clock: the tracker, a model of the firmware's gaze standing in
/// for the robot (each `L` reaching it 30 ms after it is sent), and a camera that
/// sees a user where [user] says, 100 ms before each frame arrives.
class Rig {
  Rig(this.async) {
    tracker = GazeTracker(
      send: (command) {
        sent.add(command);
        Future<void>.delayed(
          const Duration(milliseconds: 30),
          () => robot.aim(command.pan, command.tilt),
        );
      },
      centre: CentreModel(now),
      look: LookModel(now),
      now: now,
    );
    robot.reset();
  }

  final FakeAsync async;
  final start = DateTime(2026, 10, 16);
  late final GazeTracker tracker;
  late final LookModel robot = LookModel(now);
  final sent = <LookAt>[];

  /// Where the user is, as pan and tilt from neutral; null when out of sight.
  (double, double)? user = (0, 0);
  final pans = <double>[];

  DateTime now() => start.add(async.elapsed);

  /// Frames at 12 a second for [time].
  void watch(Duration time, {List<FaceBox> extra = const []}) {
    final end = async.elapsed + time;
    while (async.elapsed < end) {
      final taken = now().subtract(const Duration(milliseconds: 100));
      final (headPan, headTilt) = robot.at(taken);
      final faces = <FaceBox>[...extra];
      if (user case (final pan, final tilt)) {
        final right = rad(pan - headPan);
        final up = rad(headTilt - tilt);
        final x = 0.5 + tan(right) / (2 * tan(rad(hfov) / 2));
        final eyes = 0.5 - tan(up) / (2 * tan(rad(vfov) / 2));
        const size = 0.3;
        faces.add(FaceBox(x - size / 2, eyes - size * 0.4, size, size));
      }
      tracker.see(
        FaceFrame(
          faces: faces,
          age: const Duration(milliseconds: 100),
          detect: const Duration(milliseconds: 5),
          hfov: hfov,
          vfov: vfov,
        ),
      );
      async.elapse(const Duration(milliseconds: 83));
      pans.add(robot.at().$1);
    }
  }
}

void main() {
  test('turns to a user off to the side, and stops there', () {
    fakeAsync((async) {
      final rig = Rig(async)..user = (20, -10);
      rig.watch(const Duration(milliseconds: 1500));
      final (pan, tilt) = rig.robot.at();
      expect(pan, closeTo(20, 1));
      expect(tilt, closeTo(-10, 1));
      rig.watch(const Duration(seconds: 2));
      expect(rig.pans.reduce(max), lessThan(22)); // overshoots under 2°
      expect(rig.robot.at().$1, closeTo(20, 1));
    });
  });

  test('follows a user who walks across', () {
    fakeAsync((async) {
      final rig = Rig(async);
      for (var pan = 0.0; pan <= 30; pan += 2.5) {
        rig.user = (pan, 0);
        rig.watch(const Duration(milliseconds: 250));
      }
      rig.watch(const Duration(seconds: 1));
      expect(rig.robot.at().$1, closeTo(30, 1.5));
    });
  });

  test('lets small moves be', () {
    fakeAsync((async) {
      final rig = Rig(async)..user = (2, -1.5);
      rig.watch(const Duration(seconds: 2));
      expect(rig.sent, isEmpty);
    });
  });

  test('aims nothing while held, then catches up', () {
    fakeAsync((async) {
      final rig = Rig(async)..user = (15, 0);
      rig.tracker.hold = true;
      rig.watch(const Duration(seconds: 2));
      expect(rig.sent, isEmpty);
      rig.tracker.hold = false;
      rig.watch(const Duration(milliseconds: 1500));
      expect(rig.robot.at().$1, closeTo(15, 1));
    });
  });

  test('without a face keeps its aim a while, then looks ahead', () {
    fakeAsync((async) {
      final rig = Rig(async)..user = (20, 0);
      rig.watch(const Duration(seconds: 2));
      rig.user = null;
      rig.watch(const Duration(seconds: 3));
      expect(rig.robot.at().$1, closeTo(20, 1)); // still where the user was
      rig.watch(const Duration(seconds: 4));
      expect(rig.sent.last.line, 'L 0 0');
      expect(rig.robot.at().$1, closeTo(0, 1));
    });
  });

  test('keeps to the face it follows over a slightly larger one', () {
    fakeAsync((async) {
      final rig = Rig(async)..user = (10, 0);
      rig.watch(const Duration(seconds: 2));
      final followed = rig.tracker.face!;
      // Someone a little nearer at the edge of the picture.
      rig.watch(
        const Duration(seconds: 1),
        extra: [FaceBox(0.0, 0.3, followed.width * 1.2, followed.height * 1.2)],
      );
      expect(rig.robot.at().$1, closeTo(10, 1.5));
    });
  });

  test('the gaze model follows as the firmware does, without overshoot', () {
    fakeAsync((async) {
      final start = DateTime(2026);
      final look = LookModel(() => start.add(async.elapsed))..reset();
      look.aim(20, -10);
      var most = 0.0;
      for (var i = 0; i < 60; i++) {
        async.elapse(const Duration(milliseconds: 20));
        most = max(most, look.at().$1);
      }
      expect(look.at().$1, closeTo(20, 0.5));
      expect(look.at().$2, closeTo(-10, 0.5));
      expect(most, lessThanOrEqualTo(20.0001));
      // A frame from 300 ms ago sees the gaze as it was then.
      final then = look.at(
        start.add(async.elapsed - const Duration(milliseconds: 900)),
      );
      expect(then.$1, lessThan(look.at().$1));
    });
  });
}
