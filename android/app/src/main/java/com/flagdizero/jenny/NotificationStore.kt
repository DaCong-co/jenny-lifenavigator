package com.flagdizero.jenny

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * App-private ring buffer for notifications captured by [JennyNotificationListener].
 *
 * The Python side drains it on the Navigator cycle, so the file is a queue, not a
 * history: whatever Python has read is deleted. A hard byte cap keeps a phone
 * whose gateway is disabled/offline from growing it forever.
 *
 * Nothing here interprets a notification. Redaction of sensitive packages and
 * the "keep package/count only" policy lives in Python
 * (`jenny/navigator/perception.py`), so one policy exists and it stays testable
 * off-device.
 */
internal object NotificationStore {
    private const val DIR_NAME = "perception"
    private const val FILE_NAME = "notifications.jsonl"
    private const val PREFS_NAME = "perception"
    private const val CAPTURE_KEY = "capture_notifications"
    private const val MAX_BYTES = 1024 * 1024L
    private const val KEEP_LINES = 300

    private val lock = Any()

    fun setCaptureEnabled(context: Context, enabled: Boolean) {
        try {
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit().putBoolean(CAPTURE_KEY, enabled).apply()
        } catch (_: Exception) {
        }
    }

    fun isCaptureEnabled(context: Context): Boolean = try {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .getBoolean(CAPTURE_KEY, false)
    } catch (_: Exception) {
        false
    }

    private fun file(context: Context): File =
        // Cache, not files: notification text is short-lived, and Android's
        // auto-backup does not normally include cacheDir. Losing it to system
        // pressure is acceptable — it is a queue, not a history.
        File(File(context.cacheDir, DIR_NAME), FILE_NAME)

    fun append(context: Context, line: String) {
        synchronized(lock) {
            try {
                val target = file(context)
                target.parentFile?.mkdirs()
                target.appendText(line + "\n")
                if (target.length() > MAX_BYTES) {
                    val tail = target.readLines().takeLast(KEEP_LINES)
                    target.writeText(tail.joinToString("\n") + "\n")
                }
            } catch (_: Exception) {
                // A notification is best-effort: losing one must never crash the
                // listener service (Android would then stop delivering them).
            }
        }
    }

    /** Return the buffered notifications newer than [sinceMs] as a JSON array. */
    fun drain(context: Context, sinceMs: Long, limit: Int): String {
        synchronized(lock) {
            val target = file(context)
            if (!target.isFile) return "[]"
            val selected = JSONArray()
            try {
                for (line in target.readLines()) {
                    val item = try {
                        JSONObject(line)
                    } catch (_: Exception) {
                        null
                    }
                    if (item == null) continue
                    if (item.optLong("ts", 0L) <= sinceMs) continue
                    selected.put(item)
                }
            } catch (_: Exception) {
                return "[]"
            } finally {
                // Everything read is consumed, including entries older than the
                // requested window: the queue's job is to bound growth.
                try {
                    target.delete()
                } catch (_: Exception) {
                }
            }
            if (selected.length() <= limit) return selected.toString()
            val tail = JSONArray()
            for (index in (selected.length() - limit) until selected.length()) {
                tail.put(selected.getJSONObject(index))
            }
            return tail.toString()
        }
    }

    fun clear(context: Context) {
        synchronized(lock) {
            try {
                file(context).delete()
            } catch (_: Exception) {
            }
        }
    }
}
