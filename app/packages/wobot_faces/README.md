# wobot_faces

Faces seen by the phone's front camera, for the robot to turn toward. A plugin of
the app's workspace (`app/pubspec.yaml`), not published.

- **iOS**: Apple's Vision finds the faces (`VNDetectFaceRectanglesRequest`), at most
  12 frames a second from the front camera at 640×480. Frames stay in memory in
  `WobotFacesPlugin.swift`; only the boxes reach Dart. The camera adds no audio input
  and leaves the audio session alone, so speech recognition and playback go on.
- **Android**: not yet. The same `wobot/faces` event channel, written in Kotlin with
  CameraX and MediaPipe's Face Detector, is all it needs: everything above the boxes
  is Dart. MediaPipe was not used on iOS: its Swift package links the whole app with
  `-all_load`, which pulls every task's graphs into the app.

Each event is a map: `faces`, a list of `[x, y, width, height]` from 0 to 1 with the
origin top left, mirrored as a selfie, upright in portrait; `age_ms`, how old the
frame is; `detect_ms`, how long the detection took; `hfov` and `vfov`, the picture's
field of view in degrees. Errors are `camera_denied` and `no_camera`.
