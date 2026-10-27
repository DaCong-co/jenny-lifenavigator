package com.flagdizero.jenny

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import org.json.JSONObject

/**
 * Captures posted notifications for the Life Navigator perception layer.
 *
 * This service is deliberately dumb: it stores a bounded, factual snapshot
 * (package, app label, title/text, category, time) and never interprets it. The
 * user must enable "notification access" for Jenny in Android settings; until
 * then Android never binds this service and no notification is captured.
 *
 * Notifications from Jenny itself and group summaries are ignored: they are
 * either our own output or a roll-up with no additional user signal.
 */
@Suppress("DEPRECATION")
class JennyNotificationListener : NotificationListenerService() {

    companion object {
        private const val TAG = "JennyNotificationListener"
        private const val MAX_FIELD = 500
    }

    override fun onListenerConnected() {
        Log.i(TAG, "notification listener connected")
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        val status = sbn ?: return
        try {
            if (!NotificationStore.isCaptureEnabled(applicationContext)) return
            if (status.packageName == applicationContext.packageName) return
            val notification = status.notification ?: return
            if ((notification.flags and Notification.FLAG_GROUP_SUMMARY) != 0) return

            val extras = notification.extras
            val title = extras.getCharSequence(Notification.EXTRA_TITLE)
                ?.toString()?.trim().orEmpty()
            val bigText = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
                ?.toString()?.trim()
            val text = (bigText ?: extras.getCharSequence(Notification.EXTRA_TEXT)
                ?.toString()?.trim()).orEmpty()
            val ticker = notification.tickerText?.toString()?.trim().orEmpty()
            if (title.isEmpty() && text.isEmpty() && ticker.isEmpty()) return

            val app = try {
                val info = packageManager.getApplicationInfo(status.packageName, 0)
                packageManager.getApplicationLabel(info).toString()
            } catch (_: Exception) {
                status.packageName
            }
            val item = JSONObject()
                .put("ts", status.postTime)
                .put("package", status.packageName)
                .put("app", app.take(MAX_FIELD))
                .put("title", title.take(MAX_FIELD))
                .put("text", (text.ifEmpty { ticker }).take(MAX_FIELD))
                .put("category", notification.category ?: "")
                .put("ongoing", (notification.flags and Notification.FLAG_ONGOING_EVENT) != 0)
            NotificationStore.append(applicationContext, item.toString())
        } catch (error: Exception) {
            // Never let a malformed notification kill the listener; Android
            // treats a crashing listener as a reason to stop delivering.
            Log.w(TAG, "could not capture notification", error)
        }
    }
}
