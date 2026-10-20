import 'package:flutter/material.dart';

import 'thinking.dart';

/// Wobot's W writing itself in one stroke, then fading to write again, shown while the
/// app starts.
class WMark extends StatefulWidget {
  const WMark({super.key, this.size = 48});

  final double size;

  @override
  State<WMark> createState() => _WMarkState();
}

class _WMarkState extends State<WMark> with SingleTickerProviderStateMixin {
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
    return RepaintBoundary(
      child: AnimatedBuilder(
        animation: _clock,
        builder: (context, _) => CustomPaint(
          size: Size.square(widget.size),
          painter: _StrokePainter(_clock.value),
        ),
      ),
    );
  }
}

/// How much of the W is written at `t`, 0 to 1, and how opaque it is: written in the
/// first 30%, so a short start still shows it whole, held, then faded in the last fifth.
({double written, double opacity}) wStroke(double t) => (
  written: Curves.easeOutCubic.transform((t / 0.3).clamp(0, 1)),
  opacity: t < 0.8 ? 1 : (1 - t) / 0.2,
);

class _StrokePainter extends CustomPainter {
  _StrokePainter(this.t);

  final double t;

  @override
  void paint(Canvas canvas, Size size) {
    final side = size.shortestSide;
    final stroke = wStroke(t);
    if (stroke.written == 0) return;
    final metric = wPath(wCorners(side)).computeMetrics().single;
    final paint = wPaint(side);
    paint.color = paint.color.withValues(alpha: stroke.opacity);
    canvas.drawPath(
      metric.extractPath(0, metric.length * stroke.written),
      paint,
    );
  }

  @override
  bool shouldRepaint(_StrokePainter old) => old.t != t;
}
