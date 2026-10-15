import 'dart:async';

import 'package:flutter/material.dart';

import '../avatar/animated_avatar.dart';
import '../avatar/avatar_look.dart';
import '../avatar/avatar_session.dart';
import '../chat.dart';
import '../theme.dart';
import '../widgets/linked_text.dart';
import '../widgets/thinking.dart';

/// Avatar mode (spec 22): the avatar alone on the screen, speaking its replies in the
/// account's voice. A tap shows the controls, which hide again after a few seconds;
/// the first tap only shows them, never presses one.
class AvatarPage extends StatefulWidget {
  const AvatarPage({
    super.key,
    required this.look,
    required this.session,
    required this.chat,
  });

  final Future<AvatarLook> look;

  /// Owned by the page: disposed when it closes.
  final AvatarSession session;

  /// The same conversation as Text mode: back there, the text goes on.
  final ChatController chat;

  static const hideAfter = Duration(seconds: 4);
  static const revealKey = ValueKey('avatar-reveal');

  @override
  State<AvatarPage> createState() => _AvatarPageState();
}

class _AvatarPageState extends State<AvatarPage> with WidgetsBindingObserver {
  bool _shown = false;
  bool _panel = false;
  Timer? _hide;

  AvatarSession get _session => widget.session;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _session.addListener(_changed);
    unawaited(_session.prepare());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _hide?.cancel();
    _session.removeListener(_changed);
    _session.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Out of sight: nothing is heard or played (CHAT-011); the chat page stops the
    // reply itself. Back again, nothing starts by itself.
    if (state == AppLifecycleState.hidden) unawaited(_session.pause());
  }

  void _changed() {
    if (!mounted) return;
    setState(() {
      // What needs the controls brings them back: a question heard, an error.
      if (_kept) _shown = true;
    });
    _later();
  }

  /// Controls that must stay: while a question is heard, an error is shown, the
  /// transcript is open, or a screen reader moves through them.
  bool get _kept =>
      _session.state == AvatarState.listening ||
      _session.error != null ||
      _panel ||
      MediaQuery.accessibleNavigationOf(context);

  void _reveal() {
    setState(() => _shown = true);
    _later();
  }

  /// Hides the controls after a while, unless they must stay by then.
  void _later() {
    _hide?.cancel();
    _hide = Timer(AvatarPage.hideAfter, () {
      if (mounted && !_kept) setState(() => _shown = false);
    });
  }

  void _act(VoidCallback action) {
    _later();
    action();
  }

  Future<void> _toText() async {
    await _session.pause();
    if (mounted) Navigator.of(context).pop();
  }

  Future<void> _showTranscript() async {
    setState(() => _panel = true);
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: WobotColors.surface,
      builder: (_) => _Transcript(chat: widget.chat, session: _session),
    );
    if (!mounted) return;
    setState(() => _panel = false);
    _later();
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      onPopInvokedWithResult: (popped, _) {
        if (popped) unawaited(_session.pause());
      },
      child: Scaffold(
        backgroundColor: WobotColors.background,
        body: FutureBuilder<AvatarLook>(
          future: widget.look,
          builder: (context, snapshot) {
            if (snapshot.hasError) {
              return _Unavailable(message: 'Your avatar could not be loaded.');
            }
            if (!snapshot.hasData) {
              return const Center(child: ThinkingMark(size: 36));
            }
            return _stage(snapshot.requireData);
          },
        ),
      ),
    );
  }

  Widget _stage(AvatarLook look) {
    final visible = _shown || _kept;
    return Stack(
      children: [
        Positioned.fill(
          child: SafeArea(
            child: Center(
              child: AspectRatio(
                aspectRatio: 1,
                child: ListenableBuilder(
                  listenable: Listenable.merge([_session, _session.speaking]),
                  builder: (context, _) => AnimatedAvatar(
                    frames: look.images,
                    expression: _session.expression,
                    speaking: _session.speaking.value,
                  ),
                ),
              ),
            ),
          ),
        ),
        // A tap on the screen keeps the controls a while longer.
        Positioned.fill(
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTap: _later,
          ),
        ),
        Positioned.fill(
          child: IgnorePointer(
            ignoring: !visible,
            child: ExcludeSemantics(
              excluding: !visible,
              child: AnimatedOpacity(
                opacity: visible ? 1 : 0,
                duration: const Duration(milliseconds: 180),
                child: _controls(),
              ),
            ),
          ),
        ),
        // While the controls are hidden, the first tap only shows them.
        if (!visible)
          Positioned.fill(
            child: Semantics(
              button: true,
              label: 'Show controls',
              child: GestureDetector(
                key: AvatarPage.revealKey,
                behavior: HitTestBehavior.opaque,
                onTap: _reveal,
              ),
            ),
          ),
      ],
    );
  }

  Widget _controls() {
    final session = _session;
    final listening = session.state == AvatarState.listening;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(8, 4, 8, 24),
        child: Column(
          children: [
            Row(
              children: [
                IconButton(
                  tooltip: 'Text mode',
                  icon: const Icon(Icons.chat_bubble_outline),
                  onPressed: () => _act(_toText),
                ),
                const Spacer(),
                IconButton(
                  tooltip: 'Show transcript',
                  icon: const Icon(Icons.subject),
                  onPressed: () => _act(_showTranscript),
                ),
              ],
            ),
            const Spacer(),
            _Status(session: session),
            const SizedBox(height: 16),
            if (session.canReadOn) ...[
              TextButton(
                onPressed: () => _act(session.readOn),
                child: const Text('Continue reading'),
              ),
              const SizedBox(height: 8),
            ],
            IconButton.filled(
              tooltip: listening ? 'Send' : 'Speak',
              iconSize: 32,
              style: IconButton.styleFrom(
                fixedSize: const Size.square(72),
                backgroundColor: WobotColors.accent,
                foregroundColor: const Color(0xFF1A0E06),
              ),
              icon: Icon(listening ? Icons.arrow_upward : Icons.mic),
              onPressed: () => _act(session.talk),
            ),
          ],
        ),
      ),
    );
  }
}

/// What the avatar is doing, in words under it.
class _Status extends StatelessWidget {
  const _Status({required this.session});

  final AvatarSession session;

  @override
  Widget build(BuildContext context) {
    final style = Theme.of(
      context,
    ).textTheme.bodyLarge?.copyWith(color: WobotColors.muted);
    final error = session.error;
    if (error != null) {
      return Text(
        error,
        textAlign: TextAlign.center,
        style: style?.copyWith(color: WobotColors.danger),
      );
    }
    final text = switch (session.state) {
      AvatarState.listening =>
        session.words.isEmpty ? 'Listening…' : session.words,
      AvatarState.waiting => 'Thinking…',
      AvatarState.speaking => '',
      AvatarState.idle => 'Tap the microphone to ask.',
    };
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Text(
        text,
        textAlign: TextAlign.center,
        maxLines: 4,
        overflow: TextOverflow.ellipsis,
        style: style,
      ),
    );
  }
}

/// The conversation in words: the whole of each reply, its lists, and reading on.
class _Transcript extends StatelessWidget {
  const _Transcript({required this.chat, required this.session});

  final ChatController chat;
  final AvatarSession session;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.7,
      maxChildSize: 0.95,
      builder: (context, scroll) => ListenableBuilder(
        listenable: Listenable.merge([chat, session]),
        builder: (context, _) => ListView(
          controller: scroll,
          reverse: true, // the latest reply first in sight
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 32),
          children: [
            if (session.canReadOn)
              Align(
                child: TextButton(
                  onPressed: () {
                    Navigator.of(context).pop();
                    session.readOn();
                  },
                  child: const Text('Continue reading'),
                ),
              ),
            for (final turn in chat.turns.reversed)
              Padding(
                padding: const EdgeInsets.only(top: 20),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      turn.question,
                      style: text.bodyMedium?.copyWith(
                        color: WobotColors.muted,
                      ),
                    ),
                    const SizedBox(height: 6),
                    if (turn.text.isNotEmpty)
                      LinkedText(turn.text, style: text.bodyLarge)
                    else if (turn.running)
                      const ShimmerText('Thinking…'),
                    if (turn.error case final error?)
                      Text(
                        error,
                        style: text.bodyMedium?.copyWith(
                          color: WobotColors.danger,
                        ),
                      ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _Unavailable extends StatelessWidget {
  const _Unavailable({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Column(
        children: [
          Align(
            alignment: Alignment.centerLeft,
            child: IconButton(
              tooltip: 'Text mode',
              icon: const Icon(Icons.chat_bubble_outline),
              onPressed: () => Navigator.of(context).pop(),
            ),
          ),
          Expanded(
            child: Center(
              child: Padding(
                padding: const EdgeInsets.all(32),
                child: Text(message, textAlign: TextAlign.center),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
