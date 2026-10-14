import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme.dart';
import '../voice/cer.dart';
import '../voice/lab_script.dart';
import '../voice/listener.dart';
import '../widgets/notes.dart';

/// Debug builds only: tries the phone's speech recognition (DEC-092), shows when a
/// question ends and why, and measures it against a script read aloud.
class VoiceLabPage extends StatefulWidget {
  const VoiceLabPage({super.key, this.engine});

  /// The phone's recognizer by default; a test gives its own.
  final SpeechEngine? engine;

  @override
  State<VoiceLabPage> createState() => _VoiceLabPageState();
}

/// A script line read, and what was heard.
class _Reading {
  const _Reading(this.id, this.said, this.locale, this.heard);

  final String id;
  final String said;
  final String locale;
  final Heard heard;

  int get length => normalize(said).runes.length;
  int get errors => edits(
    normalize(said).runes.toList(),
    normalize(heard.text).runes.toList(),
  );
}

class _VoiceLabPageState extends State<VoiceLabPage>
    with WidgetsBindingObserver {
  late final _listener = QuestionListener(
    widget.engine ?? DeviceSpeechEngine(),
  );
  String _locale = 'zh-TW';
  bool _script = false;
  int _line = 0;
  Heard? _last;
  final Map<String, _Reading> _readings = {};

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _listener.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Nothing listens out of sight (VOI-010); the permission dialog only makes the
    // app inactive.
    if (state == AppLifecycleState.hidden) _listener.cancel();
  }

  Future<void> _tap() async {
    if (_listener.listening) return _listener.finish();
    final line = _script ? labScript[_line] : null;
    setState(() => _last = null);
    final heard = await _listener.listen(localeId: _locale);
    if (!mounted || heard == null) return;
    setState(() {
      _last = heard;
      if (line != null) {
        _readings[line.$1] = _Reading(line.$1, line.$2, _locale, heard);
      }
    });
  }

  void _move(int by) => setState(() {
    _line = (_line + by).clamp(0, labScript.length - 1);
    _last = null;
  });

  String _seconds(Duration? d) =>
      d == null ? '–' : '${(d.inMilliseconds / 1000).toStringAsFixed(1)} s';

  String _percent(int errors, int length) =>
      length == 0 ? '–' : '${(100 * errors / length).toStringAsFixed(1)}%';

  /// CER over the lines read in a group: all edits over all characters said.
  String _groupCer(bool Function(int n) inGroup) {
    final read = _readings.values.where(
      (r) => inGroup(int.parse(r.id.substring(4))),
    );
    final errors = read.fold(0, (sum, r) => sum + r.errors);
    final length = read.fold(0, (sum, r) => sum + r.length);
    return '${_percent(errors, length)} (${read.length})';
  }

  Future<void> _copy() async {
    final lines = [
      'id\tlocale\treason\tcer\tfirst_words_s\tlast_words_to_end_s\t'
          'final_wait_s\tfinal\tsaid\theard',
      for (final (id, _) in labScript)
        if (_readings[id] case final r?)
          [
            r.id,
            r.locale,
            r.heard.reason.name,
            (r.errors / r.length).toStringAsFixed(3),
            _seconds(r.heard.firstWords).replaceAll(' s', ''),
            _seconds(r.heard.lastWordsToEnd).replaceAll(' s', ''),
            _seconds(r.heard.finalWait).replaceAll(' s', ''),
            r.heard.finalArrived,
            r.said,
            r.heard.text,
          ].join('\t'),
    ];
    await Clipboard.setData(ClipboardData(text: lines.join('\n')));
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('${_readings.length} readings copied.')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Voice lab'),
        actions: [
          if (_readings.isNotEmpty)
            IconButton(
              tooltip: 'Copy results',
              icon: const Icon(Icons.copy_all_outlined),
              onPressed: _copy,
            ),
        ],
      ),
      body: ListenableBuilder(
        listenable: _listener,
        builder: (context, _) {
          final listening = _listener.listening;
          final line = _script ? labScript[_line] : null;
          final last = _last;
          final reading = line == null ? null : _readings[line.$1];
          return ListView(
            padding: const EdgeInsets.fromLTRB(20, 12, 20, 32),
            children: [
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'zh-TW', label: Text('zh-TW')),
                  ButtonSegment(value: 'en-US', label: Text('en-US')),
                ],
                selected: {_locale},
                showSelectedIcon: false,
                onSelectionChanged: listening
                    ? null
                    : (chosen) => setState(() => _locale = chosen.single),
              ),
              const SizedBox(height: 8),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Read the script'),
                subtitle: Text('${_readings.length}/${labScript.length} read'),
                value: _script,
                onChanged: listening
                    ? null
                    : (on) => setState(() {
                        _script = on;
                        _last = null;
                      }),
              ),
              if (line != null) ...[
                Row(
                  children: [
                    IconButton(
                      onPressed: listening || _line == 0
                          ? null
                          : () => _move(-1),
                      icon: const Icon(Icons.chevron_left),
                    ),
                    Expanded(
                      child: Text(
                        '${line.$1} · ${_line + 1}/${labScript.length}',
                        textAlign: TextAlign.center,
                        style: const TextStyle(color: WobotColors.muted),
                      ),
                    ),
                    IconButton(
                      onPressed: listening || _line == labScript.length - 1
                          ? null
                          : () => _move(1),
                      icon: const Icon(Icons.chevron_right),
                    ),
                  ],
                ),
                Text(
                  line.$2,
                  textAlign: TextAlign.center,
                  style: text.titleLarge,
                ),
                const SizedBox(height: 16),
              ],
              Center(
                child: SizedBox.square(
                  dimension: 88,
                  child: FilledButton(
                    style: FilledButton.styleFrom(
                      shape: const CircleBorder(),
                      backgroundColor: listening
                          ? WobotColors.danger
                          : WobotColors.accent,
                    ),
                    onPressed: _tap,
                    child: Icon(listening ? Icons.stop : Icons.mic, size: 36),
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Text(
                listening ? 'Listening… tap to send' : 'Tap to speak',
                textAlign: TextAlign.center,
                style: const TextStyle(color: WobotColors.faint),
              ),
              const SizedBox(height: 16),
              Container(
                constraints: const BoxConstraints(minHeight: 72),
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  color: WobotColors.surface,
                  borderRadius: BorderRadius.circular(16),
                ),
                child: Text(
                  _listener.words.isEmpty && last?.empty == true
                      ? 'No speech detected. Try again.'
                      : _listener.words,
                  style: text.bodyLarge,
                ),
              ),
              if (_listener.error case final error?) ErrorNote(error),
              if (last != null) ...[
                const SizedBox(height: 12),
                _Facts([
                  ('Ended by', last.reason.name),
                  ('First words', _seconds(last.firstWords)),
                  ('Last words → end', _seconds(last.lastWordsToEnd)),
                  (
                    'End → text settled',
                    '${_seconds(last.finalWait)}'
                        '${last.finalArrived ? '' : ' (partial kept)'}',
                  ),
                  ('Total', _seconds(last.total)),
                  if (reading != null)
                    ('CER', _percent(reading.errors, reading.length)),
                ]),
              ],
              if (_readings.isNotEmpty) ...[
                const SizedBox(height: 24),
                Text('Script', style: text.titleMedium),
                const SizedBox(height: 8),
                _Facts([
                  ('Chinese 01–16', _groupCer((n) => n <= 16)),
                  ('English 17–20', _groupCer((n) => n >= 17 && n <= 20)),
                  ('Both 21–24', _groupCer((n) => n >= 21)),
                  ('All', _groupCer((_) => true)),
                ]),
                const SizedBox(height: 8),
                for (final (id, _) in labScript)
                  if (_readings[id] case final r?)
                    ListTile(
                      dense: true,
                      contentPadding: EdgeInsets.zero,
                      title: Text(r.heard.text.isEmpty ? '–' : r.heard.text),
                      subtitle: Text('$id · ${r.locale}'),
                      trailing: Text(_percent(r.errors, r.length)),
                    ),
              ],
            ],
          );
        },
      ),
    );
  }
}

class _Facts extends StatelessWidget {
  const _Facts(this.facts);

  final List<(String, String)> facts;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        for (final (label, value) in facts)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 3),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    label,
                    style: const TextStyle(color: WobotColors.muted),
                  ),
                ),
                Text(value),
              ],
            ),
          ),
      ],
    );
  }
}
