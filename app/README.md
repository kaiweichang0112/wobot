# Wobot app

The Flutter client, iOS first; Android follows before the pilot. Signed in with
Google, an allowlisted account chats with the API: replies stream in, a reply can be
stopped, copied or tried again, and past chats are kept, titled and deleted. In
Settings it records the voice and takes the photo the cloud makes its voice and avatar
from; in Avatar mode the avatar speaks the replies in that voice.

| File | What it holds |
| --- | --- |
| `lib/api.dart` | The API client: the Firebase ID token on every request, refreshed once when refused, and the `{"error"}` envelope as `ApiException` |
| `lib/sse.dart` | Server-sent events read from a streamed response |
| `lib/chat.dart` | `ChatController` and `HistoryController` (`ChangeNotifier`): sending, stopping, retrying, and the chats list |
| `lib/assets.dart` | What voices and avatars share: their status in a word, failures, and reading one being made every 2.5 s |
| `lib/voice/` | `VoiceSetupController`, and the microphone and speaker it records and plays with; `QuestionListener`, which turns a spoken question into text on the phone; `SpeechQueue`, which plays a reply's sentences in order; the question language; Voice lab's script and CER |
| `lib/avatar/` | `AvatarSetupController`, the 21 frame keys, and the frames kept on the device; `AvatarSession`, Avatar mode's state; `AnimatedAvatar`, the face that breathes and speaks |
| `lib/robot/` | The robot's wire format; `RobotLink`, which finds the robot over Bluetooth, attaches it, keeps its lease and writes commands in order; the motions and their budget; `Director`, which plays them as Avatar mode goes; `Robot`, what Avatar mode holds of it |
| `lib/pages/` | Sign-in, chat, the chats drawer, settings, the Voice and Avatar pages, Avatar mode, and the debug Voice and Robot labs |
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
- **A spoken question becomes text on the phone** (DEC-092): Apple's recognizer,
  on the device, so the audio never leaves it. A question ends when no new words come
  for 1.8 s, on a tap, after 60 s, or after 10 s with no words; all four go through one
  finalize, so it is sent once, and nothing is sent when nothing was heard. Voice lab
  (Settings, debug builds) shows when and why a question ended, and measures CER against
  a script read aloud.
- **Avatar mode needs the account's own avatar and voice** (MODE-004): the top-right
  button reads `/v1/me` each time, and says what to set up when either is missing.
  It speaks on the chat's own conversation, so back in Text the reply goes on there.
- **One button, one generation per question** (spec 22). A tap listens; while
  listening it sends; while the avatar speaks it stops the voice here at once, then
  listens, and tells the server with `playback` (`stopped`), so no more is spoken; the
  text is still written (P19). Each question, interruption or reading on starts a new
  generation, and speech of an earlier one is dropped. A question asked while the reply
  before is still written waits for it, and stops it, keeping its words, after 15 s.
- **Each sentence is played as it comes** (`speech` events, MP3 in base64), one after
  another; its `emotion` becomes the avatar's face when it starts, unless it is too
  short to see, and the last face stays 1.6 s. The mouth moves, at random, only while a
  sentence is heard. A list reply says how many items are left: Continue reading speaks
  the next ones from where `completed` said (DEC-096).
- **The controls hide.** A first tap only shows them; they hide after 4 s, except while
  a question is heard, an error shows, the transcript is open or a screen reader is on,
  and hidden ones are left out of what it reads. Out of sight, nothing is heard or
  played, and the reply stops; back, nothing starts by itself.
- **The robot is found by itself, and never needed** (spec 26). Opening Avatar mode
  looks for a `Wobot-Robot-*`: the one this phone remembers, or else the strongest
  heard in 2 s, which it then remembers (Settings can forget it). iOS asks for
  Bluetooth the first time; without Bluetooth, permission or a robot, Avatar mode is
  unchanged and the robot button says why, and looks again when tapped. Connected, it
  writes `attach` and an `O` that sets a 2.5 s lease, then sends the last `O` again
  whenever 0.8 s pass in silence, so a phone that crashes or leaves leaves the head at
  neutral. Every command is one write of at most 20 bytes, whole degrees, so it fits
  the smallest MTU; poses go without a response, since the next one sets the head
  again, and a look still waiting is replaced by a newer one. Leaving Avatar mode or
  going out of sight sends `X` and lets go; back in sight, it connects again. Robot
  lab (Settings, debug builds) connects the same way, sends commands by hand, plays
  each motion and scripted replies, and runs a minute of chat for the slip test.
- **The robot moves with each sentence as it starts** (DEC-103), when the avatar
  shows its face. A feeling (only chat has them, DEC-097) starts with one of four or
  five motions, never the one before, at random sizes and sides; said again it
  calms, the third time to a nod. A sentence without one gets a beat, or none, and
  the path adds its way: a list steps from item to item, knowledge shows the centre
  or the company it introduces; a question tilts the head as it ends. Listening
  leans in, waiting ponders, the end of a reply nods. Accents are chains of `M`
  moves, never cut into; carriers are slow `O`s. Every motion is checked against
  80% of the rig's measured limits (`Budget`), and a move between motions that
  would exceed them is slowed.
- **Questions are spoken in the language chosen in Settings** (中文 or English, kept on
  the phone), never switched on the Avatar screen (VOI-012).

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
