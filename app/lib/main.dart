import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:google_sign_in/google_sign_in.dart';

import 'firebase_options.dart';
import 'home_page.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final options = DefaultFirebaseOptions.currentPlatform;
  await Firebase.initializeApp(options: options);
  // The iOS OAuth client comes from the generated Firebase options, so no client
  // ID is written into Info.plist.
  await GoogleSignIn.instance.initialize(clientId: options.iosClientId);
  runApp(const WobotApp());
}

class WobotApp extends StatelessWidget {
  const WobotApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Wobot',
      theme: ThemeData(colorSchemeSeed: Colors.teal),
      home: const HomePage(),
    );
  }
}
