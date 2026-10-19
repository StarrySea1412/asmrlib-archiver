"""Dark glass components shared by the APK's bundled pages.

The app deliberately stays dark, including when Android uses a light theme.
The material takes cues from Apple's Liquid Glass: a clear content hierarchy,
softly lit edges, translucent controls and short, responsive motion. This is a
CSS interpretation, not Apple's proprietary optical renderer.

Only the compact sticky navigation uses a live backdrop blur. Cards and large
surfaces use layered highlights over an opaque dark base, so long lists remain
cheap to scroll and older WebViews retain readable controls. Loading states
animate their icons, and reduced-motion users retain static progress feedback.

Regenerate bundled pages with ``python -m asmrlib_archiver.mobile_discover``.
"""

from __future__ import annotations

# One material in both OS modes, including before the first WebView paint.
THEME_COLOR_META = '<meta name="theme-color" content="#0a0b0f">\n'

#: Tokens, base layer and components.  The pages append only their own layout.
SKIN = """
/* =====================================================================
   1. TOKENS
   Dark is the product theme, independent of the operating system setting.
   ===================================================================== */
:root {
  color-scheme: dark;

  --bg: #0a0b0f;
  --surface: #15171e;
  --surface-2: #1e212a;
  --surface-3: #262a34;
  --fill: rgba(255,255,255,.065);
  --fill-strong: rgba(255,255,255,.12);
  --sep: rgba(255,255,255,.085);
  --sep-strong: rgba(255,255,255,.17);
  /* Opaque fallback; transparency is enabled only with working blur. */
  --nav-bg: #0e1016;
  --glass-nav: rgba(17,20,29,.82);
  --glass-edge: rgba(232,240,255,.24);
  --glass-highlight: rgba(255,255,255,.12);
  --glass-wash: rgba(174,196,255,.06);
  --glass-sheen: linear-gradient(145deg, var(--glass-highlight), transparent 45%, var(--glass-wash));

  --text: #f1f3f8;
  --text-2: #a2acbd;
  --text-3: #6e7a8c;
  --text-on-accent: #06101f;

  /* Cool periwinkle tint; full white is reserved for the optical edge. */
  --accent: #7c9cff;
  /* Second stop of the brand mark's gradient.  Native mirrors both stops in
     Theme.java, and test_the_web_and_native_accents_are_the_same_colour fails
     if they drift apart. */
  --accent-strong: #9b7cf0;
  --accent-soft: rgba(124,156,255,.16);
  --accent-line: rgba(124,156,255,.34);
  --danger: #ff7a7a;
  --danger-soft: rgba(255,122,122,.14);

  --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui,
    "Segoe UI", Roboto, "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
  --fs-large-title: 30px;
  --fs-title: 21px;
  --fs-headline: 16px;
  --fs-body: 15px;
  --fs-subhead: 13.5px;
  --fs-footnote: 12.5px;
  --fs-caption: 11.5px;
  --lh-tight: 1.25;
  --lh-body: 1.5;
  --lh-relaxed: 1.68;

  --sp-1: 4px;
  --sp-2: 8px;
  --sp-3: 12px;
  --sp-4: 16px;
  --sp-5: 20px;
  --sp-6: 24px;
  --sp-7: 32px;

  --r-xs: 8px;
  --r-sm: 11px;
  --r-md: 14px;
  --r-lg: 20px;
  --r-xl: 26px;
  --r-pill: 999px;

  --shadow-card: inset 0 1px 0 rgba(255,255,255,.06), 0 5px 18px rgba(0,0,0,.18);
  --shadow-pop: inset 0 1px 0 rgba(255,255,255,.14), 0 16px 40px rgba(0,0,0,.4);

  /* Motion.  Four durations, because a press and an arrival are not the same
     event: a press has to feel like it happened *now* (.13s), a colour
     settling takes a beat (.22s), a thing moving into place takes longer
     (.42s).  One duration for all three reads either twitchy or sluggish,
     never right - which is a large part of why an untuned UI feels cheap. */
  --dur-press: .13s;
  --dur-fast: .18s;
  --dur: .26s;
  --dur-enter: .42s;
  /* Decelerating: fast off the mark, settling at the end, like a thrown
     object.  --ease-in-out is for colour and opacity, where the ends are not
     a physical event and a symmetric curve looks calmer. */
  --ease: cubic-bezier(.32,.72,0,1);
  --ease-in-out: cubic-bezier(.4,0,.2,1);
  /* How far an arriving element travels, and how long each one waits behind
     the one before it.  --i is the per-item rank: the markup sets it inline,
     and the default here is what an element with no rank gets. */
  --enter-y: 10px;
  --stagger: 24ms;
  --i: 0;

  --inset: 16px;
  --nav-h: 52px;
  --tap: 44px;
  /* Eara's cards are posters: the cover is SQUARE (AlbumGridItem
     .aspectRatio(1f)), 12dp-rounded, floating on a soft depth shadow, and
     the text column under it starts at 16/22 SemiBold with up to three
     lines. These are the reference implementation's own numbers, read from
     its source - not guesses from a screenshot. */
  --thumb-ratio: 1 / 1;
  --card-min: clamp(148px, 43vw, 208px);
}

/* The light-system branch deliberately repeats the dark palette. Existing
   cross-surface token checks can verify both OS configurations without
   treating the absence of an override as proof of consistent colours. */
@media (prefers-color-scheme: light) {
  :root {
    color-scheme: dark;
    --bg: #0a0b0f;
    --surface: #15171e;
    --surface-2: #1e212a;
    --surface-3: #262a34;
    --fill: rgba(255,255,255,.065);
    --fill-strong: rgba(255,255,255,.12);
    --sep: rgba(255,255,255,.085);
    --sep-strong: rgba(255,255,255,.17);
    --nav-bg: #0e1016;
    --text: #f1f3f8;
    --text-2: #a2acbd;
    --text-3: #6e7a8c;
    --text-on-accent: #06101f;
    --accent: #7c9cff;
    --accent-strong: #9b7cf0;
    --accent-soft: rgba(124,156,255,.16);
    --accent-line: rgba(124,156,255,.34);
    --danger: #ff7a7a;
    --danger-soft: rgba(255,122,122,.14);
    --shadow-card: inset 0 1px 0 rgba(255,255,255,.06), 0 5px 18px rgba(0,0,0,.18);
    --shadow-pop: inset 0 1px 0 rgba(255,255,255,.14), 0 16px 40px rgba(0,0,0,.4);
  }
}

/* =====================================================================
   2. BASE
   ===================================================================== */
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; background: var(--bg); min-height: 100%; }

body {
  margin: 0;
  position: relative;
  background: var(--bg);
  background-image: radial-gradient(ellipse 90% 340px at 15% 0%, rgba(124,156,255,.085), transparent 75%);
  background-repeat: no-repeat;
  min-height: 100vh;
  color: var(--text);
  font: var(--fs-body)/var(--lh-body) var(--font);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  /* A rubber-banding page shows the shell's own background through it, which
     is where the flash of a different colour came from. */
  overscroll-behavior-y: none;
}

/* The system tap highlight is off because every tappable carries its own
   :active press animation; with only one of the two, taps either double up
   or give no feedback at all. */
body, a, button, input, select, textarea { touch-action: manipulation; }
a, button { -webkit-tap-highlight-color: transparent; }
a { color: var(--accent); text-decoration: none; }
button, input, select, textarea { font: inherit; color: inherit; }
h1, h2, h3, p, figure { margin: 0; }
img { max-width: 100%; }

:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--r-xs); }

[hidden] { display: none !important; }
.muted { color: var(--text-2); }
.tnum { font-variant-numeric: tabular-nums; }

/* =====================================================================
   3. LAYOUT
   ===================================================================== */
.wrap {
  position: relative;
  padding: var(--sp-3) var(--inset) calc(var(--sp-6) + env(safe-area-inset-bottom));
}

/* ---- nav bar ---------------------------------------------------------- */
.nav {
  position: sticky;
  top: 0;
  z-index: 40;
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  min-height: var(--nav-h);
  padding: calc(env(safe-area-inset-top) + var(--sp-1)) var(--inset) var(--sp-1);
  background: var(--glass-sheen), var(--nav-bg);
  border-bottom: 1px solid transparent;
  transition: border-color var(--dur) var(--ease);
}
.nav.is-stuck { border-bottom-color: var(--sep); }
/* Keep the fallback opaque: unsupported blur must not expose sharp text
   underneath. This is the only live blur surface in a scrolling page, and the
   opaque --nav-bg is repeated as the bottom layer of the guarded rule too, so
   the bar still cannot ghost even if a compositor drops the blur. */
@supports ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  .nav {
    background: var(--glass-sheen), var(--glass-nav), var(--nav-bg);
    -webkit-backdrop-filter: blur(22px) saturate(145%);
    backdrop-filter: blur(22px) saturate(145%);
  }
}

.nav-title {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-headline);
  font-weight: 600;
  letter-spacing: -.012em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  /* The inline title is the large title's stand-in: it fades in as the large
     one scrolls away, which is the entire point of the pattern. A page with
     no large title pins it visible with .nav-title-always. */
  opacity: 0;
  transform: translateY(4px);
  transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.nav.is-stuck .nav-title,
.nav-title-always { opacity: 1; transform: none; }

.nav-mark {
  flex: 0 0 auto;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-on-accent);
  background: linear-gradient(150deg, var(--accent), var(--accent-strong));
  box-shadow: inset 0 -1px 0 rgba(0,0,0,.16);
}
.nav-mark svg { width: 15px; height: 15px; }
.brand-mark { display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: 9px; color: var(--text-on-accent);
  background: linear-gradient(150deg, var(--accent), var(--accent-strong));
  box-shadow: inset 0 -1px 0 rgba(0,0,0,.16); }
.brand-ico { width: 15px; height: 15px; }

.icon-btn {
  flex: 0 0 auto;
  width: var(--tap);
  height: var(--tap);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--sep-strong);
  border-radius: var(--r-pill);
  background: var(--glass-sheen), var(--fill);
  color: var(--text);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.09), 0 3px 10px rgba(0,0,0,.12);
  cursor: pointer;
  transition: transform var(--dur-fast) var(--ease),
    background-color var(--dur-fast) var(--ease);
}
.icon-btn svg { width: 19px; height: 19px; }
.icon-btn:active { transform: scale(.9); background-color: var(--fill-strong); }
.icon-btn.is-busy svg { animation: skin-spin .7s linear infinite; }
@keyframes skin-spin { to { transform: rotate(360deg); } }

/* ---- large title ------------------------------------------------------
   Not sticky: it is meant to scroll off and hand over to the inline title. */
.page-head { padding: var(--sp-1) var(--inset) var(--sp-4); }
.page-head h1 {
  font-size: var(--fs-large-title);
  font-weight: 700;
  line-height: 1.14;
  letter-spacing: -.024em;
}
.page-head p { margin-top: 5px; font-size: var(--fs-subhead); color: var(--text-2); }

/* =====================================================================
   4. COMPONENTS
   ===================================================================== */

/* ---- filter rail ------------------------------------------------------
   Bled to both edges so it reads as a rail rather than a cut, faded at each
   end, and padded so a pill is never clipped mid-label. */
.chips {
  display: flex;
  gap: var(--sp-2);
  overflow-x: auto;
  overflow-y: hidden;
  margin: 0 calc(var(--inset) * -1) var(--sp-4);
  padding: var(--sp-2) var(--inset) var(--sp-2);
  scroll-padding-inline: var(--inset);
  scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
  -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 24px,
    #000 calc(100% - 24px), transparent 100%);
  mask-image: linear-gradient(90deg, transparent 0, #000 24px,
    #000 calc(100% - 24px), transparent 100%);
}
.chips::-webkit-scrollbar { display: none; }
/* Nothing loaded yet: an empty rail would still spend its bottom margin. */
.chips:empty { display: none; }

.chip {
  position: relative;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 34px;
  padding: 0 13px;
  border: 1px solid var(--sep);
  border-radius: var(--r-pill);
  background: var(--glass-sheen), var(--fill);
  color: var(--text-2);
  font-size: var(--fs-subhead);
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
  transition: transform var(--dur-press) var(--ease),
    background-color var(--dur) var(--ease-in-out),
    color var(--dur) var(--ease-in-out);
}
/* Touch target, not visual size.  A rail of 44px lozenges reads chunky, so
   the pill stays 34px and the tappable box grows to the 44px minimum with
   five invisible pixels above and below.  The rail's own padding-block is
   what keeps the expander inside the scroller's overflow-y: hidden. */
.chip::after {
  content: '';
  position: absolute;
  inset: -5px 0;
}
.chip em { font-style: normal; color: var(--text-3); font-variant-numeric: tabular-nums; }
.chip:active { transform: scale(.95); }

/* Source switch: two pills above the tag rail. The active one is filled
   with the accent — the same selected-voice the reference uses for its
   bottom nav's current item. */
.ds-tabs {
  display: flex;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.ds-tab {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  height: 36px;
  padding: 0 16px;
  border: 1px solid var(--sep);
  border-radius: var(--r-pill);
  background: var(--glass-sheen), var(--fill);
  color: var(--text-2);
  font-size: var(--fs-subhead);
  font-weight: 600;
  cursor: pointer;
  transition: transform var(--dur-press) var(--ease),
    background-color var(--dur) var(--ease-in-out),
    color var(--dur) var(--ease-in-out),
    border-color var(--dur) var(--ease-in-out);
}
.ds-tab:active { transform: scale(.96); }
.ds-tab.is-active {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--text-on-accent);
}

/* The asmr.one search row: one input + go button, sitting where a user's
   thumb reaches — directly under the source pills. */
.ds-search {
  display: flex;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
  max-height: 40px;
  overflow: hidden;
  transition: max-height var(--dur) var(--ease), opacity var(--dur) var(--ease),
    margin-bottom var(--dur) var(--ease), visibility 0s;
}
/* The search field belongs to one source only, so switching sources adds or
   removes a block of chrome (94px with the mirror row). `display: none` made
   that a jump of the whole feed; a max-height collapse makes it a movement.
   `visibility` flips at the END of the collapse, so the input leaves the tab
   order with the box and not a frame early. */
.ds-search.is-off, .ds-lines.is-off {
  max-height: 0;
  margin-bottom: 0;
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transition: max-height var(--dur) var(--ease), opacity var(--dur) var(--ease),
    margin-bottom var(--dur) var(--ease), visibility 0s var(--dur);
}
.ds-search input {
  flex: 1 1 auto;
  min-width: 0;
  height: 40px;
  padding: 0 14px;
  border: 1px solid var(--sep);
  border-radius: 12px;
  background: var(--glass-sheen), var(--fill);
  color: var(--text);
  font-size: var(--fs-body);
  -webkit-appearance: none;
  appearance: none;
}
.ds-search input::placeholder { color: var(--text-3); }
.ds-search input:focus { outline: none; border-color: var(--accent); }
.ds-search-go {
  flex: 0 0 auto;
  height: 40px;
  padding: 0 16px;
  border: none;
  border-radius: 12px;
  background: var(--accent);
  color: var(--text-on-accent);
  font-size: var(--fs-subhead);
  font-weight: 600;
  cursor: pointer;
}
.ds-search-go:active { transform: scale(.96); }

/* The mirror lines (asmr.one / -100 / -200 / -300): quiet compact chips so
   the row reads as a setting, not a second source switch — the active line
   takes the same filled voice as the source pill above it. */
.ds-lines {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
  max-height: 30px;
  overflow: hidden;
  transition: max-height var(--dur) var(--ease), opacity var(--dur) var(--ease),
    margin-bottom var(--dur) var(--ease), visibility 0s;
}
.ds-lines-label {
  flex: 0 0 auto;
  font-size: var(--fs-caption);
  color: var(--text-3);
}
.ds-line {
  flex: 0 0 auto;
  height: 30px;
  padding: 0 13px;
  border: 1px solid var(--sep);
  border-radius: var(--r-pill);
  background: var(--glass-sheen), var(--fill);
  color: var(--text-2);
  font-size: var(--fs-caption);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  transition: transform var(--dur-press) var(--ease),
    background-color var(--dur) var(--ease-in-out),
    color var(--dur) var(--ease-in-out),
    border-color var(--dur) var(--ease-in-out);
}
.ds-line:active { transform: scale(.95); }
.ds-line.is-active {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--text-on-accent);
}
.chip.is-on { background: linear-gradient(145deg, var(--accent), var(--accent-strong)); color: var(--text-on-accent); font-weight: 600; border-color: var(--glass-edge); box-shadow: inset 0 1px 0 rgba(255,255,255,.24), 0 3px 10px rgba(0,0,0,.15); }
.chip.is-on em { color: inherit; opacity: .66; }

/* ---- section header ---------------------------------------------------
   One component, two page-local names: the pages already ship .ds-sec and
   .pl-sec and their JS and tests speak both. */
.ds-sec, .pl-sec {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
  margin: 0 var(--sp-1) var(--sp-3);
}
.ds-sec[hidden], .pl-sec[hidden] { display: none; }
.ds-sec-title, .pl-sec-title {
  font-size: var(--fs-footnote);
  font-weight: 600;
  letter-spacing: .08em;
  color: var(--text-2);
}
.ds-sec-count, .pl-sec-count {
  font-size: var(--fs-footnote);
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}

/* ---- media card ------------------------------------------------------- */
.cover-grid {
  /* A staggered wall, not a row-aligned grid - Eara drives its list with
     StaggeredGridCells.Adaptive, so card heights vary with the text they
     carry and the wall packs tight. CSS columns give the same masonry;
     the skeleton block spans all columns so the loading state keeps the
     full-width grid it always had. */
  columns: 2;
  column-gap: var(--sp-3);
}
/* A source switch keeps the cards that are already on screen and dims them
   while the other source resolves: blanking the feed for a skeleton and then
   refilling it is the blink this removes. The tag rail and the section header
   change with the same data, so they ride the same dim. */
.cover-grid, .chips, .ds-sec { transition: opacity var(--dur) var(--ease); }
.cover-grid.is-switching, .chips.is-switching, .ds-sec.is-switching {
  opacity: .38;
}

.cover-card {
  position: relative;
  display: flex;
  flex-direction: column;
  border-radius: var(--r-md);
  /* The Eara pass: the card is the cover, not a box around it. A glass
     rectangle behind every cover read as one grey slab repeated; the cover
     itself, rounded and set straight on the page, is what a poster wall
     should be. The class keeps its name and its animation contract (the
     motion tests pin the block) - only the chrome is gone.
     Masonry: a card must never split across two columns. */
  break-inside: avoid;
  margin-bottom: var(--sp-4);
  color: inherit;
  /* Offscreen cards still cost paint work unless told not to. */
  content-visibility: auto;
  contain-intrinsic-size: auto 260px;
  transition: transform var(--dur-fast) var(--ease);
}
.cover-card:active { transform: scale(.975); }

.cover-thumb {
  position: relative;
  width: 100%;
  height: auto;
  inset: auto;
  aspect-ratio: var(--thumb-ratio);
  /* With no card surface the rounding belongs to the image itself - 12dp in
     the reference (AlbumGridItemCornerRadius), --r-md here. */
  border-radius: var(--r-md);
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  background: var(--surface-3);
  /* AlbumCoverDepthShadow: the cover floats above the page on a soft dark
     pool instead of sitting in a drawn box. */
  box-shadow: 0 12px 28px -10px rgba(0,0,0,.55);
}
.cover-thumb-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
/* Fades the foot of a photo so a bright cover cannot fight the badge row. */
.cover-thumb::after {
  content: "";
  position: absolute;
  inset: auto 0 0 0;
  height: 46%;
  pointer-events: none;
  background: linear-gradient(180deg, transparent, rgba(0,0,0,.32));
}

/* Placeholder. All eight tints sit in one low-saturation family over the same
   neutral surface. The old set was eight muddy dark gradients - a brown, a
   swamp green, a plum - which read as broken artwork rather than "no image",
   and the initial was a 2.1rem glyph in 34% white on top of it. */
.cover-thumb-fallback {
  position: absolute;
  inset: 0;
  display: none;
  align-items: center;
  justify-content: center;
  background-color: var(--surface-3);
  background-image: linear-gradient(150deg,
    rgba(var(--tint, 124,156,255), .20), transparent 72%);
}
.cover-thumb.is-plate .cover-thumb-fallback,
.cover-thumb.has-cover-fallback .cover-thumb-fallback { display: flex; }
.cover-thumb-fallback[hidden] { display: none; }
.plate-0 { --tint: 124,156,255; }
.plate-1 { --tint: 156,138,250; }
.plate-2 { --tint: 104,176,232; }
.plate-3 { --tint: 122,196,186; }
.plate-4 { --tint: 178,146,222; }
.plate-5 { --tint: 118,166,248; }
.plate-6 { --tint: 148,188,158; }
.plate-7 { --tint: 206,168,140; }

.cover-initial {
  position: relative;
  z-index: 1;
  transform: none;
  width: 42px;
  height: 42px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: 600;
  color: var(--text-2);
  background: var(--fill);
  border: 1px solid var(--sep);
}

.cover-flag {
  position: absolute;
  right: var(--sp-2);
  bottom: var(--sp-2);
  z-index: 3;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 9px;
  border-radius: var(--r-pill);
  font-size: var(--fs-caption);
  font-weight: 600;
  line-height: 1.45;
  color: #fff;
  font-variant-numeric: tabular-nums;
  background: rgba(10,12,18,.66);
  border: 1px solid rgba(255,255,255,.18);
  white-space: nowrap;
}
.cover-flag svg { width: 11px; height: 11px; opacity: .9; }
/* Where Eara pins its play count, this page pins the only measurement the
   source hands over: the duration. Same anatomy - a quiet dark pill in the
   cover's foot, away from the text block. */

.cover-scrim { display: none; }

.cover-info {
  position: static;
  inset: auto;
  left: auto;
  right: auto;
  bottom: auto;
  display: flex;
  flex-direction: column;
  /* AlbumGridItem's info column: h-padding 2dp, v 8dp, 3dp between rows. */
  gap: 4px;
  padding: var(--sp-2) var(--sp-1) 0;
  pointer-events: none;
}
.cover-title {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  /* 16/22 SemiBold, three lines - the reference's own title metrics
     (titleSmall overridden to 16sp/22sp, maxLines 3). */
  -webkit-line-clamp: 3;
  overflow: hidden;
  font-size: var(--fs-headline);
  line-height: 1.4;
  font-weight: 600;
  letter-spacing: -.006em;
  color: var(--text);
}
/* The one colourful row: the source host with a globe glyph in the accent -
   the same prominent-identity voice the reference uses for its CV line,
   while the date (this page's play-count analogue) lives on the cover. */
.cover-meta-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-subhead);
  font-weight: 600;
  white-space: nowrap;
  color: var(--accent);
}
.cover-meta-row svg { width: 13px; height: 13px; flex: 0 0 auto; opacity: .9; }
.cover-src {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* Tag words ride under the title as bare #words in the secondary colour -
   the reference sets its tags to textSecondary with a # prefix, in a Flow
   row. Two clamped lines; long CJK tags wrap, the rest is cut. */
.cover-tags {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  font-size: var(--fs-footnote);
  line-height: 1.6;
  color: var(--text-2);
}
.cover-tag {
  font-style: normal;
  white-space: nowrap;
  margin-right: 10px;
}

/* ---- inset grouped list ----------------------------------------------
   iOS's grouped table: one surface, hairlines inset to the text, rounded at
   the ends. The play page's source rows were separate floating cards with a
   10px gap, which is what made a two-item list look cluttered. */
.play-enter {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  min-height: 60px;
  margin: 0;
  padding: 11px var(--sp-3);
  border: 1px solid var(--sep);
  border-bottom-width: 0;
  border-radius: 0;
  overflow: hidden;
  background: var(--glass-sheen), var(--surface);
  color: inherit;
  box-shadow: none;
  content-visibility: auto;
  contain-intrinsic-size: auto 60px;
  transition: background-color var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
}
.play-enter:first-of-type {
  border-top-left-radius: var(--r-lg);
  border-top-right-radius: var(--r-lg);
}
.play-enter:last-of-type {
  border-bottom-width: 1px;
  border-bottom-left-radius: var(--r-lg);
  border-bottom-right-radius: var(--r-lg);
}
.play-enter + .play-enter::before {
  content: "";
  position: absolute;
  /* the hairline starts where the text does: 12px padding + 44px plate + gap */
  left: 68px;
  right: 0;
  top: 0;
  height: 1px;
  background: var(--sep);
}
.play-enter:active { background-color: var(--surface-3); transform: scale(.985); }
.play-enter.is-primary { background: var(--glass-sheen), linear-gradient(110deg, var(--accent-soft), var(--surface)); }
.play-enter.is-primary::after {
  content: "";
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--accent);
}
.play-enter:hover { transform: none; }

/* The in-app playback affordance: a compact accent pill at the row's right
   edge, inside the same <a> row. Tapping it must not also jump to the embed
   page (the page script stops the propagation); visually it reads as
   "this source can also play right here" without stealing the row tap. */
.play-enter.is-inapp { cursor: pointer; }
.play-inapp {
  flex: 0 0 auto;
  margin-left: auto;
  padding: 5px 11px;
  border: 1px solid var(--accent-line);
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font: inherit;
  font-size: var(--fs-caption);
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  transition: transform var(--dur-fast) var(--ease), background-color var(--dur-fast) var(--ease);
}
.play-inapp:active { transform: scale(.96); background: var(--accent-line); }
/* With the pill present the chevron is dropped (the JS omits it) and the
   hairline still starts at the icon, so nothing else changes. */

/* A multi-track work gets one strip above the list: the whole queue in a
   single tap. It reads as the list's head rather than another source row —
   accent-washed, taller, no hairline — so it cannot be mistaken for a
   playable source of its own. */
.play-queuebar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  margin: 0 0 var(--sp-3);
  padding: 11px 14px;
  border: 1px solid var(--accent-line);
  border-radius: var(--r-md);
  background: var(--glass-sheen), var(--accent-soft);
  color: var(--accent);
  font: inherit;
  font-size: var(--fs-subhead);
  font-weight: 600;
  text-align: left;
  cursor: pointer;
  transition: transform var(--dur-fast) var(--ease), background-color var(--dur-fast) var(--ease);
}
.play-queuebar:active { transform: scale(.985); background: var(--accent-line); }
.play-queuebar svg { flex: 0 0 auto; width: 15px; height: 15px; }
.play-queuebar .pl-qc {
  margin-left: auto;
  color: var(--text-2);
  font-size: var(--fs-caption);
  font-weight: 400;
}

/* The per-track "+" (Eara's addedToQueue): a second, quieter affordance next
   to the in-app pill. The tick is the whole confirmation — the page does not
   navigate, so nothing else would tell the user the tap landed. */
.play-addq {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  margin-left: 6px;
  padding: 0;
  border: 1px solid var(--sep-strong);
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-2);
  font: inherit;
  font-size: var(--fs-headline);
  line-height: 1;
  cursor: pointer;
  transition: transform var(--dur-fast) var(--ease), background-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease);
}
.play-addq:active { transform: scale(.92); background: var(--accent-soft); color: var(--accent); }
.play-addq.is-added {
  border-color: var(--accent-line);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: var(--fs-body);
}

.src-ico {
  position: relative;
  flex: 0 0 auto;
  width: 44px;
  height: 44px;
  border-radius: 13px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
  background: var(--glass-sheen), var(--accent-soft);
  border: 1px solid var(--accent-line);
  box-shadow: inset 0 1px 0 var(--glass-highlight), 0 3px 8px rgba(0,0,0,.12);
}
.src-ico svg { width: 19px; height: 19px; }
.play-enter.is-primary .src-ico {
  background: linear-gradient(145deg, var(--accent), var(--accent-strong));
  color: var(--text-on-accent);
  border-color: transparent;
}
.src-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.play-enter strong {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
  font-size: var(--fs-body);
  font-weight: 600;
  letter-spacing: -.008em;
}
/* The name owns this line: it is what the row IS, so it takes what is left
   and ellipsises instead of wrapping. The folder path used to be joined into
   this slot and a work whose folders read 「06: 特典 · 03: 闹钟音频 · 01_mp3」
   grew every row to five lines — the name sat at the end of the paragraph. */
.src-name {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.src-badge {
  flex: 0 0 auto;
  padding: 2.5px 8px;
  border-radius: var(--r-pill);
  font-size: var(--fs-caption);
  font-weight: 700;
  color: var(--text-on-accent);
  background: var(--accent);
}
.src-host {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  font-size: var(--fs-footnote);
  color: var(--text-2);
  white-space: nowrap;
}
.src-host svg { flex: 0 0 auto; width: 11px; height: 11px; opacity: .85; }
/* One line, always: a CDN host plus a folder plus 应用内 is wider than the
   row, and wrapping it is what turned the meta line into a three-line stack
   under a name that had already been pushed out of its own line. */
.src-meta {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.src-chev {
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
  color: var(--text-3);
  transition: transform var(--dur) var(--ease), color var(--dur) var(--ease);
}
.play-enter:active .src-chev { transform: translateX(3px); color: var(--accent); }
.play-enter.is-loading .src-ico svg,
.play-enter[aria-busy="true"] .src-ico svg { opacity: 0; }
.play-enter.is-loading .src-ico::after,
.play-enter[aria-busy="true"] .src-ico::after {
  content: "";
  position: absolute;
  width: 20px;
  height: 20px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-bottom-color: transparent;
  border-radius: 50%;
  animation: skin-spin .8s linear infinite;
}

/* ---- buttons ---------------------------------------------------------- */
.button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: var(--tap);
  padding: 0 var(--sp-4);
  border: 1px solid transparent;
  border-radius: var(--r-md);
  background: var(--glass-sheen), var(--fill);
  color: var(--text);
  font-size: var(--fs-headline);
  font-weight: 600;
  cursor: pointer;
  transition: transform var(--dur-fast) var(--ease),
    opacity var(--dur-fast) var(--ease),
    background-color var(--dur-fast) var(--ease);
}
.button svg { width: 15px; height: 15px; }
.button:active { transform: scale(.97); opacity: .85; }
.button:disabled { opacity: .38; }
.button-primary { background: linear-gradient(145deg, var(--accent), var(--accent-strong)); color: var(--text-on-accent); border-color: var(--glass-edge); box-shadow: inset 0 1px 0 rgba(255,255,255,.22), 0 4px 14px rgba(0,0,0,.16); }
.button-secondary, .button-ghost { background: var(--glass-sheen), var(--fill); color: var(--text); border-color: var(--sep-strong); }
.button-danger { background: var(--danger-soft); color: var(--danger); }

.ds-pager { display: flex; gap: var(--sp-2); margin-top: var(--sp-5); }
.ds-pager[hidden] { display: none; }
.ds-pager .button { flex: 1 1 0; min-width: 0; border-color: var(--sep); }

/* Small pill link, used by the play page's footer row. */
.glass-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 36px;
  padding: 0 13px;
  border: 1px solid var(--sep);
  border-radius: var(--r-pill);
  background: var(--glass-sheen), var(--fill);
  color: var(--text-2);
  font-size: var(--fs-subhead);
  font-weight: 500;
  transition: transform var(--dur-fast) var(--ease),
    background-color var(--dur-fast) var(--ease);
}
.glass-chip:active { transform: scale(.95); }
.glass-chip svg { width: 13px; height: 13px; opacity: .85; }

/* ---- states ----------------------------------------------------------- */
/* .glass-panel survives as a plain container: it used to be a blurred glass
   card, which made an error message look like a floating widget. */
.glass-panel { background: transparent; border: 0; box-shadow: none; }

.err-hero {
  min-height: 52vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: var(--sp-7) var(--sp-3);
}
.err-glyph {
  width: 72px;
  height: 72px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: var(--sp-4);
  color: var(--text-2);
  background: var(--glass-sheen), var(--surface);
  border: 1px solid var(--glass-edge);
  box-shadow: var(--shadow-pop);
}
.err-glyph svg { width: 32px; height: 32px; color: var(--accent); }
.err-title { font-size: var(--fs-title); font-weight: 700; letter-spacing: -.016em; color: var(--text); }
.err-sub {
  margin-top: 7px;
  max-width: 30em;
  font-size: var(--fs-subhead);
  line-height: var(--lh-relaxed);
  color: var(--text-2);
}
/* Centred CJK wraps badly on its own: a break can land between the two
   characters of a word, which is what left a two-character orphan on its own
   line.  `text-wrap: pretty` is the fix, but the shipped WebView is Chrome 113
   and only 117+ implements it - written bare it is silently dropped.  Behind
   @supports it costs nothing today and starts working when the system WebView
   updates; until then the copy keeps each <br> segment inside one line. */
@supports (text-wrap: pretty) {
  .err-sub { text-wrap: pretty; }
}
.err-actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
  justify-content: center;
  margin-top: var(--sp-5);
}
.err-actions .button { min-width: 132px; border-radius: var(--r-md); }
.err-link { margin-top: var(--sp-4); font-size: var(--fs-footnote); }

/* ---- inline proxy row in the error panel ------------------------------- */
/* The site's TLS is reset by the firewall from inside mainland networks
 * without a VPN, and the phone WiFi proxy path does not reach the WebView's
 * own network stack — so the error panel exposes an in-app proxy form that
 * pushes host+port into the JVM via the bridge. Lives inside `.err-hero`
 * like a small drawer: one line of state when closed, a two-field form
 * with a save-and-retry button when open. */
.ds-proxy {
  margin-top: var(--sp-4);
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-footnote);
  color: var(--text-2);
}
.ds-proxy-state {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--text);
  border: 1px solid var(--accent-line);
  font-variant-numeric: tabular-nums;
}
.ds-proxy-state-empty {
  background: var(--fill);
  color: var(--text-2);
  border-color: var(--sep);
}
.ds-proxy-clear {
  border: none;
  background: transparent;
  color: inherit;
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
  cursor: pointer;
  opacity: 0.7;
}
.ds-proxy-toggle {
  border: 1px solid var(--sep);
  background: var(--fill);
  color: var(--text);
  padding: 4px 10px;
  border-radius: 999px;
  font-size: var(--fs-footnote);
  cursor: pointer;
}
.ds-proxy-form {
  margin-top: var(--sp-3);
  display: grid;
  gap: var(--sp-2);
  width: min(320px, 100%);
  padding: var(--sp-3);
  border-radius: var(--r-md);
  background: var(--fill);
  border: 1px solid var(--sep);
  /* Subtle entrance: pops in rather than flashing into existence. */
  animation: ds-fade-in 180ms var(--ease) backwards;
}
.ds-proxy-field {
  display: grid;
  grid-template-columns: 56px 1fr;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-footnote);
  color: var(--text-2);
}
.ds-proxy-field input {
  width: 100%;
  padding: 8px 10px;
  border-radius: 10px;
  border: 1px solid var(--sep);
  background: var(--bg);
  color: var(--text);
  font: inherit;
  font-variant-numeric: tabular-nums;
  font-size: 14px;
  /* iOS / Android keyboards do not always honour inputmode; this nudges
   * numeric/host-typing layouts on the keyboards that do. */
  outline: none;
}
.ds-proxy-field input:focus {
  border-color: var(--accent-line);
}
.ds-proxy-hint {
  margin: 0;
  font-size: var(--fs-footnote);
  color: var(--text-2);
}
.ds-proxy-actions {
  display: flex;
  gap: var(--sp-2);
  justify-content: flex-end;
  margin-top: 2px;
}
.ds-proxy-actions .button {
  padding: 6px 14px;
  border-radius: 10px;
  font-size: var(--fs-footnote);
  min-width: 0;
}
@keyframes ds-fade-in {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ---- skeleton --------------------------------------------------------- */
/* One shimmer keyframe, shared: both pages drive their skeletons off it. */
@keyframes ds-skeleton { from { background-position: 100% 0; } to { background-position: -100% 0; } }
.sk {
  background-image: linear-gradient(100deg, var(--fill) 20%, var(--fill-strong) 38%, var(--fill) 56%);
  background-size: 220% 100%;
  animation: ds-skeleton 1.35s ease-in-out infinite;
}

#ds-grid > .live-skeleton-grid {
  column-span: all;
  display: grid;
  grid-column: 1 / -1;
  width: 100%;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, var(--card-min)), 1fr));
  gap: var(--sp-4);
  padding: 0;
  align-content: start;
}
.live-skeleton-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--sep);
  border-radius: var(--r-lg);
  background: var(--surface);
}
.live-skeleton-card::before {
  content: "";
  display: block;
  flex: 0 0 auto;
  aspect-ratio: var(--thumb-ratio);
}
.live-skeleton-card::before,
.live-skeleton-card i {
  background-image: linear-gradient(100deg, var(--fill) 20%, var(--fill-strong) 38%, var(--fill) 56%);
  background-size: 220% 100%;
  animation: ds-skeleton 1.35s ease-in-out infinite;
}
.live-skeleton-card i {
  display: block;
  height: 10px;
  flex: 0 0 10px;
  margin: 12px var(--sp-3) 0;
  border-radius: 5px;
}
.live-skeleton-card i:last-child { width: 54%; margin-bottom: 13px; }

.pl-skel { display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-2) 0 var(--sp-1); }
.pl-skel::before {
  content: "";
  display: block;
  flex: 0 0 32px;
  width: 32px;
  height: 32px;
  margin: var(--sp-4) auto var(--sp-5);
  border: 2px solid var(--accent-line);
  border-top-color: var(--accent);
  border-right-color: var(--accent-strong);
  border-radius: 50%;
  animation: skin-spin .85s linear infinite;
}
.pl-skel-row {
  /* Reuse the same shimmer as the discover page skeleton so the loading
   * rhythm looks the same.  Without a background the row was a 60px tall
   * transparent box - dark mode invisible, light mode washed into the
   * page - and on top of that the row lived inside a <p>, which the
   * parser auto-closes around the inner <div> so the wrapper went
   * `:empty { display: none }` and the skeleton never rendered at all.
   */
  height: 60px;
  border-radius: var(--r-lg);
  background-image: linear-gradient(100deg,
    var(--fill) 20%, var(--fill-strong) 38%, var(--fill) 56%);
  background-size: 220% 100%;
  animation: ds-skeleton 1.35s ease-in-out infinite;
}

.ds-status, .pl-status {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  margin: var(--sp-3) 0 0;
  font-size: var(--fs-footnote);
  color: var(--text-2);
  text-align: center;
}
.ds-status:empty, .pl-status:empty { display: none; }
.ds-status.is-loading::before, .pl-status.is-loading::before {
  content: "";
  flex: 0 0 16px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid var(--accent-line);
  border-top-color: var(--accent);
  border-right-color: var(--accent);
  animation: skin-spin .8s linear infinite;
}
@keyframes skin-pulse { 50% { opacity: .3; transform: scale(.7); } }

/* The launch mark is alive while data is loading: its four bars breathe in
   sequence inside a glass tile. No layout or blur is animated. */
.ds-splash .ds-splash-glyph {
  overflow: hidden;
  color: var(--accent);
  background: var(--glass-sheen), var(--surface-2);
  border: 1px solid var(--glass-edge);
  border-radius: 19px;
  box-shadow: var(--shadow-pop);
  animation: skin-float 2.4s var(--ease-in-out) infinite;
}
.ds-splash .ds-splash-glyph::after {
  content: "";
  position: absolute;
  inset: -40%;
  background: linear-gradient(110deg, transparent 35%, rgba(255,255,255,.16) 49%, transparent 63%);
  transform: translateX(-80%) rotate(10deg);
  animation: skin-glint 3s var(--ease-in-out) infinite;
  pointer-events: none;
}
.ds-splash-glyph rect {
  transform-box: fill-box;
  transform-origin: center;
  animation: skin-wave .95s ease-in-out infinite alternate;
}
.ds-splash-glyph rect:nth-child(2) { animation-delay: -.2s; }
.ds-splash-glyph rect:nth-child(3) { animation-delay: -.4s; }
.ds-splash-glyph rect:nth-child(4) { animation-delay: -.6s; }
@keyframes skin-wave { from { transform: scaleY(.4); opacity: .55; } to { transform: scaleY(1); opacity: 1; } }
@keyframes skin-float { 50% { transform: translateY(-3px); } }
@keyframes skin-glint { 55%, 100% { transform: translateX(80%) rotate(10deg); } }

/* =====================================================================
   5. MOTION
   Motion is what separates a list of things from an interface.  A card that
   is already on screen when you arrive needs no animation; a card that
   arrives *because you tapped* does, because otherwise the tap appears to
   have done nothing until the whole batch is there at once.

   Everything below is transform and opacity only, so it stays on the
   compositor: an animation that forces layout would drop frames exactly
   while images are decoding, which is when it is least affordable.
   ===================================================================== */
@keyframes skin-rise {
  from { opacity: 0; transform: translate3d(0, var(--enter-y), 0); }
  to   { opacity: 1; transform: translate3d(0, 0, 0); }
}

/* ---- arrival ---------------------------------------------------------
   Cards cascade in.  `backwards` is load-bearing: without it a card is fully
   painted during its own delay and then snaps back to the start of the
   animation, which is worse than no animation at all. */
.cover-card {
  animation: skin-rise var(--dur-enter) var(--ease) backwards;
  animation-delay: calc(min(var(--i, 0), 9) * var(--stagger));
}
/* Page-level arrivals: the error hero and the play page's hero. */
.err-hero, .pl-hero { animation: skin-rise var(--dur-enter) var(--ease) backwards; }
/* Source rows cascade like cards, one rank lower in the hierarchy. */
.play-enter {
  animation: skin-rise var(--dur-enter) var(--ease) backwards;
  animation-delay: calc(min(var(--i, 0), 7) * var(--stagger));
}

/* ---- press -----------------------------------------------------------
   Press-in is quicker than the release, on every tappable.  The finger wants
   the change immediately; the spring back is the part worth watching.  Each
   selector already declares its own transition, so this only overrides the
   duration - and only while the press is held. */
.icon-btn:active, .chip:active, .cover-card:active,
.button:active, .glass-chip:active, .play-enter:active {
  transition-duration: var(--dur-press);
}

/* ---- reduced motion ---------------------------------------------------
   Targeted rather than a blanket override, and it removes *travel* while
   keeping *feedback* - the same trade iOS's Reduce Motion makes.  Blanket
   overrides are wrong here for two reasons: the pull-to-refresh arc fill is a
   transition that carries information (the pull distance) and the page
   contracts pin it as staying, and killing every transition also kills the
   press states, which leaves a UI with no response to touch at all. */
@media (prefers-reduced-motion: reduce) {
  .sk, .live-skeleton-card::before, .live-skeleton-card i,
  .pl-skel-row, .ds-status.is-loading::before,
  .pl-status.is-loading::before { animation: none; }
  .icon-btn.is-busy svg, .ds-splash .ds-splash-glyph,
  .ds-splash-glyph rect, .ds-splash .ds-splash-glyph::after, .pl-skel::before,
  .play-enter.is-loading .src-ico::after,
  .play-enter[aria-busy="true"] .src-ico::after { animation: none; }
  .ds-splash .ds-splash-glyph::after { opacity: 0; }
  .play-enter:active .src-chev { transform: none; }
  /* No travel: the element is simply there, at its final position. */
  .cover-card, .err-hero, .pl-hero, .play-enter { animation: none; }
  .icon-btn:active, .chip:active, .cover-card:active,
  .button:active, .glass-chip:active, .play-enter:active { transform: none; }
  .button:active { opacity: .7; }
  /* The card's press answer used to be a border lighting up, but the card
     has no border anymore - so travel-free feedback is a dim of the cover. */
  .cover-card:active { opacity: .7; }
  /* A source switch moves the page: the chrome collapses by ~94px and the
     feed changes height under it. Reduce Motion means the rows are simply
     there, or not — so the collapse itself is dropped and only the fade is
     kept, because a fade is not travel. The pull arc is deliberately NOT
     listed: its transition reports the pull distance and stays. */
  .ds-search, .ds-lines { transition: opacity var(--dur) var(--ease); }
}
@media (prefers-reduced-transparency: reduce) {
  .nav {
    background: var(--nav-bg);
    -webkit-backdrop-filter: none;
    backdrop-filter: none;
  }
}
"""
