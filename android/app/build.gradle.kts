plugins {
    // No `kotlin.android` here. AGP 9 compiles Kotlin itself, and applying the
    // standalone plugin alongside it is a hard error rather than a warning.
    // `:core` still applies `kotlin.jvm`, which is a different plugin for a
    // module AGP knows nothing about.
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.serialization)
    // Compose's compiler has been its own plugin since Kotlin 2.0; `compose =
    // true` below only turns the feature on, it does not bring a compiler.
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "nl.buitjes.android"
    // 37 because the AndroidX versions above refuse to be compiled against
    // anything older, not because this app wants an API from it. Compiling
    // against a newer SDK than you target is the normal arrangement: it decides
    // which APIs exist to call, while `targetSdk` decides which runtime
    // behaviours apply.
    compileSdk = 37

    defaultConfig {
        applicationId = "nl.buitjes.android"
        // 26 is where adaptive icons and notification channels both land, which
        // means no legacy icon set and no pre-channel notification branch. The
        // devices below it are not the ones anyone is running a self-hosted
        // radar client on.
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            // Off deliberately. This is sideloaded (Obtainium, or a cable), so
            // there is no download size to defend, and R8 plus reflective
            // kotlinx.serialization plus WorkManager's class-name lookups is a
            // way to discover at 6am that the alert worker cannot be
            // instantiated. Turn it on when there is a reason to.
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
    }
    // No `composeOptions.kotlinCompilerExtensionVersion` here: that is the
    // Kotlin 1.9 way of pinning the Compose compiler, and on 2.0 the plugin
    // applied above owns the version instead. Setting both is how you get a
    // version conflict between two things that are trying to agree.
}

// `kotlinOptions` inside the android block is the old spelling and is gone in
// AGP 9; the Kotlin plugin owns this now.
kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    // The forecast model, the JSON parsing and the alert state machine, none of
    // which know what Android is. Keeping them there is what lets the alerting
    // rules be tested on the JVM rather than on a phone at dawn.
    implementation(project(":core"))

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.activity.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.material3)

    // glance-material3 is deliberately absent. It would supply `GlanceTheme`,
    // but the widget resolves day/night itself and passes the same palette to
    // the chart renderer — otherwise the composed text and the baked bitmap
    // could disagree about which theme they are in, which is exactly the sort
    // of thing that only shows up on someone else's phone at dusk.
    implementation(libs.androidx.glance.appwidget)

    implementation(libs.androidx.work.runtime.ktx)
    implementation(libs.androidx.datastore.preferences)

    implementation(libs.okhttp)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)
}
