# Wobot app

The Flutter client, iOS first; Android follows before the pilot. Signed in with
Google, an allowlisted account chats with the API: replies stream in, a reply can be
stopped, copied or tried again, and past chats are kept, titled and deleted. In
Settings it records the voice and takes the photo the cloud makes its voice and avatar
from.

| File | What it holds |
| --- | --- |
| `lib/api.dart` | The API client: the Firebase ID token on every request, refreshed once when refused, and the `{"error"}` envelope as `ApiException` |
| `lib/sse.dart` | Server-sent events read from a streamed response |
| `lib/chat.dart` | `ChatController` and `HistoryController` (`ChangeNotifier`): sending, stopping, retrying, and the chats list |
| `lib/assets.dart` | What voices and avatars share: their status in a word, failures, and reading one being made every 2.5 s |
| `lib/voice/` | `VoiceSetupController`, and the microphone and speaker it records and plays with |
| `lib/avatar/` | `AvatarSetupController`, the 21 frame keys, and the frames kept on the device |
| `lib/pages/` | Sign-in, chat, the chats drawer, settings, and the Voice and Avatar pages |
| `lib/widgets/` | The thinking mark shown while a reply is awaited, replies with tappable links, and the setup pages' notes |
| `lib/theme.dart` | The dark theme and its orange accent |

How the app keeps to the API's rules:

- **A stop is a request.** Stop closes the stream and sends `cancel` with the words
  shown: on Cloud Run the closed stream alone never reaches the instance.
- **A turn ends at `completed` or `error`**, not when the connection closes: the server
  sends its traces before closing.
- **Out of sight, a reply stops** (CHAT-011); back in the foreground, the app warms the
  service with `GET /health` and reads each turn as it stands. A system dialog only
  makes the app inactive, which is not leaving it.
- **The app names each turn** (a random UUID), so a message sent again is never answered
  twice. A stream lost mid-reply is followed by reading the turn until the server ends it.
- **Replies are plain text.** They are written to be read aloud. A web address shows
  as the label before it (`PDF`, `產品網頁`, `Product page`), or as its site, and opens
  in the browser; Copy keeps the whole address.
- **A voice or an avatar is made in the cloud** (DEC-086). The app sends the sample or
  the photo under an ID it names, so one sent again makes nothing twice, then reads its
  progress while the page is open and the app in sight; leaving stops the reading, not
  the making. The one in use stays until the new one is activated.
- **A sample is 60 to 180 seconds of AAC**, recorded in the app and deleted from it once
  the server has it, or when the page or the app is left. A photo comes from the front
  camera as a JPEG, so a HEIC photo never reaches the API.
- **Frames are kept by their SHA-256**, under the account and the avatar in the app's
  support directory: a frame drawn again has a new hash, so no kept frame is ever
  stale. They go when the avatar is replaced or deleted, and at sign-out.
- **Avatar mode** has its place (the top-right button) and says it comes in a later
  version.

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

`flutter analyze` needs the generated `lib/firebase_options.dart`. CI (`app-ci.yml`)
writes a stand-in with placeholder values after checking formatting, then analyzes and
tests; nothing in it reaches Firebase.

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
