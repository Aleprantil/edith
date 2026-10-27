"""
L'isola di EDITH: un pannello GTK4 che scende da sotto la barra (layer-shell, come Waybar), parla con EDITH e si richiude.
Tutta la logica sta in edith_core: qui solo grafica. La rete gira in thread separati, la grafica non si blocca mai.
"""
import math
import os
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
import cairo  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
except (ValueError, ImportError):
    LayerShell = None

import edith_core as core  # noqa: E402
import edith_voice as voice  # noqa: E402

HERE = Path(__file__).resolve().parent
CHIPS = [("Oggi", "Cosa faccio oggi?"), ("Domani", "Cosa ho domani?"), ("Promemoria", "Che promemoria ho?"),
         ("Spese", "Quanto ho speso questo mese?"), ("Server", "Come sta il server?")]
REFRESH_EVERY = 60                                   # secondi: la striscia «Adesso» si rinfresca solo se è vecchia


def in_thread(work, done):
    """Esegue work() fuori dalla grafica e poi done(risultato, errore) nella grafica."""
    def run():
        try:
            res, err = work(), None
        except Exception as e:                       # noqa: BLE001 — qualsiasi errore diventa un messaggio, mai un blocco
            res, err = None, e
        GLib.idle_add(lambda: (done(res, err), False)[1])
    threading.Thread(target=run, daemon=True).start()


def _rrect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Avatar(Gtk.DrawingArea):
    """EDITH come personaggio: una piccola sfera ambra con gli occhi e un satellite in orbita.
    Umori: idle, thinking, greet, happy, sad, sleepy. Si anima solo quando start() è attivo (isola aperta)."""
    SIZE = 56

    def __init__(self, on_click, size: int = None):
        super().__init__()
        self.SIZE = size or self.SIZE
        self.set_content_width(self.SIZE)
        self.set_content_height(self.SIZE)
        self.add_css_class("avatar")
        self.set_tooltip_text("Tocca per salutare")
        self.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        self.set_draw_func(self._draw)
        now = time.monotonic()
        self.t0, self.base, self.mood, self.mood_at, self.mood_until = now, "idle", "idle", now, 0.0
        self.next_blink, self.blink_at = now + random.uniform(2.0, 4.5), -10.0
        self.look, self.target = [0.0, 0.0], [0.0, 0.0]
        self._tick, self._frame = None, 0
        self.level, self.level_s, self.env, self.env_t0 = 0.0, 0.0, [], 0.0
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: on_click())
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self._aim(0.0, 0.0))
        self.add_controller(motion)

    # -- controllo --
    def start(self):
        if self._tick is None:
            self._tick = self.add_tick_callback(self._on_tick)

    def stop(self):
        if self._tick is not None:
            self.remove_tick_callback(self._tick)
            self._tick = None

    def set_base(self, mood: str):
        self.base = mood
        if time.monotonic() >= self.mood_until:
            self.mood, self.mood_at = mood, time.monotonic()
        self.queue_draw()

    def set_level(self, v: float):
        """Il volume della tua voce mentre ascolta (0..1)."""
        self.level = max(0.0, min(1.0, float(v)))
        return False

    def start_voice(self, env: list):
        """Parla: la bocca segue l'audio vero (un valore ogni 40 ms)."""
        self.env, self.env_t0 = list(env), time.monotonic()
        self.calm()
        self.set_base("voice")

    def calm(self):
        """Interrompe l'espressione del momento (per esempio la faccia felice della frase prima)."""
        self.mood_until = 0.0

    def flash(self, mood: str, seconds: float):
        now = time.monotonic()
        self.mood, self.mood_at, self.mood_until = mood, now, now + seconds
        self.queue_draw()

    def _aim(self, x: float, y: float):
        self.target = [x, y]

    def _motion(self, _ctl, x, y):
        w, h = self.get_width() or self.SIZE, self.get_height() or self.SIZE
        self._aim(max(-1.0, min(1.0, (x - w / 2) / (w / 2))), max(-1.0, min(1.0, (y - h / 2) / (h / 2))))

    def _on_tick(self, _widget, _clock):
        now = time.monotonic()
        if self.mood_until and now >= self.mood_until:
            self.mood, self.mood_at, self.mood_until = self.base, now, 0.0
        for i in (0, 1):
            self.look[i] += (self.target[i] - self.look[i]) * 0.18
        self.level_s += (self.level - self.level_s) * 0.35
        if now >= self.next_blink:
            self.blink_at, self.next_blink = now, now + random.uniform(2.5, 5.5)
        self._frame += 1
        if self._frame % 2 == 0:                       # una trentina di fotogrammi al secondo bastano
            self.queue_draw()
        return GLib.SOURCE_CONTINUE

    # -- disegno --
    def _draw(self, _area, cr, w, h):
        now = time.monotonic()
        t, mood, dt = now - self.t0, self.mood, now - self.mood_at
        cx, cy, r = w / 2, h / 2 + math.sin(t * 2.0) * 1.2, min(w, h) * 0.27
        if mood == "greet":
            hop = abs(math.sin(dt * 7.0)) * max(0.0, 1.0 - dt / 1.3)
            cy -= hop * r * 0.55
            r *= 1.0 + hop * 0.08
        elif mood == "sad":
            cy += 1.5
        asleep = mood == "sleepy"
        amber = (0.75, 0.70, 0.64) if asleep else (1.0, 0.624, 0.110)
        listening = mood == "listen"
        if listening:
            r *= 1.0 + self.level_s * 0.06

        glow = cairo.RadialGradient(cx, cy, r * 0.2, cx, cy, r * 1.85)
        glow.add_color_stop_rgba(0, *amber, 0.15 if asleep else min(0.85, 0.36 + (0.45 * self.level_s if listening else 0.0)))
        glow.add_color_stop_rgba(1, *amber, 0.0)
        cr.set_source(glow)
        cr.arc(cx, cy, r * 1.85, 0, 2 * math.pi)
        cr.fill()

        tilt, orbit = -0.38, r * 1.62
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(tilt)
        cr.scale(1.0, 0.3)
        cr.arc(0, 0, orbit, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(*amber, 0.22 if asleep else 0.45)
        cr.set_line_width(1.0)
        cr.stroke()

        a = t * (2.6 if mood == "thinking" else 1.0)
        ex, ey = orbit * math.cos(a), orbit * 0.3 * math.sin(a)
        sx, sy = cx + ex * math.cos(tilt) - ey * math.sin(tilt), cy + ex * math.sin(tilt) + ey * math.cos(tilt)
        front = math.sin(a) > 0

        def satellite():
            cr.set_source_rgba(1.0, 0.77, 0.42, 0.5 if asleep else 1.0)
            cr.arc(sx, sy, max(1.4, r * 0.11), 0, 2 * math.pi)
            cr.fill()
        if not front:
            satellite()

        body = cairo.RadialGradient(cx - r * 0.35, cy - r * 0.4, r * 0.1, cx, cy, r)
        if asleep:
            body.add_color_stop_rgb(0, 0.86, 0.82, 0.76)
            body.add_color_stop_rgb(1, 0.50, 0.45, 0.40)
        else:
            body.add_color_stop_rgb(0, 1.0, 0.87, 0.58)
            body.add_color_stop_rgb(0.55, 1.0, 0.624, 0.110)
            body.add_color_stop_rgb(1, 0.80, 0.42, 0.03)
        cr.set_source(body)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()

        cr.set_source_rgb(0.10, 0.07, 0.02)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        lx, ly = self.look
        ox, eye_y = lx * r * 0.12, cy - r * 0.06 + ly * r * 0.10
        ew, eh, gap = r * 0.21, r * 0.36, r * 0.36
        if mood == "thinking":
            ox, eye_y = r * 0.10, eye_y - r * 0.09
        if mood in ("greet", "happy"):
            cr.set_line_width(max(1.6, r * 0.11))
            for side in (-1, 1):
                cr.new_sub_path()
                cr.arc(cx + side * gap, eye_y + eh * 0.18, ew * 0.85, math.pi * 1.12, math.pi * 1.88)
                cr.stroke()
            cr.set_line_width(max(1.4, r * 0.08))
            cr.new_sub_path()
            cr.arc(cx, cy + r * 0.20, r * 0.24, math.pi * 0.18, math.pi * 0.82)
            cr.stroke()
        elif asleep:
            cr.set_line_width(max(1.4, r * 0.09))
            for side in (-1, 1):
                cr.move_to(cx + side * gap - ew * 0.6, eye_y + eh * 0.15)
                cr.line_to(cx + side * gap + ew * 0.6, eye_y + eh * 0.15)
                cr.stroke()
        elif mood in ("voice", "listen"):
            bt = now - self.blink_at
            k = 1.0 - math.sin(bt / 0.16 * math.pi) if 0 <= bt < 0.16 else 1.0
            big = 1.12 if listening else 1.0
            for side in (-1, 1):
                hh = eh * big * max(0.12, k)
                _rrect(cr, cx + side * gap + ox - ew * big / 2, eye_y - hh / 2, ew * big, hh, ew * big / 2)
                cr.fill()
            if mood == "voice":
                i = int((now - self.env_t0) / 0.04)
                v = self.env[i] if 0 <= i < len(self.env) else 0.0
                mh = r * (0.03 + 0.21 * v)
                cr.save()
                cr.translate(cx, cy + r * 0.36)
                cr.scale(r * 0.15, max(0.5, mh))
                cr.arc(0, 0, 1.0, 0, 2 * math.pi)
                cr.restore()
                cr.fill()
            else:
                cr.set_line_width(max(1.3, r * 0.07))                                          # ascolta: bocca chiusa e attenta
                cr.move_to(cx - r * 0.12, cy + r * 0.38)
                cr.line_to(cx + r * 0.12, cy + r * 0.38)
                cr.stroke()
                for k2 in range(3):                                                            # le onde della tua voce
                    a = max(0.0, self.level_s * 1.3 - k2 * 0.25)
                    if a <= 0.02:
                        continue
                    cr.set_source_rgba(1.0, 0.77, 0.42, min(1.0, a))
                    cr.set_line_width(1.6)
                    cr.new_sub_path()
                    cr.arc(cx, cy, r * (1.22 + 0.2 * k2), -0.55, 0.55)
                    cr.stroke()
                    cr.new_sub_path()
                    cr.arc(cx, cy, r * (1.22 + 0.2 * k2), math.pi - 0.55, math.pi + 0.55)
                    cr.stroke()
                cr.set_source_rgb(0.10, 0.07, 0.02)
        elif mood == "talk":
            bt = now - self.blink_at
            k = 1.0 - math.sin(bt / 0.16 * math.pi) if 0 <= bt < 0.16 else 1.0
            for side in (-1, 1):
                hh = eh * max(0.12, k)
                _rrect(cr, cx + side * gap + ox - ew / 2, eye_y - hh / 2, ew, hh, ew / 2)
                cr.fill()
            mh = r * 0.16 * (0.25 + 0.75 * abs(math.sin(t * 12.5)))                       # la bocca che parla
            cr.save()
            cr.translate(cx, cy + r * 0.36)
            cr.scale(r * 0.14, max(0.5, mh))
            cr.arc(0, 0, 1.0, 0, 2 * math.pi)
            cr.restore()
            cr.fill()
        else:
            bt = now - self.blink_at
            k = 1.0 - math.sin(bt / 0.16 * math.pi) if 0 <= bt < 0.16 else 1.0
            hh = eh * (0.8 if mood == "sad" else 1.0) * max(0.12, k)
            for side in (-1, 1):
                _rrect(cr, cx + side * gap + ox - ew / 2, eye_y - hh / 2 + (r * 0.06 if mood == "sad" else 0), ew, hh, ew / 2)
                cr.fill()
            if mood == "sad":
                cr.set_line_width(max(1.4, r * 0.08))
                cr.new_sub_path()
                cr.arc(cx, cy + r * 0.62, r * 0.2, math.pi * 1.22, math.pi * 1.78)
                cr.stroke()

        if front:
            satellite()

        if mood == "greet" and dt < 1.8:                          # la mano che saluta
            wave = math.sin(dt * 13.0) * 0.55
            hx, hy = cx + r * 1.12 + math.sin(wave) * r * 0.25, cy - r * 0.15 - math.cos(wave) * r * 0.25
            cr.set_source_rgb(*amber)
            cr.arc(hx, hy, r * 0.26, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*amber, 0.6)
            cr.set_line_width(1.2)
            for k2 in (1, 2):
                cr.new_sub_path()
                cr.arc(hx, hy, r * (0.26 + 0.17 * k2), -0.9 + wave * 0.3, -0.2 + wave * 0.3)
                cr.stroke()

        if mood == "thinking":                                     # tre puntini che compaiono uno dopo l'altro
            for i in range(3):
                on = (int(t * 3) % 4) > i
                cr.set_source_rgba(1.0, 0.77, 0.42, 0.95 if on else 0.2)
                cr.arc(cx + r * 0.65 + i * r * 0.32, cy - r * 1.15, max(1.2, r * 0.09), 0, 2 * math.pi)
                cr.fill()


STATUS_TEXT = {"ok": "pronta", "alert": "qualcosa non va nel server", "offline": "non raggiungibile", "auth": "accesso negato"}
STATUS_MOOD = {"ok": "idle", "alert": "sad", "offline": "sleepy", "auth": "sleepy"}
BALLOON = {"edith": (1.0, 0.957, 0.886), "error": (1.0, 0.89, 0.863)}          # il colore del fumetto (serve anche alla punta)
STALE_SCENE = 600                                                              # dopo 10 minuti, riaprendo, si torna al riepilogo
GREET_COOLDOWN = 60                                                            # aprendo l'isola saluta al massimo una volta al minuto


def strongify(markup: str) -> str:
    """Nel fumetto chiaro il grassetto è anche color ambra scuro, come nel progetto."""
    return markup.replace("<b>", '<span weight="bold" foreground="#b85700">').replace("</b>", "</span>")


class Tail(Gtk.DrawingArea):
    """La punta del fumetto, rivolta verso il personaggio."""

    def __init__(self, island):
        super().__init__(content_width=34, content_height=14, halign=Gtk.Align.START)
        self.island = island
        self.set_draw_func(self._draw)

    def _draw(self, _a, cr, w, h):
        cr.set_source_rgb(*BALLOON.get(self.island.kind, BALLOON["edith"]))
        cr.move_to(0, h + 1)
        cr.line_to(w / 2, 0)
        cr.line_to(w, h + 1)
        cr.close_path()
        cr.fill()


class Timeline(Gtk.DrawingArea):
    """La linea della giornata: fatto, adesso, prossimo, dopo."""

    def __init__(self):
        super().__init__(content_height=48, hexpand=True)
        self.data = None
        self.set_draw_func(self._draw)

    def set_data(self, data):
        self.data = data
        self.set_visible(bool(data))
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        if not self.data:
            return
        pad, y = 12, 13
        x = lambda f: pad + f * (w - 2 * pad)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(3)
        cr.set_source_rgb(0.914, 0.843, 0.749)
        cr.move_to(x(0), y)
        cr.line_to(x(1), y)
        cr.stroke()
        if self.data.get("now_x"):
            cr.set_source_rgb(0.77, 0.37, 0.0)
            cr.move_to(x(0), y)
            cr.line_to(x(self.data["now_x"]), y)
            cr.stroke()
        cr.select_font_face("Space Mono", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10.5)
        last_label = -99.0
        pts = self.data["points"]
        order = sorted(range(len(pts)), key=lambda i: pts[i]["state"] not in ("next", "now"))   # le etichette importanti per prime
        drawn = []
        for i in order:
            p = pts[i]
            px, st = x(p["x"]), p["state"]
            if st in ("next", "now"):
                cr.set_source_rgba(1.0, 0.624, 0.110, 0.30)
                cr.arc(px, y, 13, 0, 2 * math.pi)
                cr.fill()
            cr.set_source_rgb(1.0, 0.957, 0.886)
            cr.arc(px, y, 10, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(*{"done": (0.77, 0.37, 0.0), "now": (1.0, 0.624, 0.110), "next": (1.0, 0.624, 0.110)}.get(st, (0.914, 0.843, 0.749)))
            cr.arc(px, y, 7, 0, 2 * math.pi)
            cr.fill()
            ext = cr.text_extents(p["label"])
            tx = min(max(px - ext.width / 2, 0), w - ext.width)
            if any(abs(tx - d) < ext.width + 6 for d in drawn):
                continue                                                   # orari troppo vicini: resta il più importante
            drawn.append(tx)
            strong = st in ("next", "now")
            cr.select_font_face("Space Mono", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if strong else cairo.FONT_WEIGHT_NORMAL)
            cr.set_source_rgb(*((0.14, 0.09, 0.04) if strong else (0.42, 0.35, 0.27)))
            cr.move_to(tx, h - 4)
            cr.show_text(p["label"])
            last_label = tx
        del last_label


class Island(Gtk.Application):
    def __init__(self, first_command: str = None):
        super().__init__(application_id="it.edith.island", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.conf = core.settings()
        self.client = core.Client(self.conf["EDITH_URL"])
        self.history = core.History()
        self.first = first_command
        self.data, self.data_at, self.quotes = {}, 0.0, None
        self.busy, self.open, self.layer = False, False, False
        self.status, self.greeted, self.pending_greet, self.last_greet = "ok", False, False, 0.0
        self.kind, self.scene, self.scene_at = "edith", "idle", 0.0
        self.view = None                                                   # None = dal vivo; altrimenti indice nella cronologia
        self._tw = None
        self.voice_mode = self.conf["EDITH_ISLAND_VOICE"]                  # auto: a voce quando le parli; always; off
        self.speaker = voice.Speaker(self.conf["EDITH_PIPER_PYTHON"], self.conf["EDITH_VOICE_MODEL"], float(self.conf["EDITH_VOICE_SPEED"]))
        self.recorder, self._listen_timer, self._voice_end, self.voicing = None, 0, 0, False
        self.ipc = core.CommandServer(lambda cmd: GLib.idle_add(self.command, cmd))
        self.win = None

    # -- ciclo di vita ----------------------------------------------------------------------------------------------
    def do_startup(self):
        Gtk.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is None:
            print("Nessuno schermo disponibile: l'isola va avviata dentro la sessione di Hyprland.", file=sys.stderr)
            self.quit()
            return
        self.hold()
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        if not self.ipc.start():
            print("Un'isola di EDITH è già aperta.", file=sys.stderr)
            self.quit()
            return
        self.build()

    def do_activate(self):
        if self.first:
            cmd, self.first = self.first, None
            self.command(cmd)

    def do_shutdown(self):
        self.ipc.stop()
        if self.recorder:
            self.recorder.stop(keep=False)
        self.speaker.close()
        Gtk.Application.do_shutdown(self)

    def command(self, cmd: str):
        if not self.win:
            return False
        if cmd == "toggle":
            self.hide() if self.open else self.show(welcome=True)
        elif cmd == "show":
            self.show(welcome=True)
        elif cmd == "ask":
            self.show(focus=True)
        elif cmd == "hide":
            self.hide()
        elif cmd == "refresh":
            self.refresh_now(force=True)
        elif cmd == "quit":
            self.quit()
        elif cmd == "listen":
            self.show(focus=False)
            self.start_listening()
        elif cmd == "stop":
            self.stop_listening(send=True)
        elif cmd == "talk":
            if self.recorder:
                self.stop_listening(send=True)
            else:
                self.show(focus=False)
                self.start_listening()
        return False

    # -- costruzione ------------------------------------------------------------------------------------------------
    def build(self):
        win = Gtk.ApplicationWindow(application=self, title="EDITH")
        win.set_decorated(False)
        win.set_resizable(False)
        win.add_css_class("edith-window")
        width = max(int(self.conf["EDITH_ISLAND_WIDTH"]), 460)
        win.set_default_size(width, -1)
        self.layer = bool(LayerShell and LayerShell.is_supported())
        if self.layer:
            LayerShell.init_for_window(win)
            LayerShell.set_namespace(win, "edith-island")
            LayerShell.set_layer(win, LayerShell.Layer.TOP)
            LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
            pos = self.conf["EDITH_ISLAND_POSITION"]
            if pos in ("left", "right"):
                edge = LayerShell.Edge.LEFT if pos == "left" else LayerShell.Edge.RIGHT
                LayerShell.set_anchor(win, edge, True)
                LayerShell.set_margin(win, edge, 12)
            LayerShell.set_margin(win, LayerShell.Edge.TOP, 6)
            LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        win.add_controller(keys)

        self.rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=240)
        self.rev.connect("notify::child-revealed", self.on_revealed)
        plate = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["plate"])
        plate.set_size_request(width - 24, -1)
        self.rev.set_child(plate)
        win.set_child(self.rev)

        # in alto: il personaggio, il nome, lo stato, la domanda
        top = Gtk.Box(spacing=14, css_classes=["top"])
        self.avatar = Avatar(self.greet, size=132)
        top.append(self.avatar)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        info.append(Gtk.Label(label="EDITH", xalign=0, css_classes=["name"]))
        st = Gtk.Box(spacing=7)
        self.dot = Gtk.Label(label="●", css_classes=["dot", "ok"])
        self.sub = Gtk.Label(label="pronta", xalign=0, css_classes=["sub"], ellipsize=Pango.EllipsizeMode.END)
        st.append(self.dot)
        st.append(self.sub)
        info.append(st)
        self.asked = Gtk.Label(xalign=0, halign=Gtk.Align.START, css_classes=["asked"], ellipsize=Pango.EllipsizeMode.END, max_width_chars=32)
        self.asked.set_visible(False)
        info.append(self.asked)
        top.append(info)
        btns = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.START)
        self.voice_btn = Gtk.Button(css_classes=["hbtn"])
        self.voice_btn.connect("clicked", self.on_voice_toggle)
        self._voice_last = self.voice_mode if self.voice_mode != "off" else "auto"
        self.update_voice_btn()
        for icon, tip, fn in (("×", "Chiudi (Esc)", lambda *_: self.hide()), ("↗", "Apri EDITH nel browser", self.on_open_web), ("⟲", "Ricomincia", self.on_clear)):
            b = Gtk.Button(label=icon, tooltip_text=tip, css_classes=["hbtn"])
            b.connect("clicked", fn)
            btns.append(b)
        btns.append(self.voice_btn)
        top.append(btns)
        plate.append(top)

        # il fumetto
        self.tail = Tail(self)
        self.tail.set_margin_start(66 - 17)
        plate.append(self.tail)
        self.balloon = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["balloon"])
        skip = Gtk.GestureClick()
        skip.connect("pressed", lambda *_: self.finish_typing())
        self.balloon.add_controller(skip)
        self.say_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=380)
        self.say = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=["say"], max_width_chars=40)
        self.say.set_focusable(False)
        self.say_scroll.set_child(self.say)
        self.balloon.append(self.say_scroll)
        self.confirm_box = Gtk.Box(spacing=8, css_classes=["confirm"])
        self.confirm_box.set_visible(False)
        self.balloon.append(self.confirm_box)
        self.timeline = Timeline()
        self.timeline.set_visible(False)
        self.balloon.append(self.timeline)
        nav = Gtk.Box(spacing=4, halign=Gtk.Align.END, css_classes=["nav"])
        self.prev_btn = Gtk.Button(label="‹ prima", tooltip_text="La risposta di prima")
        self.next_btn = Gtk.Button(label="dopo ›", tooltip_text="La risposta dopo")
        self.pos_lbl = Gtk.Label(css_classes=["pos"])
        self.prev_btn.connect("clicked", lambda *_: self.browse(-1))
        self.next_btn.connect("clicked", lambda *_: self.browse(+1))
        for w in (self.prev_btn, self.pos_lbl, self.next_btn):
            nav.append(w)
        self.nav = nav
        self.balloon.append(nav)
        plate.append(self.balloon)

        # sotto: le domande rapide e la casella
        chips = Gtk.Box(spacing=8, css_classes=["chips"])
        for label, text in CHIPS:
            b = Gtk.Button(label=label, tooltip_text=text)
            b.connect("clicked", lambda _b, t=text: self.send(t))
            chips.append(b)
        plate.append(chips)
        row = Gtk.Box(spacing=10, css_classes=["talk"])
        self.entry = Gtk.Entry(hexpand=True, placeholder_text="Parla con EDITH…", css_classes=["ask"])
        self.entry.connect("activate", lambda e: self.send(e.get_text()))
        self.send_btn = Gtk.Button(label="↑", tooltip_text="Invia (Invio)", css_classes=["send"])
        self.send_btn.connect("clicked", lambda *_: self.send(self.entry.get_text()))
        self.mic = Gtk.Button(label="🎙", tooltip_text="Tieni premuto e parla", css_classes=["mic"])
        hold = Gtk.GestureClick()
        hold.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        hold.connect("pressed", lambda *_: self.start_listening())
        hold.connect("released", lambda *_: self.stop_listening(send=True))
        self.mic.add_controller(hold)
        row.append(self.entry)
        row.append(self.mic)
        row.append(self.send_btn)
        plate.append(row)

        nick = self.conf["EDITH_NICKNAME"]
        self.speak(f"Ciao{(' ' + GLib.markup_escape_text(nick)) if nick else ''}. Un attimo che guardo com'è la giornata…", animate=False)
        self.update_nav()
        self.win = win

    # -- aprire e chiudere ------------------------------------------------------------------------------------------
    def show(self, focus: bool = True, welcome: bool = False):
        if self.layer:
            LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.EXCLUSIVE)
        self.win.set_visible(True)
        self.win.present()
        self.open = True
        self.avatar.start()
        GLib.idle_add(lambda: (self.rev.set_reveal_child(True), False)[1])
        if focus:
            GLib.timeout_add(60, lambda: (self.entry.grab_focus(), False)[1])
        voice_on = self.voice_mode != "off"
        if (not self.greeted or (welcome and voice_on and time.time() - self.last_greet >= GREET_COOLDOWN)) \
                and not self.busy and not self.recorder:                     # benvenuto: la prima volta, e a voce aprendo dall'icona
            self.greeted, self.pending_greet = True, True
            GLib.timeout_add(1500, self._greet_fallback)
        elif self.scene == "answer" and time.time() - self.scene_at > STALE_SCENE and not self.busy:
            self.scene = "idle"                                               # risposta vecchia: si torna al riepilogo
            self.view = None
            if self.data:
                self.render_idle()
        self.refresh_now(force=self.pending_greet)

    def hide(self):
        if not self.open:
            return
        self.open = False
        if self.recorder:
            self.stop_listening(send=False)
        self.stop_voice()
        self.finish_typing()
        self.rev.set_reveal_child(False)
        if self.layer:
            LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)

    def on_revealed(self, rev, _param):
        if not rev.get_child_revealed() and not self.open:
            self.win.set_visible(False)
            self.avatar.stop()

    def on_key(self, _ctl, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        if keyval == Gdk.KEY_Up and not self.entry.get_text():
            last = self.history.last_question()
            if last:
                self.entry.set_text(last)
                self.entry.set_position(-1)
            return True
        if keyval in (Gdk.KEY_Page_Up, Gdk.KEY_Page_Down):
            self.browse(-1 if keyval == Gdk.KEY_Page_Up else 1)
            return True
        if keyval in (Gdk.KEY_l, Gdk.KEY_L) and state & Gdk.ModifierType.CONTROL_MASK:
            self.on_clear()
            return True
        return False

    # -- il fumetto -------------------------------------------------------------------------------------------------
    def speak(self, markup: str, kind: str = "edith", asked: str = None, confirm: dict = None, timeline=None, animate: bool = True,
              duration: float = None, heard: bool = False):
        """EDITH dice qualcosa nel fumetto: testo (markup già sicuro), domanda a cui risponde, conferma, linea della giornata."""
        self.finish_typing(silent=True)
        self.kind = kind
        for c in ("edith", "error", "thinking"):
            self.balloon.remove_css_class(c)
        self.balloon.add_css_class(kind)
        self.tail.queue_draw()
        self.asked.set_visible(bool(asked))
        if asked:
            self.asked.set_text(f"{'Hai detto' if heard else 'Hai chiesto'} · «{asked}»")
        self.timeline.set_data(timeline)
        self._pending_confirm = confirm
        self.confirm_box.set_visible(False)
        total = core.visible_len(markup)
        if not animate or total == 0:
            self.say.set_markup(markup)
            self._after_speak(talked=False)
            return
        secs = max(0.3, duration * 0.92) if duration else max(0.45, min(1.8, total / 70))  # con la voce: al ritmo dell'audio
        step = max(1, round(total / secs / 60)) if not duration else max(1, math.ceil(total / secs / 60))
        self._tw = {"markup": markup, "n": 0, "total": total, "step": step}
        self.say.set_selectable(False)
        self.avatar.calm()
        self.avatar.set_base("talk")
        self.set_sub("ti sta rispondendo")
        self._tw["id"] = GLib.timeout_add(16, self._type_tick)

    def _type_tick(self):
        tw = self._tw
        if not tw:
            return False
        tw["n"] = min(tw["total"], tw["n"] + tw["step"])
        self.say.set_markup(core.markup_prefix(tw["markup"], tw["n"]))
        if tw["n"] >= tw["total"]:
            self._tw = None
            self._after_speak(talked=True)
            return False
        return True

    def finish_typing(self, silent: bool = False):
        tw, self._tw = self._tw, None
        if tw:
            GLib.source_remove(tw["id"])
            self.say.set_markup(tw["markup"])
            if not silent:
                self._after_speak(talked=True)

    def mood_now(self):
        """Umore e riga di stato giusti per questo momento: ascolto e voce hanno la precedenza su tutto."""
        if self.recorder:
            return "listen", "ti ascolto… lascia per inviare"
        if self.voicing:
            return None, "ti sta rispondendo"                                  # None: la bocca segue già l'audio
        if self.busy:
            return "thinking", "sto pensando…"
        return STATUS_MOOD.get(self.status, "idle"), STATUS_TEXT.get(self.status, "")

    def _after_speak(self, talked: bool):
        self.say.set_selectable(True)
        mood, sub = self.mood_now()
        if mood:
            self.avatar.set_base(mood)
            if talked and self.kind == "edith" and mood not in ("listen", "thinking"):
                self.avatar.flash("happy", 1.0)
        self.set_sub(sub)
        c = getattr(self, "_pending_confirm", None)
        self._pending_confirm = None
        child = self.confirm_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.confirm_box.remove(child)
            child = nxt
        if c:
            self.fill_confirm(c)
        self.say_scroll.get_vadjustment().set_value(0)

    def render_idle(self, greet: bool = False, aloud: bool = False):
        now = datetime.now().astimezone()
        text = core.idle_line(core.summary(self.data, now))
        if greet:
            text = GLib.markup_escape_text(core.greeting(datetime.now(), self.conf["EDITH_NICKNAME"])) + " " + text
        self.scene, self.view = "idle", None
        self.present(strongify(text), timeline=core.timeline(self.data.get("today") or {}, now), say_text=core.plain(text), force_voice=aloud)
        self.update_nav()

    def greet(self):
        """Il saluto (toccando la faccia, o aprendo l'isola): con l'audio attivo lo dice anche a voce."""
        if self.recorder or self.busy:
            return
        self.stop_voice()
        self.last_greet = time.time()
        self.avatar.flash("greet", 1.6)
        if self.data:
            self.render_idle(greet=True, aloud=True)
            GLib.timeout_add(80, lambda: (self.avatar.flash("greet", 1.6), False)[1])   # il saluto con la mano vince sulla bocca
        else:
            hello = core.greeting(datetime.now(), self.conf["EDITH_NICKNAME"])
            self.present(GLib.markup_escape_text(hello), say_text=hello, force_voice=True)

    def _greet_fallback(self):
        if self.pending_greet:                                                 # i dati tardano: intanto saluta
            self.pending_greet = False
            self.greet()
        return False

    # -- dati del momento -------------------------------------------------------------------------------------------
    def refresh_now(self, force: bool = False):
        if not force and time.time() - self.data_at < REFRESH_EVERY:
            return
        self.data_at = time.time()
        in_thread(self.client.cards, self.on_cards)

    def on_cards(self, data, err):
        if err:
            auth = isinstance(err, core.AuthError)
            self.set_dot("auth" if auth else "offline")
            self.data_at = 0.0
            if self.scene == "idle" and not self.busy:
                self.pending_greet = False
                msg = ("Questo PC non è tra gli accessi ammessi: va aggiunto a WEB_ALLOWED_LOGINS sul server." if auth
                       else "Non riesco a raggiungere il server. Controlla che Tailscale sia acceso.")
                self.speak(GLib.markup_escape_text(msg), kind="error")
            return
        self.data = data
        self.set_dot(core.summary(data, datetime.now().astimezone())["status"])
        if self.pending_greet:
            self.pending_greet = False
            self.greet()
        elif self.scene == "idle" and not self.busy and not self._tw and not self.recorder and not self.voicing:
            self.render_idle()

    def set_dot(self, status: str):
        for c in ("ok", "alert", "offline", "auth"):
            self.dot.remove_css_class(c)
        self.dot.add_css_class(status)
        self.status = status
        if not self.busy and not self._tw and not self.recorder and not self.voicing:
            self.avatar.set_base(STATUS_MOOD.get(status, "idle"))
            self.set_sub(STATUS_TEXT.get(status, ""))

    def set_sub(self, text: str):
        self.sub.set_text(text)

    # -- conversazione ----------------------------------------------------------------------------------------------
    def send(self, text: str):
        text = " ".join((text or "").split())
        if not text or self.busy:
            return
        self.entry.set_text("")
        self.history.add("me", text)
        self.view = None
        self.set_busy(True)
        self.speak('<span foreground="#b85700" weight="bold">●  ●  ●</span>', kind="thinking", asked=text, animate=False)
        in_thread(lambda: self.client.ask(text), lambda r, e: self.on_reply(r, e, text))

    def on_reply(self, reply, err, asked, from_voice: bool = False):
        self.set_busy(False)
        self.scene, self.scene_at = "answer", time.time()
        if from_voice and reply and (reply.get("heard") or "").strip():
            asked = reply["heard"].strip()
            self.history.add("me", asked)
        if err:
            msg = str(err) if isinstance(err, core.EdithError) else "Qualcosa è andato storto. Riprova."
            self.history.add("error", msg)
            self.avatar.flash("sad", 2.2)
            self.present(GLib.markup_escape_text(msg), kind="error", asked=asked, say_text=msg, from_voice=from_voice)
            if isinstance(err, core.AuthError):
                self.set_dot("auth")
        elif reply.get("error") and not reply.get("html"):
            self.history.add("error", str(reply["error"]))
            self.present(GLib.markup_escape_text(str(reply["error"])), kind="error", asked=asked, say_text=str(reply["error"]), from_voice=from_voice)
        else:
            html_text = reply.get("html") or "…"
            ok = reply.get("ok", True)
            self.history.add("edith" if ok else "error", html_text)
            tl = core.timeline((self.data or {}).get("today") or {}, datetime.now().astimezone()) if core.is_agenda(html_text) else None
            self.present(strongify(core.to_pango(html_text)) if ok else GLib.markup_escape_text(core.plain(html_text)),
                         kind="edith" if ok else "error", asked=asked, confirm=reply.get("confirm"), timeline=tl,
                         say_text=(reply.get("speak") or core.plain(html_text)), from_voice=from_voice)
            if reply.get("refresh"):
                self.refresh_now(force=True)
        self.update_nav()
        GLib.timeout_add(80, lambda: (self.entry.grab_focus(), False)[1])

    def set_busy(self, busy: bool):
        self.busy = busy
        self.send_btn.set_sensitive(not busy)
        self.mic.set_sensitive(not busy)
        mood, sub = self.mood_now()
        if mood:
            self.avatar.set_base(mood)
        self.set_sub(sub)

    def fill_confirm(self, c: dict):
        buttons = []

        def decide(token, yes):
            for b in buttons:
                b.set_sensitive(False)
            self.set_busy(True)
            self.speak('<span foreground="#b85700" weight="bold">●  ●  ●</span>', kind="thinking", asked=self.asked.get_text()[13:-1] or None, animate=False)
            in_thread(lambda: self.client.confirm(token, yes), lambda r, e: self.on_reply(r, e, None))
        if c.get("choices"):
            for i, ch in enumerate(c["choices"], 1):
                b = Gtk.Button(label=f"{i}. {ch.get('label', '')}", css_classes=["yes"])
                b.connect("clicked", lambda _b, t=ch.get("token"): decide(t, True))
                buttons.append(b)
        else:
            b = Gtk.Button(label=c.get("yes") or "Sì", css_classes=["yes"])
            b.connect("clicked", lambda _b: decide(c.get("token"), True))
            buttons.append(b)
        b = Gtk.Button(label=c.get("no") or "Annulla")
        b.connect("clicked", lambda _b: decide(c.get("token"), False))
        buttons.append(b)
        for b in buttons:
            self.confirm_box.append(b)
        self.confirm_box.set_visible(True)


    # -- la voce: ascoltare (tieni premuto) e rispondere a voce (Piper) ---------------------------------------------
    def present(self, markup: str, kind: str = "edith", asked: str = None, confirm: dict = None, timeline=None,
                say_text: str = None, from_voice: bool = False, force_voice: bool = False):
        """Come speak(), ma se serve dà anche la voce: prima prepara l'audio (fuori dalla grafica), poi testo e voce insieme.
        A voce se l'audio è attivo e: la voce è su «sempre», oppure le hai parlato, oppure è un saluto (force_voice)."""
        want = bool(say_text) and self.voice_mode != "off" and (self.voice_mode == "always" or from_voice or force_voice)
        problem = self.speaker.problem() if want else None
        if not want or problem:
            if problem:
                print("Voce:", problem, file=sys.stderr)
            self.speak(markup, kind, asked, confirm, timeline, heard=from_voice)
            if problem and from_voice:
                self.set_sub("voce non disponibile: guarda la guida")
            return
        self.set_busy(True)

        def done(wav, err):
            self.set_busy(False)
            dur = 0.0
            if not err:
                try:
                    dur = self.speaker.play(wav)
                except voice.VoiceError as e:
                    err = e
            if err:
                print("Voce:", err, file=sys.stderr)
                self.speak(markup, kind, asked, confirm, timeline, heard=from_voice)
                self.set_sub("voce non disponibile")
                return
            self.voicing = True
            self.speak(markup, kind, asked, confirm, timeline, duration=dur, heard=from_voice)
            self.avatar.start_voice(voice.envelope(wav))
            self.set_sub("ti sta rispondendo")
            self._voice_end = GLib.timeout_add(int(dur * 1000) + 150, self.end_voice)
        in_thread(lambda: self.speaker.synthesize(say_text), done)

    def end_voice(self):
        self._voice_end = 0
        if self.voicing:
            self.voicing = False
            if not self._tw:
                mood, sub = self.mood_now()
                self.avatar.set_base(mood or "idle")
                if mood not in ("listen", "thinking"):
                    self.avatar.flash("happy", 0.8)
                self.set_sub(sub)
        return False

    def stop_voice(self):
        self.speaker.stop()
        if self._voice_end:
            GLib.source_remove(self._voice_end)
            self._voice_end = 0
        self.end_voice()

    def start_listening(self):
        if self.recorder or self.busy:
            return
        self.stop_voice()
        self.finish_typing(silent=True)
        rec = voice.Recorder(on_level=lambda v: GLib.idle_add(self.avatar.set_level, v))
        try:
            rec.start()
        except voice.VoiceError as e:
            self.speak(GLib.markup_escape_text(str(e)), kind="error")
            return
        self.recorder = rec
        self.scene = "listen"
        self.mic.add_css_class("on")
        self.avatar.calm()
        self.speak(GLib.markup_escape_text("Ti ascolto…"), kind="thinking", animate=False)       # _after_speak mette «listen»
        self._listen_timer = GLib.timeout_add(int(voice.MAX_SECONDS * 1000), lambda: (self.stop_listening(send=True), False)[1])

    def stop_listening(self, send: bool = True):
        rec, self.recorder = self.recorder, None
        if not rec:
            return
        if self._listen_timer:
            GLib.source_remove(self._listen_timer)
            self._listen_timer = 0
        self.mic.remove_css_class("on")

        mood, sub = self.mood_now()                                            # il registratore è già sganciato: stato normale
        self.avatar.set_base(mood or "idle")
        self.set_sub(sub)

        def done(wav, err):
            self.avatar.set_level(0.0)
            if not send:
                return
            if err or not wav:
                self.scene = "answer"
                self.speak(GLib.markup_escape_text("Non ho sentito niente: tieni premuto mentre parli."), kind="error")
                return
            self.send_voice(wav)
        in_thread(lambda: rec.stop(keep=send), done)

    def send_voice(self, wav: bytes):
        self.view = None
        self.set_busy(True)
        self.speak('<span foreground="#b85700" weight="bold">●  ●  ●</span>', kind="thinking", animate=False)
        in_thread(lambda: self.client.voice(wav), lambda r, e: self.on_reply(r, e, None, from_voice=True))

    def on_voice_toggle(self, *_):
        if self.voice_mode == "off":
            self.voice_mode = self._voice_last
        else:
            self._voice_last, self.voice_mode = self.voice_mode, "off"
            self.stop_voice()
        self.update_voice_btn()

    def update_voice_btn(self):
        off = self.voice_mode == "off"
        self.voice_btn.set_label("🔇" if off else "🔊")
        self.voice_btn.set_tooltip_text("Voce spenta: clic per riaccenderla" if off else
                                        ("Risponde sempre anche a voce" if self.voice_mode == "always" else "Risponde a voce quando le parli") + " · clic per spegnere")

    # -- le risposte di prima ---------------------------------------------------------------------------------------
    def browse(self, delta: int):
        pairs = core.history_pairs(self.history.items)
        if not pairs or self.busy:
            return
        cur = len(pairs) - 1 if self.view is None else self.view
        new = max(0, min(len(pairs) - 1, cur + delta))
        if self.view is None and delta > 0:
            return
        self.view = None if new == len(pairs) - 1 and delta > 0 else new
        p = pairs[new]
        self.scene, self.scene_at = "answer", time.time()
        text = strongify(core.to_pango(p["a"])) if p["who"] == "edith" else GLib.markup_escape_text(core.plain(p["a"]))
        self.speak(text, kind=p["who"], asked=p["q"], animate=False)
        self.update_nav(len(pairs), new)

    def update_nav(self, total: int = None, index: int = None):
        if total is None:
            total = len(core.history_pairs(self.history.items))
            index = None if self.view is None else self.view
        self.nav.set_visible(total > 1 or (total == 1 and self.scene == "idle"))
        live = index is None
        shown = total - 1 if live else index
        self.prev_btn.set_sensitive(total > 0 and (shown > 0 or (live and self.scene == "idle")))
        self.next_btn.set_sensitive(not live and shown < total - 1)
        self.pos_lbl.set_text("" if live and self.scene == "idle" else f"{shown + 1}/{total}")

    def on_clear(self, *_):
        self.history.clear()
        self.view = None
        self.update_nav()
        if self.data:
            self.render_idle()

    def on_open_web(self, *_):
        Gio.AppInfo.launch_default_for_uri(self.conf["EDITH_URL"], None)
        self.hide()


def run(first_command: str = None) -> int:
    app = Island(first_command)
    snap = os.environ.get("EDITH_ISLAND_SNAPSHOT")                 # solo per le prove: salva un'immagine e chiude
    if snap:
        _snapshot_mode(app, snap, os.environ.get("EDITH_ISLAND_SCRIPT", ""))
    return app.run([sys.argv[0]])


def _snapshot_mode(app, path, script):
    app.greeted = script not in ("", ":saluta")                     # foto «normale» e «saluta»: il saluto parte; nelle altre no

    def shoot():
        win = app.win
        w, h = win.get_width(), win.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(win).snapshot(snap, w, h)
        node = snap.to_node()
        if node:
            win.get_native().get_renderer().render_texture(node, None).save_to_png(path)
        app.quit()
        return False

    def start():
        if not app.win:
            app.quit()
            return False
        app.show()
        if script == ":ascolta":
            GLib.timeout_add(1200, lambda: (app.start_listening(), False)[1])
        elif script == ":parla":
            GLib.timeout_add(1200, lambda: (app.start_listening(), False)[1])
            GLib.timeout_add(2900, lambda: (app.stop_listening(send=True), False)[1])
        elif script == ":pensa":
            def think():
                app.history.add("me", "che promemoria ho?")
                app.set_busy(True)
                app.speak('<span foreground="#b85700" weight="bold">●  ●  ●</span>', kind="thinking", asked="che promemoria ho?", animate=False)
                return False
            GLib.timeout_add(1500, think)
        elif script.startswith(":storia"):
            GLib.timeout_add(1200, lambda: (app.browse(-1), False)[1])
        elif script and not script.startswith(":"):
            GLib.timeout_add(1500, lambda: (app.send(script), False)[1])
        GLib.timeout_add(int(os.environ.get("EDITH_ISLAND_SNAPSHOT_MS", "4500")), shoot)
        return False
    app.connect("activate", lambda *_: GLib.timeout_add(200, start))
