import 'package:flutter/services.dart';

/// A face in a picture, from 0 to 1 with the origin top left, as a selfie shows it:
/// mirrored, so a user who steps to their own right moves right in it.
class FaceBox {
  const FaceBox(this.x, this.y, this.width, this.height);

  final double x;
  final double y;
  final double width;
  final double height;

  double get area => width * height;
  double get centreX => x + width / 2;

  /// Where the eyes are, about two fifths down a face: what to look at.
  double get eyesY => y + height * 0.4;
}

/// What one frame of the front camera showed.
class FaceFrame {
  const FaceFrame({
    required this.faces,
    required this.age,
    required this.detect,
    required this.hfov,
    required this.vfov,
  });

  factory FaceFrame.fromEvent(Map<Object?, Object?> event) => FaceFrame(
    faces: [
      for (final box in event['faces']! as List<Object?>)
        switch ((box! as List<Object?>).cast<num>()) {
          [final x, final y, final w, final h] => FaceBox(
            x.toDouble(),
            y.toDouble(),
            w.toDouble(),
            h.toDouble(),
          ),
          _ => throw FormatException('not a face box: $box'),
        },
    ],
    age: Duration(microseconds: ((event['age_ms']! as num) * 1000).round()),
    detect: Duration(
      microseconds: ((event['detect_ms']! as num) * 1000).round(),
    ),
    hfov: (event['hfov']! as num).toDouble(),
    vfov: (event['vfov']! as num).toDouble(),
  );

  final List<FaceBox> faces;

  /// How long ago the frame was taken, when it arrived.
  final Duration age;

  /// How long finding the faces took.
  final Duration detect;

  /// The picture's field of view, degrees, across and up.
  final double hfov;
  final double vfov;
}

/// Where faces come from; a test gives its own.
abstract class FaceSource {
  /// Frames while listened to; the camera stops when the listening does. Errors
  /// are `camera_denied` and `no_camera` [PlatformException]s.
  Stream<FaceFrame> frames();
}

/// The phone's front camera, with faces found on the phone (iOS: Apple's Vision).
class PhoneFaceSource implements FaceSource {
  static const _channel = EventChannel('wobot/faces');

  @override
  Stream<FaceFrame> frames() => _channel.receiveBroadcastStream().map(
    (event) => FaceFrame.fromEvent(event as Map<Object?, Object?>),
  );
}
