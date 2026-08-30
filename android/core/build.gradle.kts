plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

// No Android dependency, deliberately: see the note in settings.gradle.kts.
dependencies {
    implementation(libs.kotlinx.serialization.json)
    testImplementation(kotlin("test"))
}

// Target 17 bytecode without demanding a JDK 17 be installed. `jvmToolchain(17)`
// would be the tidier spelling, but it makes the build fail on a machine whose
// only JDK is the one Android Studio ships (25 today) — and Studio is what
// builds this. The app module compiles to 17 as well, so the two agree.
kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

tasks.test {
    useJUnitPlatform()
}
