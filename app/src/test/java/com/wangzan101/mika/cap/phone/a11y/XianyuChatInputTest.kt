package com.wangzan101.mika.cap.phone.a11y

import com.wangzan101.mika.core.uistate.UiElement
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class XianyuChatInputTest {
    private val composer = UiElement(
        "@e1", "View", "想跟TA说点什么...", 440, 960, listOf(144, 938, 744, 984),
        resourceId = null, clickable = false, editable = false, scrollable = false,
    )

    @Test fun syntheticComposerIsScopedToXianyu() {
        assertTrue(XianyuChatInput.isComposer(XianyuChatInput.PACKAGE, composer))
        assertTrue(XianyuChatInput.isComposer(XianyuChatInput.PACKAGE, composer.copy(name = "想跟TA说点什么…")))
        assertFalse(XianyuChatInput.isComposer("other.app", composer))
        assertFalse(XianyuChatInput.isComposer(XianyuChatInput.PACKAGE, composer.copy(editable = true)))
        assertFalse(XianyuChatInput.isComposer(XianyuChatInput.PACKAGE, composer.copy(name = "搜索")))
    }

    @Test fun composerHintDoesNotHideAnExistingDraft() {
        assertFalse(XianyuChatInput.isDraftLabel(null))
        assertFalse(XianyuChatInput.isDraftLabel("想跟TA说点什么..."))
        assertTrue(XianyuChatInput.isDraftLabel("先核对这一题的版本"))
    }

    @Test fun onlyANearbyPasteButtonMatches() {
        val paste = composer.copy(role = "Button", name = "粘贴", centerY = 900)
        assertTrue(XianyuChatInput.isPasteFor(paste, composer, screenHeight = 1000))
        assertFalse(XianyuChatInput.isPasteFor(paste.copy(name = "发送"), composer, screenHeight = 1000))
        assertFalse(XianyuChatInput.isPasteFor(paste.copy(name = "粘贴并发送"), composer, screenHeight = 1000))
        assertFalse(XianyuChatInput.isPasteFor(paste.copy(role = "View"), composer, screenHeight = 1000))
        assertFalse(XianyuChatInput.isPasteFor(paste.copy(centerY = 700), composer, screenHeight = 1000))
        assertFalse(XianyuChatInput.isPasteFor(paste.copy(centerY = 990), composer, screenHeight = 1000))
    }
}
