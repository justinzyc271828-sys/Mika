package com.wangzan101.mika.cap.phone.a11y

import android.view.Display
import android.view.KeyEvent
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.wangzan101.mika.cap.phone.a11y.DeviceSupport.TEST_PACKAGE
import com.wangzan101.mika.cap.phone.shell.AsciiTextKeys
import com.wangzan101.mika.core.assembly.toolDispatcher
import com.wangzan101.mika.core.uistate.UiElement
import com.wangzan101.mika.shell.IMikaShell
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class UnseenTextKeysDeviceTest {
    @Test
    fun typesLiteralAsciiInTwoHiddenEditorsAfterRejectedPaste() = runBlocking {
        withEditor(failPasteAfter = null) { shell, id, screen ->
            val title = screen.awaitEditor("Title")
            val task = shell.topTaskId(id)
            assertEquals(AsciiTextKeys.NOT_SENT, shell.injectAsciiText(Display.DEFAULT_DISPLAY, task, TEST_PACKAGE, "Wrong"))
            assertEquals(AsciiTextKeys.NOT_SENT, shell.injectAsciiText(id, task + 1, TEST_PACKAGE, "Wrong"))
            assertEquals(AsciiTextKeys.NOT_SENT, shell.injectAsciiText(id, task, TEST_PACKAGE, "Partial中文"))
            assertEquals(AsciiTextKeys.NOT_SENT, shell.injectAsciiText(id, task, TEST_PACKAGE, "Wrong\n"))

            val tools = toolDispatcher(screen)
            val first = "A'z %s & 2"
            val firstResult = tools.typeAtTestPoint(title, first, pressEnter = true)
            assertKeyDispatch(firstResult, screen.peek().elements)
            assertEquals(firstResult.toString(), "true", firstResult["enterSkipped"]?.jsonPrimitive?.content)
            screen.awaitEditor("Title: $first")
            val description = screen.awaitEditor("Description")
            val second = "HermesTest"
            val secondResult = tools.typeAtTestPoint(description, second)
            assertKeyDispatch(secondResult, screen.peek().elements)
            screen.awaitEditor("Description: $second")
            screen.awaitEditor("Title: $first")
            assertFalse(screen.keyboardUp())
        }
    }

    @Test
    fun doesNotReplayWhenRejectedPasteAlreadyChangedTheField() = runBlocking {
        withEditor(failPasteAfter = "Prefix") { _, _, screen ->
            val title = screen.awaitEditor("Title")
            val result = toolDispatcher(screen).typeAtTestPoint(title, "PrefixAndRest", pressEnter = true)
            assertEquals(result.toString(), "unknown", result["verification"]?.jsonPrimitive?.content)
            assertEquals(result.toString(), "true", result["enterSkipped"]?.jsonPrimitive?.content)
            assertTrue(result.toString(), result["hint"]?.jsonPrimitive?.content?.contains("The paste shortcut") == true)
            screen.awaitEditor("Title: Prefix")
        }
    }

    private suspend fun withEditor(
        failPasteAfter: String?, check: suspend (IMikaShell, Int, AndroidScreen) -> Unit,
    ) {
        val shell = DeviceSupport.shell()
        val agent = DeviceSupport.newAgent()
        val id = agent.display.displayId
        try {
            val rejectingPaste = object : IMikaShell by shell {
                override fun injectCtrlKey(displayId: Int, keyCode: Int): Boolean {
                    if (keyCode == KeyEvent.KEYCODE_V && failPasteAfter != null) {
                        assertEquals(AsciiTextKeys.DISPATCHED,
                            shell.injectAsciiText(id, shell.topTaskId(id), TEST_PACKAGE, failPasteAfter))
                    }
                    return false
                }
            }
            val screen = AndroidScreen(DeviceSupport.service(), rejectingPaste, DeviceSupport.ready(agent.display))
            agent.startEditor(KeyOnlyEditorTestActivity::class.java)
            check(shell, id, screen)
        } finally {
            agent.release()
        }
    }

    private suspend fun AndroidScreen.awaitEditor(label: String): UiElement = withTimeout(8_000) {
        var found: UiElement? = null
        while (found == null) {
            found = peek().elements.firstOrNull { it.name == label }
            if (found == null) delay(100)
        }
        assertFalse(found.editable)
        found
    }

    private fun assertKeyDispatch(result: JsonObject, elements: List<UiElement>) {
        assertEquals("$result with $elements", "true", result["ok"]?.jsonPrimitive?.content)
        assertEquals(result.toString(), "unknown", result["verification"]?.jsonPrimitive?.content)
        assertTrue(result.toString(), result["hint"]?.jsonPrimitive?.content?.contains("Sent keyboard events") == true)
    }
}
