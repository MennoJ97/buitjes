package nl.buitjes.android.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * A fixed palette rather than dynamic colour.
 *
 * Material You would tint this app with whatever is on the wallpaper, which for
 * most apps is a nice thing and for this one is not: half of what is on screen
 * is a chart drawn on a Canvas in the web app's blues, and a surface tinted
 * lilac to match somebody's wallpaper would sit around it looking like a
 * mistake. Matching the browser matters more here than matching the launcher.
 */
private val LightColours = lightColorScheme(
    primary = Color(0xFF2563EB),
    onPrimary = Color.White,
    surface = Color(0xFFFBFDFF),
    onSurface = Color(0xFF111827),
    onSurfaceVariant = Color(0xFF5B6472),
    background = Color(0xFFFBFDFF),
    onBackground = Color(0xFF111827),
    error = Color(0xFFB3261E),
)

private val DarkColours = darkColorScheme(
    primary = Color(0xFF60A5FA),
    onPrimary = Color(0xFF06203F),
    surface = Color(0xFF11151D),
    onSurface = Color(0xFFE6EAF2),
    onSurfaceVariant = Color(0xFF8B94A7),
    background = Color(0xFF0A0C11),
    onBackground = Color(0xFFE6EAF2),
    error = Color(0xFFF87171),
)

@Composable
fun BuitjesTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) DarkColours else LightColours,
        content = content,
    )
}
