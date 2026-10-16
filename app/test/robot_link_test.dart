import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/robot/link.dart';
import 'package:wobot/robot/protocol.dart';

import 'fake_robot.dart';

void main() {
  late FakeBle ble;
  late FakeMemory memory;
  late RobotLink link;
  late int connects;

  const near = FoundRobot('near', 'Wobot-Robot-AAAA', -40);
  const far = FoundRobot('far', 'Wobot-Robot-BBBB', -80);

  setUp(() {
    ble = FakeBle();
    memory = FakeMemory();
    connects = 0;
    link = RobotLink(
      ble,
      memory,
      choose: const Duration(milliseconds: 20),
      search: const Duration(milliseconds: 80),
      writeTimeout: const Duration(milliseconds: 200),
      keepAlive: const Duration(milliseconds: 60),
      backoff: const [Duration(milliseconds: 10)],
    )..onConnected = () => connects++;
  });

  tearDown(() async {
    await link.close();
    link.dispose();
  });

  test(
    'connects to the strongest robot, attaches it and sets the lease',
    () async {
      ble.inReach.addAll([far, near]);
      await link.open();
      expect(link.status.value.state, RobotState.connected);
      expect(link.status.value.name, 'Wobot-Robot-AAAA');
      expect(ble.connected, ['near']);
      expect(ble.written.take(2), ['attach', 'O t 0 1000 2500']);
      expect(memory.robot?.id, 'near');
      expect(connects, 1);
    },
  );

  test('keeps to the remembered robot, even when another is nearer', () async {
    memory.robot = const RememberedRobot('far', 'Wobot-Robot-BBBB');
    ble.inReach.addAll([near, far]);
    await link.open();
    expect(ble.connected, ['far']);
  });

  test('never takes another robot when the remembered one is away', () async {
    memory.robot = const RememberedRobot('far', 'Wobot-Robot-BBBB');
    ble.inReach.add(near);
    await link.open();
    await until(() => link.status.value.state == RobotState.notFound);
    expect(ble.connected, isEmpty);
    expect(ble.scans, 3); // three tries, then it waits for a tap
  });

  test('says why Bluetooth cannot be used, without scanning', () async {
    ble.state = Bluetooth.denied;
    await link.open();
    expect(link.status.value.state, RobotState.unavailable);
    expect(link.status.value.bluetooth, Bluetooth.denied);
    expect(link.status.value.message, contains('Allow it in Settings'));
    expect(ble.scans, 0);
  });

  test('a tap looks again once a robot is in reach', () async {
    await link.open();
    await until(() => link.status.value.state == RobotState.notFound);
    ble.inReach.add(near);
    await link.retry();
    expect(link.status.value.state, RobotState.connected);
  });

  test('a device without the robot service is not taken', () async {
    ble
      ..inReach.add(near)
      ..robotService = false;
    await link.open();
    await until(() => link.status.value.state == RobotState.notFound);
    expect(ble.disconnected, contains('near'));
    expect(ble.written, isEmpty);
  });

  test('writes in order, keeping only the newest look waiting', () async {
    ble.inReach.add(near);
    await link.open();
    ble.writeTakes = const Duration(milliseconds: 20);
    link
      ..send(const MoveTo(Pose(tilt: 5), 220))
      ..send(const LookAt(5, 0))
      ..send(const LookAt(10, 0))
      ..send(const Sway(RobotAxis.pan, 2, 2400, 9999));
    await until(() => ble.written.contains('O p 2 2400 2500'));
    expect(ble.written.skip(2).take(3), [
      'M 90 165 90 220',
      'L 10 0',
      'O p 2 2400 2500', // every lease is the link's own
    ]);
  });

  test('a quiet link refreshes the lease with the last sway', () async {
    ble.inReach.add(near);
    await link.open();
    link.send(const Sway(RobotAxis.tilt, 1.5, 1500, 2500));
    await until(
      () =>
          ble.written.where((line) => line == 'O t 1.5 1500 2500').length >= 3,
    );
    expect(ble.written.where((line) => line.startsWith('M')), isEmpty);
  });

  test('a dropped robot is found and attached again', () async {
    ble.inReach.add(near);
    await link.open();
    ble.drop('near');
    await until(() => connects == 2);
    expect(link.status.value.state, RobotState.connected);
    expect(ble.written.where((line) => line == 'attach').length, 2);
  });

  test('a failed write is a lost robot', () async {
    ble.inReach.add(near);
    await link.open();
    ble.failWrites = true;
    link.send(const MoveTo(Pose.neutral, 800));
    await until(() => link.status.value.state == RobotState.lost);
    ble.failWrites = false;
    await until(() => connects == 2);
  });

  test('closing sends the head to neutral, then lets go', () async {
    ble.inReach.add(near);
    await link.open();
    await link.close();
    expect(ble.written.last, 'X');
    expect(ble.disconnected, ['near']);
    expect(link.status.value.state, RobotState.idle);
    final before = ble.written.length;
    link.send(const MoveTo(Pose.neutral, 800));
    await Future<void>.delayed(const Duration(milliseconds: 100));
    expect(ble.written.length, before); // nothing is sent, not even the lease
  });

  test('closing while looking stops the scan and connects nothing', () async {
    final opening = link.open();
    await until(() => ble.scans == 1);
    await link.close();
    ble.inReach.add(near);
    await opening;
    await Future<void>.delayed(const Duration(milliseconds: 100));
    expect(ble.connected, isEmpty);
    expect(link.status.value.state, RobotState.idle);
  });
}
