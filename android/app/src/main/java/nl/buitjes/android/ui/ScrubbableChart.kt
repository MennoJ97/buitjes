package nl.buitjes.android.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.PointerEventType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import nl.buitjes.core.Centre
import nl.buitjes.core.Step

/**
 * A rendered chart you can put a finger on.
 *
 * The web page answers this with a hover: a crosshair follows the pointer and a
 * tooltip names the value and *its spread* under it, because the median is the
 * one number you can already read off an axis and the band's edges are the ones
 * you cannot. The same argument holds harder on a phone, where the chart is a
 * few hundred pixels wide and a six-hour window is squeezed into it.
 *
 * There is no hover on a touchscreen, so the gesture is a touch — press to read,
 * drag to scrub. Both are wired separately from the mouse and stylus path,
 * which does hover properly on a desktop-mode display or with an S Pen.
 *
 * The chart itself stays a bitmap. Only the crosshair and the tooltip are
 * Compose, drawn on top from the geometry the renderer hands back, so scrubbing
 * costs a recomposition of two small elements rather than a redraw of the whole
 * chart on every frame of a drag.
 */
@Composable
fun ScrubbableChart(
    rendered: Rendered,
    series: List<Step>,
    centre: Centre,
    unit: String,
    height: Dp,
    contentDescription: String?,
    modifier: Modifier = Modifier,
) {
    val geometry = rendered.geometry
    // A message where a chart would be — out of coverage, nothing fetched yet.
    // Nothing to point at, so nothing to make touchable.
    if (geometry == null || series.isEmpty()) {
        Image(
            bitmap = rendered.bitmap.asImageBitmap(),
            contentDescription = contentDescription,
            contentScale = ContentScale.FillBounds,
            modifier = modifier.fillMaxWidth().height(height),
        )
        return
    }

    var touchFraction by remember(rendered) { mutableStateOf<Float?>(null) }

    BoxWithConstraints(modifier = modifier.fillMaxWidth().height(height)) {
        val boxWidth = maxWidth
        val boxHeight = maxHeight

        Image(
            bitmap = rendered.bitmap.asImageBitmap(),
            contentDescription = contentDescription,
            contentScale = ContentScale.FillBounds,
            modifier = Modifier
                .fillMaxWidth()
                .height(boxHeight)
                // Press and hold to read a value, exactly where the web page
                // would hover. Released, the crosshair goes away rather than
                // being left behind pointing at a moment nobody is looking at.
                .pointerInput(series) {
                    detectTapGestures(
                        onPress = { offset ->
                            touchFraction = offset.x / size.width
                            tryAwaitRelease()
                            touchFraction = null
                        },
                    )
                }
                // Dragging sideways scrubs. A horizontal detector rather than a
                // general one on purpose: it waits for the gesture to declare
                // itself, so a vertical swipe still scrolls the page under it
                // instead of being eaten by the chart.
                .pointerInput(series) {
                    detectHorizontalDragGestures(
                        onDragStart = { offset -> touchFraction = offset.x / size.width },
                        onDragEnd = { touchFraction = null },
                        onDragCancel = { touchFraction = null },
                        onHorizontalDrag = { change, _ ->
                            touchFraction = change.position.x / size.width
                            change.consume()
                        },
                    )
                }
                // The mouse and stylus path. Read on the initial pass and never
                // consumed, so this cannot interfere with either gesture above
                // or with the scroll: a hover is not a claim on the event.
                .pointerInput(series) {
                    awaitPointerEventScope {
                        while (true) {
                            val event = awaitPointerEvent(PointerEventPass.Initial)
                            val change = event.changes.firstOrNull() ?: continue
                            if (change.pressed) continue
                            touchFraction = when (event.type) {
                                PointerEventType.Exit -> null
                                PointerEventType.Move, PointerEventType.Enter ->
                                    change.position.x / size.width

                                else -> touchFraction
                            }
                        }
                    }
                },
        )

        val fraction = touchFraction
        if (fraction != null) {
            // The step nearest the finger, not the one under it. Snapping means
            // the numbers shown are a step the document actually carries rather
            // than an interpolation nobody forecast.
            val target = geometry.timeAt(fraction)
            val step = series.minByOrNull { kotlin.math.abs(it.t - target) }
            val value = step?.let { centre.valueOf(it) }

            if (step != null && value != null) {
                val stepX = boxWidth * geometry.fractionOf(step.t)
                val markerY = boxHeight * geometry.fractionOfValue(value)

                Box(
                    modifier = Modifier
                        .offset(x = stepX - 1.dp, y = boxHeight * geometry.top)
                        .width(2.dp)
                        .height(boxHeight * (geometry.bottom - geometry.top))
                        .background(MaterialTheme.colorScheme.onSurface.copy(alpha = 0.55f)),
                )
                Box(
                    modifier = Modifier
                        .offset(x = stepX - 4.dp, y = markerY - 4.dp)
                        .size(8.dp)
                        .clip(RoundedCornerShape(4.dp))
                        .background(MaterialTheme.colorScheme.onSurface),
                )

                Tooltip(
                    step = step,
                    value = value,
                    centre = centre,
                    unit = unit,
                    // Flipped to the other side near the right edge, so the
                    // tooltip never leaves the card it belongs to.
                    modifier = Modifier
                        .align(Alignment.TopStart)
                        .offset(
                            x = if (stepX > boxWidth * 0.55f) {
                                (stepX - 150.dp).coerceAtLeast(0.dp)
                            } else {
                                (stepX + 8.dp)
                            },
                            y = 4.dp,
                        ),
                )
            }
        }
    }
}

@Composable
private fun Tooltip(
    step: Step,
    value: Double,
    centre: Centre,
    unit: String,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .widthIn(max = 190.dp)
            .clip(RoundedCornerShape(6.dp))
            .background(MaterialTheme.colorScheme.inverseSurface.copy(alpha = 0.94f))
            .padding(horizontal = 8.dp, vertical = 6.dp),
    ) {
        val onTip = MaterialTheme.colorScheme.inverseOnSurface
        Text(
            formatClock(step.t),
            style = MaterialTheme.typography.labelSmall,
            color = onTip.copy(alpha = 0.75f),
        )
        Text(
            "${precise(value)} $unit",
            style = MaterialTheme.typography.titleSmall,
            color = onTip,
        )
        // Which of the chain's keys this number came from, because on a
        // coordinate's series it changes part way through: the measured hour,
        // then the neighbourhood median. A tooltip that did not say so would
        // present two different statistics under one label.
        centre.keyOf(step)?.let { key ->
            val label = centre.labels.getOrNull(centre.keys.indexOf(key)).orEmpty()
            if (label.isNotBlank()) {
                Text(label, style = MaterialTheme.typography.labelSmall, color = onTip.copy(alpha = 0.75f))
            }
        }

        centre.bands.forEach { band ->
            val low = step.value(band.low)
            val high = step.value(band.high)
            if (low != null && high != null) {
                Text(
                    "${band.label}: ${range(low, high)} $unit",
                    style = MaterialTheme.typography.labelSmall,
                    color = onTip.copy(alpha = 0.75f),
                )
            }
        }

        // Only a members-backed document can answer this, and where it can it
        // is the most useful number on the chart.
        step.probability?.let { chance ->
            Text(
                "chance of rain: ${(chance * 100).toInt()}%",
                style = MaterialTheme.typography.labelSmall,
                color = onTip.copy(alpha = 0.75f),
            )
        }
    }
}

/**
 * Values for reading, not for an axis.
 *
 * The axis formatter is deliberately coarse — whole degrees, whole W/m² — and
 * reusing it here would round a sub-degree band away and print both edges as
 * the same number, which reads as a bug rather than as agreement.
 */
private fun precise(value: Double): String =
    if (kotlin.math.abs(value) < 100) {
        String.format(java.util.Locale.ROOT, "%.1f", value)
    } else {
        value.toInt().toString()
    }

private fun range(low: Double, high: Double): String =
    if (precise(low) == precise(high)) precise(low) else "${precise(low)}–${precise(high)}"
