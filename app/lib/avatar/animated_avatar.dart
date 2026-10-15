import 'dart:async';
import 'dart:math';

import 'package:flutter/material.dart';

import 'frames.dart';

/// The avatar, alive: it breathes all the time, and while a sentence is heard its
/// mouth moves between the three mouths of the expression shown (spec 21). Every
/// frame is a whole face, so showing one is a lookup. Give it a square box.
///
/// Ported from the reference app's AnimatedAvatar. The mouth is random, not read
/// from the audio: the baseline the spec sets.
class AnimatedAvatar extends StatefulWidget {
  const AnimatedAvatar({
    super.key,
    required this.frames,
    required this.speaking,
    this.expression = 'neutral',
  });

  /// The 21 frames, by key, such as `joy_half`.
  final Map<String, ImageProvider> frames;
  final bool speaking;
  final String expression;

  static const fade = Duration(milliseconds: 250);

  @override
  State<AnimatedAvatar> createState() => _AnimatedAvatarState();
}

class _AnimatedAvatarState extends State<AnimatedAvatar>
    with SingleTickerProviderStateMixin {
  final _random = Random();
  late final AnimationController _breath = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 2600),
  )..repeat(reverse: true);
  Timer? _mouthTimer;
  String _mouth = 'closed';

  @override
  void initState() {
    super.initState();
    if (widget.speaking) _tickMouth();
  }

  @override
  void didUpdateWidget(AnimatedAvatar old) {
    super.didUpdateWidget(old);
    if (widget.speaking && !old.speaking) _tickMouth();
    if (!widget.speaking && old.speaking) {
      _mouthTimer?.cancel();
      _mouth = 'closed';
    }
  }

  String get _expression =>
      widget.frames.containsKey(frameKey(widget.expression, 'closed'))
      ? widget.expression
      : 'neutral';

  String get _key => frameKey(_expression, widget.speaking ? _mouth : 'closed');

  void _tickMouth() {
    _mouthTimer?.cancel();
    if (!mounted || !widget.speaking) return;
    final r = _random.nextDouble();
    setState(() => _mouth = r < 0.2 ? 'closed' : (r < 0.6 ? 'half' : 'wide'));
    // 95 to 189 ms: about as often as syllables change.
    _mouthTimer = Timer(
      Duration(milliseconds: 95 + _random.nextInt(95)),
      _tickMouth,
    );
  }

  @override
  void dispose() {
    _mouthTimer?.cancel();
    _breath.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final frame = widget.frames[_key];
    final face = AnimatedSwitcher(
      duration: AnimatedAvatar.fade,
      layoutBuilder: (current, previous) =>
          Stack(fit: StackFit.expand, children: [...previous, ?current]),
      transitionBuilder: (child, animation) =>
          FadeTransition(opacity: animation, child: child),
      // Keyed on the expression alone: a new expression fades in, while the mouth
      // changes at once inside the same child.
      child: SizedBox.expand(
        key: ValueKey(_expression),
        child: frame == null
            ? const SizedBox.shrink()
            : Image(image: frame, fit: BoxFit.contain, gaplessPlayback: true),
      ),
    );
    return AnimatedBuilder(
      animation: _breath,
      builder: (context, child) {
        final t = sin(_breath.value * pi);
        return Transform.translate(
          offset: Offset(0, -2.5 * t),
          child: Transform.scale(scale: 1 + 0.012 * t, child: child),
        );
      },
      child: ExcludeSemantics(child: face),
    );
  }
}
