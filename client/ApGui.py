"""
FF1 PSP Archipelago client GUI -- the Tracker, Shops and Boost tabs.

Built on Archipelago's kvui GameManager (raw Kivy widgets, no .kv strings, no
KivyMD beyond what kvui itself uses). Layout:

  [ Archipelago | Tracker | Shops | Boost ]  <- kvui top-level tabs (add_client_tab)
    key-item strip                          <- pinned, always visible
    [ Summary | Start | Ship | Canal | ... ] <- our own wrapping sub-tab bar
      grid of area tiles

Reading a tile, which is the whole point of the thing:

  BRIGHT + full color  = in logic, checks remain. Go here.
  DIM + slashes        = out of logic. The face says what it needs.
  GRAYSCALE            = every check found. Nothing left; stop looking at it.
  (hidden)             = the seed's pool has no locations here at all.

All the reachability math lives in ff1psp/tracker.py (pure, headless-testable);
this file only paints it. Widgets are built once and then recolored in place --
`update_tracker` runs on every ReceivedItems, so it must not churn the canvas.

No emoji anywhere: labels are alphanumeric + keyboard symbols only.
"""

import logging

from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import (Color, Line, Rectangle, StencilPop, StencilPush,
                           StencilUnUse, StencilUse)
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.widget import Widget

from .. import tracker as TR

logger = logging.getLogger("Client")

# Pre-allocated slash count per tile. The slashes are created once and toggled by
# ALPHA ONLY -- adding/removing canvas instructions on every refresh is what makes
# naive trackers stutter. Must cover a tile's full diagonal span (w + h) / gap at
# the widest the window gets; excess lines just sit at zero alpha.
_SLASH_COUNT = 64
_SLASH_GAP = dp(14)

TILE_HEIGHT = dp(74)
TILE_COLS = 4
_STRIP_COLS = 10

# Key-item chip states.
CHIP_UNUSED = (0.18, 0.70, 0.33)     # held, nothing it unlocks has been checked
CHIP_USED = (0.30, 0.62, 0.95)       # held and cashed in
CHIP_MISSING = (0.17, 0.17, 0.19)

# Markup hex mirrors of the colors above (label text can't take an rgb tuple).
def _hex(rgb):
    return "".join(f"{int(round(c * 255)):02X}" for c in rgb)

UNUSED_HEX = _hex(CHIP_UNUSED)       # bright green -- "not used yet" / gate met
TOTALS_HEX = "C8C8C8"                # neutral grey -- default totals-line text


def _lum(rgb):
    return 0.30 * rgb[0] + 0.59 * rgb[1] + 0.11 * rgb[2]


def _grayscale(rgb, factor=0.55):
    g = _lum(rgb) * factor
    return (g, g, g)


# The one dark-grey every out-of-logic surface lands on. A FIXED dark base
# (Elfheim green) is grayscaled -- not the section's own color, because pale
# sections grayscaled to light-on-light = illegible.
_OUT_LOGIC_GREY = _grayscale(TR.SECTION_COLOR["elfheim"], 0.62)


class _BevelToggle(ToggleButton):
    """A ToggleButton that actually looks like a button: Kivy's stock background is
    disabled and replaced with a flat fill plus a two-tone bevel that inverts when
    pressed. Used for the sub-tab bar."""

    def __init__(self, base_rgb=(0.5, 0.5, 0.5), **kw):
        super().__init__(**kw)
        self._base_rgb = tuple(base_rgb)
        self.background_normal = ""
        self.background_down = ""
        self.background_disabled_normal = ""
        self.background_disabled_down = ""
        self.background_color = (0, 0, 0, 0)
        with self.canvas.before:
            self._bg_col = Color(*self._base_rgb, 1.0)
            self._bg_rect = Rectangle(pos=(0, 0), size=(0, 0))
            # In-logic shine: two anti-diagonal bands, clipped to the button, shown
            # only when the section behind this tab has reachable work left.
            StencilPush()
            self._clip_in = Rectangle(pos=(0, 0), size=(0, 0))
            StencilUse()
            self._shine_col = Color(1, 1, 1, 0.0)
            self._shine = [Line(points=[0, 0, 0, 0], width=dp(5)) for _ in range(2)]
            # Out-of-logic slashes, matching the tiles: lower-left -> upper-right,
            # shown only when the whole section behind this tab is out of logic.
            self._slash_col = Color(0, 0, 0, 0.0)
            self._slashes = [Line(points=[0, 0, 0, 0], width=dp(2))
                             for _ in range(_SLASH_COUNT)]
            StencilUnUse()
            self._clip_out = Rectangle(pos=(0, 0), size=(0, 0))
            StencilPop()
        self._default_color = tuple(self.color)
        with self.canvas.after:
            self._hi_col = Color(1, 1, 1, 0.5)
            self._hi_top = Line(points=[0, 0, 0, 0], width=2.0)
            self._hi_left = Line(points=[0, 0, 0, 0], width=2.0)
            self._sh_col = Color(0, 0, 0, 0.6)
            self._sh_bot = Line(points=[0, 0, 0, 0], width=2.0)
            self._sh_right = Line(points=[0, 0, 0, 0], width=2.0)
        self.bind(pos=self._redraw, size=self._redraw, state=self._redraw)

    def set_base(self, rgb):
        self._base_rgb = tuple(rgb)
        self._bg_col.rgba = (rgb[0], rgb[1], rgb[2], 1.0)

    def set_shine(self, on):
        # Dark shine on pale/bright tabs, white on dark ones -- same rule the tiles
        # use so the cue reads on every section color.
        if on:
            self._shine_col.rgba = ((0.12, 0.12, 0.12, 0.32) if _lum(self._base_rgb) > 0.62
                                    else (1, 1, 1, 0.28))
        else:
            self._shine_col.a = 0.0

    def set_out_logic(self, on):
        """Whole-section out-of-logic look: dark-grey face, slashes, white text --
        the tab-strip mirror of the out-of-logic tiles."""
        if on:
            self.set_base(_OUT_LOGIC_GREY)
            self.set_shine(False)
            self._slash_col.rgba = (0, 0, 0, 0.55)
            self.color = (1, 1, 1, 1)
        else:
            self._slash_col.rgba = (0, 0, 0, 0.0)
            self.color = self._default_color

    def _redraw(self, *_a):
        x, y, w, h = self.x, self.y, self.width, self.height
        self._bg_rect.pos = (x, y)
        self._bg_rect.size = (w, h)
        self._clip_in.pos = self._clip_out.pos = (x, y)
        self._clip_in.size = self._clip_out.size = (w, h)
        for i, ln in enumerate(self._shine):
            off = w * (0.20 + 0.18 * i)
            ln.points = [x + off, y + h, x + off - h, y]
        for i, ln in enumerate(self._slashes):
            off = -h + i * _SLASH_GAP
            ln.points = ([x + off, y, x + off + h, y + h] if off <= w
                         else [0, 0, 0, 0])
        self._hi_top.points = [x, y + h - 1, x + w, y + h - 1]
        self._hi_left.points = [x + 1, y, x + 1, y + h]
        self._sh_bot.points = [x, y + 1, x + w, y + 1]
        self._sh_right.points = [x + w - 1, y, x + w - 1, y + h]
        if self.state == "down":
            # Pressed: invert the bevel so the face reads as pushed in.
            self._hi_col.rgba = (0, 0, 0, 0.6)
            self._sh_col.rgba = (1, 1, 1, 0.35)
        else:
            self._hi_col.rgba = (1, 1, 1, 0.5)
            self._sh_col.rgba = (0, 0, 0, 0.6)


class _WrapTabBar(BoxLayout):
    """Tab bar that wraps across rows and swaps the content below it.

    Kivy's TabbedPanel only lays tabs out in a single row, which our nine sections
    would squeeze into unreadable slivers. This is a GridLayout of radio-grouped
    _BevelToggles plus a content holder."""

    def __init__(self, cols=5, btn_height=dp(40), **kw):
        super().__init__(orientation="vertical", spacing=dp(4), **kw)
        self._btn_height = btn_height
        self._group = f"wraptab_{id(self)}"
        self._buttons = []
        self._contents = []
        self._bar = GridLayout(cols=cols, spacing=dp(3), size_hint_y=None)
        self._bar.bind(minimum_height=self._bar.setter("height"))
        self._holder = BoxLayout()
        self._on_select = {}      # tab index -> callback fired when it is shown
        self.add_widget(self._bar)
        self.add_widget(self._holder)

    def add_tab(self, text, rgb, content, dark_text=False, on_select=None):
        idx = len(self._buttons)
        if on_select is not None:
            self._on_select[idx] = on_select
        btn = _BevelToggle(
            base_rgb=rgb, text=text, group=self._group, markup=True,
            size_hint_y=None, height=self._btn_height,
            color=((0, 0, 0, 1) if dark_text else (1, 1, 1, 1)),
        )
        btn.bind(on_press=lambda _b, i=idx: self.select(i))
        self._bar.add_widget(btn)
        self._buttons.append(btn)
        self._contents.append(content)
        if idx == 0:
            self.select(0)
        return idx

    def select(self, idx):
        if not (0 <= idx < len(self._contents)):
            return
        self._holder.clear_widgets()
        self._holder.add_widget(self._contents[idx])
        for i, b in enumerate(self._buttons):
            b.state = "down" if i == idx else "normal"
        cb = self._on_select.get(idx)
        if cb is not None:
            cb()

    def set_text(self, idx, text):
        if 0 <= idx < len(self._buttons):
            self._buttons[idx].text = text

    def set_base(self, idx, rgb):
        if 0 <= idx < len(self._buttons):
            self._buttons[idx].set_base(rgb)

    def set_shine(self, idx, on):
        if 0 <= idx < len(self._buttons):
            self._buttons[idx].set_shine(on)

    def set_out_logic(self, idx, on):
        if 0 <= idx < len(self._buttons):
            self._buttons[idx].set_out_logic(on)

    def set_enabled(self, idx, on):
        # A disabled ToggleButton swallows its own touches (kivy blocks on_touch_down
        # on disabled widgets), so a locked town tab can't be opened.
        if 0 <= idx < len(self._buttons):
            self._buttons[idx].disabled = not on


class _AreaTile(BoxLayout):
    """One area. Three stacked labels (name / counts / needs) over a hand-drawn
    background, with pre-allocated diagonal slashes for the locked look and a pair
    of anti-diagonal shine bands for the in-logic look.

    The slashes run lower-left -> upper-right and the shine anti-diagonally, so the
    two cues can never be mistaken for each other at a glance."""

    def __init__(self, area_key, display, base_rgb, on_press=None, **kw):
        super().__init__(orientation="vertical", padding=(dp(6), dp(4)),
                         spacing=dp(1), size_hint_y=None, height=TILE_HEIGHT, **kw)
        self.area_key = area_key
        self._base_rgb = tuple(base_rgb)
        self._on_press = on_press

        with self.canvas.before:
            self._bg_col = Color(*self._base_rgb, 1.0)
            self._bg_rect = Rectangle(pos=(0, 0), size=(0, 0))
            # Everything diagonal is drawn INSIDE a stencil clipped to the tile
            # rect. Kivy's canvas has no implicit clipping -- a Line simply paints
            # wherever its points say, so without this the shine bands and slashes
            # run out across neighbouring tiles.
            StencilPush()
            self._clip_in = Rectangle(pos=(0, 0), size=(0, 0))
            StencilUse()
            self._shine_col = Color(1, 1, 1, 0.0)
            self._shine = [Line(points=[0, 0, 0, 0], width=dp(6)) for _ in range(2)]
            self._slash_col = Color(0, 0, 0, 0.0)
            self._slashes = [Line(points=[0, 0, 0, 0], width=dp(2))
                             for _ in range(_SLASH_COUNT)]
            StencilUnUse()
            self._clip_out = Rectangle(pos=(0, 0), size=(0, 0))
            StencilPop()
        with self.canvas.after:
            self._edge_col = Color(1, 1, 1, 0.0)
            self._edge = Line(rectangle=(0, 0, 0, 0), width=dp(1.4))

        self.name_lbl = Label(text=display, markup=True, size_hint_y=None,
                              height=dp(20), halign="left", valign="middle",
                              shorten=True, shorten_from="right")
        self.count_lbl = Label(text="", markup=True, size_hint_y=None,
                               height=dp(18), halign="left", valign="middle")
        self.needs_lbl = Label(text="", markup=True, size_hint_y=None,
                               height=dp(16), halign="left", valign="middle",
                               shorten=True, shorten_from="right")
        for lbl in (self.name_lbl, self.count_lbl, self.needs_lbl):
            lbl.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
            self.add_widget(lbl)

        self.bind(pos=self._redraw, size=self._redraw)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self._on_press:
            self._on_press(self.area_key)
            return True
        return super().on_touch_down(touch)

    def _redraw(self, *_a):
        x, y, w, h = self.x, self.y, self.width, self.height
        self._bg_rect.pos = (x, y)
        self._bg_rect.size = (w, h)
        self._edge.rectangle = (x, y, w, h)
        self._clip_in.pos = self._clip_out.pos = (x, y)
        self._clip_in.size = self._clip_out.size = (w, h)
        # The stencil does the clipping, so these are drawn full-length and simply
        # sliced at the tile edge. Slashes run lower-left -> upper-right...
        for i, ln in enumerate(self._slashes):
            off = -h + i * _SLASH_GAP
            ln.points = ([x + off, y, x + off + h, y + h] if off <= w
                         else [0, 0, 0, 0])
        # ...and the shine anti-diagonally, so the two cues can't be confused.
        for i, ln in enumerate(self._shine):
            off = w * (0.18 + 0.16 * i)
            ln.points = [x + off, y + h, x + off - h, y]

    def apply(self, area):
        """Recolor + relabel from a tracker.AreaState. Idempotent, canvas-stable."""
        base = self._base_rgb
        state = area.state

        if state == TR.CLEARED:
            # Fully checked: kill the color entirely. A cleared area should fall out
            # of your attention, not merely rank lower in it.
            self._bg_col.rgba = (0.30, 0.30, 0.30, 1.0)
            self._shine_col.a = 0.0
            self._slash_col.rgba = (0, 0, 0, 0.0)
            self._edge_col.rgba = (1, 1, 1, 0.0)
            name_c, body_c = "B0B0B0", "8C8C8C"
        elif state == TR.IN_LOGIC:
            self._bg_col.rgba = (base[0], base[1], base[2], 1.0)
            # Shine only until the player checks something here: once any location
            # in this area is found, the "go here first" cue has done its job.
            if area.found == 0:
                # White shine washes out on our pale sections; use a dark band there.
                shine_dark = _lum(base) > 0.62
                self._shine_col.rgba = ((0.15, 0.15, 0.15, 0.35) if shine_dark
                                       else (1, 1, 1, 0.22))
            else:
                self._shine_col.a = 0.0
            self._slash_col.rgba = (0, 0, 0, 0.0)
            self._edge_col.rgba = (1, 1, 1, 0.85)
            dark_text = _lum(base) > 0.62
            name_c, body_c = (("000000", "202020") if dark_text
                              else ("FFFFFF", "E8E8E8"))
        else:  # OUT_LOGIC
            # Shared dark-grey (see _OUT_LOGIC_GREY); slashes stay as the cue.
            r, g, b = _OUT_LOGIC_GREY
            self._bg_col.rgba = (r, g, b, 1.0)
            self._shine_col.a = 0.0
            self._slash_col.rgba = (0, 0, 0, 0.55)
            self._edge_col.rgba = (0, 0, 0, 0.0)
            name_c, body_c = "FFFFFF", "C8C8C8"

        self.name_lbl.text = f"[color={name_c}][b]{area.display}[/b][/color]"

        counts = f"{area.found}/{area.total}"
        if area.mixed:
            # In logic but partly gated: say how much is actually reachable so the
            # count doesn't over-promise.
            counts += f"   ({area.open_now} in logic)"
        self.count_lbl.text = f"[color={body_c}]{counts}[/color]"

        if state == TR.OUT_LOGIC and area.needs:
            self.needs_lbl.text = (f"[color={body_c}]needs: "
                                   f"{TR.short_reqs(area.needs)}[/color]")
        elif area.mixed and area.needs:
            self.needs_lbl.text = (f"[color={body_c}]{area.gated} gated: "
                                   f"{TR.short_reqs(area.needs)}[/color]")
        elif state == TR.CLEARED:
            self.needs_lbl.text = f"[color={body_c}]cleared[/color]"
        else:
            self.needs_lbl.text = ""


class _KeyItemChip(Label):
    """One cell of the pinned key-item strip. Three states:

      GREEN  held, and nothing it unlocks has been checked yet -> go spend it
      BLUE   held and already cashed in
      GRAY   not held

    Stretches horizontally with its row (size_hint_x=1), so the strip tracks the
    window width the way the sub-tab bar does instead of sitting at a fixed size.
    """

    def __init__(self, label, **kw):
        super().__init__(text=label, markup=True, size_hint=(1, None),
                         height=dp(20), halign="center",
                         valign="middle", font_size=dp(10), **kw)
        # NOT self._label: kivy's Label uses that name for its internal CoreLabel,
        # and shadowing it breaks texture_update with a bare AttributeError.
        self._chip_text = label
        with self.canvas.before:
            self._col = Color(0.22, 0.22, 0.24, 1.0)
            self._rect = Rectangle(pos=(0, 0), size=(0, 0))
        self.bind(pos=self._redraw, size=self._redraw)
        self.text_size = self.size

    def _redraw(self, *_a):
        self._rect.pos = self.pos
        self._rect.size = self.size
        self.text_size = self.size

    def set_state(self, held, unused):
        if not held:
            self._col.rgba = CHIP_MISSING + (1.0,)
            self.text = f"[color=6A6A6A]{self._chip_text}[/color]"
            return
        rgb = CHIP_UNUSED if unused else CHIP_USED
        self._col.rgba = rgb + (1.0,)
        self.text = f"[color=FFFFFF][b]{self._chip_text}[/b][/color]"


# ---------------------------------------------------------------- Boost tab --
# Intensity ramp along a slider's travel: left = mildest, right = most extreme.
# Deliberately NOT a good/bad scale -- "5x gil" and "2x bosses" both sit on the
# red end because both are the far edge of their knob, not because either is bad.
_RAMP_STOPS = ((0.18, 0.56, 0.32), (0.78, 0.68, 0.20), (0.74, 0.26, 0.22))

# Fraction of the unlock slider's travel that counts as unlocked. High enough that
# a stray brush of the track can't open it, low enough to be reachable in one drag.
_UNLOCK_AT = 0.85

BOOST_LOCKED_RGB = (0.30, 0.30, 0.33)
BOOST_OPEN_RGB = (0.16, 0.44, 0.26)


def _ramp_at(frac):
    """Color at `frac` (0..1) along _RAMP_STOPS (piecewise-linear)."""
    t = min(1.0, max(0.0, frac)) * (len(_RAMP_STOPS) - 1)
    lo = min(int(t), len(_RAMP_STOPS) - 2)
    f = t - lo
    a, b = _RAMP_STOPS[lo], _RAMP_STOPS[lo + 1]
    return tuple(a[j] + (b[j] - a[j]) * f for j in range(3))


def _fmt_mult(v):
    """A multiplier as the row titles show it, in percent: 'off' / '100%' / '35%'.
    Matches the yaml options, which are all percentages (e.g. 200 = x2)."""
    if v <= 0:
        return "off"
    return f"{v * 100:g}%"


# Point size of the live value in a slider's title line. Kivy markup [size=] takes
# a bare number in PIXELS, so this has to be dp()-converted here rather than left as
# a dp string -- on a high-DPI display an unconverted 17 would come out tiny.
_VALUE_SIZE = int(dp(17))


def _fmt_bound(v):
    """A slider end cap, in percent ('off ---- 500%')."""
    return "off" if v <= 0 else f"{v * 100:g}%"


# ---- Boss Power danger zones -------------------------------------------------
# These are NOT round numbers picked for feel. boot_patch.scale_boss_stats scales a
# boss's attack LINEARLY INTO A BYTE and its HP linearly into a u16, so each of the
# 39 bosses has its own cap multiplier -- 255/attack and 65535/hp -- and those smear
# all the way from x1.16 to x17. There is no single breakpoint to mark.
#
# What IS one boundary rather than 39 is the SHAPE of that spread, measured off
# ff1_data.MONSTER_STATS_BLOCK (2026-07-30, 39 boss records):
#
#     mult   attack-capped   HP overflowed into extra hits/turn
#     x1.5       3/39                0/39
#     x2.0       7/39                9/39
#     x3.0      16/39               11/39
#     x4.0      20/39               13/39
#     x5.0      26/39               14/39
#
# The HARDEST bosses cap FIRST (the 220-attack record clamps at x1.16), so the whole
# endgame tier is attack-capped by x2.25 -- the last of them, at attack 115, caps at
# x2.22 -- past which the knob stops making bosses hit harder and starts making them
# hit MORE OFTEN and soak longer. x4 is where that is true of half the roster. Hence:
BOSS_CAUTION_AT = 2.25   # top-tier attack fully capped; HP overflow has begun
BOSS_EXTREME_AT = 4.0    # half of ALL bosses attack-capped
# test_boost_zones.py re-derives both off the live table, so a table edit that moves
# these cannot pass silently.

def _boss_danger_note(v):
    """The advice line under the Boss Power slider, in the terms the scaling code
    actually works in rather than a vague 'this is hard'."""
    if v < 1.0:
        return ("Below 100% bosses are weaker than the game was balanced around.")
    if v < BOSS_CAUTION_AT:
        return ("100% to 115% is a tough but fair fight. Everything up to 225% "
                "scales with more damage and defense.")
    if v < BOSS_EXTREME_AT:
        return ("[color=E0C060]Caution:[/color] past 225% the strongest bosses "
                "have hit their attack cap, so more power arrives as HP and extra "
                "attacks.")
    return ("[color=F08080]Extreme:[/color] above 400% half of all bosses are "
            "attack-capped and the toughest gain extra hits per turn. Expect very "
            "long fights and heavy grinding.")


# ---- Monster Power danger zones ----------------------------------------------
# Same scaling code (boot_patch.scale_monster_stats == the boss math on the
# non-boss records), but the regular roster is a different shape, so the boss
# thresholds do not carry over -- measured off ff1_data.MONSTER_STATS_BLOCK
# (2026-08-04, 165 non-boss records):
#
#   * the strongest mob has vanilla attack 128 (caps at x1.99); the whole
#     top tier (attack >= 100, seven mobs) is attack-capped by x2.55.
#   * mid tier too (attack >= 60) is fully capped by x4.25.
#   * HP NEVER overflows the u16 -- regular HP is small -- so, unlike bosses,
#     no extra-hits-per-turn effect exists; past the cap the extra power lands
#     on defense and magic defense instead.
#
# So the two boundaries the note marks are attack-cap fronts, not HP overflow:
MONSTER_CAUTION_AT = 2.55   # every top-tier (attack >= 100) mob attack-capped
MONSTER_EXTREME_AT = 4.25   # every mid-tier (attack >= 60) mob attack-capped
# test_boost_zones.py re-derives both off the live table, so a table edit that
# moves them cannot pass silently.


def _monster_danger_note(v):
    """The advice line under the Monster Power slider, in the terms the scaling
    code actually works in."""
    if v < 1.0:
        return ("Below 100% regular enemies are weaker than the game was balanced "
                "around.")
    if v < MONSTER_CAUTION_AT:
        return ("100% to 255% scales every non-boss enemy with more damage and "
                "defense.")
    if v < MONSTER_EXTREME_AT:
        return ("[color=E0C060]Caution:[/color] past 255% the strongest regular "
                "enemies have hit their attack cap, so more power arrives as "
                "defense rather than damage.")
    return ("[color=F08080]Extreme:[/color] above 425% even mid-tier enemies are "
            "attack-capped; further power mostly makes every fight tankier and "
            "longer, not deadlier.")


class _CommitSlider(Slider):
    """A Slider that reports the value ONCE, when the drag ends.

    Every one of these drives a RAM write, so committing on `value` would fire a
    write per pixel of travel. The readout still follows the handle live (the row
    binds `value` for that); only the write waits for the release. A programmatic
    `value` write -- a repaint from the client's own numbers -- must NOT commit, so
    the trigger is the touch itself, not the value change."""

    def __init__(self, on_commit=None, **kw):
        super().__init__(**kw)
        self._on_commit = on_commit
        self._dragging = False

    def on_touch_down(self, touch):
        r = super().on_touch_down(touch)
        if r:
            self._dragging = True
        return r

    def on_touch_up(self, touch):
        r = super().on_touch_up(touch)
        # Slider ungrabs the touch inside its own on_touch_up, so grab_current is
        # already None here -- the drag flag set above is what tells us this release
        # belongs to us.
        if self._dragging:
            self._dragging = False
            if self._on_commit:
                self._on_commit(self.value)
        return r


class _BoostSlider(BoxLayout):
    """One stat as a snapped slider: title + live readout above, track below.

    The handle snaps to `step`, so there is no way to land on a value the game
    cannot take, and the filled part of the track carries the same green -> red
    intensity ramp the rest of the tab uses. `set_enabled(False)` greys the row and
    blocks its touches (a disabled kivy widget never sees on_touch_down), which is
    what holds the locked xp/gil/boss rows shut."""

    def __init__(self, title, lo, hi, step, on_commit, note="", tick=0.5,
                 note_fn=None, **kw):
        # `note_fn(value)` replaces a static note with a live one (the Monster/
        # Boss Power rows use it for their caution/extreme advice lines).
        h = dp(26) + dp(36) + dp(4) + (dp(30) if (note or note_fn) else 0)
        super().__init__(orientation="vertical", size_hint_y=None, height=h,
                         spacing=dp(2), **kw)
        self._title = title
        self._lo, self._hi = lo, hi
        self._tick = tick
        self._on_commit = on_commit
        self._note_fn = note_fn
        self._enabled = True
        # The value the GAME is running, as last reported by the client -- which is
        # NOT always a value this slider can represent: slot_data carries yaml
        # percentages, so a seed can boot at x0.35 on a 0.5-step slider. The readout
        # shows this number and the handle sits on the nearest notch, rather than
        # rounding the readout and claiming a rate the game isn't using.
        self._real = None
        self._last_geo = None       # last (x, width, center_y) the ticks were laid
                                    # out against; see sync_geometry

        self.title_lbl = Label(text="", markup=True, size_hint_y=None,
                               height=dp(26), halign="left", valign="middle")
        self.title_lbl.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
        self.add_widget(self.title_lbl)

        self.slider = _CommitSlider(on_commit=self._commit, min=lo, max=hi,
                                    step=step, value=lo, size_hint_y=None,
                                    height=dp(36), cursor_size=(dp(20), dp(20)),
                                    value_track=True, value_track_width=dp(4))
        # Tick VALUES, on the absolute `tick` grid plus both ends -- not `lo + n*tick`,
        # which on the boss slider (lo 0.1) would march 0.1 / 0.6 / 1.1 and put no
        # tick on a single whole multiplier.
        marks = {round(lo, 4), round(hi, 4)}
        k = 1
        while k * tick < hi:
            if k * tick > lo:
                marks.add(round(k * tick, 4))
            k += 1
        self._tick_vals = sorted(marks)
        # canvas.AFTER, in a band BELOW the track: canvas.before drew them behind the
        # slider, where the filled part of the track and the 20 px handle hid whichever
        # ticks they happened to cover -- so a perfectly even row of 11 read as a
        # ragged 6. Every tick is the same height for the same reason; whole
        # multipliers are picked out by weight and brightness instead, which cannot
        # make the spacing look uneven.
        with self.canvas.after:
            self._tick_col = Color(1, 1, 1, 0.30)
            self._ticks = [Line(points=[0, 0, 0, 0], width=dp(1))
                           for _ in self._tick_vals]
        self.slider.bind(value=self._on_value)

        # End caps: the range, spelled out, so the ceiling is visible without
        # dragging to find it. Real Labels flanking the track rather than text drawn
        # on the canvas -- the layout keeps them pinned to the track's actual ends,
        # with no geometry of our own to keep in sync.
        track = BoxLayout(orientation="horizontal", size_hint_y=None,
                          height=dp(36), spacing=dp(4))
        self._lo_lbl = self._bound_label(_fmt_bound(lo), "right")
        self._hi_lbl = self._bound_label(_fmt_bound(hi), "left")
        track.add_widget(self._lo_lbl)
        track.add_widget(self.slider)
        track.add_widget(self._hi_lbl)
        self.add_widget(track)
        # Bind the SLIDER's geometry, not the row's: the row is laid out first and
        # the slider gets its final pos a pass later, so ticks drawn off the row's
        # own resize would sit at the previous frame's coordinates. The tab's
        # visibility tick calls sync_geometry() as a backstop -- a tab that is built
        # while its Screen is detached never gets a layout pass at all, and its
        # sliders would otherwise keep the default 100 px width the ticks were first
        # laid out against.
        self.slider.bind(pos=self._redraw, size=self._redraw)

        self.note_lbl = None
        if note or note_fn:
            # Two lines' worth of height: the live Boss Power note is a full sentence
            # and has to be able to wrap without shoving the next row down.
            self.note_lbl = Label(text="", markup=True, size_hint_y=None,
                                  height=dp(30), font_size=dp(11),
                                  halign="left", valign="top")
            self.note_lbl.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
            self.add_widget(self.note_lbl)
        self._static_note = note

        self._paint_title()
        self._paint_track()
        self._paint_note()

    @staticmethod
    def _bound_label(text, halign):
        lbl = Label(text=text, markup=True, size_hint_x=None, width=dp(30),
                    font_size=dp(11), halign=halign, valign="middle")
        lbl.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
        return lbl

    def sync_geometry(self):
        """Re-lay the ticks if the track has moved or resized since the last pass.
        Cheap enough to poll: a no-op unless the geometry actually changed."""
        s = self.slider
        geo = (s.x, s.width, s.center_y)
        if geo != self._last_geo:
            self._last_geo = geo
            self._redraw()

    def _redraw(self, *_a):
        s = self.slider
        self._last_geo = (s.x, s.width, s.center_y)
        # Mirror the slider's own geometry: the handle travels between x+padding
        # and x+width-padding, so the ticks have to use the same inset or they
        # would not line up with the values they mark.
        pad = s.padding
        x0, x1 = s.x + pad, s.right - pad
        span = max(1e-9, self._hi - self._lo)

        def vx(v):
            return x0 + (x1 - x0) * ((v - self._lo) / span)

        # A band under the track, clear of both the 4 px fill and the 20 px handle
        # (which reaches cy - dp(10)), so no tick can be covered up.
        y0, y1 = s.center_y - dp(17), s.center_y - dp(11)
        for i, ln in enumerate(self._ticks):
            fx = vx(self._tick_vals[i])
            ln.points = [fx, y0, fx, y1]
            # Whole multipliers read heavier -- by WEIGHT, not height, so the comb
            # stays visibly even.
            v = self._tick_vals[i]
            ln.width = dp(1.6) if abs(v - round(v)) < 1e-6 else dp(1)

    def _on_value(self, _w, _val):
        # Fires on both a drag and a programmatic move; either way only the readout
        # and the track color follow. The write itself waits for _commit.
        self._paint_title()
        self._paint_track()
        self._paint_note()

    def _paint_note(self):
        if self.note_lbl is None:
            return
        if self._note_fn is not None:
            body = self._note_fn(self.slider.value)
        else:
            body = self._static_note
        self.note_lbl.text = f"[color=8C8C8C]{body}[/color]"

    def _commit(self, val):
        if self._enabled and self._on_commit:
            self._on_commit(val)

    def _paint_track(self):
        frac = (self.slider.value - self._lo) / max(1e-9, self._hi - self._lo)
        rgb = _ramp_at(frac)
        if not self._enabled:
            rgb = _grayscale(rgb)
        self.slider.value_track_color = rgb + (1.0,)
        self._tick_col.rgba = (1, 1, 1, 0.30 if self._enabled else 0.10)
        cap = (0.62, 0.62, 0.64, 1) if self._enabled else (0.34, 0.34, 0.36, 1)
        self._lo_lbl.color = self._hi_lbl.color = cap

    def _paint_title(self):
        # The running value is the thing a player actually looks for on this tab, so
        # it is the biggest text in the row -- the stat NAME is the caption, not the
        # headline. _VALUE_SIZE is in px because kivy markup [size=] takes no unit.
        col = TOTALS_HEX if self._enabled else "6E6E6E"
        name = "FFFFFF" if self._enabled else "8C8C8C"
        live = "--" if self._real is None else _fmt_mult(self._real)
        text = (f"[color={name}][b]{self._title}[/b][/color]   "
                f"[size={_VALUE_SIZE}][b][color={col}]{live}[/color][/b][/size]")
        # Mid-drag the handle is a PROPOSAL, not the running value. Show both so the
        # readout never claims a setting the game hasn't been given yet.
        held = self.slider.value
        if self._real is None or abs(held - self._real) > 1e-9:
            pending = _fmt_mult(held)
            if self.slider._dragging:
                text += (f"   [color=8C8C8C]->[/color] [size={_VALUE_SIZE}][b]"
                         f"[color={UNUSED_HEX}]{pending}[/color][/b][/size]"
                         f"   [color=8C8C8C](release to apply)[/color]")
            elif self._real is not None:
                # Off-grid running value (a yaml x0.35 on a 0.5-step slider): the
                # handle can only sit on the nearest notch, so say which it is.
                text += f"   [color=8C8C8C](handle at {pending})[/color]"
        self.title_lbl.text = text

    def set_current(self, value):
        """Take the client's value: readout exact, handle on the nearest notch.
        Commits nothing -- this is a repaint, not a user action."""
        if value is None:
            return
        if self.slider._dragging:
            return          # the player owns the handle right now; don't yank it
        changed = self._real is None or abs(self._real - float(value)) > 1e-9
        self._real = float(value)
        v = min(self._hi, max(self._lo, self._real))
        step = self.slider.step or 0
        if step:
            v = self._lo + round((v - self._lo) / step) * step
        if abs(self.slider.value - v) > 1e-9:
            self.slider.value = v       # -> _on_value repaints (no commit: no touch)
        elif changed:
            self._paint_title()         # handle already right, readout was not

    def set_enabled(self, on):
        if on == self._enabled:
            return
        self._enabled = on
        self.slider.disabled = not on
        self.slider.opacity = 1.0 if on else 0.45
        self._paint_title()
        self._paint_track()


class _UnlockSlider(Slider):
    """The Boost tab's lock. Drag (or click) past _UNLOCK_AT to open the xp / gil /
    boss rows; anything short of that snaps back to zero on release, so a
    half-hearted nudge is not an unlock."""

    def on_touch_up(self, touch):
        r = super().on_touch_up(touch)
        if self.collide_point(*touch.pos) or touch.grab_current is self:
            self.value = 1.0 if self.value >= _UNLOCK_AT else 0.0
        return r


class _LockBar(BoxLayout):
    """The lock over the xp / gil / boss rows, built to read as a physical switch:
    a labelled OFF..ON track with the two end states named, a heading that says
    which state it is in, and a face that goes green once open.

    Deliberately the tallest thing on the tab. It is the one control that decides
    whether the others can be touched at all, and it re-closes every time the tab is
    left, so it has to be findable at a glance rather than looking like a fifth
    stat slider."""

    def __init__(self, on_change, **kw):
        super().__init__(orientation="horizontal", size_hint_y=None,
                         height=dp(50), spacing=dp(14),
                         padding=(dp(12), dp(5)), **kw)
        self._on_change = on_change
        self._unlocked = False
        with self.canvas.before:
            self._col = Color(*BOOST_LOCKED_RGB, 1.0)
            self._rect = Rectangle(pos=(0, 0), size=(0, 0))
        with self.canvas.after:
            # A bright edge is the cheapest "this is a control, not a banner" cue,
            # and it doubles as the open/closed signal on a colorblind-safe axis
            # (edge brightness) rather than on hue alone.
            self._edge_col = Color(1, 1, 1, 0.18)
            self._edge = Line(rectangle=(0, 0, 0, 0), width=dp(1.4))
        self.bind(pos=self._redraw, size=self._redraw)

        # LEFT: the switch itself -- OFF / track / ON, stacked so the end labels sit
        # under the ends they name.
        sw = BoxLayout(orientation="vertical", size_hint_x=None, width=dp(230),
                       spacing=dp(2))
        self.slider = _UnlockSlider(min=0.0, max=1.0, value=0.0, step=0.01,
                                    size_hint_y=None, height=dp(24),
                                    cursor_size=(dp(20), dp(20)),
                                    value_track=True, value_track_width=dp(6),
                                    value_track_color=(0.35, 0.85, 0.45, 0.85))
        self.slider.bind(value=self._on_value)
        sw.add_widget(self.slider)
        ends = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(12))
        self._off_lbl = Label(text="", markup=True, font_size=dp(11),
                              halign="left", valign="middle")
        self._on_lbl = Label(text="", markup=True, font_size=dp(11),
                             halign="right", valign="middle")
        for lbl in (self._off_lbl, self._on_lbl):
            lbl.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
            ends.add_widget(lbl)
        sw.add_widget(ends)
        self.add_widget(sw)

        # RIGHT: state heading over its explanation.
        text = BoxLayout(orientation="vertical", spacing=dp(1))
        self.head_lbl = Label(text="", markup=True, halign="left", valign="bottom",
                              size_hint_y=None, height=dp(18))
        self.body_lbl = Label(text="", markup=True, halign="left", valign="top",
                              font_size=dp(12))
        for lbl in (self.head_lbl, self.body_lbl):
            lbl.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
            text.add_widget(lbl)
        self.add_widget(text)
        self._paint()

    def _redraw(self, *_a):
        self._rect.pos = self.pos
        self._rect.size = self.size
        self._edge.rectangle = (self.x, self.y, self.width, self.height)

    def _on_value(self, _w, val):
        want = val >= _UNLOCK_AT
        if want != self._unlocked:
            self._unlocked = want
            self._paint()
            if self._on_change:
                self._on_change(want)

    def _paint(self):
        head = int(dp(14))
        if self._unlocked:
            self._col.rgba = BOOST_OPEN_RGB + (1.0,)
            self._edge_col.rgba = (1, 1, 1, 0.55)
            self.head_lbl.text = (f"[size={head}][b][color={UNUSED_HEX}]"
                                  f"UNLOCKED[/color][/b][/size]")
            self.body_lbl.text = ("[color=C8C8C8]XP / Gil / Monster / Boss are live. "
                                  "Relocks by itself when you leave this tab.[/color]")
            self._off_lbl.text = "[color=6E6E6E]LOCKED[/color]"
            self._on_lbl.text = f"[color={UNUSED_HEX}][b]UNLOCKED[/b][/color]"
        else:
            self._col.rgba = BOOST_LOCKED_RGB + (1.0,)
            self._edge_col.rgba = (1, 1, 1, 0.18)
            self.head_lbl.text = (f"[size={head}][b][color=E0C060]"
                                  f"LOCKED[/color][/b][/size]")
            self.body_lbl.text = ("[color=9AA0A6]Slide (or click) the switch right "
                                  "to change XP / Gil / Monster / Boss.[/color]")
            self._off_lbl.text = "[color=E0C060][b]LOCKED[/b][/color]"
            self._on_lbl.text = "[color=6E6E6E]UNLOCKED[/color]"

    @property
    def unlocked(self):
        return self._unlocked

    def relock(self):
        """Force closed. Idempotent, and fires on_change only on a real transition
        (the slider write goes through _on_value)."""
        self.slider.value = 0.0
        if self._unlocked:      # value was already 0 but state disagreed
            self._unlocked = False
            self._paint()
            if self._on_change:
                self._on_change(False)


class TrackerMixin:
    """The Tracker tab, as a mixin over whatever kvui GameManager subclass this
    Archipelago build hands us (see make_manager). Mixing in rather than
    subclassing kvui.GameManager directly means we inherit that build's
    logging_pairs / base_title / log panels instead of re-deriving them, which is
    exactly the sort of thing that silently rots across AP versions."""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._tracker_tab = None
        self._tabbar = None
        self._tiles = {}          # area key -> _AreaTile
        self._section_grids = {}  # section key -> GridLayout
        self._section_shown = {}  # section key -> tuple of currently-attached keys
        self._section_tab_idx = {}   # section key -> tab index
        self._summary_grid = None
        self._summary_tiles = {}  # area key -> _AreaTile (Summary's own copies)
        self._summary_empty = None
        self._chips = {}          # item/token name -> _KeyItemChip
        self._totals_lbl = None
        self._crystals_lbl = None   # right side of totals line: "Crystals x/n"
        self._tablets_lbl = None    # right side of totals line: "Lute Tablets x/n"
        self._runes_lbl = None      # right side of totals line: "Runes x/n"
        self._shards_lbl = None     # right side of totals line: "Levi Shards x/n"
        self._last_state = None
        # Shops tab (top-level sibling of Tracker)
        self._shops_tab = None
        self._shops_tabbar = None
        self._shops_note = None
        self._shops_town_content = {}   # city id -> the town's column row
        self._shops_town_idx = {}       # city id -> tab index
        self._shops_town_last = {}      # city id -> last painted town dict
        self._shops_search = None       # the search TextInput
        self._shops_find_box = None     # Find tab's result column
        self._shops_query = ""
        self._search_focus_evts = []    # pending re-focus Clock events
        self._shops_expanded = set()    # (city, shop) groups opened by the user
        self._last_shops = None
        # Boost tab (top-level sibling of Tracker/Shops)
        self._boost_tab = None
        self._boost_rows = {}           # stat key -> _BoostSlider
        self._boost_lock = None         # _LockBar over the xp/gil/boss rows
        self._boost_status = None       # last action's result
        self._boost_attach_lbl = None   # PPSSPP bridge state (repainted per second)
        self._last_boost = None
        self._boost_visible = None      # last seen visibility, for the relock edge
        self._boost_sm_bound = False     # ScreenManager relock hook installed yet

    # ---------------------------------------------------------------- build --
    def on_start(self):
        super().on_start()
        # add_client_tab has to run on the Kivy main thread while the tab bar is
        # still being laid out; building eagerly here (with empty tiles that later
        # fill in) is the only reliably-supported timing. Deferring it to the first
        # data arrival silently no-ops on some kvui versions.
        try:
            self.build_tracker_tab()
        except Exception as ex:
            logger.warning(f"Could not build the Tracker tab: {ex}")
        try:
            self.build_shops_tab()
        except Exception as ex:
            logger.warning(f"Could not build the Shops tab: {ex}")
        try:
            self.build_boost_tab()
        except Exception as ex:
            logger.warning(f"Could not build the Boost tab: {ex}")
        if self._last_state is not None:
            self._paint(self._last_state)
        if self._last_shops is not None:
            self._paint_shops(self._last_shops)
        if self._last_boost is not None:
            self._paint_boost(self._last_boost)

    def build_tracker_tab(self):
        if self._tracker_tab is not None:
            return
        root = BoxLayout(orientation="vertical", padding=dp(6), spacing=dp(5))

        root.add_widget(self._build_key_strip())

        self._tabbar = _WrapTabBar(cols=5, btn_height=dp(38))
        root.add_widget(self._tabbar)

        # Summary first: the one tab that answers "where do I go now".
        self._tabbar.add_tab("Summary", TR.SECTION_COLOR[TR.SUMMARY],
                             self._build_summary(),
                             dark_text=(TR.SUMMARY in TR.BRIGHT_SECTIONS))
        self._section_tab_idx[TR.SUMMARY] = 0

        for key, display, rgb in TR.SECTIONS:
            if key == TR.SUMMARY:
                continue
            idx = self._tabbar.add_tab(
                display, rgb, self._build_section(key),
                dark_text=(key in TR.BRIGHT_SECTIONS))
            self._section_tab_idx[key] = idx

        self.add_client_tab("Tracker", root)
        self._tracker_tab = root

    def _build_key_strip(self):
        box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(2))
        box.bind(minimum_height=box.setter("height"))
        # One grid for the whole strip. A GridLayout splits its width evenly across
        # `cols` regardless of how many cells the last row holds, so with
        # size_hint_x=1 chips the columns stay aligned and the strip spans the full
        # window width and wraps to as many rows as it needs.
        grid = GridLayout(cols=_STRIP_COLS, spacing=dp(2), size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))
        for name, label in TR.STRIP_ITEMS:
            chip = _KeyItemChip(label)
            self._chips[name] = chip
            grid.add_widget(chip)
        box.add_widget(grid)
        # Totals line: "Checks x/y ..." on the left, then right-aligned
        # "Crystals x/n", "Lute Tablets x/n" and "Runes x/n" gate readouts. One
        # horizontal row. Runes lives here because the in-game Key Items line
        # cannot be shown inside Whisperwind Cove (it borrows a key slot the
        # robot-part minigame owns) -- this readout is always true and always
        # visible, including on an ISO with no padded slot at all.
        line = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(18))
        self._totals_lbl = Label(text="", markup=True, halign="left",
                                 valign="middle")
        self._totals_lbl.bind(
            size=lambda w, _v: setattr(w, "text_size", w.size))
        self._crystals_lbl = Label(text="", markup=True, halign="right",
                                   valign="middle", size_hint_x=None, width=dp(110))
        self._crystals_lbl.bind(
            size=lambda w, _v: setattr(w, "text_size", w.size))
        self._tablets_lbl = Label(text="", markup=True, halign="right",
                                  valign="middle", size_hint_x=None, width=dp(150))
        self._tablets_lbl.bind(
            size=lambda w, _v: setattr(w, "text_size", w.size))
        self._runes_lbl = Label(text="", markup=True, halign="right",
                                valign="middle", size_hint_x=None, width=dp(110))
        self._runes_lbl.bind(
            size=lambda w, _v: setattr(w, "text_size", w.size))
        self._shards_lbl = Label(text="", markup=True, halign="right",
                                 valign="middle", size_hint_x=None, width=dp(150))
        self._shards_lbl.bind(
            size=lambda w, _v: setattr(w, "text_size", w.size))
        line.add_widget(self._totals_lbl)
        line.add_widget(self._crystals_lbl)
        line.add_widget(self._tablets_lbl)
        line.add_widget(self._runes_lbl)
        line.add_widget(self._shards_lbl)
        box.add_widget(line)
        return box

    def _tile_grid(self):
        grid = GridLayout(cols=TILE_COLS, spacing=dp(4), padding=(0, dp(2)),
                          size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))
        sv = ScrollView(do_scroll_x=False)
        sv.add_widget(grid)
        return sv, grid

    def _build_section(self, section_key):
        sv, grid = self._tile_grid()
        rgb = TR.SECTION_COLOR[section_key]
        # Tiles are constructed once and cached; which of them are ATTACHED to the
        # grid is decided per refresh by _paint_section (a hidden tile has to leave
        # the grid, not just shrink -- a zero-height widget still holds its cell and
        # punches a hole in the layout).
        for area_key in TR.AREAS_BY_SECTION.get(section_key, ()):
            tile = _AreaTile(area_key, TR.AREA_DISPLAY[area_key], rgb,
                             on_press=self._jump_to_area)
            self._tiles[area_key] = tile
        self._section_grids[section_key] = grid
        return sv

    def _build_summary(self):
        sv, grid = self._tile_grid()
        self._summary_grid = grid
        self._summary_empty = Label(
            text="[color=8C8C8C]Connect to a slot to populate the "
                 "tracker.[/color]",
            markup=True, size_hint_y=None, height=dp(30))
        grid.add_widget(self._summary_empty)
        return sv

    def _jump_to_area(self, area_key):
        """Clicking a tile jumps to its home section -- the Summary's whole job is
        to be a launchpad, so its tiles have to go somewhere."""
        section = TR.AREA_SECTION.get(area_key)
        idx = self._section_tab_idx.get(section)
        if idx is not None:
            self._tabbar.select(idx)

    # --------------------------------------------------------------- update --
    def update_tracker(self, state):
        """Thread-safe entry point. The client calls this from its asyncio loop;
        every widget touch has to happen on the Kivy main thread."""
        self._last_state = state
        if self._tracker_tab is None:
            return      # on_start hasn't run yet; it repaints _last_state itself
        Clock.schedule_once(lambda _dt: self._paint(state), 0)

    def _paint(self, state):
        try:
            self._paint_inner(state)
        except Exception as ex:
            logger.warning(f"Tracker repaint failed: {ex}")

    def _paint_inner(self, state):
        for name, chip in self._chips.items():
            chip.set_state(name in state.tokens, name in state.unused)

        n_unused = len(state.unused)
        totals = f"Checks {state.found_total}/{state.pool_total}"
        if n_unused:
            totals += (f"   -   {n_unused} key item"
                       f"{'s' if n_unused != 1 else ''} not used yet")
        self._totals_lbl.text = f"[color={TOTALS_HEX}]{totals}[/color]"

        # Right-aligned endgame gate readouts. Each turns bright green once its
        # threshold is met (mirrors the unused-key-item green). Crystals hides when
        # there is no crystal gate (crystals_needed 0); Lute Tablets hides when the
        # lute_tablets option is off (tablets_need 0).
        if state.crystals_need > 0:
            c_hex = (UNUSED_HEX if state.crystals_have >= state.crystals_need
                     else TOTALS_HEX)
            self._crystals_lbl.text = (
                f"[color={c_hex}]Crystals {state.crystals_have}/"
                f"{state.crystals_need}[/color]")
        else:
            self._crystals_lbl.text = ""
        if state.tablets_need > 0:
            t_hex = (UNUSED_HEX if state.tablets_have >= state.tablets_need
                     else TOTALS_HEX)
            self._tablets_lbl.text = (
                f"[color={t_hex}]Lute Tablets {state.tablets_have}/"
                f"{state.tablets_need}[/color]")
        else:
            self._tablets_lbl.text = ""
        # Runes goes green the moment activatable equipment unlocks -- the whole
        # point of the readout. ('/' is safe here: the Kivy font has one, unlike
        # the in-game menu font, which drops it silently.)
        if getattr(state, "runes_need", 0) > 0:
            r_hex = (UNUSED_HEX if state.runes_have >= state.runes_need
                     else TOTALS_HEX)
            self._runes_lbl.text = (
                f"[color={r_hex}]Runes {state.runes_have}/"
                f"{state.runes_need}[/color]")
        else:
            self._runes_lbl.text = ""
        # Levistone Shards: the ratio is progress-only. At assembly it becomes
        # the ITEM (green "Levistone"), exactly like the in-game menu line hands
        # its borrowed slot back to the real Levistone entry -- a completed
        # ratio is not what the player wants to read there (user 2026-08-12).
        if getattr(state, "shards_need", 0) > 0:
            done = state.shards_have >= state.shards_need
            s_hex = UNUSED_HEX if done else TOTALS_HEX
            self._shards_lbl.text = (
                f"[color={s_hex}]Levistone[/color]" if done else
                f"[color={s_hex}]Levi Shards {state.shards_have}/"
                f"{state.shards_need}[/color]")
        else:
            self._shards_lbl.text = ""

        for section in self._section_grids:
            self._paint_section(section, state)

        # summary_areas() walks the whole area map -- compute once, share with
        # both painters (this runs on every ReceivedItems repaint)
        summary = state.summary_areas()
        self._paint_summary(state, summary)
        self._paint_tab_titles(state, summary)

    def _paint_section(self, section, state):
        grid = self._section_grids[section]
        visible = tuple(k for k in TR.AREAS_BY_SECTION.get(section, ())
                        if (state.areas.get(k) is not None
                            and state.areas[k].state != TR.EMPTY))
        if self._section_shown.get(section) != visible:
            # Membership changed -> re-attach. Only fires when an area gains or
            # loses its first location (essentially once, on Connected), not on
            # every refresh.
            grid.clear_widgets()
            for key in visible:
                grid.add_widget(self._tiles[key])
            self._section_shown[section] = visible
        for key in visible:
            self._tiles[key].apply(state.areas[key])

    def _paint_summary(self, state, wanted):
        keys = [a.key for a in wanted]

        if set(keys) != set(self._summary_tiles):
            # Membership changed -> rebuild. Recoloring alone can't reorder a grid,
            # and this only fires when an area enters or leaves logic (a handful of
            # times per session), not on every refresh.
            self._summary_grid.clear_widgets()
            self._summary_tiles.clear()
            for a in wanted:
                rgb = TR.SECTION_COLOR[a.section]
                tile = _AreaTile(a.key, a.display, rgb,
                                 on_press=self._jump_to_area)
                self._summary_tiles[a.key] = tile
                self._summary_grid.add_widget(tile)
            if not wanted:
                self._summary_grid.add_widget(self._summary_empty)
                self._summary_empty.text = (
                    "[color=8C8C8C]Nothing in logic. Everything reachable has "
                    "been checked -- you are waiting on an item from another "
                    "world.[/color]"
                    if state.pool_total else
                    "[color=8C8C8C]Connect to a slot to populate the "
                    "tracker.[/color]")
        for a in wanted:
            tile = self._summary_tiles.get(a.key)
            if tile is not None:
                tile.apply(a)

    def _paint_tab_titles(self, state, summary):
        for section, idx in self._section_tab_idx.items():
            if section == TR.SUMMARY:
                n = len(summary)
                self._tabbar.set_text(idx, f"Summary  {n}")
                self._tabbar.set_shine(idx, n > 0)
                continue
            found, total = state.section_rollup(section)
            display = TR.SECTION_DISPLAY[section]
            if total <= 0:
                self._tabbar.set_out_logic(idx, False)
                self._tabbar.set_text(idx, display)
                self._tabbar.set_base(idx, _grayscale(TR.SECTION_COLOR[section]))
                self._tabbar.set_shine(idx, False)
                continue
            pct = found * 100 // total
            self._tabbar.set_text(idx, f"{display}  {pct}%")
            base = TR.SECTION_COLOR[section]
            if found >= total:
                # Nothing left to do -> gray, so the strip tells you where to look
                # before you even open a tab.
                self._tabbar.set_out_logic(idx, False)
                self._tabbar.set_base(idx, _grayscale(base))
                self._tabbar.set_shine(idx, False)
            elif found == 0 and not state.section_in_logic(section):
                # The "don't worry about this yet" extreme: nothing reachable AND
                # nothing found here -> dark-grey + slashes, matching the tiles.
                # The moment you check anything (found > 0), it reverts to the
                # section color below even if the remainder is out of logic.
                self._tabbar.set_out_logic(idx, True)
            else:
                # Full section color. Shine only while the section is in logic and
                # no reachable check has been found yet -- the same rule as tiles.
                self._tabbar.set_out_logic(idx, False)
                self._tabbar.set_base(idx, base)
                self._tabbar.set_shine(idx, state.section_shine(section))


    # ------------------------------------------------------------- shops tab --
    def build_shops_tab(self):
        if self._shops_tab is not None:
            return
        root = BoxLayout(orientation="vertical", padding=dp(6), spacing=dp(5))

        # Header: the seed note on the left, the always-visible search box on the
        # right. Typing anything jumps to the Find tab (tab 0).
        head = BoxLayout(orientation="horizontal", size_hint_y=None,
                         height=dp(28), spacing=dp(6))
        self._shops_note = Label(
            text="", markup=True, size_hint_x=0.42,
            halign="left", valign="middle")
        self._shops_note.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
        head.add_widget(self._shops_note)
        cap = Label(text="[b]Search:[/b]", markup=True, size_hint_x=None,
                    width=dp(56), halign="right", valign="middle")
        cap.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
        head.add_widget(cap)
        self._shops_search = TextInput(
            text="", multiline=False, size_hint_x=0.42,
            hint_text="item or spell name...",
            padding=(dp(6), dp(4)), font_size=dp(14),
            background_color=(0.14, 0.14, 0.16, 1),
            foreground_color=(0.92, 0.92, 0.92, 1),
            cursor_color=(0.92, 0.92, 0.92, 1),
            hint_text_color=(0.55, 0.55, 0.58, 1))
        self._shops_search.bind(text=self._on_shops_query)
        head.add_widget(self._shops_search)
        clear = Button(text="Clear", size_hint_x=None, width=dp(56),
                       font_size=dp(13))
        clear.bind(on_press=lambda _b: self.set_shops_query(""))
        head.add_widget(clear)
        root.add_widget(head)

        # 9 tabs (Find + 8 towns) in a 3x3 grid -- 4 columns would leave a ragged
        # single-button third row.
        self._shops_tabbar = _WrapTabBar(cols=3, btn_height=dp(40))
        root.add_widget(self._shops_tabbar)

        find_sv = ScrollView(do_scroll_x=False)
        # Half width, with a spacer beside it: the price column right-aligns to
        # its container, and pinned to a full-width window the prices would sit a
        # screen away from the names they belong to.
        find_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                             padding=(dp(4), dp(4)))
        find_row.bind(minimum_height=find_row.setter("height"))
        self._shops_find_box = BoxLayout(
            orientation="vertical", size_hint=(0.5, None), spacing=dp(2),
            pos_hint={"top": 1})
        self._shops_find_box.bind(
            minimum_height=self._shops_find_box.setter("height"))
        find_row.add_widget(self._shops_find_box)
        find_row.add_widget(BoxLayout(size_hint=(0.5, None), height=dp(1)))
        find_sv.add_widget(find_row)
        # Opening Find puts the caret in the search box: the box is small and the
        # AP command prompt at the bottom of the window is the obvious place to
        # type, so without this the first search silently goes to the wrong field.
        self._shops_tabbar.add_tab("Find", TR.SECTION_COLOR["shops"], find_sv,
                                   on_select=self._focus_shops_search)

        for city, name, _idxs in TR.SHOP_TOWNS:
            shop_rgb = TR.SHOP_TOWN_COLOR[city]
            sv = ScrollView(do_scroll_x=False)
            # One column per shop (weapon / armor / item / caravan) plus a magic
            # column, filled in by _fill_town -- which shops a town has varies.
            cols = BoxLayout(orientation="horizontal", size_hint_y=None,
                             spacing=dp(10), padding=(dp(4), dp(4)))
            cols.bind(minimum_height=cols.setter("height"))
            sv.add_widget(cols)
            idx = self._shops_tabbar.add_tab(
                name, shop_rgb, sv,
                dark_text=(city in TR.BRIGHT_SHOP_TOWNS))
            self._shops_town_content[city] = cols
            self._shops_town_idx[city] = idx

        self._paint_find()
        self._shops_tabbar.select(self._shops_town_idx.get(0, 0))
        self.add_client_tab("Shops", root)
        self._shops_tab = root

    def _on_shops_query(self, _widget, text):
        self._shops_query = (text or "").strip()
        self._paint_find()
        if self._shops_query and self._shops_tabbar is not None:
            self._shops_tabbar.select(0)

    # Re-assert the caret at these delays after the Find tab opens. One deferred
    # set is not enough: the focus takes, then something in the kvui window
    # (which owns its own text inputs and refreshes on a timer) drops it about
    # half a second later, and the user's first keystrokes land in the AP command
    # prompt. Re-asserting across ~1.5s outlasts that.
    _SEARCH_REFOCUS_AT = (0.0, 0.05, 0.3, 0.6, 1.0, 1.5)

    def _focus_shops_search(self):
        """Put the caret in the search box and keep it there briefly -- unless the
        user clicks somewhere else first, which cancels the whole sequence (never
        yank focus back out of a field somebody deliberately clicked into)."""
        box = self._shops_search
        if box is None:
            return
        self._cancel_search_refocus()

        def attempt(_dt):
            if self._shops_search is not None and not self._shops_search.focus:
                self._shops_search.focus = True

        self._search_focus_evts = [Clock.schedule_once(attempt, t)
                                   for t in self._SEARCH_REFOCUS_AT]
        # Bind the guard NEXT frame: the touch that selected the tab is still
        # being dispatched, and it would cancel the sequence it just started.
        self._search_focus_evts.append(Clock.schedule_once(
            lambda _dt: Window.bind(on_touch_down=self._search_touch_guard), 0))
        self._search_focus_evts.append(Clock.schedule_once(
            lambda _dt: self._cancel_search_refocus(),
            self._SEARCH_REFOCUS_AT[-1] + 0.1))

    def _search_touch_guard(self, _window, touch):
        box = self._shops_search
        if box is None or not box.collide_point(*box.to_widget(*touch.pos)):
            self._cancel_search_refocus()
        return False              # never consume the touch

    def _cancel_search_refocus(self):
        for ev in getattr(self, "_search_focus_evts", ()):
            try:
                ev.cancel()
            except Exception:
                pass
        self._search_focus_evts = []
        try:
            Window.unbind(on_touch_down=self._search_touch_guard)
        except Exception:
            pass

    def set_shops_query(self, text):
        """Drive the search from outside the GUI thread -- the /shop_find client
        command, and the Clear button. Setting .text fires _on_shops_query, so the
        repaint and the jump to Find come along for free."""
        def apply(_dt=None):
            if self._shops_search is None:
                return
            self._shops_search.text = text or ""
            self._focus_shops_search()
        Clock.schedule_once(apply, 0)

    def update_shops(self, payload):
        """Thread-safe entry point from the client's asyncio loop."""
        self._last_shops = payload
        if self._shops_tab is None:
            return
        Clock.schedule_once(lambda _dt: self._paint_shops_safe(payload), 0)

    def _paint_shops_safe(self, payload):
        try:
            self._paint_shops(payload)
        except Exception as ex:
            logger.warning(f"Shops repaint failed: {ex}")

    def _paint_shops(self, payload):
        self._shops_note.text = self._shops_note_text(payload)
        for town in payload["towns"]:
            city = town["city"]
            shop_rgb = TR.SHOP_TOWN_COLOR.get(city, TR.SECTION_COLOR["shops"])
            idx = self._shops_town_idx.get(city)
            cols = self._shops_town_content.get(city)
            if idx is None or cols is None:
                continue
            visited = town["visited"]
            self._shops_tabbar.set_enabled(idx, visited)
            self._shops_tabbar.set_base(idx, shop_rgb if visited
                                        else _grayscale(shop_rgb))
            self._shops_tabbar.set_text(
                idx, town["name"] if visited else f"{town['name']}  (locked)")
            # A full town is ~40 labels and a repaint fires on every ReceivedItems;
            # rebuilding eight of them when nothing changed is pure jank.
            if self._shops_town_last.get(city) != town:
                self._fill_town(cols, town)
                self._shops_town_last[city] = town
        self._paint_find()

    @staticmethod
    def _shops_note_text(payload):
        """The one-line header note: whether the seed sells AP items at all, and
        what the usability shading is being computed against."""
        bits = []
        if not payload.get("any_ap"):
            bits.append("This seed does not place AP items in shops.")
        party = payload.get("party") or {}
        if not party.get("shaded"):
            bits.append("Party unknown, so nothing is shaded.")
        elif not party.get("live"):
            bits.append("Game not attached: shading uses your yaml's base "
                        "classes (promotion and magic level unknown).")
        return f"[color=8C8C8C]{'  '.join(bits)}[/color]" if bits else ""

    # Columns per town row. A town has at most 4 (weapon / armor / item / magic,
    # or Onrac's item / caravan / magic); towns with fewer keep the same column
    # WIDTH and pad with a spacer, so every town reads at the same scale.
    _SHOP_COLS = 4

    def _town_column(self):
        col = BoxLayout(orientation="vertical",
                        size_hint=(1.0 / self._SHOP_COLS, None),
                        spacing=dp(3), pos_hint={"top": 1})
        col.bind(minimum_height=col.setter("height"))
        return col

    def _fill_town(self, cols, town):
        cols.clear_widgets()
        if not town["visited"]:
            col = BoxLayout(orientation="vertical", size_hint_y=None,
                            pos_hint={"top": 1})
            col.bind(minimum_height=col.setter("height"))
            col.add_widget(Label(
                text=f"[color=8C8C8C]Visit {town['name']} in game to reveal "
                     f"what its shops are selling.[/color]",
                markup=True, size_hint_y=None, height=dp(30),
                halign="left", valign="middle"))
            cols.add_widget(col)
            return

        used = 0
        # One column per shop: header + category-colored rule, then this seed's AP
        # offers (quality-colored, tagged AP), then the shop's whole native stock
        # with its buy price in the right-hand price column. Rows zebra-stripe from
        # the top of each shop. A shop whose AP offers are all bought goes dark
        # grey header and all -- the done-with-this-shop cue.
        city = town["city"]
        for shop in town["shops"]:
            col = self._town_column()
            kind = shop.get("kind")
            col.add_widget(_shop_header(
                shop["name"], None if shop.get("bought") else kind))
            n = 0
            if shop.get("note"):
                col.add_widget(_offer_label(
                    f"   [color=6E6E6E]({shop['note']})[/color]"))
            for off in shop["offers"]:
                col.add_widget(_offer_row(off, stripe=(n % 2 == 1)))
                n += 1
            for h in shop.get("hints") or []:
                col.add_widget(_hint_row(h, stripe=(n % 2 == 1)))
                n += 1
            stock = _by_use(shop.get("stock") or [])
            usable = [it for it in stock if it.get("use") != "never"]
            never = [it for it in stock if it.get("use") == "never"]
            for it in usable:
                col.add_widget(_stock_row(it, kind, stripe=(n % 2 == 1)))
                n += 1
            n = self._add_never_group(col, (city, shop["name"]), never, n,
                                      lambda it, s: _stock_row(it, kind, stripe=s))
            if not n:
                col.add_widget(_offer_label("[color=6E6E6E]   (empty)[/color]"))
            cols.add_widget(col)
            used += 1

        # Last column: native white/black magic shops (name, shuffled level, price).
        magic = town.get("magic")
        if magic and (magic.get("black") or magic.get("white")):
            col = self._town_column()
            n = 0
            for school, key in (("Black Magic", "black"), ("White Magic", "white")):
                spells = _by_use(magic.get(key) or [])
                if not spells:
                    continue
                col.add_widget(_shop_header(school, "magic"))
                for sp in [s for s in spells if s.get("use") != "never"]:
                    col.add_widget(_spell_row(sp, stripe=(n % 2 == 1)))
                    n += 1
                n = self._add_never_group(
                    col, (city, school),
                    [s for s in spells if s.get("use") == "never"], n,
                    lambda sp, s: _spell_row(sp, stripe=s))
            cols.add_widget(col)
            used += 1

        if used < self._SHOP_COLS:
            spacer = BoxLayout(size_hint=(
                (self._SHOP_COLS - used) / self._SHOP_COLS, None), height=dp(1))
            cols.add_widget(spacer)

    def _add_never_group(self, col, key, rows, n, make_row):
        """Append the collapsed "your party can never use these" tail of a shop
        column. Returns the running stripe counter. Collapsed by default: at the
        fade these rows render at they are hard to read anyway, and they are
        exactly the ones you never want to shop from."""
        if not rows:
            return n
        expanded = key in self._shops_expanded
        col.add_widget(_ExpandRow(
            f"   [color=6E6E6E]{'-' if expanded else '+'} {len(rows)} "
            f"your party cannot use[/color]",
            lambda k=key: self._toggle_never(k)))
        if expanded:
            for it in rows:
                col.add_widget(make_row(it, n % 2 == 1))
                n += 1
        return n

    def _toggle_never(self, key):
        """Flip one collapsed group and repaint just that town. The town cache in
        _paint_shops keys off the payload, which has not changed -- so this has to
        rebuild the column itself."""
        city = key[0]
        if key in self._shops_expanded:
            self._shops_expanded.discard(key)
        else:
            self._shops_expanded.add(key)
        cols = self._shops_town_content.get(city)
        town = self._shops_town_last.get(city)
        if cols is not None and town is not None:
            self._fill_town(cols, town)

    # ------------------------------------------------------------ shops find --
    def _paint_find(self):
        """Rebuild the Find tab: every line, in every shop of every VISITED town,
        whose item or spell name contains the query."""
        box = self._shops_find_box
        if box is None:
            return
        box.clear_widgets()
        found = TR.shop_search(self._last_shops, self._shops_query)
        seen, total = found["towns_visited"], found["towns_total"]
        if not self._shops_query:
            names = [t["name"] for t in
                     ((self._last_shops or {}).get("towns") or [])
                     if t.get("visited")]
            box.add_widget(_offer_label(
                "[color=9AA0A6]Type part of an item, spell or hint place to "
                "search every shop you have visited (or just \"hint\").[/color]"))
            box.add_widget(_offer_label(
                f"[color=6E6E6E]{seen} of {total} towns visited: "
                f"{', '.join(names) or 'none yet'}[/color]"))
            return

        hits = found["hits"]
        q = self._shops_query
        box.add_widget(_offer_label(
            f"[color=8C8C8C]{hits} match{'' if hits == 1 else 'es'} for "
            f"\"{q}\" in {seen} visited "
            f"town{'' if seen == 1 else 's'}[/color]"))
        for grp in found["groups"]:
            box.add_widget(_shop_header(grp["title"], grp["kind"]))
            n = 0
            for off in grp["offers"]:
                box.add_widget(_offer_row(off, stripe=(n % 2 == 1), query=q))
                n += 1
            for h in grp.get("hints") or []:
                box.add_widget(_hint_row(h, stripe=(n % 2 == 1), query=q))
                n += 1
            # Usable-first here too, but nothing is ever collapsed: hiding a row
            # the user explicitly searched for is the one thing Find must not do.
            for it in _by_use(grp["stock"]):
                box.add_widget(_stock_row(it, grp["kind"],
                                          stripe=(n % 2 == 1), query=q))
                n += 1
            for sp in _by_use(grp["spells"]):
                box.add_widget(_spell_row(sp, stripe=(n % 2 == 1), query=q))
                n += 1
        if not hits:
            box.add_widget(_offer_label(
                "[color=6E6E6E]   Nothing in the towns you have visited matches. "
                "Locked towns are not searched.[/color]"))


    # ------------------------------------------------------------- boost tab --
    # One snapped slider per stat: (key, title, lo, hi, step, note). The step is
    # what keeps a drag on a value the tables can take; 5x is the ceiling every one
    # of the matching yaml options already uses (see options.EncounterRate /
    # XPBoostPercentage / GilBoostPercentage / BossDifficultyPercentage).
    #
    # Boss Power floors at 0.1 -- the same floor its yaml option uses -- rather than
    # 0: at 0 every boss lands on the block builder's 1-HP / 0-attack clamp
    # (boot_patch.scale_boss_stats), which is not a difficulty setting so much as a
    # broken one. Encounter/XP/Gil DO reach 0 -- no encounters, no XP and no gil are
    # all coherent ways to play.
    BOOST_SLIDERS = (
        ("enc", "Encounter Rate", 0.0, 5.0, 0.25, ""),
        ("xp", "XP Gain", 0.0, 5.0, 0.5, ""),
        ("gil", "Gil Gain", 0.0, 5.0, 0.5, ""),
        ("monster", "Monster Power", 0.1, 5.0, 0.05, None),
        ("boss", "Boss Power", 0.1, 5.0, 0.05, None),
    )
    # Per-key live advice note. Monster Power takes the slot Boss Power used to
    # sit in; Boss Power drops just below it.
    _BOOST_NOTE_FN = {
        "monster": _monster_danger_note,
        "boss": _boss_danger_note,
    }

    def build_boost_tab(self):
        if self._boost_tab is not None:
            return
        # Content is content-sized (size_hint_y=None + minimum_height), so the full
        # stack of rows keeps its natural height instead of being squeezed to fit --
        # the top row (Encounter Rate) was getting clipped off the top of a short
        # window. A ScrollView holds it: taller-than-window scrolls, shorter sits at
        # the top (scroll_y=1, do_scroll_x off so it never drifts sideways).
        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6),
                         size_hint_y=None)
        root.bind(minimum_height=root.setter("height"))

        # Header row: what this tab is on the left, the bridge state on the right.
        # The bridge state gets its own label because it is repainted every second;
        # sharing the status line would keep wiping the last action's result.
        head_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                             height=dp(22))
        # Left side is an empty spacer: the header text is gone, but the bridge
        # state still needs to sit flush right.
        head_row.add_widget(Label(text=""))
        self._boost_attach_lbl = Label(text="", markup=True, halign="right",
                                       valign="middle", font_size=dp(12),
                                       size_hint_x=None, width=dp(150))
        self._boost_attach_lbl.bind(
            size=lambda w, _v: setattr(w, "text_size", w.size))
        head_row.add_widget(self._boost_attach_lbl)
        root.add_widget(head_row)

        rows = {k: (title, lo, hi, step, note)
                for k, title, lo, hi, step, note in self.BOOST_SLIDERS}

        # Encounter rate is unlocked: it is a pacing knob, not a difficulty one, and
        # players retune it constantly.
        title, lo, hi, step, note = rows["enc"]
        self._boost_rows["enc"] = _BoostSlider(title, lo, hi, step,
                                              self._pick_enc, note)
        root.add_widget(self._boost_rows["enc"])

        self._boost_lock = _LockBar(self._on_boost_unlock)
        root.add_widget(self._boost_lock)

        # ...the reward/difficulty knobs are not. These change what the seed is
        # worth, so they sit behind the lock and the lock re-closes every time the
        # tab is left, making every change a deliberate act.
        for key in ("xp", "gil", "monster", "boss"):
            title, lo, hi, step, note = rows[key]
            # No row draws danger bands (that machinery was removed -- its band
            # geometry never tracked the track width). The Monster/Boss Power
            # rows warn through their live note_fn advice line instead.
            row = _BoostSlider(title, lo, hi, step,
                               lambda v, k=key: self._pick_scaling(k, v),
                               note or "",
                               note_fn=self._BOOST_NOTE_FN.get(key))
            row.set_enabled(False)
            self._boost_rows[key] = row
            root.add_widget(row)

        self._boost_status = Label(text="", markup=True, size_hint_y=None,
                                   height=dp(20), font_size=dp(12),
                                   halign="left", valign="middle")
        self._boost_status.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
        root.add_widget(self._boost_status)

        # Top-align the content and let it scroll when it overflows a short window.
        scroll = ScrollView(do_scroll_x=False, scroll_y=1.0,
                            bar_width=dp(6))
        scroll.add_widget(root)
        tab = self.add_client_tab("Boost", scroll)
        self._boost_tab = root
        self._bind_boost_relock(tab)
        # Two watchdogs. The fast one closes the lock whenever the tab is navigated
        # away from and back (the tab-widget bind above is best-effort: kvui has
        # changed its tab widget class across AP versions, so it cannot be the only
        # thing holding the lock shut). The slow one keeps the readout honest about
        # whether PPSSPP is attached.
        Clock.schedule_interval(self._boost_visibility_tick, 0.25)
        Clock.schedule_interval(self._boost_refresh_tick, 1.0)

    def _bind_boost_relock(self, tab):
        """Relock on the tab being selected, whatever this kvui build's tab widget
        happens to be: KivyMD navigation items expose `active`, plain kvui
        TabbedPanelItems are ToggleButtons with `state` and an on_press event."""
        if tab is None:
            return
        for prop in ("active", "state"):
            if hasattr(tab, prop):
                try:
                    tab.bind(**{prop: lambda *_a: self._boost_relock()})
                    break
                except Exception:
                    pass
        try:
            tab.bind(on_press=lambda *_a: self._boost_relock())
        except Exception:
            pass    # no such event on this widget class; the watchdog covers it

    def _boost_relock(self, *_a):
        if self._boost_lock is not None:
            self._boost_lock.relock()

    def _boost_visibility_tick(self, _dt):
        if not self._boost_sm_bound:
            self._bind_screen_manager()
        # A tab that is not the current one is not in the widget tree at all (kvui
        # puts each tab's content on its own Screen), so no root window == hidden.
        vis = self._boost_tab is not None and self._boost_tab.get_root_window() is not None
        if vis != self._boost_visible:
            self._boost_visible = vis
            self._boost_relock()    # relock on both edges: leaving and returning
        # Keep the tick marks on the track they belong to. Deliberately NOT gated on
        # `vis`: the rows are built during on_start while this tab's Screen is still
        # detached and its sliders sit at kivy's default width, and swapping the
        # Screen in does not reliably re-fire the size bind the ticks hang off. Gating
        # this left the ticks stale until the window was manually resized. It is a
        # no-op unless the geometry actually moved, so polling it costs nothing.
        for row in self._boost_rows.values():
            row.sync_geometry()

    def _bind_screen_manager(self):
        """Relock on any tab switch, by watching the ScreenManager that kvui puts
        our tab's content on. This is the version-independent hook: `current`
        changes on every tab switch no matter what widget class the tab bar itself
        uses. Bound lazily -- while a tab is not showing its Screen has no parent,
        so the chain is only walkable once, after the tab has been visited."""
        self._boost_sm_bound = True     # one attempt per tick is enough; never retry
        w = self._boost_tab             # a hidden Screen keeps `manager`, not `parent`
        for _ in range(12):             # bounded walk: no chance of a parent cycle
            if w is None:
                break
            if hasattr(w, "current") and hasattr(w, "screen_names"):
                try:
                    w.bind(current=lambda *_a: self._boost_relock())
                    return
                except Exception:
                    break
            w = getattr(w, "manager", None) or w.parent
        self._boost_sm_bound = False    # not reachable yet; try again next tick

    def _boost_refresh_tick(self, _dt):
        ctx = getattr(self, "ctx", None)
        if ctx is not None and hasattr(ctx, "refresh_boost"):
            try:
                ctx.refresh_boost()
            except Exception:
                pass

    def _on_boost_unlock(self, unlocked):
        for key in ("xp", "gil", "monster", "boss"):
            row = self._boost_rows.get(key)
            if row is not None:
                row.set_enabled(unlocked)
        if self._last_boost is not None:
            self._paint_boost(self._last_boost)

    # --- presses -----------------------------------------------------------
    def _pick_enc(self, mult):
        ctx = getattr(self, "ctx", None)
        if ctx is None or not hasattr(ctx, "set_encounter_rate"):
            return
        self._boost_note(f"encounter rate -> {_fmt_mult(mult)} ...")
        self._boost_send(ctx, ctx.set_encounter_rate(mult),
                         f"encounter rate {_fmt_mult(mult)}")

    def _pick_scaling(self, key, mult):
        ctx = getattr(self, "ctx", None)
        if ctx is None or not hasattr(ctx, "set_monster_scaling"):
            return
        label = {"xp": "XP", "gil": "Gil", "monster": "Monster power",
                 "boss": "Boss power"}[key]
        kwargs = {"xp": "xp_mult", "gil": "gil_mult",
                  "monster": "monster_mult", "boss": "boss_mult"}[key]
        self._boost_note(f"{label} -> {_fmt_mult(mult)} ...")
        self._boost_send(ctx, ctx.set_monster_scaling(**{kwargs: mult}),
                         f"{label} {_fmt_mult(mult)}")

    def _boost_send(self, ctx, coro, what):
        """Hand a live-write coroutine to the client's asyncio loop. We are on the
        Kivy thread here, so this must not be create_task; and the completion
        callback lands back on the asyncio thread, so it bounces through Clock
        before touching a widget."""
        def done(ok):
            Clock.schedule_once(
                lambda _dt: self._boost_note(
                    f"{what} applied." if ok else
                    f"{what} set -- takes effect when the game is attached."), 0)

        fut = None
        if hasattr(ctx, "run_async_threadsafe"):
            fut = ctx.run_async_threadsafe(coro, done)
        if fut is None:
            self._boost_note("client is not running its event loop yet.")

    def _boost_note(self, text):
        if self._boost_status is not None:
            self._boost_status.text = f"[color={TOTALS_HEX}]{text}[/color]"

    # --- paint -------------------------------------------------------------
    def update_boost(self, payload):
        """Thread-safe entry point from the client's asyncio loop."""
        self._last_boost = payload
        if self._boost_tab is None:
            return      # on_start hasn't run yet; it repaints _last_boost itself
        Clock.schedule_once(lambda _dt: self._paint_boost_safe(payload), 0)

    def _paint_boost_safe(self, payload):
        try:
            self._paint_boost(payload)
        except Exception as ex:
            logger.warning(f"Boost repaint failed: {ex}")

    def _paint_boost(self, payload):
        for key in ("enc", "xp", "gil", "monster", "boss"):
            row = self._boost_rows.get(key)
            if row is not None:
                row.set_current(payload.get(key))
        if self._boost_attach_lbl is not None:
            self._boost_attach_lbl.text = (
                f"[color={UNUSED_HEX}]game attached[/color]"
                if payload.get("attached") else
                "[color=E0C060]game not attached[/color]")


# Every AP offer renders in one purple, whatever its Archipelago quality flag --
# the tab's other colors mean "which shop category is this", and an offer's
# quality is not that axis. The AP tag plus this color is what marks a row as
# multiworld stock. (The shop's own consumables are green; see _SHOP_KIND_COLOR.)
_AP_COLOR = "C9A0F5"

# Shop-type header tints: weapon red, armor blue, consumables green. Purple is
# reserved for AP offers (_AP_COLOR), so the shop's own stock never wears it. The
# Onrac desert caravan takes gold -- it sells consumables, but it is not a town
# store, and the distinct header is the only place that difference shows (its
# ROWS are green like any other consumable shelf).
_SHOP_KIND_COLOR = {
    "weapon": "F0A0A0", "armor": "A0C0F0", "item": "A0E0A0",
    "caravan": "E8CC80", "magic": "D8D8D8",
}

# Native-stock row tint per shop kind -- the same three category colors, dropped
# in brightness so a shelf reads as background against the AP offers above it.
_STOCK_KIND_COLOR = {
    "weapon": "D08C8C", "armor": "8CA8D8", "item": "8CC88C",
    "caravan": "8CC88C", "magic": "C0C0C0",
}

# Zebra striping: every other row gets this wash. Kept very low alpha -- it has
# to band the column without competing with the category colors.
_STRIPE_RGBA = (1, 1, 1, 0.045)
_RULE_RGBA = (1, 1, 1, 0.22)
ROW_HEIGHT = dp(20)
PRICE_WIDTH = dp(62)


class _ShopRow(BoxLayout):
    """One shelf line: name on the left, price RIGHT-ALIGNED in its own fixed
    column, over an optional zebra wash. Two labels rather than one markup string
    because kivy only aligns a label's whole text block -- with one label the
    prices zig-zag with the name length, which is exactly what makes a 15-line
    shop unreadable."""

    def __init__(self, left, right="", stripe=False, **kw):
        super().__init__(orientation="horizontal", size_hint_y=None,
                         height=ROW_HEIGHT, **kw)
        if stripe:
            with self.canvas.before:
                Color(*_STRIPE_RGBA)
                self._bg = Rectangle(pos=self.pos, size=self.size)
            self.bind(pos=self._sync_bg, size=self._sync_bg)
        self._left = Label(text=left, markup=True, halign="left",
                           valign="middle", shorten=True,
                           shorten_from="right")
        self._left.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
        self._right = Label(text=right, markup=True, halign="right",
                            valign="middle", size_hint_x=None,
                            width=PRICE_WIDTH)
        self._right.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
        self.add_widget(self._left)
        self.add_widget(self._right)

    def _sync_bg(self, *_a):
        self._bg.pos, self._bg.size = self.pos, self.size


class _ShopHeaderRow(BoxLayout):
    """A shop name with a hairline rule under it, in the shop's category color.
    The rule is what turns a stack of shops into visible blocks."""

    def __init__(self, text, rgba, **kw):
        super().__init__(orientation="vertical", size_hint_y=None,
                         height=dp(24), **kw)
        lbl = Label(text=f"[b]{text}[/b]", markup=True, halign="left",
                    valign="bottom", size_hint_y=None, height=dp(21))
        lbl.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
        self.add_widget(lbl)
        rule = Widget(size_hint_y=None, height=dp(2))
        with rule.canvas:
            Color(*rgba)
            self._line = Rectangle(pos=rule.pos, size=(0, dp(1)))
        rule.bind(pos=lambda w, _v: self._sync(w),
                  size=lambda w, _v: self._sync(w))
        self.add_widget(rule)

    def _sync(self, w):
        self._line.pos = (w.x, w.y + dp(1))
        self._line.size = (w.width, dp(1))


def _shop_header(name, kind=None):
    """The shop's name over a hairline rule in its category color."""
    hexc = _SHOP_KIND_COLOR.get(kind, "D0D0D0")
    rgba = tuple(int(hexc[i:i + 2], 16) / 255 for i in (0, 2, 4)) + (0.55,)
    return _ShopHeaderRow(f"[color={hexc}]{name}[/color]", rgba)


def _offer_label(markup):
    lbl = Label(text=markup, markup=True, size_hint_y=None, height=ROW_HEIGHT,
                halign="left", valign="middle")
    lbl.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
    return lbl


def _hl(name, query):
    """`name` with the searched substring bolded (Find results only)."""
    if not query:
        return name
    i = name.lower().find(query.lower())
    if i < 0:
        return name
    j = i + len(query)
    return f"{name[:i]}[b]{name[i:j]}[/b]{name[j:]}"


# Usability shading. "now" = this party can equip/learn it today, "later" = only
# after a class change (or a higher magic level), "never" = no member's class can
# ever use it. Each row's color is faded toward the tab background by these
# factors, so the shelf keeps its category colors and only its BRIGHTNESS carries
# the state -- nothing moves, nothing hides.
# Grouping (usable first, "never" collapsed) now carries most of the signal, so
# the fades only have to separate the groups -- "never" no longer has to be
# unreadable, since you only ever see those rows after asking for them.
_USE_FADE = {"now": 1.0, "later": 0.62, "never": 0.5}
_USE_PRICE_FADE = {"now": 1.0, "later": 0.72, "never": 0.6}


# Sort order within a shop: usable now, then after a class change, then never.
# Stable, so each group keeps the in-game shelf order.
_USE_ORDER = {"now": 0, "later": 1, "never": 2}


def _by_use(items):
    return sorted(items, key=lambda it: _USE_ORDER.get(it.get("use", "now"), 0))


class _ExpandRow(Button):
    """The "+ N your party can never use" line that opens a collapsed group."""

    def __init__(self, text, on_toggle, **kw):
        super().__init__(text=text, markup=True, size_hint_y=None,
                         height=ROW_HEIGHT, halign="left", valign="middle",
                         background_normal="", background_down="",
                         background_color=(0, 0, 0, 0), font_size=dp(13), **kw)
        self.bind(size=lambda w, _v: setattr(w, "text_size", w.size))
        self.bind(on_press=lambda _b: on_toggle())


def _fade(hexc, factor):
    """`hexc` pulled toward the dark tab background. factor 1.0 = unchanged."""
    if factor >= 1.0:
        return hexc
    out = []
    for i in (0, 2, 4):
        out.append(min(255, max(0, int(int(hexc[i:i + 2], 16) * factor))))
    return "".join(f"{v:02X}" for v in out)


def _price_markup(price, muted=False, use="now"):
    if not price:
        return ""
    base = "4E4E4E" if muted else "8C8C8C"
    return f"[color={_fade(base, _USE_PRICE_FADE.get(use, 1.0))}]{price}g[/color]"


def _offer_row(off, stripe=False, query=""):
    """An AP offer: "AP <item> (player)" with the price in the right-hand column.
    AP offers share a column with ordinary stock, so they carry the AP tag as well
    as the AP purple -- color alone reads as decoration on a 15-line shelf."""
    color = _AP_COLOR
    item = _hl(off["item"], query)
    if off["found"]:
        # Already bought: mute it and tag it, so the town reads as a shopping list.
        tag = "[color=4E4E4E]AP[/color] "
        body = f"[color=6E6E6E]{item}[/color]  [color=4E4E4E](bought)[/color]"
    else:
        tag = f"[color={color}][b]AP[/b][/color] "
        body = f"[color={color}]{item}[/color]"
    who = f"  [color=9AA0A6]({off['player']})[/color]" if off["player"] else ""
    return _ShopRow(f" {tag}{body}{who}",
                    _price_markup(off.get("price"), muted=off["found"]),
                    stripe=stripe)


_HINT_COLOR = "E0B341"      # gold: a hint is neither an AP item nor native stock


def _hint_row(h, stripe=False, query=""):
    """A HINT shelf row: "HINT <place> - N checks" with its price. Spent rows
    (bought, or the place fully found) mute exactly like a bought AP offer, so
    a shop reads as a shopping list either way."""
    place = _hl(h.get("place") or "", query)
    left = h.get("left", h.get("checks", 0))
    if h.get("spent"):
        tag = "[color=4E4E4E]HINT[/color] "
        body = (f"[color=6E6E6E]{place}[/color]  "
                f"[color=4E4E4E]({'spent' if left else 'all found'})[/color]")
    else:
        tag = f"[color={_HINT_COLOR}][b]HINT[/b][/color] "
        unit = "check" if left == 1 else "checks"
        body = (f"[color={_HINT_COLOR}]{place}[/color]  "
                f"[color=9AA0A6]{left} {unit}[/color]")
    return _ShopRow(f" {tag}{body}",
                    _price_markup(h.get("price"), muted=bool(h.get("spent"))),
                    stripe=stripe)


def _stock_row(it, kind=None, stripe=False, query=""):
    """One line of a shop's NATIVE stock, tinted by category (weapons red, armor
    blue, consumables purple) at a lower brightness than the AP offers above it,
    and faded further when this party cannot equip it."""
    use = it.get("use", "now")
    color = _fade(_STOCK_KIND_COLOR.get(kind, "C4C4C4"),
                  _USE_FADE.get(use, 1.0))
    return _ShopRow(f"   [color={color}]{_hl(it['name'], query)}[/color]",
                    _price_markup(it.get("price"), use=use), stripe=stripe)


def _spell_row(sp, stripe=False, query=""):
    # "<level>. <name>" + price -- level is the spell's shuffled shop tier, and it
    # is also why a row dims: a spell above the party's magic level reads "later".
    use = sp.get("use", "now")
    fade = _USE_FADE.get(use, 1.0)
    return _ShopRow(f"   [color={_fade('9AA0A6', fade)}]{sp['level']}.[/color] "
                    f"[color={_fade('D8D8D8', fade)}]{_hl(sp['name'], query)}"
                    f"[/color]",
                    _price_markup(sp.get("price"), use=use), stripe=stripe)


def make_manager(base_cls):
    """Build the FF1 PSP manager class on top of this build's kvui manager."""

    class FF1PSPManager(TrackerMixin, base_cls):
        base_title = "Archipelago FF1 PSP Client"

        def on_stop(self, *args):
            # The window is going away NOW. Arm the hard-exit backstop from the
            # Kivy thread, before anything on the asyncio side runs: if the loop
            # is wedged (debugger socket against a closing PPSSPP, a sync call
            # stuck in the default executor), ctx.shutdown never even starts and
            # the process would sit there "Not Responding" forever.
            try:
                from .ApClient import arm_exit_watchdog
                arm_exit_watchdog()
            except Exception:
                pass
            return super().on_stop(*args)

    return FF1PSPManager
