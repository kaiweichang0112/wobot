import 'dart:math';

import 'package:flutter/material.dart';

import '../theme.dart';

/// Wobot's mark: eight orange rays. While `animate`, the rays breathe in turn and the
/// mark turns slowly, so a reader waiting on a reply sees the app is working.
class ThinkingMark extends StatefulWidget {
  const ThinkingMark({super.key, this.size = 22, this.animate = true});

  final double size;
  final bool animate;

  @override
  State<ThinkingMark> createState() => _ThinkingMarkState();
}

class _ThinkingMarkState extends State<ThinkingMark>
    with SingleTickerProviderStateMixin {
  late final AnimationController _clock = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1800),
  );

  @override
  void initState() {
    super.initState();
    if (widget.animate) _clock.repeat();
  }

  @override
  void didUpdateWidget(ThinkingMark old) {
    super.didUpdateWidget(old);
    if (widget.animate && !_clock.isAnimating) _clock.repeat();
    if (!widget.animate && _clock.isAnimating) _clock.stop();
  }

  @override
  void dispose() {
    _clock.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return RepaintBoundary(
      child: AnimatedBuilder(
        animation: _clock,
        builder: (context, _) => CustomPaint(
          size: Size.square(widget.size),
          painter: _RaysPainter(widget.animate ? _clock.value : null),
        ),
      ),
    );
  }
}

class _RaysPainter extends CustomPainter {
  _RaysPainter(this.t);

  /// Where the animation is, 0 to 1; null for the mark at rest.
  final double? t;

  static const _rays = 8;

  @override
  void paint(Canvas canvas, Size size) {
    final center = size.center(Offset.zero);
    final radius = size.shortestSide / 2;
    final paint = Paint()
      ..color = WobotColors.accent
      ..strokeCap = StrokeCap.round
      ..strokeWidth = radius * 0.24;
    final phase = t ?? 0;
    final turn = t == null ? 0.0 : phase * pi / 2;
    for (var i = 0; i < _rays; i++) {
      final angle = turn + i * 2 * pi / _rays;
      // Each ray breathes a little after the one before it.
      final breath = t == null
          ? 1.0
          : 0.55 + 0.45 * (0.5 + 0.5 * sin(2 * pi * phase - i * pi / 4));
      final direction = Offset(cos(angle), sin(angle));
      canvas.drawLine(
        center + direction * radius * 0.22,
        center + direction * radius * (0.22 + 0.68 * breath),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(_RaysPainter old) => old.t != t;
}

/// A label with light sweeping across it, as the mark's companion while waiting.
class ShimmerText extends StatefulWidget {
  const ShimmerText(this.text, {super.key, this.style});

  final String text;
  final TextStyle? style;

  @override
  State<ShimmerText> createState() => _ShimmerTextState();
}

class _ShimmerTextState extends State<ShimmerText>
    with SingleTickerProviderStateMixin {
  late final AnimationController _clock = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1600),
  )..repeat();

  @override
  void dispose() {
    _clock.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _clock,
      builder: (context, child) {
        final x = _clock.value * 3 - 1;
        return ShaderMask(
          blendMode: BlendMode.srcIn,
          shaderCallback: (bounds) => LinearGradient(
            begin: Alignment(x - 1, 0),
            end: Alignment(x + 1, 0),
            colors: const [
              WobotColors.faint,
              WobotColors.text,
              WobotColors.faint,
            ],
          ).createShader(bounds),
          child: child,
        );
      },
      child: Text(widget.text, style: widget.style),
    );
  }
}
