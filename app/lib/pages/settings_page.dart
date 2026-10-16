import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';

import '../api.dart';
import '../assets.dart';
import '../auth.dart';
import '../avatar/avatar_setup.dart';
import '../avatar/frame_cache.dart';
import '../chat.dart';
import '../theme.dart';
import '../voice/audio.dart';
import '../voice/question_language.dart';
import '../voice/voice_setup.dart';
import 'avatar_setup_page.dart';
import 'voice_lab_page.dart';
import 'voice_setup_page.dart';

/// Recording, cloning, hearing and putting a voice to use.
VoiceSetupPage voiceSetupPage(WobotApi api) => VoiceSetupPage(
  controller: VoiceSetupController(
    api,
    recorder: MicrophoneRecorder(),
    player: SpeakerPlayer(),
    temporary: getTemporaryDirectory,
  ),
);

/// Taking a photo, drawing its 21 frames, reviewing and putting them to use.
AvatarSetupPage avatarSetupPage(WobotApi api, FrameCache frames) =>
    AvatarSetupPage(controller: AvatarSetupController(api, frames));

class SettingsPage extends StatefulWidget {
  const SettingsPage({
    super.key,
    required this.api,
    required this.accountId,
    required this.email,
    required this.idToken,
    required this.settings,
    required this.onSettings,
    required this.beforeSignOut,
    this.frames,
    this.reauthenticate = signInAgain,
    this.deleteUser = deleteFirebaseUser,
  });

  final WobotApi api;
  final String accountId;
  final String email;
  final Future<String?> Function() idToken;
  final AssistantSettings settings;
  final ValueChanged<AssistantSettings> onSettings;
  final Future<void> Function() beforeSignOut;

  /// Signs in with Google again before a deletion; false when the user cancels.
  final Future<bool> Function() reauthenticate;

  /// Deletes the Firebase user and signs out, once the account is deleted.
  final Future<void> Function() deleteUser;

  /// Where the avatar's frames are kept; the account's own directory by default.
  final FrameCache? frames;

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late AssistantSettings _settings = widget.settings;
  late final FrameCache _frames =
      widget.frames ?? FileFrameCache(widget.accountId);

  /// How the voice and the avatar stand, read on opening and after each page.
  late Future<Me> _me = widget.api.me();

  String _language = QuestionLanguage.chinese;

  @override
  void initState() {
    super.initState();
    QuestionLanguage.read().then((locale) {
      if (mounted) setState(() => _language = locale);
    });
  }

  Future<void> _speakIn(String locale) async {
    setState(() => _language = locale);
    try {
      await QuestionLanguage.write(locale);
    } on Exception {
      _show('The language could not be saved on this phone.');
    }
  }

  void _show(String message) => ScaffoldMessenger.of(
    context,
  ).showSnackBar(SnackBar(content: Text(message)));

  Future<void> _rename() async {
    final name = await showDialog<String>(
      context: context,
      builder: (_) => _NameDialog(current: _settings.chatbotName),
    );
    if (name == null || name.trim() == _settings.chatbotName) return;
    try {
      final settings = await widget.api.renameAssistant(
        name,
        expectedVersion: _settings.version,
      );
      setState(() => _settings = settings);
      widget.onSettings(settings);
      _show(
        'From the next reply on, the assistant is ${settings.chatbotName}.',
      );
    } on ApiException catch (e) {
      if (e.code == 'REVISION_CONFLICT') {
        // Renamed on another device meanwhile: show that name, not overwrite it.
        final me = await widget.api.me();
        setState(() => _settings = me.settings);
        widget.onSettings(me.settings);
        _show('The name was changed on another device. Try again.');
      } else {
        _show(e.message);
      }
    } on Exception catch (e) {
      _show(describe(e));
    }
  }

  Future<void> _signOut() async {
    final navigator = Navigator.of(context);
    await widget.beforeSignOut();
    navigator.popUntil((route) => route.isFirst);
    try {
      await _frames.clear();
    } on Exception {
      // Signing out goes on; the frames are the account's own, under its ID.
    }
    await signOutAll();
  }

  Future<void> _deleteAccount() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete account?'),
        content: const Text(
          'Your conversations, voice and avatar are deleted, and this account '
          'is signed out. This cannot be undone. You will be asked to sign in '
          'with Google again first.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.of(context).pop(true),
            style: TextButton.styleFrom(foregroundColor: WobotColors.danger),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    final navigator = Navigator.of(context);
    try {
      if (!await widget.reauthenticate()) return;
      await widget.beforeSignOut();
      await widget.api.deleteAccount();
    } on ApiException catch (e) {
      _show(e.message);
      return;
    } on Exception catch (e) {
      _show(describe(e));
      return;
    }
    navigator.popUntil((route) => route.isFirst);
    try {
      await _frames.clear();
    } on Exception {
      // The account is deleted; the frames are its own, under its ID.
    }
    await widget.deleteUser();
  }

  Future<void> _copyIdToken() async {
    final token = await widget.idToken();
    if (token == null) return;
    await Clipboard.setData(ClipboardData(text: token));
    _show('ID token copied. It expires in an hour.');
  }

  Future<void> _open(Widget Function() page) async {
    await Navigator.of(
      context,
    ).push(MaterialPageRoute<void>(builder: (_) => page()));
    if (mounted) setState(() => _me = widget.api.me());
  }

  VoiceSetupPage _voicePage() => voiceSetupPage(widget.api);

  AvatarSetupPage _avatarPage() => avatarSetupPage(widget.api, _frames);

  Widget _assetTile({
    required IconData icon,
    required String title,
    required AssetSummary Function(Me me) of,
    required Widget Function() page,
  }) {
    return FutureBuilder<Me>(
      future: _me,
      builder: (context, snapshot) {
        final asset = snapshot.hasData ? of(snapshot.requireData) : null;
        final status = asset != null
            ? assetStatus(asset)
            : snapshot.hasError
            ? 'Could not be read'
            : '…';
        final failed = asset?.candidate?.status == 'failed';
        return ListTile(
          leading: Icon(icon),
          title: Text(title),
          subtitle: Text(
            status,
            style: failed ? const TextStyle(color: WobotColors.danger) : null,
          ),
          trailing: const Icon(Icons.chevron_right, color: WobotColors.faint),
          onTap: () => _open(page),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: ListView(
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          const _Header('Account'),
          _Section(
            children: [
              ListTile(
                leading: const Icon(Icons.mail_outline),
                title: Text(widget.email),
              ),
            ],
          ),
          const _Header('Assistant'),
          _Section(
            children: [
              ListTile(
                leading: const Icon(Icons.badge_outlined),
                title: const Text('Name'),
                subtitle: const Text('What the assistant calls itself'),
                trailing: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      _settings.chatbotName,
                      style: const TextStyle(color: WobotColors.muted),
                    ),
                    const Icon(Icons.chevron_right, color: WobotColors.faint),
                  ],
                ),
                onTap: _rename,
              ),
            ],
          ),
          const _Header('Avatar & voice'),
          _Section(
            children: [
              _assetTile(
                icon: Icons.record_voice_over_outlined,
                title: 'Voice',
                of: (me) => me.voice,
                page: _voicePage,
              ),
              _assetTile(
                icon: Icons.face_retouching_natural,
                title: 'Avatar',
                of: (me) => me.avatar,
                page: _avatarPage,
              ),
            ],
          ),
          const Padding(
            padding: EdgeInsets.fromLTRB(28, 8, 28, 0),
            child: Text(
              'Avatar mode starts once both your voice and your avatar are ready.',
              style: TextStyle(color: WobotColors.faint, fontSize: 13),
            ),
          ),
          const _Header('Speaking'),
          _Section(
            children: [
              ListTile(
                leading: const Icon(Icons.translate),
                title: const Text('Question language'),
                subtitle: const Text('What you speak in Avatar mode'),
                trailing: SegmentedButton<String>(
                  showSelectedIcon: false,
                  segments: const [
                    ButtonSegment(
                      value: QuestionLanguage.chinese,
                      label: Text('中文'),
                    ),
                    ButtonSegment(
                      value: QuestionLanguage.english,
                      label: Text('English'),
                    ),
                  ],
                  selected: {_language},
                  onSelectionChanged: (chosen) => _speakIn(chosen.single),
                ),
              ),
            ],
          ),
          if (kDebugMode) ...[
            const _Header('Developer'),
            _Section(
              children: [
                // Debug builds only: lets a developer call the API with curl.
                ListTile(
                  leading: const Icon(Icons.key_outlined),
                  title: const Text('Copy ID token'),
                  subtitle: const Text('Valid for an hour'),
                  onTap: _copyIdToken,
                ),
                // Debug builds only: tries the phone's speech recognition.
                ListTile(
                  leading: const Icon(Icons.mic_none),
                  title: const Text('Voice lab'),
                  subtitle: const Text('Speech recognition on this phone'),
                  onTap: () => Navigator.of(context).push(
                    MaterialPageRoute<void>(
                      builder: (_) => const VoiceLabPage(),
                    ),
                  ),
                ),
              ],
            ),
          ],
          const SizedBox(height: 24),
          _Section(
            children: [
              ListTile(
                leading: const Icon(Icons.logout, color: WobotColors.danger),
                title: const Text(
                  'Sign out',
                  style: TextStyle(color: WobotColors.danger),
                ),
                onTap: _signOut,
              ),
              ListTile(
                leading: const Icon(
                  Icons.delete_forever_outlined,
                  color: WobotColors.danger,
                ),
                title: const Text(
                  'Delete account',
                  style: TextStyle(color: WobotColors.danger),
                ),
                onTap: _deleteAccount,
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header(this.label);

  final String label;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(28, 20, 28, 8),
      child: Text(
        label,
        style: Theme.of(
          context,
        ).textTheme.labelMedium?.copyWith(color: WobotColors.faint),
      ),
    );
  }
}

class _Section extends StatelessWidget {
  const _Section({required this.children});

  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    // Material, not a colored box: a tile's ripple is painted on it.
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: Material(
        color: WobotColors.surface,
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        child: Column(
          children: [
            for (final (i, child) in children.indexed) ...[
              if (i > 0) const Divider(indent: 56),
              child,
            ],
          ],
        ),
      ),
    );
  }
}

class _NameDialog extends StatefulWidget {
  const _NameDialog({required this.current});

  final String current;

  @override
  State<_NameDialog> createState() => _NameDialogState();
}

class _NameDialogState extends State<_NameDialog> {
  late final _name = TextEditingController(text: widget.current);

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Assistant name'),
      content: TextField(
        controller: _name,
        autofocus: true,
        maxLength: 40,
        decoration: const InputDecoration(
          hintText: 'Wobot',
          border: UnderlineInputBorder(),
          focusedBorder: UnderlineInputBorder(
            borderSide: BorderSide(color: WobotColors.accent),
          ),
        ),
        onSubmitted: (name) => Navigator.of(context).pop(name),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text(
            'Cancel',
            style: TextStyle(color: WobotColors.text),
          ),
        ),
        ValueListenableBuilder(
          valueListenable: _name,
          builder: (context, value, _) => TextButton(
            onPressed: value.text.trim().isEmpty
                ? null
                : () => Navigator.of(context).pop(value.text),
            child: const Text('Save'),
          ),
        ),
      ],
    );
  }
}
