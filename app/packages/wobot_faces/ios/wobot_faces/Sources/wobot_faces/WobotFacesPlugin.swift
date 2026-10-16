import AVFoundation
import Flutter
import QuartzCore
import UIKit
import Vision

/// Faces seen by the front camera, found on the phone with Apple's Vision, sent to
/// Dart a dozen times a second on the `wobot/faces` event channel while it is
/// listened to. Frames never leave this class: only where the faces are does.
///
/// Each event: `faces`, a list of `[x, y, width, height]` from 0 to 1 with the
/// origin top left, as a selfie shows them (mirrored, upright in portrait); `age_ms`,
/// how long ago the frame was taken; `detect_ms`, how long finding the faces took;
/// `hfov` and `vfov`, the picture's field of view in degrees.
public class WobotFacesPlugin: NSObject, FlutterPlugin, FlutterStreamHandler,
  AVCaptureVideoDataOutputSampleBufferDelegate
{
  public static func register(with registrar: FlutterPluginRegistrar) {
    let instance = WobotFacesPlugin()
    let channel = FlutterEventChannel(
      name: "wobot/faces", binaryMessenger: registrar.messenger())
    channel.setStreamHandler(instance)
  }

  /// At most this many frames a second are looked at; the rest are dropped.
  private static let perSecond = 12.0

  private let session = AVCaptureSession()
  private let queue = DispatchQueue(label: "wobot.faces")
  private var configured = false
  private var sink: FlutterEventSink?
  private var lastLook: CFTimeInterval = 0
  private var hfov = 0.0
  private var vfov = 0.0

  public func onListen(
    withArguments arguments: Any?, eventSink events: @escaping FlutterEventSink
  ) -> FlutterError? {
    sink = events
    switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized:
      start()
    case .notDetermined:
      AVCaptureDevice.requestAccess(for: .video) { granted in
        DispatchQueue.main.async {
          if granted {
            self.start()
          } else {
            self.fail("camera_denied", "Wobot may not use the camera.")
          }
        }
      }
    default:
      return FlutterError(
        code: "camera_denied", message: "Wobot may not use the camera.", details: nil)
    }
    return nil
  }

  public func onCancel(withArguments arguments: Any?) -> FlutterError? {
    sink = nil
    queue.async {
      if self.session.isRunning { self.session.stopRunning() }
    }
    return nil
  }

  private func start() {
    queue.async {
      if !self.configured {
        guard self.configure() else {
          DispatchQueue.main.async {
            self.fail("no_camera", "This phone has no front camera to use.")
          }
          return
        }
        self.configured = true
      }
      if !self.session.isRunning { self.session.startRunning() }
    }
  }

  /// The front camera at 640×480, its late frames dropped. No microphone, and the
  /// audio session is left alone: speech is heard and played meanwhile.
  private func configure() -> Bool {
    guard
      let camera = AVCaptureDevice.default(
        .builtInWideAngleCamera, for: .video, position: .front),
      let input = try? AVCaptureDeviceInput(device: camera)
    else { return false }
    session.beginConfiguration()
    defer { session.commitConfiguration() }
    session.automaticallyConfiguresApplicationAudioSession = false
    if session.canSetSessionPreset(.vga640x480) { session.sessionPreset = .vga640x480 }
    guard session.canAddInput(input) else { return false }
    session.addInput(input)
    let output = AVCaptureVideoDataOutput()
    output.alwaysDiscardsLateVideoFrames = true
    output.videoSettings = [
      kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
    ]
    output.setSampleBufferDelegate(self, queue: queue)
    guard session.canAddOutput(output) else { return false }
    session.addOutput(output)

    // The format's field of view is across its long side, the sensor's landscape
    // width; held in portrait, that is the picture's height.
    let size = CMVideoFormatDescriptionGetDimensions(camera.activeFormat.formatDescription)
    let long = Double(camera.activeFormat.videoFieldOfView) * .pi / 180
    let ratio = Double(min(size.width, size.height)) / Double(max(size.width, size.height))
    let short = 2 * atan(tan(long / 2) * ratio)
    hfov = short * 180 / .pi
    vfov = long * 180 / .pi
    return true
  }

  public func captureOutput(
    _ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
    from connection: AVCaptureConnection
  ) {
    let now = CACurrentMediaTime()
    guard now - lastLook >= 1 / Self.perSecond,
      let pixels = CMSampleBufferGetImageBuffer(sampleBuffer)
    else { return }
    lastLook = now
    let taken = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))

    // The front camera held in portrait, mirrored as a selfie is.
    let request = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(
      cvPixelBuffer: pixels, orientation: .leftMirrored, options: [:])
    do {
      try handler.perform([request])
    } catch {
      return
    }
    let faces: [[Double]] = (request.results ?? []).map { face in
      let box = face.boundingBox  // origin bottom left
      return [
        Double(box.origin.x), Double(1 - box.origin.y - box.height),
        Double(box.width), Double(box.height),
      ]
    }
    let done = CACurrentMediaTime()
    let event: [String: Any] = [
      "faces": faces,
      "age_ms": max(0, (done - taken) * 1000),
      "detect_ms": (done - now) * 1000,
      "hfov": hfov,
      "vfov": vfov,
    ]
    DispatchQueue.main.async { self.sink?(event) }
  }

  private func fail(_ code: String, _ message: String) {
    sink?(FlutterError(code: code, message: message, details: nil))
  }
}
