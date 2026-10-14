import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:image_picker/image_picker.dart';

import '../api.dart';
import '../assets.dart';
import '../avatar/avatar_setup.dart';
import '../avatar/frames.dart';
import '../theme.dart';
import '../widgets/notes.dart';
import '../widgets/thinking.dart';

/// Takes a photo, follows its 21 frames being drawn in the cloud, and puts the avatar
/// to use once each frame has been looked at (AC-AVA-07).
class AvatarSetupPage extends StatefulWidget {
  const AvatarSetupPage({super.key, required this.controller, this.takePhoto});

  final AvatarSetupController controller;

  /// Defaults to the front camera; a test gives its own.
  final Future<Uint8List?> Function(ImageSource source)? takePhoto;

  @override
  State<AvatarSetupPage> createState() => _AvatarSetupPageState();
}

class _AvatarSetupPageState extends State<AvatarSetupPage>
    with WidgetsBindingObserver {
  AvatarSetupController get _avatar => widget.controller;

  /// A photo taken, not yet sent.
  Uint8List? _photo;
  bool _cameraRefused = false;
  String? _cameraError;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _avatar.load();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _avatar.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    switch (state) {
      // The camera's permission dialog only makes the app inactive (spec 10).
      case AppLifecycleState.hidden:
        _avatar.pause();
      case AppLifecycleState.resumed:
        _avatar.resume();
      default:
    }
  }

  Future<void> _take(ImageSource source) async {
    setState(() {
      _cameraRefused = false;
      _cameraError = null;
    });
    try {
      final photo = await (widget.takePhoto ?? _pick)(source);
      if (photo != null && mounted) setState(() => _photo = photo);
    } on PlatformException catch (e) {
      if (!mounted) return;
      setState(() {
        if (e.code == 'camera_access_denied') {
          _cameraRefused = true;
        } else {
          _cameraError = 'The camera could not be opened.';
        }
      });
    }
  }

  /// A JPEG at most 1536 pixels a side: enough for a 1024-pixel frame, and a HEIC
  /// photo arrives as JPEG.
  static Future<Uint8List?> _pick(ImageSource source) async {
    final file = await ImagePicker().pickImage(
      source: source,
      preferredCameraDevice: CameraDevice.front,
      maxWidth: 1536,
      maxHeight: 1536,
      imageQuality: 90,
    );
    return file?.readAsBytes();
  }

  Future<void> _send() async {
    final photo = _photo;
    if (photo == null) return;
    await _avatar.upload(photo);
    if (mounted && _avatar.unsent == null) setState(() => _photo = null);
  }

  Future<bool> _confirm(String title, String body, String action) async =>
      await showDialog<bool>(
        context: context,
        builder: (dialog) => AlertDialog(
          title: Text(title),
          content: Text(body),
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
                action,
                style: const TextStyle(color: WobotColors.danger),
              ),
            ),
          ],
        ),
      ) ??
      false;

  Future<void> _delete(Avatar avatar) async {
    final inUse = identical(avatar, _avatar.active);
    final sure = await _confirm(
      inUse ? 'Delete your avatar?' : 'Discard the new avatar?',
      inUse
          ? 'Avatar mode stops until you make a new one. Its frames are removed.'
          : 'Its frames are removed. Your current avatar, if any, stays.',
      inUse ? 'Delete' : 'Discard',
    );
    if (sure) await _avatar.delete(avatar);
  }

  void _view(Avatar avatar, String key) => Navigator.of(context).push(
    MaterialPageRoute<void>(
      fullscreenDialog: true,
      builder: (_) => _FrameViewer(
        controller: _avatar,
        avatarId: avatar.id,
        initial: key,
        confirm: _confirm,
      ),
    ),
  );

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Avatar')),
      body: ListenableBuilder(
        listenable: _avatar,
        builder: (context, _) {
          if (_avatar.loading && _avatar.active == null) {
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
    final error = _avatar.error;
    final photoNotes = [
      if (_cameraRefused) const PermissionNote('camera'),
      if (_cameraError case final message?) ErrorNote(message),
    ];
    final photo = _photo;
    if (photo != null) {
      return [..._photoReview(photo), ?_errorNote(error)];
    }
    final candidate = _avatar.candidate;
    final active = _avatar.active;
    if (candidate == null && active == null) {
      return [..._intro(), ...photoNotes, ?_errorNote(error)];
    }
    final shown = candidate ?? active!;
    return [
      _heading(shown),
      if (candidate != null && active != null)
        const Padding(
          padding: EdgeInsets.only(bottom: 12),
          child: InfoNote(
            'Your current avatar stays in use until you use the new one.',
          ),
        ),
      FrameGrid(
        avatar: shown,
        png: _avatar.png,
        onTap: (key) => _view(shown, key),
      ),
      const SizedBox(height: 20),
      ?_errorNote(error),
      ...photoNotes,
      ..._actions(shown),
    ];
  }

  Widget? _errorNote(String? error) => error == null ? null : ErrorNote(error);

  List<Widget> _intro() {
    final text = Theme.of(context).textTheme;
    return [
      const SizedBox(height: 8),
      const Icon(
        Icons.face_retouching_natural,
        size: 48,
        color: WobotColors.accent,
      ),
      const SizedBox(height: 20),
      Text(
        'Take a photo of yourself, and Wobot draws an avatar that speaks with '
        'seven expressions.',
        style: text.titleMedium,
      ),
      const SizedBox(height: 20),
      const Steps([
        'Face the camera in good, even light, your face alone in the frame.',
        'Wobot draws 21 frames in the cloud, in about three minutes; you can '
            'leave the app meanwhile.',
        'Look at each frame, draw again any that look wrong, then use it.',
      ]),
      const SizedBox(height: 16),
      ..._cameraButtons('Take a photo'),
    ];
  }

  List<Widget> _cameraButtons(String label) => [
    FilledButton.icon(
      onPressed: _avatar.busy ? null : () => _take(ImageSource.camera),
      icon: const Icon(Icons.photo_camera_outlined),
      label: Text(label),
    ),
    // The simulator has no camera.
    if (kDebugMode)
      TextButton(
        onPressed: _avatar.busy ? null : () => _take(ImageSource.gallery),
        child: const Text('Choose a photo (debug)'),
      ),
  ];

  List<Widget> _photoReview(Uint8List photo) {
    final unsent = _avatar.unsent != null;
    return [
      ClipRRect(
        borderRadius: BorderRadius.circular(16),
        child: AspectRatio(
          aspectRatio: 1,
          child: Image.memory(photo, fit: BoxFit.cover),
        ),
      ),
      const SizedBox(height: 16),
      const InfoNote(
        'Your photo goes to OpenAI to draw the avatar, and is deleted once the '
        'first frame is drawn. The frames stay in Wobot until you delete them.',
      ),
      const SizedBox(height: 20),
      FilledButton(
        onPressed: _avatar.busy ? null : (unsent ? _avatar.sendAgain : _send),
        child: Text(
          _avatar.busy
              ? 'Sending…'
              : unsent
              ? 'Send again'
              : 'Use this photo',
        ),
      ),
      const SizedBox(height: 8),
      OutlinedButton(
        onPressed: _avatar.busy
            ? null
            : () {
                _avatar.unsent = null;
                setState(() => _photo = null);
              },
        child: const Text('Retake'),
      ),
    ];
  }

  Widget _heading(Avatar shown) {
    final text = Theme.of(context).textTheme;
    final (title, detail) = switch (shown.status) {
      'queued' || 'running' => (
        'Drawing your avatar',
        'You can leave this page or the app; Wobot keeps drawing.',
      ),
      'ready' => (
        'Look at each frame',
        'Tap one to see it larger, and draw it again if it looks wrong. '
            'Closed lips should be closed; each mouth keeps its expression.',
      ),
      'failed' => ('Some frames were not drawn', assetFailure(shown.errorCode)),
      _ => ('Your avatar', 'In use in Avatar mode.'),
    };
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(child: Text(title, style: text.titleMedium)),
              Text(
                '${shown.readyFrames}/${frameKeys.length}',
                style: const TextStyle(color: WobotColors.muted),
              ),
            ],
          ),
          if (isMaking(shown.status)) ...[
            const SizedBox(height: 10),
            LinearProgressIndicator(
              value: shown.readyFrames / frameKeys.length,
              backgroundColor: WobotColors.surface,
              borderRadius: BorderRadius.circular(4),
            ),
          ],
          const SizedBox(height: 8),
          Text(detail, style: const TextStyle(color: WobotColors.muted)),
        ],
      ),
    );
  }

  List<Widget> _actions(Avatar shown) {
    final busy = _avatar.busy;
    return switch (shown.status) {
      'queued' || 'running' => [
        OutlinedButton(
          onPressed: busy ? null : () => _delete(shown),
          child: const Text('Cancel and discard'),
        ),
      ],
      'ready' => [
        FilledButton(
          onPressed: busy || !_avatar.canActivate ? null : _avatar.activate,
          child: const Text('Use this avatar'),
        ),
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: busy ? null : () => _delete(shown),
          child: const Text('Discard'),
        ),
      ],
      'failed' => [
        FilledButton(
          onPressed: busy ? null : _avatar.retry,
          child: const Text('Try again'),
        ),
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: busy ? null : () => _delete(shown),
          child: const Text('Discard'),
        ),
      ],
      _ => [
        ..._cameraButtons('Replace with a new photo'),
        const SizedBox(height: 8),
        OutlinedButton(
          style: OutlinedButton.styleFrom(foregroundColor: WobotColors.danger),
          onPressed: busy ? null : () => _delete(shown),
          child: const Text('Delete avatar'),
        ),
      ],
    };
  }
}

String _title(String word) => word[0].toUpperCase() + word.substring(1);

/// The 21 frames, an expression to a row and a mouth to a column, on the backdrop
/// Avatar mode draws them on (P24).
class FrameGrid extends StatelessWidget {
  const FrameGrid({
    super.key,
    required this.avatar,
    required this.png,
    required this.onTap,
  });

  final Avatar avatar;
  final Uint8List? Function(AvatarFrame frame) png;
  final ValueChanged<String> onTap;

  @override
  Widget build(BuildContext context) {
    const label = TextStyle(color: WobotColors.faint, fontSize: 12);
    return Column(
      children: [
        Row(
          children: [
            const SizedBox(width: 72),
            for (final mouth in mouths)
              Expanded(
                child: Text(
                  _title(mouth),
                  textAlign: TextAlign.center,
                  style: label,
                ),
              ),
          ],
        ),
        const SizedBox(height: 6),
        for (final expression in expressions)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Row(
              children: [
                SizedBox(
                  width: 72,
                  child: Text(_title(expression), style: label),
                ),
                for (final mouth in mouths)
                  Expanded(
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 3),
                      child: _cell(frameKey(expression, mouth)),
                    ),
                  ),
              ],
            ),
          ),
      ],
    );
  }

  Widget _cell(String key) {
    final frame = avatar.frame(key);
    final image = frame == null ? null : png(frame);
    final Widget inside = switch (frame?.status) {
      // Decoded at the cell's size: 21 frames at 1024 pixels would take 84 MB.
      'ready' when image != null => Image.memory(
        image,
        fit: BoxFit.cover,
        cacheWidth: 320,
        gaplessPlayback: true,
      ),
      'ready' || 'running' => const Center(
        child: SizedBox.square(
          dimension: 16,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      ),
      'failed' => const Icon(
        Icons.error_outline,
        color: WobotColors.danger,
        size: 20,
      ),
      _ => const Icon(Icons.more_horiz, color: WobotColors.faint, size: 18),
    };
    return Semantics(
      label: '$key ${frame?.status ?? 'missing'}',
      button: image != null,
      child: GestureDetector(
        onTap: image == null ? null : () => onTap(key),
        child: AspectRatio(
          aspectRatio: 1,
          child: Container(
            clipBehavior: Clip.antiAlias,
            decoration: BoxDecoration(
              color: WobotColors.background,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: WobotColors.outline),
            ),
            child: inside,
          ),
        ),
      ),
    );
  }
}

/// One frame large, with its expression's other mouths and the other expressions a
/// tap away, to check each as AC-AVA-07 asks; and drawing it again.
class _FrameViewer extends StatefulWidget {
  const _FrameViewer({
    required this.controller,
    required this.avatarId,
    required this.initial,
    required this.confirm,
  });

  final AvatarSetupController controller;
  final String avatarId;
  final String initial;
  final Future<bool> Function(String title, String body, String action) confirm;

  @override
  State<_FrameViewer> createState() => _FrameViewerState();
}

class _FrameViewerState extends State<_FrameViewer> {
  late String _expression = widget.initial.split('_').first;
  late String _mouth = widget.initial.split('_').last;

  AvatarSetupController get _avatar => widget.controller;

  Avatar? get _shown => [
    _avatar.candidate,
    _avatar.active,
  ].where((a) => a?.id == widget.avatarId).firstOrNull;

  Future<void> _redo(String key) async {
    final also = descendantsOf(key).length;
    final sure = await widget.confirm(
      'Draw this frame again?',
      also == 0
          ? 'The new one replaces it in about half a minute.'
          : 'The $also frames drawn from it are drawn again too, in about a '
                'minute.',
      'Draw again',
    );
    if (sure) await _avatar.redo(key);
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _avatar,
      builder: (context, _) {
        final avatar = _shown;
        final key = frameKey(_expression, _mouth);
        final frame = avatar?.frame(key);
        final image = frame == null ? null : _avatar.png(frame);
        final redoable =
            avatar != null &&
            identical(avatar, _avatar.candidate) &&
            _avatar.editable;
        return Scaffold(
          appBar: AppBar(title: Text('${_title(_expression)} · $_mouth')),
          body: ListView(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 32),
            children: [
              AspectRatio(
                aspectRatio: 1,
                child: Container(
                  clipBehavior: Clip.antiAlias,
                  decoration: BoxDecoration(
                    color: WobotColors.background,
                    borderRadius: BorderRadius.circular(16),
                    border: Border.all(color: WobotColors.outline),
                  ),
                  child: switch (frame?.status) {
                    'ready' when image != null => Image.memory(
                      image,
                      gaplessPlayback: true,
                    ),
                    'failed' => Center(
                      child: Text(
                        assetFailure(frame?.errorCode),
                        textAlign: TextAlign.center,
                        style: const TextStyle(color: WobotColors.danger),
                      ),
                    ),
                    'pending' => const Center(
                      child: Text(
                        'Waiting for the frame it is drawn from.',
                        style: TextStyle(color: WobotColors.muted),
                      ),
                    ),
                    _ => const Center(child: ThinkingMark(size: 28)),
                  },
                ),
              ),
              const SizedBox(height: 16),
              SegmentedButton<String>(
                segments: [
                  for (final mouth in mouths)
                    ButtonSegment(value: mouth, label: Text(_title(mouth))),
                ],
                selected: {_mouth},
                showSelectedIcon: false,
                onSelectionChanged: (chosen) =>
                    setState(() => _mouth = chosen.single),
              ),
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final expression in expressions)
                    ChoiceChip(
                      label: Text(_title(expression)),
                      selected: expression == _expression,
                      onSelected: (_) =>
                          setState(() => _expression = expression),
                    ),
                ],
              ),
              const SizedBox(height: 24),
              if (redoable && key == baseFrame)
                const InfoNote(
                  'This frame is drawn from your photo, which is gone. To draw '
                  'it again, discard this avatar and take a new photo.',
                )
              else if (redoable)
                OutlinedButton.icon(
                  onPressed: _avatar.busy || frame?.status != 'ready'
                      ? null
                      : () => _redo(key),
                  icon: const Icon(Icons.refresh),
                  label: const Text('Draw this frame again'),
                ),
              if (_avatar.error case final error?) ErrorNote(error),
            ],
          ),
        );
      },
    );
  }
}
