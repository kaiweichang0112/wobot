# Wobot app

The Flutter client, iOS first; Android follows before the chat phase. It currently
proves the path end to end: Google Sign-In → Firebase ID token → `GET /v1/me`.

## Setup

Requires Flutter, Xcode, the Firebase CLI, the FlutterFire CLI
(`dart pub global activate flutterfire_cli`) and Ruby's `xcodeproj` gem
(`gem install xcodeproj`), which FlutterFire uses to add the plist to the Xcode
project; without it, it stops before writing `lib/firebase_options.dart`.

Enable Google sign-in in Firebase before generating the config
([`infra/README.md`](../infra/README.md), section 10); otherwise the config has
no iOS client ID.

Firebase config stays out of git. After cloning, in `app/`:

```bash
flutterfire configure --project=<PROJECT_ID> --platforms=ios --ios-bundle-id=com.kaiweichang.wobot
printf 'GOOGLE_REVERSED_CLIENT_ID = %s\n' \
  "$(/usr/libexec/PlistBuddy -c 'Print :REVERSED_CLIENT_ID' ios/Runner/GoogleService-Info.plist)" \
  > ios/Flutter/Firebase.xcconfig
```

- `flutterfire configure` writes `lib/firebase_options.dart` and
  `ios/Runner/GoogleService-Info.plist`.
- Google Sign-In returns to the app through a URL scheme: the reversed iOS client
  ID. `Info.plist` reads it from the gitignored `ios/Flutter/Firebase.xcconfig`,
  so no project identifier is committed.

## Run

```bash
flutter devices
flutter run -d <device> --dart-define=API_BASE_URL=<API URL>
```

The API URL is compiled in; without it the app says so instead of signing in.

`flutter devices` lists only booted simulators. Xcode 27 replaced Simulator.app with
DeviceHub, which `flutter emulators --launch` does not find in Flutter 3.44, so boot
one directly:

```bash
xcrun simctl list devices available
xcrun simctl boot <UDID>
open -a DeviceHub
```

## Checks

```bash
flutter analyze
flutter test
```

`flutter analyze` needs the generated `lib/firebase_options.dart`.

## Running on an iPhone

```bash
flutter run --release -d <device> --dart-define=API_BASE_URL=<API URL>
```

- The project sets no development team. `flutter run` takes the Apple Development
  certificate from the keychain and passes its team to `xcodebuild`, which creates
  the provisioning profile and registers the device.
- To get a certificate: Xcode → Settings → Accounts, add an Apple ID, then Manage
  Certificates → + → Apple Development.
- Building from Xcode instead needs a team under Signing & Capabilities. Don't
  commit the `DEVELOPMENT_TEAM` lines this writes to `project.pbxproj`.
- On the iPhone: trust the Mac, turn on Developer Mode (Settings → Privacy &
  Security), and after the first install trust the developer certificate
  (Settings → General → VPN & Device Management). Keep it unlocked during the
  first build so Xcode can mount the developer disk image.
- A debug build starts on a device only from `flutter run` or Xcode; a release
  build also starts from the home screen.
- Apps signed by a free Personal Team expire after 7 days; `flutter run` again.
