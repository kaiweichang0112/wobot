import 'dart:async';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/robot/link.dart';
import 'package:wobot/robot/robot.dart';
import 'package:wobot_faces/wobot_faces.dart';

import 'fake_robot.dart';

/// A camera that counts who watches it.
class FakeFaces implements FaceSource {
  StreamController<FaceFrame>? controller;
  int opened = 0;

  bool get watched => controller != null && controller!.hasListener;

  @override
  Stream<FaceFrame> frames() {
    opened++;
    return (controller = StreamController<FaceFrame>()).stream;
  }
}

void main() {
  late FakeBle ble;
  late FakeFaces faces;
  late Robot robot;

  setUp(() {
    ble = FakeBle()
      ..inReach.add(const FoundRobot('near', 'Wobot-Robot-AAAA', -40));
    faces = FakeFaces();
    robot = Robot(
      RobotLink(
        ble,
        FakeMemory(),
        choose: const Duration(milliseconds: 10),
        search: const Duration(milliseconds: 50),
      ),
      faces: faces,
    );
  });

  tearDown(() => robot.dispose());

  test('the camera is on only while a robot is connected', () async {
    expect(faces.opened, 0);
    await robot.open();
    expect(faces.watched, isTrue);

    await robot.pause();
    expect(faces.watched, isFalse);

    await robot.open();
    expect(faces.opened, 2);
    ble.drop('near');
    await until(() => !faces.watched);
  });

  test('a refused camera is said, and the robot moves on without it', () async {
    await robot.open();
    faces.controller!.addError(
      PlatformException(code: 'camera_denied', message: 'No camera.'),
    );
    await until(() => robot.camera.value == 'No camera.');
    expect(faces.watched, isFalse);
    expect(robot.status.value.connected, isTrue);
  });

  test('the director holds the gaze while it turns away on purpose', () async {
    await robot.open();
    robot.waiting();
    expect(robot.tracker.hold, isTrue);
    robot.idle();
    expect(robot.tracker.hold, isFalse);
  });
}
