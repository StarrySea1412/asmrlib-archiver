package com.asmrlib.archiver;

import android.graphics.Canvas;
import android.graphics.ColorFilter;
import android.graphics.LinearGradient;
import android.graphics.Outline;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RadialGradient;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.drawable.Drawable;

/**
 * A dark optical material for native floating controls.
 *
 * <p>Layered highlights and opposing rim reflections give the small surface
 * depth without capturing or blurring the WebView/video beneath it. Shaders
 * are cached until the bounds change, so pressing or animating the host view
 * never allocates a bitmap or introduces a video compositor dependency.
 */
public final class GlassDrawable extends Drawable {
    private final float radius;
    private final Paint body = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint light = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint edge = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint pressedWash = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF surface = new RectF();
    private final RectF rim = new RectF();
    private boolean pressed;
    private int alpha = 255;

    /** Radius is in physical pixels, like GradientDrawable.setCornerRadius. */
    public GlassDrawable(float radiusPx) {
        radius = Math.max(0f, radiusPx);
        edge.setStyle(Paint.Style.STROKE);
        edge.setStrokeWidth(1.25f);
        pressedWash.setColor(0x16FFFFFF);
    }

    @Override protected void onBoundsChange(Rect bounds) {
        surface.set(bounds);
        rim.set(surface);
        rim.inset(.75f, .75f);
        float height = Math.max(1f, surface.height());
        body.setShader(new LinearGradient(surface.left, surface.top,
                surface.right, surface.bottom,
                new int[]{0xF0383E4C, 0xF01B202D, 0xF0141821},
                new float[]{0f, .42f, 1f}, Shader.TileMode.CLAMP));
        light.setShader(new RadialGradient(surface.left + surface.width() * .18f,
                surface.top, Math.max(height, surface.width() * .75f),
                new int[]{0x27C6D8FF, 0x00C6D8FF}, null, Shader.TileMode.CLAMP));
        edge.setShader(new LinearGradient(surface.left, surface.top,
                surface.right, surface.bottom,
                new int[]{0xA9EFF5FF, 0x19C8D6F1, 0x4C9BB7EE},
                new float[]{0f, .48f, 1f}, Shader.TileMode.CLAMP));
    }

    @Override public void draw(Canvas canvas) {
        if (surface.isEmpty()) return;
        float corner = Math.min(radius, Math.min(surface.width(), surface.height()) / 2f);
        canvas.drawRoundRect(surface, corner, corner, body);
        canvas.drawRoundRect(surface, corner, corner, light);
        if (pressed) canvas.drawRoundRect(surface, corner, corner, pressedWash);
        canvas.drawRoundRect(rim, Math.max(0f, corner - .75f),
                Math.max(0f, corner - .75f), edge);
    }

    @Override public void getOutline(Outline outline) {
        outline.setRoundRect(getBounds(), radius);
        outline.setAlpha(alpha / 255f);
    }

    @Override public boolean isStateful() { return true; }

    @Override protected boolean onStateChange(int[] states) {
        boolean nextPressed = false;
        for (int state : states) {
            if (state == android.R.attr.state_pressed) nextPressed = true;
        }
        if (pressed == nextPressed) return false;
        pressed = nextPressed;
        invalidateSelf();
        return true;
    }

    @Override public void setAlpha(int value) {
        alpha = Math.max(0, Math.min(255, value));
        body.setAlpha(alpha);
        light.setAlpha(alpha);
        edge.setAlpha(alpha);
        pressedWash.setAlpha(Math.round(22f * alpha / 255f));
        invalidateSelf();
    }

    @Override public int getAlpha() { return alpha; }

    @Override public void setColorFilter(ColorFilter filter) {
        body.setColorFilter(filter);
        light.setColorFilter(filter);
        edge.setColorFilter(filter);
        pressedWash.setColorFilter(filter);
        invalidateSelf();
    }

    @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
}
