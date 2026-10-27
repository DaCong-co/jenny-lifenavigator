package com.flagdizero.jenny

import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.os.Process
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationManagerCompat
import org.json.JSONArray
import org.json.JSONObject

/**
 * Native perception bridge for the Life Navigator.
 *
 * Only reads; the decision about what is collected, how it is filtered and what
 * reaches the model lives in Python (`jenny/navigator/android_perception.py` and
 * `jenny/navigator/perception.py`). Every method is best-effort and returns an
 * empty value instead of throwing into Python: missing access, a revoked
 * permission or an OEM-specific failure must degrade to "no signal", never break
 * a Navigator cycle.
 *
 * Calls block the caller; Python invokes them through `asyncio.to_thread`.
 */
@Suppress("DEPRECATION")
class PerceptionBridge(context: Context) {

    companion object {
        private const val TAG = "PerceptionBridge"
        private const val MAX_USAGE_APPS = 40
    }

    private val appContext = context.applicationContext

    private val resumeType: Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            UsageEvents.Event.ACTIVITY_RESUMED
        } else {
            UsageEvents.Event.MOVE_TO_FOREGROUND
        }

    private val pauseType: Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            UsageEvents.Event.ACTIVITY_PAUSED
        } else {
            UsageEvents.Event.MOVE_TO_BACKGROUND
        }

    /** Whether the user has enabled Jenny in "Device & app notifications". */
    fun hasNotificationAccess(): Boolean = try {
        NotificationManagerCompat.getEnabledListenerPackages(appContext)
            .contains(appContext.packageName)
    } catch (_: Exception) {
        false
    }

    /** Whether the user has granted "Usage access" to Jenny. */
    fun hasUsageAccess(): Boolean = try {
        val appOps = appContext.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            appOps.unsafeCheckOpNoThrow(
                AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), appContext.packageName,
            )
        } else {
            appOps.checkOpNoThrow(
                AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), appContext.packageName,
            )
        }
        mode == AppOpsManager.MODE_ALLOWED
    } catch (_: Exception) {
        false
    }

    fun openNotificationAccessSettings(): Boolean = try {
        appContext.startActivity(
            Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        true
    } catch (_: Exception) {
        false
    }

    fun openUsageAccessSettings(): Boolean = try {
        appContext.startActivity(
            Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        true
    } catch (_: Exception) {
        false
    }

    /** Drains notifications newer than [sinceMs]; Python owns privacy filtering. */
    fun drainNotifications(sinceMs: Long, limit: Int): String =
        NotificationStore.drain(appContext, sinceMs, limit.coerceAtLeast(1))

    /**
     * Whether the listener should store new notifications. Python sets this from
     * the Navigator config; default false means a fresh install captures nothing
     * until a Navigator cycle explicitly enables it, and disabling capture stops
     * the listener instead of merely clearing its queue later.
     */
    fun setNotificationCapture(enabled: Boolean) {
        NotificationStore.setCaptureEnabled(appContext, enabled)
    }

    /** Discards buffered notifications when the user turned notification capture off. */
    fun clearNotifications() {
        NotificationStore.clear(appContext)
    }

    /**
     * Foreground usage between [sinceMs] and [untilMs], computed from raw
     * `UsageEvents` rather than cumulative `UsageStats` buckets: the Navigator
     * runs hourly, and a daily bucket would report the same totals every hour.
     */
    fun getUsageEvents(sinceMs: Long, untilMs: Long): String {
        val manager = appContext.getSystemService(Context.USAGE_STATS_SERVICE)
            as? UsageStatsManager ?: return "[]"
        val end = minOf(untilMs, System.currentTimeMillis())
        if (end <= sinceMs) return "[]"

        val started = HashMap<String, Long>()
        val totals = HashMap<String, Long>()
        val counts = HashMap<String, Int>()
        try {
            val events = manager.queryEvents(sinceMs, end) ?: return "[]"
            val event = UsageEvents.Event()
            while (events.hasNextEvent()) {
                events.getNextEvent(event)
                val pkg = event.packageName ?: continue
                if (pkg.isBlank()) continue
                if (pkg == appContext.packageName) continue
                val at = event.timeStamp
                when (event.eventType) {
                    resumeType -> {
                        if (!started.containsKey(pkg)) started[pkg] = at
                        counts[pkg] = (counts[pkg] ?: 0) + 1
                    }
                    pauseType -> {
                        started.remove(pkg)?.let { from ->
                            totals[pkg] = (totals[pkg] ?: 0L) + (at - from).coerceAtLeast(0L)
                        }
                    }
                }
            }
        } catch (error: Exception) {
            Log.w(TAG, "queryEvents failed", error)
            return "[]"
        }

        for ((pkg, from) in started) {
            totals[pkg] = (totals[pkg] ?: 0L) + (end - from).coerceAtLeast(0L)
        }

        val items = totals.entries
            .filter { (_, millis) -> millis >= 30_000L }
            .sortedByDescending { (_, millis) -> millis }
            .take(MAX_USAGE_APPS)
        val array = JSONArray()
        for ((pkg, millis) in items) {
            val app = try {
                val info = appContext.packageManager.getApplicationInfo(pkg, 0)
                appContext.packageManager.getApplicationLabel(info).toString()
            } catch (_: Exception) {
                pkg
            }
            array.put(
                JSONObject()
                    .put("package", pkg)
                    .put("app", app)
                    .put("minutes", Math.round(millis / 60_000.0 * 10.0) / 10.0)
                    .put("count", counts[pkg] ?: 0),
            )
        }
        return array.toString()
    }

    /** Battery, charging, screen and network state; no identifiers. */
    fun getSystemStatus(): String = try {
        val batteryManager =
            appContext.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val capacity = batteryManager.getIntProperty(
            BatteryManager.BATTERY_PROPERTY_CAPACITY,
        )
        val charging = try {
            batteryManager.isCharging
        } catch (_: Exception) {
            false
        }
        val power = appContext.getSystemService(Context.POWER_SERVICE) as PowerManager
        val connectivity =
            appContext.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val capabilities = connectivity.getNetworkCapabilities(connectivity.activeNetwork)
        val network = when {
            capabilities == null -> "none"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "vpn"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_BLUETOOTH) -> "bluetooth"
            else -> "other"
        }
        JSONObject()
            .put("battery_percent", if (capacity in 0..100) capacity else -1)
            .put("charging", charging)
            .put("screen_on", power.isInteractive)
            .put("power_save", power.isPowerSaveMode)
            .put("network", network)
            .put("uptime_s", SystemClock.elapsedRealtime() / 1000L)
            .toString()
    } catch (error: Exception) {
        Log.w(TAG, "getSystemStatus failed", error)
        "{}"
    }
}
