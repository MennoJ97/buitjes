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
this `android/` directory. Android Studio will offer to create the Gradle
wrapper; let it.

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
sdkmanager "platform-tools" "platforms;android-35" "build-tools;35.0.0"
```

```bash
yes | sdkmanager --licenses
```

You also need Gradle itself once, to generate the wrapper:

```bash
brew install gradle && cd android && gradle wrapper
```

After that `./gradlew` works and the global Gradle is no longer needed.

### Checking it took

```bash
cd android && ANDROID_HOME="$ANDROID_HOME" gradle projects
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

`:app` was written without an SDK present, so it has never been compiled.
Expect to fix build errors on the first run — most likely in the Glance and
WorkManager API surface, which move between versions.
