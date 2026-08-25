# Buitjes for Android

Notifications for rain at wherever the phone actually is, and a home-screen
widget with the six-hour rainfall graph. Everything else — the map, the
scrubbing timeline, the ensemble charts — the web app already does well, and
this links out to it rather than rebuilding it.

## Layout

```
android/
├── core/   plain Kotlin: the forecast model and the alert state machine
└── app/    the Android app: widget, background worker, screens
```

The split is not ceremony. `:core` has no Android dependency, so it compiles
and its tests run on any machine with a JDK — which is what makes the two parts
worth getting right (the document model, and the rules deciding when to
interrupt someone) verifiable at all. `settings.gradle.kts` only includes
`:app` when it finds an SDK, so this works on a machine that has none:

```bash
cd android && gradle :core:test
```

## What talks to what

The app is a thin client of the existing server. It adds no forecasting of its
own.

| It needs | It calls |
|---|---|
| the list of configured locations | `GET /api/config` → `points` |
| a forecast for one of them | `GET /api/point/<name>` — real KNMI spread |
| a forecast for wherever the phone is | `GET /api/point?lat=&lon=` |

The third is the one built for this app. A phone is not a configured location,
so that endpoint samples the published median frames on demand and returns the
same document shape. Two things it will not fudge, and the app surfaces both:
`median_only` says the ensemble spread is gone (the members were averaged away
before the frames were written, so a band drawn there would be invented), and
`out_of_coverage` says the point is outside the radar domain — which is not the
same as a forecast of dry weather, and pauses alerting rather than relaxing it.

Alerts are evaluated **on the phone**, not pushed from the server. That avoids
push infrastructure, per-device state on the server, and a location trail
leaving the handset. `core`'s `AlertEngine` is a port of the ingestor's
`alerts.py` — latch, hysteresis, quiet period — so "tell me when it is about to
rain" means the same thing whichever surface raised it. It runs every ~15
minutes against a freshly fetched forecast; the server's five-minute cadence
matters for webhook alerting on the edge, but the phone's question is "will it
rain within the hour", which survives coarser sampling.

Location is **coarse only**, deliberately. The server rounds coordinates to
about a kilometre, which is the resolution of the model being sampled, so fine
location would buy nothing and cost a harsher permission prompt.

## Setting up an Android SDK

You already have a JDK 17, which is what the Android Gradle Plugin wants.
Nothing else here needs a JDK of its own.

### The short way — Android Studio

Best if you want to *see* the app: it brings the SDK, an emulator, the widget
preview, and the Gradle integration in one install.

```bash
brew install --cask android-studio
```

Open it once and let the setup wizard run — it installs the SDK to
`~/Library/Android/sdk` and accepts the licences for you. Then **File → Open**
this `android/` directory and let the first Gradle sync finish; it downloads a
Gradle distribution and every dependency, so a couple of minutes of apparently
nothing happening is normal.

The wrapper is already committed at the version AGP needs. If Studio offers to
change it, decline — see the toolchain note at the end of this file.

### The small way — command-line tools only

Enough to build and test, without an emulator or IDE.

```bash
brew install --cask android-commandlinetools
```

Then add this to `~/.zshrc` and open a new shell (the cask installs under
Homebrew's prefix; `brew --prefix` resolves it on both Apple Silicon and Intel):

```bash
export ANDROID_HOME="$(brew --prefix)/share/android-commandlinetools"
export PATH="$ANDROID_HOME/platform-tools:$PATH"
```

Install the packages this project builds against and accept the licences:

```bash
sdkmanager "platform-tools" "platforms;android-37" "build-tools;36.0.0"
```

```bash
yes | sdkmanager --licenses
```

The Gradle wrapper is committed, so `./gradlew` works without installing
Gradle. It needs a JDK 17 or newer; Android Studio's bundled one does fine:

```bash
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
```

### Checking it took

```bash
cd android && ./gradlew projects
```

`:app` appearing in the list means the SDK was found. If only `:core` shows up,
`settings.gradle.kts` could not find an SDK — set `ANDROID_HOME`, or write
`sdk.dir=/path/to/sdk` into `android/local.properties`.

### Building and installing

```bash
cd android && ./gradlew :app:assembleDebug
```

With a phone attached over USB (developer mode and USB debugging on):

```bash
cd android && ./gradlew :app:installDebug
```

## State of the code

`:core` is written and its tests pass — including parsing tests that run
against **real captured responses** from the server rather than hand-written
approximations, since the two halves share nothing but that document and
nothing else would catch them drifting apart.

`:app` compiles and packages: `./gradlew :app:assembleDebug` produces a debug
APK, with no warnings. What it has *not* had is a single second of runtime.
Nothing below has been seen working on a device:

- whether the widget lays out sensibly at real sizes, and whether the chart
  bitmap survives the RemoteViews Binder budget on a large tablet widget
  (`ChartRenderer` caps itself at 220k pixels, which is a guess at a limit that
  is device-dependent);
- whether the refresh worker actually runs on a phone in Doze at anything like
  fifteen minutes;
- the API 26–29 branch of `LocationSource`, which needs an old device or an
  emulator image to exercise at all;
- whether an alert ever fires, which needs weather.

### Toolchain

Pinned to what Android Studio 2026.1.3 brings, because that is what builds this
in practice: **AGP 9.3, Gradle 9.5, Kotlin 2.4.10, compileSdk 37**, minSdk 26.
Two consequences worth knowing before changing any of them:

- **AGP 9 compiles Kotlin itself.** `:app` must *not* apply
  `org.jetbrains.kotlin.android` — doing so is a hard error, not a warning.
  `:core` still applies `kotlin.jvm`, which is a different plugin for a module
  AGP knows nothing about.
- **AGP 9.3 requires Gradle 9.5 or newer.** Studio generated a 9.3 wrapper on
  first open, which fails with a message naming the exact fix.

Kotlin below 2.2 cannot run on Studio's bundled JDK 25 at all: its version
parser throws `IllegalArgumentException: 25.0.2` before compiling anything.

### A hazard of this checkout

The repository lives inside a Nextcloud-synced folder, and Gradle's `build/`
directories are not excluded from that sync. One symptom has already been seen:

```
Cannot access output property ... NoSuchFileException:
  core/build/kotlin/compileKotlin/cacheable/caches-jvm
```

`rm -rf android/*/build android/build` and rebuild. Adding `build/` to
Nextcloud's ignore list is the real fix, and is worth doing — the sync client
otherwise uploads thousands of transient class files.
