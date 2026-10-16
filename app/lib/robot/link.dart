import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:universal_ble/universal_ble.dart';

import 'protocol.dart';

/// Whether the phone's Bluetooth can be used.
enum Bluetooth { on, off, denied, unsupported, unknown }

/// A robot heard advertising.
class FoundRobot {
  const FoundRobot(this.id, this.name, this.rssi);

  final String id;
  final String name;
  final int rssi;
}

/// What the link needs of the phone's Bluetooth; a test gives its own.
abstract class BleTransport {
  /// Asks for Bluetooth the first time, and waits for the answer.
  Future<Bluetooth> bluetooth();

  /// The robots heard while the stream is listened to.
  Stream<FoundRobot> scan();

  Future<void> connect(String id, Duration timeout);

  /// Whether a connected device has the robot's command characteristic.
  Future<bool> isRobot(String id);

  /// Emits false when the device is disconnected.
  Stream<bool> connection(String id);

  Future<void> write(String id, List<int> bytes, {required bool withResponse});

  /// The `Status` characteristic: the pose the firmware last computed.
  Future<String> readStatus(String id);

  Future<void> disconnect(String id);
}

/// The phone's Bluetooth, through `universal_ble`. On iOS the system asks for
/// Bluetooth the first time a scan or a state is asked for, which happens only when
/// Avatar mode opens.
class UniversalBleTransport implements BleTransport {
  static Bluetooth _of(AvailabilityState state) => switch (state) {
    AvailabilityState.poweredOn => Bluetooth.on,
    AvailabilityState.poweredOff => Bluetooth.off,
    AvailabilityState.unauthorized => Bluetooth.denied,
    AvailabilityState.unsupported => Bluetooth.unsupported,
    AvailabilityState.unknown ||
    AvailabilityState.resetting => Bluetooth.unknown,
  };

  @override
  Future<Bluetooth> bluetooth() async {
    final now = _of(await UniversalBle.getBluetoothAvailabilityState());
    if (now != Bluetooth.unknown) return now;
    // Unknown until the user answers the system's question about Bluetooth.
    return UniversalBle.availabilityStream
        .map(_of)
        .firstWhere((state) => state != Bluetooth.unknown)
        .timeout(
          const Duration(seconds: 30),
          onTimeout: () => Bluetooth.unknown,
        );
  }

  @override
  Stream<FoundRobot> scan() {
    late final StreamController<FoundRobot> found;
    StreamSubscription<BleDevice>? results;
    found = StreamController<FoundRobot>(
      onListen: () async {
        results = UniversalBle.scanStream.listen((device) {
          final name = device.name;
          if (name == null || !name.startsWith(robotNamePrefix)) return;
          found.add(FoundRobot(device.deviceId, name, device.rssi ?? -127));
        });
        try {
          await UniversalBle.startScan(
            scanFilter: ScanFilter(withNamePrefix: [robotNamePrefix]),
          );
        } on Exception catch (e) {
          found.addError(e);
        }
      },
      onCancel: () async {
        await results?.cancel();
        try {
          await UniversalBle.stopScan();
        } on Exception {
          // Bluetooth went away meanwhile: nothing is scanning.
        }
      },
    );
    return found.stream;
  }

  @override
  Future<void> connect(String id, Duration timeout) =>
      UniversalBle.connect(id, timeout: timeout);

  @override
  Future<bool> isRobot(String id) async {
    final services = await UniversalBle.discoverServices(id);
    return services.any(
      (service) =>
          service.uuid.toLowerCase() == robotService &&
          service.characteristics.any(
            (c) => c.uuid.toLowerCase() == robotCommandCharacteristic,
          ),
    );
  }

  @override
  Stream<bool> connection(String id) => UniversalBle.connectionStream(id);

  @override
  Future<void> write(
    String id,
    List<int> bytes, {
    required bool withResponse,
  }) => UniversalBle.write(
    id,
    robotService,
    robotCommandCharacteristic,
    Uint8List.fromList(bytes),
    withoutResponse: !withResponse,
  );

  @override
  Future<String> readStatus(String id) async => ascii.decode(
    await UniversalBle.read(id, robotService, robotStatusCharacteristic),
    allowInvalid: true,
  );

  @override
  Future<void> disconnect(String id) =>
      UniversalBle.disconnect(id, timeout: const Duration(seconds: 3));
}

/// The robot this phone connected to last.
class RememberedRobot {
  const RememberedRobot(this.id, this.name);

  final String id;
  final String name;
}

/// Where the phone keeps its robot; a test gives its own.
abstract class RobotMemory {
  Future<RememberedRobot?> read();
  Future<void> remember(RememberedRobot robot);
  Future<void> forget();
}

/// Kept on this phone, not with the account: the robot sits beside the phone.
class PhoneRobotMemory implements RobotMemory {
  static const _id = 'robot_id';
  static const _name = 'robot_name';

  @override
  Future<RememberedRobot?> read() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final id = prefs.getString(_id);
      final name = prefs.getString(_name);
      return id == null || name == null ? null : RememberedRobot(id, name);
    } on Exception {
      return null;
    }
  }

  @override
  Future<void> remember(RememberedRobot robot) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_id, robot.id);
    await prefs.setString(_name, robot.name);
  }

  @override
  Future<void> forget() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_id);
    await prefs.remove(_name);
  }
}

enum RobotState {
  /// Not asked to connect: Avatar mode is closed.
  idle,

  /// Bluetooth is off, refused or missing.
  unavailable,
  searching,
  connecting,
  connected,

  /// No robot was heard; a tap looks again.
  notFound,

  /// The connection dropped; looking for the robot again.
  lost,
}

class RobotStatus {
  const RobotStatus(this.state, {this.name, this.bluetooth});

  final RobotState state;

  /// The robot's advertised name, once one is chosen.
  final String? name;

  /// Why Bluetooth cannot be used, when [state] is unavailable.
  final Bluetooth? bluetooth;

  bool get connected => state == RobotState.connected;

  /// Whether a tap should look again.
  bool get retryable =>
      state == RobotState.notFound ||
      state == RobotState.unavailable ||
      state == RobotState.lost;

  String get message => switch (state) {
    RobotState.idle => 'Robot not in use.',
    RobotState.unavailable => switch (bluetooth) {
      Bluetooth.off => 'Bluetooth is off.',
      Bluetooth.denied => 'Wobot may not use Bluetooth. Allow it in Settings.',
      Bluetooth.unsupported => 'This phone has no Bluetooth.',
      _ => 'Bluetooth is not ready.',
    },
    RobotState.searching => 'Looking for the robot…',
    RobotState.connecting => 'Connecting to $name…',
    RobotState.connected => 'Connected to $name.',
    RobotState.notFound => 'No robot found. Tap to look again.',
    RobotState.lost => 'The robot is out of reach. Looking again…',
  };
}

/// The Bluetooth link to the robot (`firmware/README.md`): finds it, attaches its
/// servos, keeps its lease alive and writes commands one after another. Closing it
/// sends the head back to neutral and lets go.
///
/// The lease is the robot's own safety: without a command for [leaseMs] it goes
/// back to neutral, so a phone that crashes or walks away cannot leave it moving.
/// The link keeps it alive by sending the last `O` again, which changes nothing.
class RobotLink {
  RobotLink(
    this._ble,
    this.memory, {
    this.choose = const Duration(seconds: 2),
    this.search = const Duration(seconds: 8),
    this.connectTimeout = const Duration(seconds: 8),
    this.writeTimeout = const Duration(seconds: 2),
    this.keepAlive = const Duration(milliseconds: 800),
    this.backoff = const [
      Duration(seconds: 1),
      Duration(seconds: 2),
      Duration(seconds: 4),
      Duration(seconds: 8),
      Duration(seconds: 16),
      Duration(seconds: 30),
    ],
    this.firstTries = 3,
    this.leaseMs = 2500,
  });

  final BleTransport _ble;
  final RobotMemory memory;

  /// With no robot remembered, how long to listen before taking the strongest one.
  final Duration choose;

  /// How long to look before saying none was found.
  final Duration search;
  final Duration connectTimeout;
  final Duration writeTimeout;

  /// The longest silence before the lease is refreshed.
  final Duration keepAlive;

  /// The waits between tries to reach the robot again.
  final List<Duration> backoff;

  /// How many times to look, when no robot has been reached since opening, before
  /// waiting for a tap.
  final int firstTries;
  final int leaseMs;

  final status = ValueNotifier(const RobotStatus(RobotState.idle));

  /// Called each time the robot is connected and attached.
  VoidCallback? onConnected;

  bool _open = false;
  bool _reached = false;
  bool _disposed = false;
  int _generation = 0;
  int _tries = 0;
  String? _device;
  StreamSubscription<bool>? _watch;
  VoidCallback? _stopLooking;
  Timer? _again;
  Timer? _beat;
  final _waiting = <RobotCommand>[];
  int? _pumping;
  DateTime _lastWrite = DateTime.now();
  Sway? _last;

  bool get connected => status.value.connected;

  /// Finds and connects the robot, unless already doing so.
  Future<void> open() async {
    if (_open) return;
    _open = true;
    _reached = false;
    _tries = 0;
    await _connect(++_generation);
  }

  /// Looks again at once, after nothing was found or the robot was lost.
  Future<void> retry() async {
    if (!status.value.retryable) return;
    _again?.cancel();
    _open = true;
    await _connect(++_generation);
  }

  /// Sends a command after those before it. Nothing is sent while not connected; a
  /// look still waiting is replaced by a newer one.
  void send(RobotCommand command) {
    if (!connected) return;
    if (command is LookAt) _waiting.removeWhere((c) => c is LookAt);
    if (command is Sway && command.leaseMs != leaseMs) {
      command = Sway(
        command.axis,
        command.amplitude,
        command.periodMs,
        leaseMs,
      );
    }
    _waiting.add(command);
    if (_pumping != _generation) unawaited(_pump(_generation));
  }

  /// The pose the firmware last computed, as `pan tilt roll`; null when not connected.
  Future<String?> readStatus() async {
    final device = _device;
    if (device == null) return null;
    return _ble.readStatus(device).timeout(writeTimeout);
  }

  /// Back to neutral, then disconnected: Avatar mode closed or out of sight.
  Future<void> close() async {
    _open = false;
    _generation++;
    _again?.cancel();
    _stopLooking?.call();
    final device = _device;
    _clear();
    _set(const RobotStatus(RobotState.idle));
    if (device == null) return;
    try {
      await _ble
          .write(device, const Rest().bytes, withResponse: true)
          .timeout(writeTimeout);
    } on Exception {
      // Unreachable: its lease takes it back to neutral by itself.
    }
    try {
      await _ble.disconnect(device);
    } on Exception {
      // Already gone.
    }
  }

  void dispose() {
    _disposed = true;
    status.dispose();
  }

  Future<void> _connect(int generation) async {
    bool current() => generation == _generation && _open;
    try {
      final bluetooth = await _ble.bluetooth();
      if (!current()) return;
      if (bluetooth != Bluetooth.on) {
        _set(RobotStatus(RobotState.unavailable, bluetooth: bluetooth));
        return;
      }
      _set(
        RobotStatus(
          _reached ? RobotState.lost : RobotState.searching,
          name: status.value.name,
        ),
      );
      final remembered = await memory.read();
      final found = await _find(remembered?.id);
      if (!current()) return;
      if (found == null) return _missed(generation);
      _set(RobotStatus(RobotState.connecting, name: found.name));
      await _ble.connect(found.id, connectTimeout);
      if (!current()) return _let(found.id);
      if (!await _ble.isRobot(found.id)) {
        await _let(found.id);
        return _missed(generation);
      }
      if (!current()) return _let(found.id);
      _device = found.id;
      _watch = _ble.connection(found.id).listen((up) {
        if (!up) _lost(generation);
      });
      _last = Sway(RobotAxis.tilt, 0, 1000, leaseMs);
      await _write(found.id, const Attach());
      await _write(found.id, _last!);
      if (!current()) return;
      await memory.remember(RememberedRobot(found.id, found.name));
      if (!current()) return;
      _reached = true;
      _tries = 0;
      _beat = Timer.periodic(keepAlive ~/ 4, (_) => _refresh());
      _set(RobotStatus(RobotState.connected, name: found.name));
      onConnected?.call();
    } on Exception catch (e) {
      debugPrint('the robot could not be reached: $e');
      if (current()) _lost(generation);
    }
  }

  /// Listens for robots: the remembered one as soon as it is heard; with none
  /// remembered, the strongest heard in [choose], or the first after it.
  Future<FoundRobot?> _find(String? remembered) {
    final done = Completer<FoundRobot?>();
    final heard = <String, FoundRobot>{};
    var chosen = false;
    FoundRobot? strongest() => heard.isEmpty
        ? null
        : heard.values.reduce((a, b) => a.rssi >= b.rssi ? a : b);
    late final StreamSubscription<FoundRobot> listening;
    late final Timer choosing;
    late final Timer giving;
    void finish(FoundRobot? robot) {
      if (done.isCompleted) return;
      choosing.cancel();
      giving.cancel();
      unawaited(listening.cancel());
      _stopLooking = null;
      done.complete(robot);
    }

    listening = _ble.scan().listen((robot) {
      if (remembered != null) {
        if (robot.id == remembered) finish(robot);
        return;
      }
      heard[robot.id] = robot;
      if (chosen) finish(strongest());
    }, onError: (Object _) => finish(null));
    choosing = Timer(choose, () {
      chosen = true;
      if (remembered == null && heard.isNotEmpty) finish(strongest());
    });
    giving = Timer(search, () => finish(strongest()));
    _stopLooking = () => finish(null);
    return done.future;
  }

  /// Nothing to connect to: looks again a few times, then waits for a tap; once the
  /// robot has been reached since opening, it keeps looking.
  void _missed(int generation) {
    if (generation != _generation || !_open) return;
    if (!_reached && ++_tries >= firstTries) {
      _set(const RobotStatus(RobotState.notFound));
      return;
    }
    _set(
      RobotStatus(
        _reached ? RobotState.lost : RobotState.searching,
        name: status.value.name,
      ),
    );
    _later();
  }

  void _lost(int generation) {
    if (generation != _generation) return;
    final device = _device;
    _clear();
    if (device != null) unawaited(_let(device));
    if (!_open) return;
    if (!_reached) return _missed(generation);
    _set(RobotStatus(RobotState.lost, name: status.value.name));
    _later();
  }

  void _later() {
    _again?.cancel();
    final wait = backoff[min(_tries, backoff.length - 1)];
    if (_reached) _tries++;
    _again = Timer(wait, () {
      if (_open) unawaited(_connect(++_generation));
    });
  }

  Future<void> _pump(int generation) async {
    _pumping = generation;
    try {
      while (_waiting.isNotEmpty && generation == _generation) {
        final device = _device;
        if (device == null) break;
        await _write(device, _waiting.removeAt(0));
      }
    } on Exception catch (e) {
      debugPrint('a robot command was not written: $e');
      _lost(generation);
    } finally {
      if (_pumping == generation) _pumping = null;
    }
  }

  Future<void> _write(String device, RobotCommand command) async {
    await _ble
        .write(device, command.bytes, withResponse: command.withResponse)
        .timeout(writeTimeout);
    _lastWrite = DateTime.now();
    if (command is Sway) _last = command;
  }

  /// Sends the last `O` again when nothing else was sent for a while: the robot's
  /// lease starts over, and nothing moves.
  void _refresh() {
    final last = _last;
    if (!connected || last == null || _waiting.isNotEmpty) return;
    if (DateTime.now().difference(_lastWrite) >= keepAlive) send(last);
  }

  Future<void> _let(String device) async {
    try {
      await _ble.disconnect(device);
    } on Exception {
      // Already gone.
    }
  }

  void _clear() {
    _device = null;
    unawaited(_watch?.cancel());
    _watch = null;
    _beat?.cancel();
    _beat = null;
    _waiting.clear();
  }

  void _set(RobotStatus next) {
    if (!_disposed) status.value = next;
  }
}
