package nl.buitjes.android.data

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.os.Looper
import androidx.core.content.ContextCompat
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import java.util.concurrent.Executor
import kotlin.coroutines.resume

/**
 * Where the phone is, at the only resolution that matters here.
 *
 * Every answer this can give is a value, never an exception. The callers are a
 * background worker and a widget's render pass, and both have a sensible thing
 * to do with "no permission" or "no provider" — say so, keep the cached
 * forecast, do not fire an alert — but nothing sensible to do with a
 * `SecurityException` propagating out of a coroutine at 3am.
 */
sealed interface Fix {
    data class Known(val lat: Double, val lon: Double, val ageSeconds: Long) : Fix

    /** Never granted, or revoked since. The alerts screen is where this is fixed. */
    data object PermissionMissing : Fix

    /** Location is switched off entirely, or every provider is disabled. */
    data object NoProvider : Fix

    /** Permission and providers are fine; nothing came back in time. */
    data object Unavailable : Fix
}

object LocationSource {

    /**
     * How long to wait for a fresh fix before giving up.
     *
     * Generous, because this runs inside a worker that has ten minutes and
     * nobody is watching a spinner. A network fix on a phone that has just woken
     * from Doze can take a while, and coming back empty costs a whole 15-minute
     * cycle of alerting.
     */
    private const val TIMEOUT_MS = 20_000L

    /**
     * A last-known fix younger than this is used as-is.
     *
     * Five minutes is a compromise between two real numbers. The forecast grid
     * is about a kilometre wide and the server rounds coordinates to roughly
     * that, so movement below a kilometre is invisible to the answer — and five
     * minutes is walking pace's worth of kilometre. Cycling would beat it, but
     * asking for a fresh fix every quarter of an hour to correct for a case that
     * resolves itself on the next pass is not a trade worth the radio.
     */
    private const val FRESH_ENOUGH_SECONDS = 300L

    /**
     * Providers in the order they are worth asking, coarsest first.
     *
     * GPS is deliberately absent. With only `ACCESS_COARSE_LOCATION` granted the
     * platform fuzzes whatever any provider returns to roughly a kilometre, so a
     * satellite fix would be spending a minute of radio and a visible amount of
     * battery to produce a number that is then rounded away twice — once by
     * Android and once by the server. The network and fused providers give the
     * same usable answer for almost nothing.
     */
    private fun providers(manager: LocationManager): List<String> = buildList {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) add(LocationManager.FUSED_PROVIDER)
        add(LocationManager.NETWORK_PROVIDER)
        add(LocationManager.PASSIVE_PROVIDER)
    }.filter { provider ->
        // getProvider rather than isProviderEnabled: a provider the device does
        // not have at all throws from the latter on some vendor builds. It is
        // deprecated in favour of the LocationProvider-less overloads, but the
        // replacement only answers for providers that exist — which is the very
        // question being asked here.
        @Suppress("DEPRECATION")
        runCatching { manager.getProvider(provider) != null }.getOrDefault(false)
    }

    fun hasPermission(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    suspend fun current(context: Context): Fix {
        if (!hasPermission(context)) return Fix.PermissionMissing

        val manager = context.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
            ?: return Fix.NoProvider
        val available = providers(manager).filter { provider ->
            runCatching { manager.isProviderEnabled(provider) }.getOrDefault(false)
        }
        if (available.isEmpty()) return Fix.NoProvider

        // Every call below is wrapped, and not out of superstition: the
        // permission can be revoked between the check above and the call, in
        // which case the platform throws SecurityException rather than returning
        // null. Revocation while a worker is mid-pass is exactly the case this
        // has to survive.
        bestLastKnown(manager, available)?.let { return it }

        for (provider in available) {
            val fresh = runCatching { requestFix(manager, provider) }.getOrNull()
            if (fresh != null) return fresh.asFix()
        }
        return Fix.Unavailable
    }

    private fun bestLastKnown(manager: LocationManager, available: List<String>): Fix.Known? =
        available
            .mapNotNull { provider ->
                runCatching { manager.getLastKnownLocation(provider) }.getOrNull()
            }
            .map { it.asFix() }
            .filter { it.ageSeconds in 0..FRESH_ENOUGH_SECONDS }
            .minByOrNull { it.ageSeconds }

    private suspend fun requestFix(manager: LocationManager, provider: String): Location? =
        withTimeoutOrNull(TIMEOUT_MS) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                currentLocation(manager, provider)
            } else {
                singleUpdate(manager, provider)
            }
        }

    /** API 30+: one shot, with cancellation the platform actually honours. */
    private suspend fun currentLocation(manager: LocationManager, provider: String): Location? =
        suspendCancellableCoroutine { continuation ->
            val signal = CancellationSignal()
            continuation.invokeOnCancellation { signal.cancel() }
            // A direct executor: the callback does nothing but resume a
            // coroutine, which then continues on whatever dispatcher the caller
            // was using. Handing the platform a real thread pool here would buy
            // a context switch and nothing else.
            manager.getCurrentLocation(
                provider,
                signal,
                Executor { command -> command.run() },
            ) { location ->
                if (continuation.isActive) continuation.resume(location)
            }
        }

    /**
     * Below API 30, where `getCurrentLocation` does not exist.
     *
     * All four `LocationListener` methods are implemented even though three are
     * empty and the SDK we compile against gives them defaults. Those defaults
     * arrived in API 30 — on a device running 26 to 29 the interface has none,
     * and an object that overrides only `onLocationChanged` throws
     * `AbstractMethodError` the moment the platform calls one of the others.
     * That is a crash that cannot happen on any recent phone and therefore
     * cannot be found by testing on one.
     */
    @Suppress("DEPRECATION")
    private suspend fun singleUpdate(manager: LocationManager, provider: String): Location? =
        suspendCancellableCoroutine { continuation ->
            val listener = object : LocationListener {
                override fun onLocationChanged(location: Location) {
                    if (continuation.isActive) continuation.resume(location)
                }

                // Deprecated on the interface itself, and implemented anyway:
                // API 26–29 devices still call it, and a LocationListener that
                // does not implement it fails to load on them.
                @Deprecated("Required by LocationListener below API 30")
                override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) = Unit
                override fun onProviderEnabled(provider: String) = Unit
                override fun onProviderDisabled(provider: String) {
                    if (continuation.isActive) continuation.resume(null)
                }
            }

            continuation.invokeOnCancellation {
                runCatching { manager.removeUpdates(listener) }
            }
            // The main looper because this is called from a background thread
            // that has none of its own, and the callback does nothing but resume
            // a coroutine.
            manager.requestSingleUpdate(provider, listener, Looper.getMainLooper())
        }

    private fun Location.asFix(): Fix.Known {
        // Wall-clock age, which is not the same as elapsed-realtime age and can
        // go negative or absurd if the clock has been adjusted since the fix.
        // The caller filters on a range rather than a maximum for that reason.
        val ageSeconds = (System.currentTimeMillis() - time) / 1000
        return Fix.Known(latitude, longitude, ageSeconds)
    }
}
