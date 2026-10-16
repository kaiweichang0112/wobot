import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/robot/protocol.dart';

void main() {
  test('commands are the lines the firmware parses', () {
    expect(const Attach().line, 'attach');
    expect(const Rest().line, 'X');
    expect(const MoveTo(Pose.neutral, 800).line, 'M 90 160 90 800');
    expect(
      const MoveTo(Pose(pan: 10.4, tilt: -8, roll: 5.6), 300).line,
      'M 100 152 96 300',
    );
    expect(const Sway(RobotAxis.tilt, 4, 1500, 2500).line, 'O t 4 1500 2500');
    expect(const Sway(RobotAxis.pan, 0.8, 320, 2500).line, 'O p 0.8 320 2500');
    expect(const Sway(RobotAxis.roll, 0, 1000, 2500).line, 'O r 0 1000 2500');
    expect(const LookAt(12.6, -5.2).line, 'L 13 -5');
  });

  test('angles stay inside the soft limits', () {
    // Tilt has only 15° of nose-down travel from neutral; pan and roll 45°.
    expect(
      const MoveTo(Pose(pan: 60, tilt: 30, roll: -60), 500).line,
      'M 135 175 45 500',
    );
    expect(const LookAt(90, -90).line, 'L 40 -40');
    expect(const LookAt(-90, 90).line, 'L -40 12');
    expect(const Sway(RobotAxis.tilt, 45, 0, -1).line, 'O t 20 1 0');
  });

  test('every command fits one write at the smallest MTU', () {
    final widest = [
      const Attach(),
      const Rest(),
      const MoveTo(Pose(pan: -99, tilt: -99, roll: -99), 999999),
      const MoveTo(Pose(pan: 99, tilt: 99, roll: 99), 60000),
      const Sway(RobotAxis.pan, 19.95, 999999, 999999),
      const Sway(RobotAxis.roll, 12.34, 60000, 60000),
      const LookAt(-40.4, -40.4),
    ];
    for (final command in widest) {
      expect(
        command.bytes.length,
        lessThanOrEqualTo(maxCommandBytes),
        reason: command.line,
      );
    }
  });

  test('only the commands that start or end a session wait for a response', () {
    expect(const Attach().withResponse, isTrue);
    expect(const Rest().withResponse, isTrue);
    expect(const MoveTo(Pose.neutral, 800).withResponse, isFalse);
    expect(const Sway(RobotAxis.tilt, 1, 1500, 2500).withResponse, isFalse);
    expect(const LookAt(0, 0).withResponse, isFalse);
  });
}
