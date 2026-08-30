pluginManagement {
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "buitjes-android"

// `:core` is plain Kotlin on purpose — the forecast model and the alert state
// machine are the parts worth getting right, and neither needs Android to be
// true. Keeping them here means they compile and their tests run on any
// machine with a JDK, which is what makes them verifiable at all.
include(":core")

// `:app` is only included when there is an SDK to build it against. Gradle
// configures every included project before it runs anything, so an
// unconditional `include(":app")` would make `:core:test` fail on a machine
// with no SDK — the module would drag the Android plugin into a build that has
// no use for it. Skipping it keeps the core testable everywhere and fails
// loudly, with a reason, when someone expects the app and the SDK is missing.
val androidSdk: String? = sequenceOf(
    System.getenv("ANDROID_HOME"),
    System.getenv("ANDROID_SDK_ROOT"),
    file("local.properties")
        .takeIf { it.exists() }
        ?.readLines()
        ?.firstOrNull { it.startsWith("sdk.dir=") }
        ?.substringAfter("="),
).filterNotNull().firstOrNull { it.isNotBlank() && file(it).exists() }

if (androidSdk != null) {
    include(":app")
} else {
    logger.lifecycle(
        "No Android SDK found (ANDROID_HOME, ANDROID_SDK_ROOT or local.properties), " +
            "so only :core is included. See android/README.md to set one up.",
    )
}
