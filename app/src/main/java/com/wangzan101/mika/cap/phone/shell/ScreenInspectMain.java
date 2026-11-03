package com.wangzan101.mika.cap.phone.shell;

import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.graphics.Rect;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.util.SparseArray;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.util.List;

/** Read-only ADB entry point. Keeps the phone's accessibility services running. */
public final class ScreenInspectMain {
    static final int CONNECT_FLAGS = UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES;
    private static final int MAX_NODES = 2_500;
    private static final int MAX_DEPTH = 60;

    private ScreenInspectMain() {}

    public static void main(String[] args) throws JSONException {
        if (Process.myUid() != 2000) {
            System.out.println(new JSONObject().put("status", "error").put("code", "shell_required"));
            return;
        }
        HandlerThread thread = new HandlerThread("mika-screen-inspect");
        thread.start();
        UiAutomation automation = null;
        JSONObject result;
        try {
            Class<?> connectionType = Class.forName("android.app.IUiAutomationConnection");
            Object connection = Class.forName("android.app.UiAutomationConnection").getConstructor().newInstance();
            automation = UiAutomation.class.getConstructor(Looper.class, connectionType)
                    .newInstance(thread.getLooper(), connection);
            // The platform default (flags=0) disconnects Mika's AccessibilityService.
            // Never retry with that default, even if another automation client owns the slot.
            UiAutomation.class.getMethod("connect", int.class).invoke(
                    automation, CONNECT_FLAGS);
            AccessibilityServiceInfo service = automation.getServiceInfo();
            service.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
            automation.setServiceInfo(service);
            Budget budget = new Budget();
            JSONArray windows = new JSONArray();
            JSONArray displayIds = new JSONArray();
            SparseArray<List<AccessibilityWindowInfo>> displays = automation.getWindowsOnAllDisplays();
            for (int i = 0; i < displays.size(); i++) {
                int displayId = displays.keyAt(i);
                displayIds.put(displayId);
                for (AccessibilityWindowInfo window : displays.valueAt(i)) {
                    Rect bounds = new Rect();
                    window.getBoundsInScreen(bounds);
                    JSONObject item = new JSONObject()
                            .put("display_id", displayId)
                            .put("id", window.getId()).put("type", window.getType())
                            .put("title", text(window.getTitle()))
                            .put("bounds", bounds(bounds))
                            .put("active", window.isActive()).put("focused", window.isFocused());
                    AccessibilityNodeInfo root = window.getRoot();
                    item.put("root", root == null ? JSONObject.NULL : node(root, budget, 0, false));
                    windows.put(item);
                }
            }
            result = new JSONObject().put("status", "ok").put("display_ids", displayIds)
                    .put("accessibility_services_suppressed", false)
                    .put("captured_at_uptime_ms", SystemClock.uptimeMillis())
                    .put("node_count", budget.nodes).put("truncated", budget.truncated)
                    .put("windows", windows);
        } catch (Exception error) {
            result = new JSONObject().put("status", "error").put("code", "screen_unavailable");
        } finally {
            if (automation != null) {
                try { UiAutomation.class.getMethod("disconnect").invoke(automation); }
                catch (Exception ignored) { }
            }
            thread.quitSafely();
        }
        System.out.println(result);
        // app_process owns Binder threads as well as the temporary event thread.
        System.exit(0);
    }

    @SuppressWarnings("deprecation")
    static Object node(AccessibilityNodeInfo node, Budget budget, int depth, boolean hidden)
            throws JSONException {
        try {
            if (budget.nodes >= MAX_NODES || depth >= MAX_DEPTH) {
                budget.truncated = true;
                return JSONObject.NULL;
            }
            budget.nodes++;
            boolean secret = hidden || node.isPassword();
            Rect rect = new Rect();
            node.getBoundsInScreen(rect);
            JSONObject item = new JSONObject()
                    .put("package", text(node.getPackageName()))
                    .put("class", text(node.getClassName()))
                    .put("resource_id", node.getViewIdResourceName())
                    .put("text", secret ? JSONObject.NULL : text(node.getText()))
                    .put("description", secret ? JSONObject.NULL : text(node.getContentDescription()))
                    .put("password", secret).put("bounds", bounds(rect))
                    .put("clickable", node.isClickable()).put("editable", node.isEditable())
                    .put("enabled", node.isEnabled()).put("visible", node.isVisibleToUser());
            JSONArray children = new JSONArray();
            for (int i = 0; i < node.getChildCount(); i++) {
                if (budget.nodes >= MAX_NODES) {
                    budget.truncated = true;
                    break;
                }
                AccessibilityNodeInfo child = node.getChild(i);
                if (child != null) children.put(node(child, budget, depth + 1, secret));
            }
            return item.put("children", children);
        } finally {
            node.recycle();
        }
    }

    private static Object text(CharSequence value) {
        return value == null ? JSONObject.NULL : value.toString();
    }

    private static JSONArray bounds(Rect value) {
        return new JSONArray().put(value.left).put(value.top).put(value.right).put(value.bottom);
    }

    static final class Budget {
        int nodes;
        boolean truncated;
    }
}
