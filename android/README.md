# Buitjes for Android

Notifications for rain at wherever the phone actually is, and a home-screen
widget with the six-hour rainfall graph.

This began as those two things, with everything else — the map, the scrubbing
timeline, the ensemble charts — left to the web app on the grounds that it
already does them well. That held until each one was wanted on a phone with no
signal to load a web page with. The charts came first, then the radar, and the
argument that lost was never about quality: it was that a native app which
sends you to a browser for the picture you actually wanted is two apps.

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
| the radar's grid and frame list | `GET /api/config` → `bounds`, `frames` |
| a radar frame | `GET /api/frames/<file>.webp` |
| a forecast for one of them | `GET /api/point/<name>` — the members at that cell |
| a forecast for wherever the phone is | `GET /api/point?lat=&lon=` |

The third is the one built for this app. A phone is not a configured location,
so that endpoint assembles the same document on demand by sampling the
published frames — both of them. The rain frame gives the value; the spread
frame beside it gives a p10/p50/p90 taken over a few kilometres around that
pixel. So the phone's own position gets a real band for the whole six hours,
not just a line.

What it is a band *of* is the part the app keeps saying out loud. A configured
location's percentiles are its own twenty members at its own square kilometre.
A coordinate's are the ensemble *near* it, read back off pictures, with nothing
behind them that can count members — so there is no `probability`, and the
forecast screen says "near here" rather than "here". The keys carry the
distinction rather than leaving it to prose: `nearby_median` and
`nearby_radius_km` for the neighbourhood, `measured` for the hour of radar
composite that has no ensemble at all, `field` for what the map paints.

Two more things it will not fudge. A pixel no radar looked at is dropped rather
than published as zero, and `out_of_coverage` says the point is outside the
radar domain — which is not the same as a forecast of dry weather, and pauses
alerting rather than relaxing it.

Alerts are evaluated **on the phone**, not pushed from the server. That avoids
push infrastructure, per-device state on the server, and a location trail
leaving the handset. `core`'s `AlertEngine` is a port of the ingestor's
`alerts.py` — latch, hysteresis, quiet period — so "tell me when it is about to
rain" means the same thing whichever surface raised it. It runs every ~15
minutes against a freshly fetched forecast; the server's five-minute cadence
matters for webhook alerting on the edge, but the phone's question is "will it
rain within the hour", which survives coarser sampling.

The radar draws those frames on a MapLibre map, which is the same renderer the
web app uses — a frame is an image stretched across four corners the manifest
names, and an image source is exactly that. The basemap is OpenFreeMap's, again
the same, and it wants no key and no quota: the app still runs on a fresh
install with nothing but a server address. Frames are decoded and painted
through the ramp on the phone, at half the published resolution, because a
kilometre per pixel is finer than a phone showing the whole country can resolve
and four times the memory.

One thing that decoder will not do is let `BitmapFactory` downsample. A frame
packs a 16-bit rain rate across red and green, and averaging neighbouring
pixels averages the two halves of a number: a green channel that wraps as the
rate crosses a boundary would average to mid-grey and paint a dry cell beside a
wet one as moderate rain. It decodes at full size and thins the *colours*
afterwards.

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

The APK lands in `app/build/outputs/apk/debug/app-debug.apk`.

**Putting it on a phone.** Enable developer mode first: Settings → About phone
→ tap *Build number* seven times, then Settings → System → Developer options →
*USB debugging*. Plug the phone in and accept the "Allow USB debugging?" prompt
it shows — until that is accepted `adb` reports the device as `unauthorized`
and nothing will install.

From Android Studio: pick the phone in the device dropdown and press Run.
From a terminal:

```bash
cd android && ./gradlew :app:installDebug
```

Over Wi-Fi instead of a cable (Android 11+, both on the same network) — pair
once using the code from Developer options → *Wireless debugging* → *Pair
device with pairing code*:

```bash
adb pair PHONE_IP:PAIRING_PORT && adb connect PHONE_IP:DEBUG_PORT
```

### First run

The app does nothing until it knows where the server is.

1. Open it and enter the base URL of your Buitjes instance — the same one the
   web map is served from — plus an API key if `API_KEYS` is set. Use *Test
   connection*: it fetches `/api/config`, so a green result means the address,
   the key and the network path are all good.
2. Grant notifications and location when asked. Location has a second step
   Android will not grant from a dialog: the alerts screen explains it and
   deep-links to the system setting, where it has to be set to *Allow all the
   time* for alerts to work while the app is closed.
3. Long-press the home screen → Widgets → Buitjes, and drop the widget. It asks
   which place to watch: one of the locations from `WIDGET_LOCATIONS`, or
   "follow my location".

If the phone reaches the server only over a LAN or WireGuard, remember it needs
to be on that network for the widget to refresh — otherwise the widget greys
out and labels itself stale, which is the intended behaviour rather than a bug.

## State of the code

`:core` is written and its tests pass — including parsing tests that run
against **real captured responses** from the server rather than hand-written
approximations, since the two halves share nothing but that document and
nothing else would catch them drifting apart. Three fixtures: a coordinate, the
same coordinate outside the radar domain, and a configured location, all
captured from a running cycle.

`:app` compiles and packages, and has now run: installed on a Galaxy S24+
(Android 16), where it takes a coarse fix, fetches `/api/point?lat&lon` and
caches a document with 48 banded steps in it. The forecast-for-here path works
end to end, the hourly cards draw, the charts can be scrubbed, and the widget
has been sat on a home screen and looks right at the size it lands in.

That first run cost one bug, and it is worth reading as a warning about the
rest of this list. `LocationSource` only accepted a last-known fix younger than
five minutes and otherwise went asking for a fresh one — and on a phone in a
pocket, where nothing else requests location, the newest fix is *always* older
than five minutes and the fresh request never lands. It failed totally,
reporting "could not get a location fix" while holding a six-minute-old fix
that was perfectly good for a kilometre-wide grid. Every value in it was
defensible; the combination could not work. See `FRESH_ENOUGH_SECONDS`.

Still unseen on a device:

- whether the chart bitmap survives the RemoteViews Binder budget on a *large*
  widget — a phone-sized one is fine, but `ChartRenderer` caps itself at 220k
  pixels, which is a guess at a limit that is device-dependent, and a tablet
  home screen is where the guess would be tested. Over it the widget does not
  draw a smaller chart: it silently fails to update and keeps whatever it last
  showed, which from the home screen looks like the app having died;
- whether the refresh worker actually runs on a phone in Doze at anything like
  fifteen minutes;
- the API 26–29 branch of `LocationSource`, which needs an old device or an
  emulator image to exercise at all;
- whether an alert ever fires, which needs weather — and notification
  permission, which is requested but was not granted on the first run.

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
