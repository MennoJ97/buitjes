# Empty, and deliberately so: `isMinifyEnabled = false` in build.gradle.kts, so
# nothing here runs. The file exists because `proguardFiles` names it and Gradle
# fails on a missing one.
#
# If shrinking is ever turned on, the three things that will break first, in the
# order they will break:
#
#   1. kotlinx.serialization — the generated serializers are found reflectively
#      for @Serializable classes in :core and for the manifest models here.
#   2. WorkManager — RefreshWorker is instantiated from its class name, which
#      lives in the work database as a string R8 cannot see.
#   3. Glance — the receiver and the ActionCallback are both resolved by name
#      from the manifest and from RemoteViews.
#
# All three have consumer rules in their own artifacts, which is exactly why
# this file being empty is a reasonable starting point rather than a gap.
