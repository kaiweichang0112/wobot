import 'dart:math';

import 'package:flutter/material.dart';

import '../theme.dart';

/// Wobot's mark: an orange W. While `animate`, its corners rise and fall in turn like
/// a wave, so a reader waiting on a reply sees the app is working.
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
    duration: const Duration(milliseconds: 1600),
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
          painter: _WavePainter(widget.animate ? _clock.value : null),
        ),
      ),
    );
  }
}

/// The W's five corners in a square of side `side`: with the wave at `t`, 0 to 1, or
/// level for the mark at rest.
List<Offset> wCorners(double side, {double? t}) {
  const corners = [
    (0.1, 0.25),
    (0.3, 0.78),
    (0.5, 0.4),
    (0.7, 0.78),
    (0.9, 0.25),
  ];
  return [
    for (final (i, (x, y)) in corners.indexed)
      // Each corner rises a little after the one before it.
      Offset(
        x * side,
        (y + (t == null ? 0 : 0.07 * sin(2 * pi * t - i * 0.9))) * side,
      ),
  ];
}

/// The W through `corners`.
Path wPath(List<Offset> corners) {
  final path = Path()..moveTo(corners.first.dx, corners.first.dy);
  for (final corner in corners.skip(1)) {
    path.lineTo(corner.dx, corner.dy);
  }
  return path;
}

/// The paint the W is stroked with in a square of side `side`.
Paint wPaint(double side) => Paint()
  ..color = WobotColors.accent
  ..style = PaintingStyle.stroke
  ..strokeCap = StrokeCap.round
  ..strokeJoin = StrokeJoin.round
  ..strokeWidth = side * 0.11;

class _WavePainter extends CustomPainter {
  _WavePainter(this.t);

  /// Where the animation is, 0 to 1; null for the mark at rest.
  final double? t;

  @override
  void paint(Canvas canvas, Size size) {
    final side = size.shortestSide;
    canvas.drawPath(wPath(wCorners(side, t: t)), wPaint(side));
  }

  @override
  bool shouldRepaint(_WavePainter old) => old.t != t;
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
