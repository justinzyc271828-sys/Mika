package com.wangzan101.mika.cap.phone.shell

/** Bounds for synchronous keyboard injection. Control characters could trigger editor actions. */
internal object AsciiTextKeys {
    const val NOT_SENT = 0
    const val DISPATCHED = 1
    const val UNCONFIRMED = 2

    fun supports(text: String): Boolean = text.length in 1..256 && text.all { it in ' '..'~' }
}
