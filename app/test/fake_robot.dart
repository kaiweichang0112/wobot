import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/robot/link.dart';

/// Bluetooth with robots in reach, a write log and connections that can drop.
class FakeBle implements BleTransport {
  Bluetooth state = Bluetooth.on;
  final inReach = <FoundRobot>[];
  final written = <String>[];
  final connected = <String>[];
  final disconnected = <String>[];
  final _links = <String, StreamController<bool>>{};
  bool robotService = true;
  bool failWrites = false;
  int scans = 0;

  /// How long a write takes, so commands queue behind it.
  Duration writeTakes = Duration.zero;

  @override
  Future<Bluetooth> bluetooth() async => state;

  @override
  Stream<FoundRobot> scan() {
    scans++;
    final found = StreamController<FoundRobot>();
    found.onListen = () async {
      await Future<void>.delayed(const Duration(milliseconds: 5));
      for (final robot in List.of(inReach)) {
        if (!found.isClosed) found.add(robot);
      }
    };
    found.onCancel = found.close;
    return found.stream;
  }

  @override
  Future<void> connect(String id, Duration timeout) async {
    connected.add(id);
    _links[id] = StreamController<bool>.broadcast();
  }

  @override
  Future<bool> isRobot(String id) async => robotService;

  @override
  Stream<bool> connection(String id) => _links[id]!.stream;

  @override
  Future<void> write(
    String id,
    List<int> bytes, {
    required bool withResponse,
  }) async {
    if (writeTakes > Duration.zero) await Future<void>.delayed(writeTakes);
    if (failWrites) throw Exception('write failed');
    written.add(ascii.decode(bytes));
  }

  @override
  Future<String> readStatus(String id) async => '90 160 90';

  @override
  Future<void> disconnect(String id) async => disconnected.add(id);

  /// The robot walks out of reach.
  void drop(String id) => _links[id]!.add(false);
}

class FakeMemory implements RobotMemory {
  RememberedRobot? robot;

  @override
  Future<RememberedRobot?> read() async => robot;

  @override
  Future<void> remember(RememberedRobot robot) async => this.robot = robot;

  @override
  Future<void> forget() async => robot = null;
}

/// Waits, a little at a time, until [ok] holds.
Future<void> until(bool Function() ok) async {
  final deadline = DateTime.now().add(const Duration(seconds: 2));
  while (!ok()) {
    if (DateTime.now().isAfter(deadline)) fail('timed out waiting');
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
}
