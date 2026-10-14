import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api.dart';
import '../auth.dart';
import '../chat.dart';
import '../theme.dart';

class SettingsPage extends StatefulWidget {
  const SettingsPage({
    super.key,
    required this.api,
    required this.email,
    required this.idToken,
    required this.settings,
    required this.onSettings,
    required this.beforeSignOut,
  });

  final WobotApi api;
  final String email;
  final Future<String?> Function() idToken;
  final AssistantSettings settings;
  final ValueChanged<AssistantSettings> onSettings;
  final Future<void> Function() beforeSignOut;

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late AssistantSettings _settings = widget.settings;

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
    await signOutAll();
  }

  Future<void> _copyIdToken() async {
    final token = await widget.idToken();
    if (token == null) return;
    await Clipboard.setData(ClipboardData(text: token));
    _show('ID token copied. It expires in an hour.');
  }

  void _open(Widget page) =>
      Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => page));

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
              ListTile(
                leading: const Icon(Icons.record_voice_over_outlined),
                title: const Text('Voice'),
                subtitle: const Text('Not set up'),
                trailing: const _Soon(),
                onTap: () => _open(const _ComingSoon(kind: _Kind.voice)),
              ),
              ListTile(
                leading: const Icon(Icons.face_retouching_natural),
                title: const Text('Avatar'),
                subtitle: const Text('Not set up'),
                trailing: const _Soon(),
                onTap: () => _open(const _ComingSoon(kind: _Kind.avatar)),
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

class _Soon extends StatelessWidget {
  const _Soon();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: WobotColors.accent.withValues(alpha: 0.6)),
      ),
      child: const Text(
        'Soon',
        style: TextStyle(color: WobotColors.accent, fontSize: 12),
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

enum _Kind { voice, avatar }

/// Where making a voice and an avatar will live (spec 21, 22): described now, built in
/// a later phase.
class _ComingSoon extends StatelessWidget {
  const _ComingSoon({required this.kind});

  final _Kind kind;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final (title, icon, lead, steps, action) = switch (kind) {
      _Kind.voice => (
        'Voice',
        Icons.record_voice_over_outlined,
        'Record your voice in the app, and Wobot reads its replies aloud in it.',
        [
          'Find a quiet room.',
          'Read the sentences shown, in your usual voice.',
          'Wobot makes your voice in the cloud; you can leave the app meanwhile.',
        ],
        'Record my voice',
      ),
      _Kind.avatar => (
        'Avatar',
        Icons.face_retouching_natural,
        'Take a photo of yourself, and Wobot draws an avatar that speaks with '
            'seven expressions.',
        [
          'Face the camera in good light.',
          'Wobot draws your avatar in the cloud; you can leave the app meanwhile.',
          'Replace or delete it at any time; the old one stays until a new one is ready.',
        ],
        'Take a photo',
      ),
    };
    return Scaffold(
      appBar: AppBar(title: Text(title)),
      body: ListView(
        padding: const EdgeInsets.all(28),
        children: [
          Icon(icon, size: 48, color: WobotColors.accent),
          const SizedBox(height: 20),
          Text(lead, style: text.titleMedium),
          const SizedBox(height: 20),
          for (final (i, step) in steps.indexed)
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    '${i + 1}',
                    style: const TextStyle(
                      color: WobotColors.accent,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(width: 14),
                  Expanded(
                    child: Text(
                      step,
                      style: text.bodyLarge?.copyWith(color: WobotColors.muted),
                    ),
                  ),
                ],
              ),
            ),
          const SizedBox(height: 24),
          FilledButton(onPressed: null, child: Text(action)),
          const SizedBox(height: 12),
          const Text(
            'Coming in a later version.',
            textAlign: TextAlign.center,
            style: TextStyle(color: WobotColors.faint),
          ),
        ],
      ),
    );
  }
}
