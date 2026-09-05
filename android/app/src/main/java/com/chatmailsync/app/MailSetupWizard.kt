@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.chatmailsync.app

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp

/**
 * How the wizard's step 4 hears about a connection stage the moment it finishes.
 *
 * This is an interface rather than a Kotlin lambda because it is handed to
 * Python: check_connection's `on_stage` argument crosses the Chaquopy bridge,
 * and a lambda arrives on the far side as an object Python cannot call, while
 * a Java interface implementation arrives as one whose methods can be called
 * by name. src/mail_client.py's _emit_stage does exactly that -- it looks for
 * an `onStage` attribute first and falls back to calling the object directly,
 * which is what lets Windows keep passing a plain Python function.
 *
 * Three primitives rather than a dict, because a dict would have to be
 * converted on the way across for no gain.
 */
interface StageListener {
    fun onStage(name: String, label: String, ok: Boolean)
}

/** One row of the step-4 progress list, as named by the Python core. */
data class WizardStage(val name: String, val label: String)

// Step titles, kept in one place so the header and the "Step n of 4" line
// cannot disagree. The wizard never asks which mail backend to use (D2): a
// new account is IMAP, and the demoted Gmail sign-in stays reachable only on
// the full Mail account screen.
private val WIZARD_TITLES = listOf(
    "Who hosts your email?",
    "Get an app password",
    "Sign in",
    "Connecting",
)

/**
 * The guided four-step path to a working mailbox, for someone setting one up
 * for the first time.
 *
 * It exists next to -- not instead of -- MailAccountScreen's single-page form.
 * The form is faster once you know what the fields mean; this is for the part
 * that actually stops people, which is not the form at all but getting an app
 * password out of their provider. Hence step 2 being a full step of its own.
 *
 * Nothing here is a dialog. Every step draws in the main window (the app has
 * no pop-ups), and the way back is always a labelled button, never a bare
 * arrow.
 */
@Composable
fun MailSetupWizardScreen(
    onExit: () -> Unit,
    onDone: () -> Unit,
    imapProviders: List<ImapProviderInfo>,
    stagePlan: List<WizardStage>,
    initialProvider: String,
    initialEmail: String,
    onConnect: (String, String, Int, String, String, StageListener, (Boolean, String) -> Unit) -> Unit,
) {
    var step by remember { mutableStateOf(0) }
    var provider by remember { mutableStateOf(initialProvider) }
    var email by remember { mutableStateOf(initialEmail) }
    var password by remember { mutableStateOf("") }
    var customHost by remember { mutableStateOf("") }
    var customPort by remember { mutableStateOf("993") }

    // null = not reached yet, true/false = how that stage went. A stage list
    // that grew a line at a time would hide how much is left, so all five are
    // drawn from the start and light up as the callback arrives.
    val stageResults = remember { mutableStateMapOf<String, Boolean>() }
    var connecting by remember { mutableStateOf(false) }
    var outcome by remember { mutableStateOf<Pair<Boolean, String>?>(null) }

    val info = imapProviders.firstOrNull { it.key == provider }
    val providerLabel = info?.label ?: provider
    val isCustom = provider == "custom"
    val effectiveHost = if (isCustom) customHost else (info?.host ?: "")
    val effectivePort = if (isCustom) (customPort.toIntOrNull() ?: 993) else (info?.port ?: 993)

    fun startConnect() {
        stageResults.clear()
        outcome = null
        connecting = true
        val listener = object : StageListener {
            override fun onStage(name: String, label: String, ok: Boolean) {
                // Arrives on the worker thread the check runs on; snapshot
                // state is safe to write from anywhere.
                stageResults[name] = ok
            }
        }
        onConnect(provider, effectiveHost, effectivePort, email, password, listener) { ok, message ->
            connecting = false
            outcome = ok to message
        }
    }

    Scaffold(
        // Zero, deliberately: MainActivity's Scaffold has already padded
        // this NavHost for the status bar and the bottom bars, and insets
        // are not consumed by being turned into padding -- so a screen
        // Scaffold left on the default reserves the same strips a second
        // time. That silently cost about a row and a half of list height
        // on every screen, which is how two exports ended up below the
        // fold on the import picker.
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        topBar = {
            ChatMailTopBar(
                title = "Set up your mailbox",
                subtitle = "Step ${step + 1} of 4 - ${WIZARD_TITLES[step]}",
                // The two-line title leaves the pill too little room -- it
                // clipped to "No" on a 1080-wide screen -- and it is redundant
                // here anyway: this screen exists to change that very state,
                // and step 4 reports the outcome in far more detail.
                showConnection = false,
                backLabel = if (step == 0) "Back" else "Previous step",
                onBack = {
                    // Once the check is running there is nothing useful to go
                    // back to mid-flight, so the step-4 back button waits for
                    // it to finish rather than leaving a thread writing into a
                    // screen that has moved on.
                    if (!connecting) {
                        if (step == 0) onExit() else step -= 1
                    }
                },
            )
        },
    ) { padding ->
        // One scroll state serves all four steps, so moving between them would
        // otherwise carry the old offset across: arriving at step 2 already
        // scrolled past "I have my app password", which is the one button that
        // step exists to offer. Every step starts at its own top instead.
        val scrollState = rememberScrollState()
        LaunchedEffect(step) { scrollState.scrollTo(0) }
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp)
                .fadingEdges(scrollState, MaterialTheme.colorScheme.background)
                .verticalScrollbar(scrollState)
                .verticalScroll(scrollState),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            when (step) {
                0 -> {
                    Text(
                        "Pick the service your email address belongs to. It decides where " +
                            "Chat Mail Sync files your chats, and how you get the password it needs.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    // Two to a row rather than one. Six full-width buttons plus
                    // the explanation filled the viewport exactly, which pushed
                    // Next below the fold on a 1080x2340 screen -- the step's
                    // own primary action was the one thing you could not see.
                    // Paired rows halve that height and leave the whole step
                    // visible without scrolling.
                    imapProviders.chunked(2).forEach { pair ->
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            pair.forEach { candidate ->
                                val selected = candidate.key == provider
                                val onPick = { provider = candidate.key }
                                if (selected) {
                                    Button(onClick = onPick, modifier = Modifier.weight(1f)) {
                                        Text(candidate.label, maxLines = 1)
                                    }
                                } else {
                                    OutlinedButton(onClick = onPick, modifier = Modifier.weight(1f)) {
                                        Text(candidate.label, maxLines = 1)
                                    }
                                }
                            }
                            // An odd provider count would otherwise stretch the
                            // last button across the full width and break the
                            // grid.
                            if (pair.size == 1) {
                                Spacer(Modifier.weight(1f))
                            }
                        }
                    }
                    // D4: for a known provider the server settings are a fact
                    // to be told, not a question to be asked -- they are right
                    // in the Python preset table and getting them wrong is a
                    // failure the user cannot diagnose. Only "Other (IMAP)"
                    // gets real fields, and those are on step 3 with the rest
                    // of what has to be typed.
                    if (!isCustom && info != null) {
                        Text(
                            "Chat Mail Sync will use ${info.host}, port ${info.port}. " +
                                "Nothing to set up.",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    Button(onClick = { step = 1 }, modifier = Modifier.fillMaxWidth()) {
                        Text("Next")
                    }
                }

                1 -> {
                    // D3: the primary button sits ABOVE the numbered steps.
                    // Anybody who already has an app password -- and the second
                    // time through, most people do -- should not have to scroll
                    // past a page of instructions written for their first time.
                    Button(onClick = { step = 2 }, modifier = Modifier.fillMaxWidth()) {
                        Text("I have my app password")
                    }
                    Text(
                        "An app password is a separate password your provider issues for one " +
                            "app. It is not your normal password, and you can revoke it without " +
                            "touching your account.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    HorizontalDivider()
                    // The same body the Mail account screen shows under its
                    // collapsed help section -- shared rather than copied so
                    // the two cannot drift apart.
                    AppPasswordHelpBody(
                        providerKey = provider,
                        providerLabel = providerLabel,
                        host = effectiveHost,
                    )
                }

                2 -> {
                    if (isCustom) {
                        OutlinedTextField(
                            value = customHost,
                            onValueChange = { customHost = it },
                            label = { Text("Host *") },
                            modifier = Modifier.fillMaxWidth(),
                        )
                        OutlinedTextField(
                            value = customPort,
                            onValueChange = { customPort = it },
                            label = { Text("Port") },
                            keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
                                keyboardType = KeyboardType.Number,
                            ),
                            modifier = Modifier.fillMaxWidth(),
                        )
                    } else {
                        Text(
                            "Signing in to $providerLabel at $effectiveHost, port $effectivePort.",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    OutlinedTextField(
                        value = email,
                        onValueChange = { email = it },
                        label = { Text("Email address *") },
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
                            keyboardType = KeyboardType.Email,
                        ),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    OutlinedTextField(
                        value = password,
                        onValueChange = { password = it },
                        label = { Text("App password *") },
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
                            keyboardType = KeyboardType.Password,
                        ),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Text(
                        "The password is stored encrypted on this phone only. It is never " +
                            "shown again, and never written to a log.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Button(
                        enabled = email.isNotBlank() && password.isNotBlank() &&
                            (!isCustom || customHost.isNotBlank()),
                        onClick = { step = 3 },
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text("Connect")
                    }
                    TextButton(onClick = { step = 1 }, modifier = Modifier.fillMaxWidth()) {
                        Text("I still need an app password")
                    }
                }

                3 -> {
                    // Runs once on arrival, and again only if the user comes
                    // back for a retry -- keyed on nothing else, so a
                    // recomposition mid-check does not start a second one.
                    LaunchedEffect(Unit) { startConnect() }
                    Text(
                        "Checking the connection to $providerLabel.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    stagePlan.forEach { stage ->
                        val result = stageResults[stage.name]
                        Row(
                            horizontalArrangement = Arrangement.spacedBy(12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier.fillMaxWidth(),
                        ) {
                            // The same three marks Windows draws, rather
                            // than the words "OK" and "X": a column of ticks
                            // reads as a checklist at a glance, and five lines
                            // that settle on "OK" are not the ticks the rest of
                            // the app -- and this screen's own copy -- promise.
                            Text(
                                when (result) {
                                    true -> "\u2713"
                                    false -> "\u2715"
                                    null -> "\u2026"
                                },
                                style = MaterialTheme.typography.labelLarge,
                                color = when (result) {
                                    true -> MaterialTheme.colorScheme.tertiary
                                    false -> MaterialTheme.colorScheme.error
                                    null -> MaterialTheme.colorScheme.onSurfaceVariant
                                },
                            )
                            Text(
                                stage.label,
                                style = MaterialTheme.typography.bodyMedium,
                                color = if (result == null) {
                                    MaterialTheme.colorScheme.onSurfaceVariant
                                } else {
                                    MaterialTheme.colorScheme.onSurface
                                },
                            )
                        }
                    }
                    outcome?.let { (ok, message) ->
                        HorizontalDivider()
                        Text(
                            message,
                            style = MaterialTheme.typography.bodyMedium,
                            color = if (ok) {
                                MaterialTheme.colorScheme.onSurface
                            } else {
                                MaterialTheme.colorScheme.error
                            },
                        )
                        if (ok) {
                            Button(onClick = onDone, modifier = Modifier.fillMaxWidth()) {
                                Text("Done")
                            }
                        } else {
                            // Back to the fields rather than a blind retry: a
                            // failure at LOGIN is almost always a typo or the
                            // normal password used in place of the app one.
                            Button(onClick = { step = 2 }, modifier = Modifier.fillMaxWidth()) {
                                Text("Check the details and try again")
                            }
                            OutlinedButton(onClick = { step = 1 }, modifier = Modifier.fillMaxWidth()) {
                                Text("Get a new app password")
                            }
                        }
                    }
                }
            }
        }
    }
}
