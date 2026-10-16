// swift-tools-version: 5.9

import PackageDescription

let package = Package(
  name: "wobot_faces",
  platforms: [
    .iOS("15.0")
  ],
  products: [
    // A plugin name with `_` is a library name with `-`.
    .library(name: "wobot-faces", targets: ["wobot_faces"])
  ],
  dependencies: [
    // Flutter injects this local package when Swift Package Manager is used.
    .package(name: "FlutterFramework", path: "../FlutterFramework")
  ],
  targets: [
    .target(
      name: "wobot_faces",
      dependencies: [
        .product(name: "FlutterFramework", package: "FlutterFramework")
      ]
    )
  ]
)
