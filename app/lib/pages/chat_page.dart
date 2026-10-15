import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api.dart';
import '../avatar/avatar_look.dart';
import '../avatar/avatar_session.dart';
import '../avatar/frame_cache.dart';
import '../chat.dart';
import '../theme.dart';
import '../voice/listener.dart';
import '../voice/question_language.dart';
import '../voice/speech_queue.dart';
import '../widgets/linked_text.dart';
import '../widgets/thinking.dart';
import 'avatar_page.dart';
import 'history_drawer.dart';
import 'settings_page.dart';

class ChatPage extends StatefulWidget {
  const ChatPage({
    super.key,
    required this.api,
    required this.me,
    required this.idToken,
    required this.chat,
    required this.history,
    this.frames,
    this.avatarSession,
  });

  final WobotApi api;
  final Me me;

  /// Where the avatar's frames are kept; the account's own directory by default.
  final FrameCache? frames;

  /// Avatar mode's session on the phone's recognizer and speaker by default; a test
  /// gives its own.
  final AvatarSession Function()? avatarSession;

  /// For the debug build's "Copy ID token".
  final Future<String?> Function() idToken;
  final ChatController chat;
  final HistoryController history;

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> with WidgetsBindingObserver {
  final _input = TextEditingController();
  late AssistantSettings _settings = widget.me.settings;
  late final FrameCache _frames =
      widget.frames ?? FileFrameCache(widget.me.accountId);
  bool _opening = false;

  ChatController get _chat => widget.chat;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    widget.api.warmUp();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _input.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    switch (state) {
      // Out of sight: the reply stops (CHAT-011). A system dialog only makes the app
      // inactive, which is not leaving it (spec 10).
      case AppLifecycleState.hidden:
        _chat.stop();
      // Back: warm the service, and show how each turn stands; nothing is sent again.
      case AppLifecycleState.resumed:
        widget.api.warmUp();
        _chat.reload();
        widget.history.refresh();
      default:
    }
  }

  void _send() {
    final text = _input.text;
    if (text.trim().isEmpty || _chat.busy) return;
    _input.clear();
    _chat.send(text);
  }

  Future<void> _openSettings() async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => SettingsPage(
          api: widget.api,
          accountId: widget.me.accountId,
          email: widget.me.email,
          idToken: widget.idToken,
          settings: _settings,
          onSettings: (settings) => setState(() => _settings = settings),
          beforeSignOut: _chat.stop,
          frames: _frames,
        ),
      ),
    );
  }

  /// Opens Avatar mode on this conversation, once the server says the account's own
  /// avatar and voice are both in use (MODE-004); otherwise says what to set up.
  Future<void> _openAvatar() async {
    if (_opening) return;
    _opening = true;
    unawaited(widget.api.warmUp());
    try {
      final Me me;
      try {
        me = await widget.api.me();
      } on Exception catch (e) {
        if (mounted) _say(describe(e));
        return;
      }
      if (!mounted) return;
      final avatarId = me.avatar.active?.id;
      if (!me.avatarMode || avatarId == null) {
        await showAvatarSetup(
          context,
          me,
          voice: () => _push(voiceSetupPage(widget.api)),
          avatar: () => _push(avatarSetupPage(widget.api, _frames)),
        );
        return;
      }
      _dismissKeyboard();
      await _push(
        AvatarPage(
          look: loadLook(widget.api, _frames, avatarId),
          session: (widget.avatarSession ?? _newSession)(),
          chat: _chat,
        ),
      );
    } finally {
      _opening = false;
    }
  }

  AvatarSession _newSession() => AvatarSession(
    api: widget.api,
    chat: _chat,
    listener: QuestionListener(DeviceSpeechEngine()),
    queue: SpeechQueue(DeviceSentencePlayer()),
    locale: QuestionLanguage.read,
  );

  Future<void> _push(Widget page) =>
      Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => page));

  void _say(String message) => ScaffoldMessenger.of(
    context,
  ).showSnackBar(SnackBar(content: Text(message)));

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _chat,
      builder: (context, _) => Scaffold(
        appBar: AppBar(
          leading: Builder(
            builder: (context) => IconButton(
              tooltip: 'Chats',
              icon: const Icon(Icons.menu),
              onPressed: () => Scaffold.of(context).openDrawer(),
            ),
          ),
          title: Text(
            _chat.conversation?.title ?? _settings.chatbotName,
            overflow: TextOverflow.ellipsis,
          ),
          actions: [
            IconButton(
              tooltip: 'New chat',
              icon: const Icon(Icons.edit_square),
              onPressed: _chat.startNew,
            ),
            IconButton(
              tooltip: 'Avatar mode',
              icon: const Icon(Icons.face_retouching_natural),
              onPressed: _openAvatar,
            ),
          ],
        ),
        onDrawerChanged: (_) => _dismissKeyboard(),
        drawer: HistoryDrawer(
          history: widget.history,
          chat: _chat,
          email: widget.me.email,
          onSettings: _openSettings,
        ),
        body: SafeArea(
          top: false,
          child: Column(
            children: [
              Expanded(child: _messages()),
              _Composer(
                input: _input,
                busy: _chat.busy,
                hint: 'Message ${_settings.chatbotName}',
                onSend: _send,
                onStop: _chat.stop,
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _messages() {
    final turns = _chat.turns;
    if (turns.isEmpty) {
      if (_chat.loading) return const Center(child: ThinkingMark(size: 28));
      return _EmptyChat(
        name: _settings.chatbotName,
        error: _chat.loadError,
        onRetry: _chat.reload,
      );
    }
    final earlier = _chat.hasEarlier ? 1 : 0;
    // Reversed, so the newest stays at the bottom while a reply grows.
    return ListView.builder(
      reverse: true,
      keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
      padding: const EdgeInsets.fromLTRB(20, 12, 20, 12),
      itemCount: turns.length + earlier,
      itemBuilder: (context, i) {
        if (i == turns.length) {
          return Center(
            child: TextButton(
              onPressed: _chat.loading ? null : _chat.loadEarlier,
              child: const Text('Show earlier messages'),
            ),
          );
        }
        final turn = turns[turns.length - 1 - i];
        return _TurnView(
          turn: turn,
          retryable: identical(turn, _chat.retryable),
          onRetry: _chat.retry,
        );
      },
    );
  }
}

class _EmptyChat extends StatelessWidget {
  const _EmptyChat({required this.name, this.error, required this.onRetry});

  final String name;
  final String? error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const ThinkingMark(size: 40, animate: false),
            const SizedBox(height: 20),
            Text(
              error ?? 'How can I help?',
              textAlign: TextAlign.center,
              style: text.headlineSmall?.copyWith(fontWeight: FontWeight.w500),
            ),
            const SizedBox(height: 8),
            if (error == null)
              Text(
                'Ask $name about GRC, its products and research, '
                'or anything else.',
                textAlign: TextAlign.center,
                style: text.bodyMedium?.copyWith(color: WobotColors.muted),
              )
            else
              TextButton(onPressed: onRetry, child: const Text('Try again')),
          ],
        ),
      ),
    );
  }
}

class _TurnView extends StatelessWidget {
  const _TurnView({
    required this.turn,
    required this.retryable,
    required this.onRetry,
  });

  final ChatTurn turn;
  final bool retryable;
  final VoidCallback onRetry;

  String get _waiting => switch (turn.stage) {
    'searching' => 'Looking it up…',
    'writing' => 'Writing…',
    _ => 'Thinking…',
  };

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.only(bottom: 20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Align(
            alignment: Alignment.centerRight,
            child: ConstrainedBox(
              constraints: BoxConstraints(
                maxWidth: MediaQuery.sizeOf(context).width * 0.8,
              ),
              child: Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 10,
                ),
                decoration: BoxDecoration(
                  color: WobotColors.surface,
                  borderRadius: BorderRadius.circular(20),
                ),
                child: Text(turn.question, style: text.bodyLarge),
              ),
            ),
          ),
          const SizedBox(height: 16),
          if (turn.running && turn.text.isEmpty)
            Row(
              children: [
                const ThinkingMark(size: 20),
                const SizedBox(width: 12),
                ShimmerText(_waiting, style: text.bodyLarge),
              ],
            )
          else ...[
            if (turn.text.isNotEmpty)
              LinkedText(turn.text, style: text.bodyLarge),
            if (turn.running)
              const Padding(
                padding: EdgeInsets.only(top: 10),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: ThinkingMark(size: 18),
                ),
              )
            else
              _Footer(turn: turn, retryable: retryable, onRetry: onRetry),
          ],
        ],
      ),
    );
  }
}

/// Below a reply that has ended: copy it, and say if it stopped or failed.
class _Footer extends StatelessWidget {
  const _Footer({
    required this.turn,
    required this.retryable,
    required this.onRetry,
  });

  final ChatTurn turn;
  final bool retryable;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final small = Theme.of(context).textTheme.bodySmall;
    final note = switch (turn.state) {
      TurnState.cancelled => Text(
        'Stopped',
        style: small?.copyWith(color: WobotColors.muted),
      ),
      TurnState.failed => Flexible(
        child: Text(
          turn.error ?? 'Something went wrong.',
          style: small?.copyWith(color: WobotColors.danger),
        ),
      ),
      _ => null,
    };
    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Row(
        children: [
          if (turn.text.isNotEmpty)
            IconButton(
              tooltip: 'Copy',
              visualDensity: VisualDensity.compact,
              iconSize: 18,
              color: WobotColors.muted,
              icon: const Icon(Icons.copy_rounded),
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: turn.text));
                if (!context.mounted) return;
                ScaffoldMessenger.of(
                  context,
                ).showSnackBar(const SnackBar(content: Text('Copied')));
              },
            ),
          if (retryable)
            TextButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh_rounded, size: 18),
              label: const Text('Retry'),
            ),
          if (note != null) ...[const SizedBox(width: 8), note],
        ],
      ),
    );
  }
}

class _Composer extends StatelessWidget {
  const _Composer({
    required this.input,
    required this.busy,
    required this.hint,
    required this.onSend,
    required this.onStop,
  });

  final TextEditingController input;
  final bool busy;
  final String hint;
  final VoidCallback onSend;
  final VoidCallback onStop;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 4, 12, 10),
      child: Container(
        padding: const EdgeInsets.fromLTRB(18, 4, 6, 4),
        decoration: BoxDecoration(
          color: WobotColors.surface,
          borderRadius: BorderRadius.circular(26),
          border: Border.all(color: WobotColors.outline, width: 0.5),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: TextField(
                controller: input,
                minLines: 1,
                maxLines: 6,
                maxLength: 2000,
                maxLengthEnforcement: MaxLengthEnforcement.enforced,
                // A tap anywhere else, the chat or any button, puts the keyboard away.
                onTapOutside: (_) => _dismissKeyboard(),
                textCapitalization: TextCapitalization.sentences,
                style: Theme.of(context).textTheme.bodyLarge,
                decoration: InputDecoration(
                  hintText: hint,
                  counterText: '',
                  contentPadding: const EdgeInsets.symmetric(vertical: 12),
                ),
              ),
            ),
            const SizedBox(width: 6),
            Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: busy
                  ? _RoundButton(
                      tooltip: 'Stop',
                      icon: Icons.stop_rounded,
                      filled: false,
                      onPressed: onStop,
                    )
                  : ValueListenableBuilder(
                      valueListenable: input,
                      builder: (context, value, _) => _RoundButton(
                        tooltip: 'Send',
                        icon: Icons.arrow_upward_rounded,
                        filled: true,
                        onPressed: value.text.trim().isEmpty ? null : onSend,
                      ),
                    ),
            ),
          ],
        ),
      ),
    );
  }
}

class _RoundButton extends StatelessWidget {
  const _RoundButton({
    required this.tooltip,
    required this.icon,
    required this.filled,
    required this.onPressed,
  });

  final String tooltip;
  final IconData icon;
  final bool filled;
  final VoidCallback? onPressed;

  @override
  Widget build(BuildContext context) {
    final enabled = onPressed != null;
    return IconButton(
      tooltip: tooltip,
      onPressed: onPressed,
      icon: Icon(icon, size: 22),
      style: IconButton.styleFrom(
        fixedSize: const Size.square(40),
        backgroundColor: filled
            ? (enabled ? WobotColors.accent : WobotColors.raised)
            : Colors.transparent,
        foregroundColor: filled
            ? (enabled ? const Color(0xFF1A0E06) : WobotColors.faint)
            : WobotColors.text,
        side: filled ? null : const BorderSide(color: WobotColors.outline),
      ),
    );
  }
}

/// Avatar mode needs the account's own avatar and voice (MODE-005): says which are
/// missing, and opens where each is made.
Future<void> showAvatarSetup(
  BuildContext context,
  Me me, {
  required VoidCallback voice,
  required VoidCallback avatar,
}) {
  final text = Theme.of(context).textTheme;
  final needsAvatar = me.avatar.active == null;
  final needsVoice = me.voice.active == null;
  return showModalBottomSheet<void>(
    context: context,
    builder: (sheet) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(28, 0, 28, 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(
              Icons.face_retouching_natural,
              size: 40,
              color: WobotColors.accent,
            ),
            const SizedBox(height: 16),
            Text('Avatar mode', style: text.titleLarge),
            const SizedBox(height: 8),
            Text(
              'Talk with Wobot face to face: your avatar speaks its replies in '
              'your voice. Set up your own avatar and voice first.',
              textAlign: TextAlign.center,
              style: text.bodyMedium?.copyWith(color: WobotColors.muted),
            ),
            const SizedBox(height: 24),
            for (final (needed, label, open) in [
              (needsAvatar, 'Set up Avatar', avatar),
              (needsVoice, 'Set up Voice', voice),
            ])
              if (needed)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: SizedBox(
                    width: double.infinity,
                    child: FilledButton(
                      onPressed: () {
                        Navigator.of(sheet).pop();
                        open();
                      },
                      child: Text(label),
                    ),
                  ),
                ),
          ],
        ),
      ),
    ),
  );
}

void _dismissKeyboard() => FocusManager.instance.primaryFocus?.unfocus();
