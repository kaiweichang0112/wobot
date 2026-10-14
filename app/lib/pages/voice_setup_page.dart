import 'package:flutter/material.dart';

import '../api.dart';
import '../assets.dart';
import '../theme.dart';
import '../voice/audio.dart';
import '../voice/voice_setup.dart';
import '../widgets/notes.dart';
import '../widgets/thinking.dart';

/// What the sample is read from: Chinese and English, as the replies will be, for
/// about a minute and a half.
const _script = [
  '你好，我正在錄一段自己的聲音，讓 Wobot 學會我說話的方式。我平常會問它輔具和產品的問題，'
      '例如尺寸多大、價格多少、適合什麼樣的使用者。有時候我也會請它比較兩項產品，說說各自的'
      '優點，還有使用時要注意的地方。現在我在一個安靜的房間裡，用平常聊天的速度，一句一句慢慢地'
      '念。念錯了也沒關係，我會自然地接下去，就像在跟朋友說話一樣。等它學會以後，我希望聽到它用'
      '我的聲音回答問題時，會覺得熟悉又親切。',
  "Hello, this is my own voice. I'm reading this so that Wobot can learn how I "
      'speak. I often ask it about products: how big they are, what they cost, '
      'and who they suit best. Sometimes I ask it to compare two of them and tell '
      "me what to watch out for. I'm reading at my usual pace, in a quiet room, "
      'just as I would talk to a friend.',
];

/// Records a sample, follows the voice being cloned in the cloud, plays it, and puts
/// it to use.
class VoiceSetupPage extends StatefulWidget {
  const VoiceSetupPage({super.key, required this.controller});

  /// Owned by the page from here on: it goes when the page does.
  final VoiceSetupController controller;

  @override
  State<VoiceSetupPage> createState() => _VoiceSetupPageState();
}

class _VoiceSetupPageState extends State<VoiceSetupPage>
    with WidgetsBindingObserver {
  VoiceSetupController get _voice => widget.controller;

  /// Recording a new voice in place of the one in use or the one that failed.
  bool _recordingNew = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _voice.load();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _voice.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    switch (state) {
      // The microphone's permission dialog only makes the app inactive (spec 10).
      case AppLifecycleState.hidden:
        _voice.leave();
      case AppLifecycleState.resumed:
        _voice.resume();
      default:
    }
  }

  Future<void> _submit() async {
    await _voice.submit();
    if (mounted && _voice.recording == Recording.idle) {
      setState(() => _recordingNew = false);
    }
  }

  Future<void> _delete(Voice voice) async {
    final inUse = identical(voice, _voice.active);
    final sure =
        await showDialog<bool>(
          context: context,
          builder: (dialog) => AlertDialog(
            title: Text(
              inUse ? 'Delete your voice?' : 'Discard the new voice?',
            ),
            content: Text(
              inUse
                  ? 'Avatar mode stops until you make a new one. The voice is '
                        'removed from ElevenLabs.'
                  : 'It is removed from ElevenLabs. Your current voice, if any, '
                        'stays.',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(dialog).pop(false),
                child: const Text(
                  'Cancel',
                  style: TextStyle(color: WobotColors.text),
                ),
              ),
              TextButton(
                onPressed: () => Navigator.of(dialog).pop(true),
                child: Text(
                  inUse ? 'Delete' : 'Discard',
                  style: const TextStyle(color: WobotColors.danger),
                ),
              ),
            ],
          ),
        ) ??
        false;
    if (sure) await _voice.delete(voice);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Voice')),
      body: ListenableBuilder(
        listenable: Listenable.merge([_voice, _voice.player.playing]),
        builder: (context, _) {
          if (_voice.loading && _voice.active == null) {
            return const Center(child: ThinkingMark(size: 28));
          }
          return ListView(
            padding: const EdgeInsets.fromLTRB(20, 12, 20, 32),
            children: _content(),
          );
        },
      ),
    );
  }

  List<Widget> _content() {
    final active = _voice.active;
    final candidate = _voice.candidate;
    final recorderShown =
        _recordingNew ||
        _voice.recording != Recording.idle ||
        (active == null && candidate == null);
    return [
      if (candidate != null && !recorderShown) ..._candidate(candidate),
      if (active != null && !recorderShown) ...[
        if (candidate != null) const SizedBox(height: 28),
        ..._inUse(active, replaceable: candidate == null),
      ],
      if (recorderShown) ..._recorder(),
      if (_voice.error case final error?) ErrorNote(error),
    ];
  }

  List<Widget> _inUse(Voice voice, {required bool replaceable}) {
    final text = Theme.of(context).textTheme;
    return [
      Text('Your voice', style: text.titleMedium),
      const SizedBox(height: 6),
      const Text(
        'In use: Avatar mode speaks its replies in it.',
        style: TextStyle(color: WobotColors.muted),
      ),
      const SizedBox(height: 16),
      _previewButton(voice),
      if (replaceable) ...[
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: _voice.busy
              ? null
              : () => setState(() => _recordingNew = true),
          child: const Text('Replace with a new recording'),
        ),
      ],
      const SizedBox(height: 8),
      OutlinedButton(
        style: OutlinedButton.styleFrom(foregroundColor: WobotColors.danger),
        onPressed: _voice.busy ? null : () => _delete(voice),
        child: const Text('Delete voice'),
      ),
    ];
  }

  List<Widget> _candidate(Voice voice) {
    final text = Theme.of(context).textTheme;
    final busy = _voice.busy;
    return switch (voice.status) {
      'queued' || 'running' => [
        Text('Making your voice', style: text.titleMedium),
        const SizedBox(height: 12),
        const LinearProgressIndicator(backgroundColor: WobotColors.surface),
        const SizedBox(height: 12),
        const Text(
          'It takes about half a minute. You can leave this page or the app; '
          'Wobot keeps going.',
          style: TextStyle(color: WobotColors.muted),
        ),
        if (_voice.active != null) ...[
          const SizedBox(height: 8),
          const Text(
            'Your current voice stays in use until you use the new one.',
            style: TextStyle(color: WobotColors.muted),
          ),
        ],
      ],
      'ready' => [
        Text('Your new voice is ready', style: text.titleMedium),
        const SizedBox(height: 6),
        const Text(
          'Listen to it, then use it.',
          style: TextStyle(color: WobotColors.muted),
        ),
        const SizedBox(height: 16),
        _previewButton(voice),
        const SizedBox(height: 8),
        FilledButton(
          onPressed: busy ? null : _voice.activate,
          child: const Text('Use this voice'),
        ),
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: busy ? null : () => _delete(voice),
          child: const Text('Discard'),
        ),
      ],
      _ => [
        Text('The voice was not made', style: text.titleMedium),
        const SizedBox(height: 6),
        Text(
          assetFailure(voice.errorCode),
          style: const TextStyle(color: WobotColors.muted),
        ),
        const SizedBox(height: 16),
        FilledButton(
          onPressed: busy ? null : () => setState(() => _recordingNew = true),
          child: const Text('Record again'),
        ),
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: busy ? null : () => _delete(voice),
          child: const Text('Discard'),
        ),
      ],
    };
  }

  Widget _previewButton(Voice voice) {
    final playing = _voice.player.playing.value;
    return FilledButton.tonalIcon(
      onPressed: _voice.busy
          ? null
          : playing
          ? _voice.stopPlaying
          : () => _voice.preview(voice),
      icon: Icon(playing ? Icons.stop : Icons.play_arrow),
      label: Text(playing ? 'Stop' : 'Play a sample'),
    );
  }

  List<Widget> _recorder() {
    final text = Theme.of(context).textTheme;
    final recording = _voice.recording;
    final playing = _voice.player.playing.value;
    final short =
        recording == Recording.recorded &&
        _voice.elapsed < VoiceSetupController.shortest;
    return [
      Text(
        'Record your voice, and Wobot reads its replies aloud in it.',
        style: text.titleMedium,
      ),
      const SizedBox(height: 16),
      const Steps([
        'Find a quiet room and hold the phone as for a call.',
        'Read both passages below in your usual voice; one to three minutes.',
        'Wobot makes your voice in the cloud; you can leave meanwhile.',
      ]),
      const SizedBox(height: 4),
      Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: WobotColors.surface,
          borderRadius: BorderRadius.circular(16),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            for (final (i, passage) in _script.indexed) ...[
              if (i > 0) const SizedBox(height: 14),
              Text(passage, style: text.bodyLarge?.copyWith(height: 1.7)),
            ],
          ],
        ),
      ),
      const SizedBox(height: 20),
      _Meter(
        elapsed: _voice.elapsed,
        level: _voice.level,
        live: recording == Recording.recording,
      ),
      const SizedBox(height: 16),
      if (_voice.microphoneRefused) const PermissionNote('microphone'),
      if (short)
        const ErrorNote(
          'At least a minute is needed. Record again and read both passages.',
        ),
      ...switch (recording) {
        Recording.idle || Recording.starting => [
          FilledButton.icon(
            onPressed: recording == Recording.starting || _voice.busy
                ? null
                : _voice.startRecording,
            icon: const Icon(Icons.mic),
            label: const Text('Start recording'),
          ),
          if (_voice.active != null || _voice.candidate != null) ...[
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: () => setState(() => _recordingNew = false),
              child: const Text('Cancel'),
            ),
          ],
        ],
        Recording.recording => [
          FilledButton.icon(
            style: FilledButton.styleFrom(backgroundColor: WobotColors.danger),
            onPressed: _voice.stopRecording,
            icon: const Icon(Icons.stop),
            label: const Text('Stop'),
          ),
        ],
        Recording.recorded => [
          const InfoNote(
            'Create sends the recording to ElevenLabs to clone your voice; the '
            'recording is deleted once the voice is made. Create only with your '
            'own voice.',
          ),
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _voice.canSubmit ? _submit : null,
            child: Text(_voice.busy ? 'Sending…' : 'Create my voice'),
          ),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            onPressed: _voice.busy
                ? null
                : playing
                ? _voice.stopPlaying
                : _voice.playRecording,
            icon: Icon(playing ? Icons.stop : Icons.play_arrow),
            label: Text(playing ? 'Stop' : 'Listen to the recording'),
          ),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed: _voice.busy ? null : _voice.startRecording,
            child: const Text('Record again'),
          ),
        ],
      },
    ];
  }
}

/// How long the recording is, against the minute it needs, and how loud it is.
class _Meter extends StatelessWidget {
  const _Meter({
    required this.elapsed,
    required this.level,
    required this.live,
  });

  final Duration elapsed;
  final double level;
  final bool live;

  @override
  Widget build(BuildContext context) {
    final seconds = elapsed.inSeconds;
    final enough = elapsed >= VoiceSetupController.shortest;
    final longest = VoiceSetupController.longest.inSeconds;
    return Row(
      children: [
        Text(
          '${seconds ~/ 60}:${(seconds % 60).toString().padLeft(2, '0')}',
          style: TextStyle(
            fontSize: 28,
            fontFeatures: const [FontFeature.tabularFigures()],
            color: enough ? WobotColors.text : WobotColors.muted,
          ),
        ),
        const SizedBox(width: 16),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(4),
                child: LinearProgressIndicator(
                  value: live ? levelHeight(level) : 0,
                  minHeight: 8,
                  backgroundColor: WobotColors.surface,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                enough
                    ? 'Enough; up to ${longest ~/ 60} minutes is fine.'
                    : 'At least 1:00',
                style: const TextStyle(color: WobotColors.faint, fontSize: 12),
              ),
            ],
          ),
        ),
      ],
    );
  }
}
