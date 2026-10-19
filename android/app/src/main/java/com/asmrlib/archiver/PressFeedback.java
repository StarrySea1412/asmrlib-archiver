package com.asmrlib.archiver;

import android.content.res.ColorStateList;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.RippleDrawable;

/**
 * The pressed answer for every hand-drawn button.
 *
 * <p>A {@code GradientDrawable} background does not react to being pressed —
 * it carries no state list — so every custom pill in this app took a tap
 * with total silence, which reads as a dead control ("点了根本没有效果").
 * Wrapping the plate in a {@link RippleDrawable} gives each one the
 * platform's touch answer without changing a single plate's look; the
 * ripple is a translucent white that reads on the dark glass and on the
 * accent fills alike.
 */
final class PressFeedback {

    private static final int RIPPLE = 0x3DFFFFFF;

    private PressFeedback() {
    }

    /** Wrap a button's existing plate; pass the result to setBackground. */
    static Drawable on(Drawable plate) {
        return new RippleDrawable(ColorStateList.valueOf(RIPPLE), plate, null);
    }
}
