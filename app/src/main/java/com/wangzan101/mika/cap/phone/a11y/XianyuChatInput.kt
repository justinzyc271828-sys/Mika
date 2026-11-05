package com.wangzan101.mika.cap.phone.a11y

import com.wangzan101.mika.core.uistate.UiElement

/** Recognizes the synthetic chat field and its native paste menu. */
internal object XianyuChatInput {
    const val PACKAGE = "com.taobao.idlefish"
    private const val COMPOSER_HINT = "想跟TA说点什么"
    private val pasteLabels = setOf("粘贴", "貼上", "Paste")

    fun isComposer(pkg: String?, element: UiElement): Boolean =
        pkg == PACKAGE && element.role == "View" && !element.editable && isHint(element.name)

    fun isDraftLabel(label: String?): Boolean = label != null && !isHint(label)

    fun isPasteButton(element: UiElement): Boolean =
        element.role == "Button" && element.name in pasteLabels

    fun isPasteFor(element: UiElement, composer: UiElement, screenHeight: Int): Boolean =
        isPasteButton(element) && (composer.centerY - element.centerY) in 1..(screenHeight / 5)

    private fun isHint(label: String): Boolean = label.trimEnd('.', '…') == COMPOSER_HINT
}
