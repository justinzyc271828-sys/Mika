package com.wangzan101.mika.cap.phone.shell;

import android.app.UiAutomation;
import android.view.accessibility.AccessibilityNodeInfo;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

@RunWith(AndroidJUnit4.class)
public class ScreenInspectMainTest {
    @Test public void inspectionKeepsAccessibilityServicesRunning() {
        assertEquals(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES,
                ScreenInspectMain.CONNECT_FLAGS);
    }

    @SuppressWarnings("deprecation")
    @Test public void passwordTextAndDescriptionAreRedacted() throws Exception {
        AccessibilityNodeInfo node = AccessibilityNodeInfo.obtain();
        node.setPassword(true);
        node.setText("secret-value");
        node.setContentDescription("secret-description");
        JSONObject result = (JSONObject) ScreenInspectMain.node(node, new ScreenInspectMain.Budget(), 0, false);
        assertTrue(result.isNull("text"));
        assertTrue(result.isNull("description"));
        assertTrue(result.getBoolean("password"));
    }

    @SuppressWarnings("deprecation")
    @Test public void readableTextAndMetadataAreRetained() throws Exception {
        AccessibilityNodeInfo node = AccessibilityNodeInfo.obtain();
        node.setText("Example");
        node.setClassName("android.widget.TextView");
        ScreenInspectMain.Budget budget = new ScreenInspectMain.Budget();
        JSONObject result = (JSONObject) ScreenInspectMain.node(node, budget, 0, false);
        assertEquals("Example", result.getString("text"));
        assertEquals("android.widget.TextView", result.getString("class"));
        assertEquals(1, budget.nodes);
        assertFalse(budget.truncated);
    }

    @SuppressWarnings("deprecation")
    @Test public void depthLimitReportsTruncation() throws Exception {
        ScreenInspectMain.Budget budget = new ScreenInspectMain.Budget();
        Object result = ScreenInspectMain.node(AccessibilityNodeInfo.obtain(), budget, 60, false);
        assertEquals(JSONObject.NULL, result);
        assertTrue(budget.truncated);
    }
}
