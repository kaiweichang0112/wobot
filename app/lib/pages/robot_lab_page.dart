import 'dart:async';

import 'package:flutter/material.dart';

import '../robot/link.dart';
import '../robot/protocol.dart';
import '../theme.dart';

/// Debug builds only: connects to the robot as Avatar mode does and drives it by
/// hand, to bring it up and to try a command before it is used in a motion.
class RobotLabPage extends StatefulWidget {
  const RobotLabPage({super.key, this.link});

  /// The phone's Bluetooth by default; a test gives its own.
  final RobotLink? link;

  @override
  State<RobotLabPage> createState() => _RobotLabPageState();
}

class _RobotLabPageState extends State<RobotLabPage> {
  late final RobotLink _link =
      widget.link ?? RobotLink(UniversalBleTransport(), PhoneRobotMemory());
  final _line = TextEditingController();
  final _sent = <String>[];
  String? _pose;

  @override
  void dispose() {
    _line.dispose();
    unawaited(_link.close().then((_) => _link.dispose()));
    super.dispose();
  }

  void _send(RobotCommand command) {
    if (!_link.connected) return;
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

  /// A small nod made of two moves, as a motion would send it.
  Future<void> _nod() async {
    _send(const MoveTo(Pose(tilt: 5), 220));
    await Future<void>.delayed(const Duration(milliseconds: 240));
    _send(const MoveTo(Pose.neutral, 300));
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
                    onPressed: connected ? _nod : null,
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
}
