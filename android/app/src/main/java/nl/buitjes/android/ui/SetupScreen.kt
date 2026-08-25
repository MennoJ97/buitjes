package nl.buitjes.android.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import nl.buitjes.android.data.BuitjesClient
import nl.buitjes.android.data.FetchResult
import nl.buitjes.android.data.Settings
import nl.buitjes.android.work.RefreshWorker

/**
 * Where the server lives, and whether it will talk to us.
 *
 * The connection test is the whole point of this screen. Everything else here
 * is two text fields; what earns it its space is that a self-hosted setup has
 * four independent ways to be wrong — wrong host, no route to it, wrong key, a
 * server that has not published a cycle yet — and each of them looks identical
 * from a home-screen widget. Naming them here, once, at the moment somebody is
 * actually in a position to fix them, is worth more than any amount of error
 * handling elsewhere.
 */
@Composable
fun SetupScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var baseUrl by remember { mutableStateOf("") }
    var apiKey by remember { mutableStateOf("") }
    var status by remember { mutableStateOf<String?>(null) }
    var testing by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        val prefs = Settings.current(context)
        baseUrl = prefs.baseUrl
        apiKey = prefs.apiKey
    }

    Column(
        modifier = modifier
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Server", style = MaterialTheme.typography.titleLarge)
        Text(
            "The address of your Buitjes install. https:// is assumed if you leave the " +
                "scheme off; a plain http:// address on your own network is fine too.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        OutlinedTextField(
            value = baseUrl,
            onValueChange = { baseUrl = it; status = null },
            label = { Text("Base URL") },
            placeholder = { Text("buitjes.example.com") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(
                keyboardType = KeyboardType.Uri,
                imeAction = ImeAction.Next,
            ),
            modifier = Modifier.fillMaxWidth(),
        )

        OutlinedTextField(
            value = apiKey,
            onValueChange = { apiKey = it; status = null },
            label = { Text("API key (optional)") },
            singleLine = true,
            // Masked because it is a secret-shaped thing that people read out
            // over shoulders, not because it is much of a secret — the server is
            // clear that a key is an identifier that can be rotated rather than
            // an authenticator. Leave it empty if the server has no keys set.
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
            modifier = Modifier.fillMaxWidth(),
        )

        Button(
            onClick = {
                testing = true
                status = null
                scope.launch {
                    val normalised = Settings.normaliseUrl(baseUrl)
                    baseUrl = normalised
                    status = describe(BuitjesClient(normalised, apiKey.trim()).manifest(), normalised)

                    // Saved regardless of the outcome. A server that is down
                    // this minute is still the server they meant, and forcing a
                    // successful test before anything is remembered would mean
                    // retyping a URL on a train.
                    Settings.setServer(context, normalised, apiKey)
                    RefreshWorker.schedule(context)
                    RefreshWorker.refreshNow(context)
                    testing = false
                }
            },
            enabled = !testing && baseUrl.isNotBlank(),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(if (testing) "Testing…" else "Save and test connection")
        }

        status?.let { message ->
            Text(message, style = MaterialTheme.typography.bodyMedium)
        }

        Text(
            "Buitjes shows the next six hours of rain from the KNMI radar. The map, the " +
                "timeline and the ensemble charts live in the web app — this is the widget " +
                "and the alarm clock.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/** Each failure named as the thing to go and change. */
private fun describe(result: FetchResult<nl.buitjes.android.data.Manifest>, url: String): String =
    when (result) {
        is FetchResult.Success -> {
            val points = result.value.points
            if (points.isEmpty()) {
                "Connected to $url, but it offered no named locations. Either none are " +
                    "configured on the server, or the key is not valid for them — " +
                    "\"follow my location\" will still work."
            } else {
                "Connected. ${points.size} location(s): ${points.joinToString { it.name }}."
            }
        }

        FetchResult.Unauthorized ->
            "The server is there, but it rejected the API key."

        FetchResult.WarmingUp ->
            "The server is there but has not published a forecast yet. It does that within " +
                "a few minutes of starting up — try again shortly."

        FetchResult.Offline ->
            "Could not reach $url at all. Check the address, and whether this phone is on " +
                "the right network or VPN."

        is FetchResult.Failed -> result.reason.replaceFirstChar(Char::uppercaseChar) + "."
    }
