plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.serialization)
    // The catalog is on Kotlin 2.0, where the Compose compiler moved out of the
    // Kotlin plugin into its own. Without this, `compose = true` below fails
    // with a message about the compiler version that does not say "apply a
    // plugin", which is a bad half-hour to spend.
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "nl.buitjes.android"
    compileSdk = 35

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

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }
    // No `composeOptions.kotlinCompilerExtensionVersion` here: that is the
    // Kotlin 1.9 way of pinning the Compose compiler, and on 2.0 the plugin
    // applied above owns the version instead. Setting both is how you get a
    // version conflict between two things that are trying to agree.
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

    implementation(libs.androidx.glance.appwidget)
    implementation(libs.androidx.glance.material3)

    implementation(libs.androidx.work.runtime.ktx)
    implementation(libs.androidx.datastore.preferences)

    implementation(libs.okhttp)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)
}
