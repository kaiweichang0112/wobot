import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:wobot_faces/wobot_faces.dart';

import '../robot/director.dart';
import '../robot/link.dart';
import '../robot/motions.dart';
import '../robot/pose_model.dart';
import '../robot/protocol.dart';
import '../robot/tracker.dart';
import '../theme.dart';

/// Debug builds only: connects to the robot as Avatar mode does and drives it by
/// hand, to bring it up, to try a command before it is used in a motion, and to
/// watch each motion and a scripted reply on the rig.
class RobotLabPage extends StatefulWidget {
  const RobotLabPage({super.key, this.link, this.faces});

  /// The phone's Bluetooth by default; a test gives its own.
  final RobotLink? link;

  /// The phone's front camera by default.
  final FaceSource? faces;

  @override
  State<RobotLabPage> createState() => _RobotLabPageState();
}

class _RobotLabPageState extends State<RobotLabPage> {
  late final RobotLink _link =
      widget.link ?? RobotLink(UniversalBleTransport(), PhoneRobotMemory());
  late final Director _director = Director(_send);
  late final GazeTracker _tracker = GazeTracker(
    send: (command) {
      if (_aim) _send(command);
    },
    centre: _director.model,
    look: LookModel(DateTime.now),
  );
  late final FaceSource _faces = widget.faces ?? PhoneFaceSource();
  StreamSubscription<FaceFrame>? _watching;
  FaceFrame? _frame;
  String? _cameraError;

  /// Whether the tracker's aims are sent to the robot, or only shown.
  bool _aim = false;
  final _frameTimes = <DateTime>[];
  final _line = TextEditingController();
  final _sent = <String>[];
  String? _pose;
  int _mirror = 1;

  /// The scripted reply under way; another tap or leaving stops it.
  int _script = 0;

  @override
  void initState() {
    super.initState();
    _link.onConnected = () {
      _director.connected();
      _tracker.reset();
    };
    _director.holding.addListener(_hold);
  }

  void _hold() => _tracker.hold = _director.holding.value;

  void _watch(bool on) {
    unawaited(_watching?.cancel());
    _watching = null;
    setState(() {
      _frame = null;
      _cameraError = null;
    });
    if (!on) return;
    _watching = _faces.frames().listen(
      (frame) {
        _tracker.see(frame);
        final now = DateTime.now();
        _frameTimes
          ..add(now)
          ..removeWhere((t) => now.difference(t) > const Duration(seconds: 1));
        if (mounted) setState(() => _frame = frame);
      },
      onError: (Object error) {
        if (!mounted) return;
        setState(() {
          _cameraError = error is PlatformException ? error.message : '$error';
          _watching = null;
        });
      },
    );
  }

  @override
  void dispose() {
    unawaited(_watching?.cancel());
    _director.holding.removeListener(_hold);
    _script++;
    _line.dispose();
    _director.dispose();
    unawaited(_link.close().then((_) => _link.dispose()));
    super.dispose();
  }

  void _send(RobotCommand command) {
    if (!_link.connected || !mounted) return;
    _link.send(command);
    setState(() {
      _sent.insert(0, command.line);
      if (_sent.length > 30) _sent.removeLast();
    });
  }

  void _sendTyped() {
    final line = _line.text.trim();
    if (line.isEmpty) return;
    _send(RawLine(line));
    _line.clear();
  }

  Future<void> _readPose() async {
    try {
      final pose = await _link.readStatus();
      if (mounted) setState(() => _pose = pose ?? 'Not connected');
    } on Exception catch (e) {
      if (mounted) setState(() => _pose = 'Not read: $e');
    }
  }

  /// A reply as Avatar mode would play it: each sentence's motion, then the nod
  /// that hands the turn back; over and over until [until], for the slip test.
  Future<void> _reply(List<Spoken> lines, {Duration? until}) async {
    final script = ++_script;
    final end = DateTime.now().add(until ?? Duration.zero);
    do {
      for (final line in lines) {
        if (script != _script) return;
        _director.speaking(line);
        await Future<void>.delayed(line.length);
      }
      if (script != _script) return;
      _director.rested();
      await Future<void>.delayed(const Duration(seconds: 2));
    } while (script == _script && DateTime.now().isBefore(end));
  }

  void _stopScript() {
    _script++;
    _director.idle();
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Robot lab')),
      body: ValueListenableBuilder<RobotStatus>(
        valueListenable: _link.status,
        builder: (context, status, _) {
          final connected = status.connected;
          final typed = _line.text.trim().length;
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Text(status.message, style: text.titleMedium),
              const SizedBox(height: 12),
              Row(
                children: [
                  FilledButton(
                    onPressed: status.state == RobotState.idle
                        ? _link.open
                        : status.retryable
                        ? _link.retry
                        : null,
                    child: const Text('Connect'),
                  ),
                  const SizedBox(width: 12),
                  OutlinedButton(
                    onPressed: status.state == RobotState.idle
                        ? null
                        : _link.close,
                    child: const Text('Disconnect'),
                  ),
                ],
              ),
              const SizedBox(height: 24),
              Text('Commands', style: text.titleSmall),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final (label, command) in const [
                    ('attach', Attach()),
                    ('Neutral (X)', Rest()),
                    ('Look right', LookAt(15, 0)),
                    ('Look left', LookAt(-15, 0)),
                    ('Look up', LookAt(0, -15)),
                    ('Look ahead', LookAt(0, 0)),
                    ('Sway tilt', Sway(RobotAxis.tilt, 4, 1500, 2500)),
                    ('Stop sway', Sway(RobotAxis.tilt, 0, 1500, 2500)),
                  ])
                    OutlinedButton(
                      onPressed: connected ? () => _send(command) : null,
                      child: Text(label),
                    ),
                  OutlinedButton(
                    // The beat sentences start with, at the size it is played.
                    onPressed: connected
                        ? () => _director.play(beats.first)
                        : null,
                    child: const Text('Nod'),
                  ),
                ],
              ),
              const SizedBox(height: 16),
              TextField(
                controller: _line,
                enabled: connected,
                autocorrect: false,
                decoration: InputDecoration(
                  labelText: 'Command line, such as M 100 150 90 800',
                  helperText: typed > maxCommandBytes
                      ? 'Over $maxCommandBytes bytes: may not fit one write'
                      : null,
                  suffixIcon: IconButton(
                    tooltip: 'Send',
                    icon: const Icon(Icons.send),
                    onPressed: connected ? _sendTyped : null,
                  ),
                ),
                onChanged: (_) => setState(() {}),
                onSubmitted: (_) => _sendTyped(),
              ),
              const SizedBox(height: 16),
              Row(
                children: [
                  OutlinedButton(
                    onPressed: connected ? _readPose : null,
                    child: const Text('Read pose'),
                  ),
                  const SizedBox(width: 12),
                  Text(_pose ?? '', style: text.bodyLarge),
                ],
              ),
              const SizedBox(height: 24),
              Row(
                children: [
                  Text('Motions', style: text.titleSmall),
                  const Spacer(),
                  const Text('Mirrored'),
                  Switch(
                    value: _mirror == -1,
                    onChanged: (on) => setState(() => _mirror = on ? -1 : 1),
                  ),
                ],
              ),
              for (final (title, motions) in [
                for (final MapEntry(:key, :value) in feelings.entries)
                  (key, value),
                ('neutral', beats),
                ('knowledge', introductions),
                ('states', [waking, leaningIn, ...pondering, handingOver]),
              ]) ...[
                const SizedBox(height: 8),
                Text(title, style: text.bodyMedium),
                Wrap(
                  spacing: 8,
                  children: [
                    for (final motion in motions)
                      ActionChip(
                        label: Text(motion.id.split('.').last),
                        onPressed: connected
                            ? () => _director.play(motion, side: _mirror)
                            : null,
                      ),
                    if (feelings.containsKey(title))
                      ActionChip(
                        avatar: const Icon(Icons.shuffle, size: 16),
                        label: const Text('as a sentence'),
                        onPressed: connected
                            ? () => _director.speaking(
                                Spoken(
                                  index: 1,
                                  text: '這是一句測試的話。',
                                  emotion: title,
                                  length: const Duration(seconds: 3),
                                ),
                              )
                            : null,
                      ),
                  ],
                ),
              ],
              const SizedBox(height: 16),
              Text('Scripted replies', style: text.titleSmall),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final (label, lines) in _scripts)
                    OutlinedButton(
                      onPressed: connected ? () => _reply(lines) : null,
                      child: Text(label),
                    ),
                  OutlinedButton(
                    onPressed: connected
                        ? () => _reply(
                            _scripts.first.$2,
                            until: const Duration(seconds: 60),
                          )
                        : null,
                    child: const Text('Chat for 60 s (slip test)'),
                  ),
                  TextButton(
                    onPressed: connected ? _stopScript : null,
                    child: const Text('Stop'),
                  ),
                ],
              ),
              const SizedBox(height: 24),
              ..._facesView(text, connected),
              const SizedBox(height: 24),
              Text('Sent, latest first', style: text.titleSmall),
              const SizedBox(height: 8),
              for (final line in _sent)
                Text(
                  line,
                  style: text.bodyMedium?.copyWith(
                    fontFamily: 'Menlo',
                    color: WobotColors.muted,
                  ),
                ),
            ],
          );
        },
      ),
    );
  }

  /// Faces as the camera sees them, where the tracker thinks the user is, and what
  /// it aims; for checking the signs on the rig before the robot follows.
  List<Widget> _facesView(TextTheme text, bool connected) {
    final frame = _frame;
    final estimate = _tracker.estimate;
    final (aimPan, aimTilt) = _tracker.look.target;
    String degrees(double value) => value.toStringAsFixed(1);
    return [
      Text('Faces', style: text.titleSmall),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: const Text('Watch the front camera'),
        value: _watching != null,
        onChanged: _watch,
      ),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: const Text('Turn the robot toward the face'),
        value: _aim,
        onChanged: connected ? (on) => setState(() => _aim = on) : null,
      ),
      if (_cameraError case final error?)
        Text(
          error,
          style: text.bodyMedium?.copyWith(color: WobotColors.danger),
        ),
      if (frame != null) ...[
        Center(
          child: SizedBox(
            width: 180,
            child: AspectRatio(
              aspectRatio: frame.hfov / frame.vfov,
              child: CustomPaint(
                painter: _FacesPainter(frame.faces, _tracker.face),
              ),
            ),
          ),
        ),
        const SizedBox(height: 8),
        Text(
          '${frame.faces.length} face(s), ${_frameTimes.length} frames/s, '
          'found in ${frame.detect.inMilliseconds} ms, '
          '${frame.age.inMilliseconds} ms old',
          style: text.bodyMedium,
        ),
        Text(
          'User at pan ${estimate == null ? '-' : degrees(estimate.$1)}, '
          'tilt ${estimate == null ? '-' : degrees(estimate.$2)}; '
          'aim pan ${degrees(aimPan)}, tilt ${degrees(aimTilt)}',
          style: text.bodyMedium,
        ),
        const SizedBox(height: 4),
        Text(
          'Step to your own right: the box and pan go right, up. '
          'Raise your face: tilt goes down, below 0.',
          style: text.bodySmall?.copyWith(color: WobotColors.muted),
        ),
      ],
    ];
  }
}

Spoken _line(
  int index,
  String text,
  int seconds, {
  String emotion = 'neutral',
  String? route,
}) => Spoken(
  index: index,
  text: text,
  emotion: emotion,
  length: Duration(milliseconds: seconds * 1000),
  route: route,
);

/// Replies as each path speaks them, with their feelings and lengths made up.
final _scripts = [
  (
    'Chat',
    [
      _line(0, '哇，你今天也太早起了吧！', 2, emotion: 'joy', route: 'chat'),
      _line(1, '我還在想你什麼時候會來找我聊天。', 3, route: 'chat'),
      _line(2, '什麼？你昨天熬夜到三點？', 2, emotion: 'surprise', route: 'chat'),
      _line(3, '這樣身體會受不了的啦。', 3, emotion: 'sadness', route: 'chat'),
      _line(4, '下次再這樣我就要生氣了喔。', 2, emotion: 'anger', route: 'chat'),
      _line(5, '那你今天想先做什麼呢？', 3, route: 'chat'),
    ],
  ),
  (
    'Knowledge',
    [
      _line(0, 'GRC 是元智大學的老人福祉科技研究中心。', 4, route: 'knowledge'),
      _line(1, '中心由徐業良教授主持，研究長者照護的科技。', 4, route: 'knowledge'),
      _line(2, '世大智科是從中心衍生出來的公司。', 3, route: 'knowledge'),
      _line(3, '它的產品包括 WhizPad 和 WhizToys。', 3, route: 'knowledge'),
      _line(4, '你想進一步了解哪一個呢？', 3, route: 'knowledge'),
    ],
  ),
  (
    'List',
    [
      _line(0, '近三年有五篇期刊論文，先念前三篇。', 3, route: 'list'),
      _line(1, '2025 年，智慧床墊的離床偵測。', 3, route: 'list'),
      _line(2, '2024 年，長者互動玩具的使用研究。', 3, route: 'list'),
      _line(3, '2023 年，照護機構的感測網路。', 3, route: 'list'),
      _line(4, '還有 2 筆可以繼續聽。', 2, route: 'list'),
    ],
  ),
];

/// The picture's frame, its middle, and each face; the followed one in the accent.
class _FacesPainter extends CustomPainter {
  _FacesPainter(this.faces, this.followed);

  final List<FaceBox> faces;
  final FaceBox? followed;

  @override
  void paint(Canvas canvas, Size size) {
    final line = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1
      ..color = WobotColors.muted;
    canvas
      ..drawRect(Offset.zero & size, line)
      ..drawLine(
        Offset(size.width / 2, 0),
        Offset(size.width / 2, size.height),
        line,
      )
      ..drawLine(
        Offset(0, size.height / 2),
        Offset(size.width, size.height / 2),
        line,
      );
    for (final face in faces) {
      final paint = Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2
        ..color = identical(face, followed)
            ? WobotColors.accent
            : WobotColors.faint;
      canvas.drawRect(
        Rect.fromLTWH(
          face.x * size.width,
          face.y * size.height,
          face.width * size.width,
          face.height * size.height,
        ),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(_FacesPainter old) =>
      old.faces != faces || old.followed != followed;
}
