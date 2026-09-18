#!/usr/bin/python3
import hashlib
import json
import os
import signal
import sys
import tempfile
import tomllib
from pathlib import Path
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, GLibUnix, Graphene, Gtk, Pango


APP_ID = "io.github.sagosand.PerfectNote"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "perfect-note"


DEFAULT_PALETTE = {
    "background": "#202522", "foreground": "#eee9db", "surface": "#48523a",
    "border": "#42483d", "muted": "#a4ad9a", "accent": "#d5e5b7",
    "selection": "#526347", "selected": "#fff8e8", "error": "#edafa0",
}


def theme_paths():
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (state / "omarchy/current/theme/colors.toml", config / "omarchy/current/theme/colors.toml")


def color_channels(color):
    return tuple(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))


def mix_color(start, end, amount):
    return "#" + "".join(f"{round((a * (1 - amount) + b * amount) * 255):02x}" for a, b in zip(color_channels(start), color_channels(end)))


def contrast(first, second):
    def luminance(color):
        channels = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in color_channels(color)]
        return sum(value * weight for value, weight in zip(channels, (0.2126, 0.7152, 0.0722)))
    a, b = sorted((luminance(first), luminance(second)))
    return (b + 0.05) / (a + 0.05)


def readable_color(color, background, fallback):
    if contrast(color, background) >= 4.5:
        return color
    if contrast(fallback, background) >= 4.5:
        return fallback
    return max(("#000000", "#ffffff"), key=lambda value: contrast(value, background))


def read_theme(paths):
    for path in paths:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None
        colors = {}
        for key, value in raw.items():
            color = Gdk.RGBA()
            if isinstance(value, str) and color.parse(value):
                colors[key] = "#" + "".join(f"{round(channel * 255):02x}" for channel in (color.red, color.green, color.blue))
        def pick(*keys, default=None):
            return next((colors[key] for key in keys if key in colors), default)
        background = pick("background", "bg", "color0")
        foreground = pick("foreground", "fg", "color7")
        if not background or not foreground:
            return None
        accent = pick("accent", "blue", "color4", default=foreground)
        selection = pick("selection", "selection_background", "color8", default=mix_color(background, accent, 0.35))
        return {
            "background": background,
            "foreground": foreground,
            "surface": pick("lighter_background", "lighter_bg", default=mix_color(background, foreground, 0.08)),
            "border": mix_color(background, foreground, 0.22),
            "muted": readable_color(pick("light_foreground", "light_fg", "muted", default=foreground), background, foreground),
            "accent": readable_color(accent, background, foreground),
            "selection": selection,
            "selected": readable_color(pick("selection_foreground", default=foreground), selection, foreground),
            "error": readable_color(pick("red", "color1", default=foreground), background, foreground),
        }
    return None


def write_note(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = text.encode("utf-8") if isinstance(text, str) else text
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_text(text):
    if not isinstance(text, str) or "\0" in text:
        raise ValueError("The note must contain text without NUL characters")
    text.encode("utf-8")


class NoteView(Gtk.TextView):
    def __init__(self, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner

    def do_size_allocate(self, width, height, baseline):
        Gtk.TextView.do_size_allocate(self, width, height, baseline)
        self.owner.queue_gutter()

    def do_snapshot_layer(self, layer, snapshot):
        if layer != Gtk.TextViewLayer.ABOVE_TEXT:
            return
        visible = self.get_visible_rect()
        color = Gdk.RGBA()
        color.parse(self.owner.palette["border"])
        rect = Graphene.Rect()
        rect.init(62, visible.y, 1, visible.height)
        snapshot.append_color(color, rect)
        color.parse(self.owner.palette["muted"])
        for paste, y in self.owner.stamp_layout():
            if y + 26 < visible.y or y > visible.y + visible.height:
                continue
            if paste is self.owner.marked_paste:
                color.parse(self.owner.palette["selection"])
                rect.init(2, y - 2, 56, 26)
                snapshot.append_color(color, rect)
                color.parse(self.owner.palette["selected"])
            else:
                color.parse(self.owner.palette["muted"])
            date = datetime.fromisoformat(paste["at"])
            layout = self.create_pango_layout(date.strftime("%H:%M\n%d %b"))
            layout.set_font_description(Pango.FontDescription("Sans 7"))
            layout.set_alignment(Pango.Alignment.RIGHT)
            layout.set_width(48 * Pango.SCALE)
            point = Graphene.Point()
            point.init(4, y)
            snapshot.save()
            snapshot.translate(point)
            snapshot.append_layout(layout, color)
            snapshot.restore()


class PerfectNote(Gtk.Application):
    def __init__(self, data_dir=DATA_DIR, application_id=APP_ID, palette_paths=None):
        super().__init__(application_id=application_id, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.palette_paths = theme_paths() if palette_paths is None else palette_paths
        self.palette = None
        self.theme_source = 0
        self.style_provider = None
        self.note_path = data_dir / "note.txt"
        self.state_path = data_dir / "note.json"
        self.pastes = []
        self.marked_paste = None
        self.marked_word = None
        self.word_column = None
        self.history = []
        self.history_position = -1
        self.restoring = False
        self.action_depth = 0
        self.action_dirty = False
        self.replacement = None
        self.inserting_paste = False
        self.history_changed = False
        self.edit_count = 0
        self.replay_count = 0
        self.pending_pastes = []
        self.quit_requested = False
        self.selecting = False
        self.gutter_source = 0
        self.window = None
        self.save_source = 0
        self.dirty = False
        self.load_error = False
        self.connect("shutdown", self.shutdown)

    def do_activate(self):
        if self.window is None:
            self.build_window()
        elif self.window.get_visible():
            self.hide()
            return
        self.refresh_theme()
        self.prepare_line()
        self.window.present()
        self.editor.grab_focus()
        self.queue_gutter()
        GLib.idle_add(self.scroll_to_cursor)

    def refresh_theme(self):
        palette = read_theme(self.palette_paths) or self.palette or DEFAULT_PALETTE.copy()
        if palette != self.palette:
            definitions = "\n".join(f"@define-color pn_{key} {value};" for key, value in palette.items())
            self.style_provider.load_from_data((definitions + "\n" + self.style_css).encode())
            self.palette = palette
            if self.window is not None:
                self.editor.queue_draw()
        return GLib.SOURCE_CONTINUE

    def stop_theme(self):
        if self.theme_source:
            GLib.source_remove(self.theme_source)
            self.theme_source = 0
        if self.style_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), self.style_provider)
            self.style_provider = None

    def shutdown(self, *_):
        self.save()
        self.stop_theme()

    def build_window(self):
        self.style_css = Path(__file__).with_name("style.css").read_text(encoding="utf-8")
        self.style_provider = Gtk.CssProvider()
        self.refresh_theme()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.style_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Omarchy replaces the theme directory, so re-open the path on each check.
        self.theme_source = GLib.timeout_add_seconds(2, self.refresh_theme)
        self.window = Gtk.ApplicationWindow(application=self, title="Perfect Note")
        self.window.set_default_size(638, 845)
        self.window.set_size_request(420, 440)
        self.window.add_css_class("perfect-note")
        self.window.connect("close-request", self.hide)

        header = Gtk.HeaderBar()
        header.set_show_title_buttons(False)
        header.set_title_widget(Gtk.Label(label="YOUR EVERYDAY SCRATCHPAD", css_classes=["eyebrow"]))
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Hide (Esc)")
        close.connect("clicked", self.hide)
        header.pack_end(close)
        self.window.set_titlebar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window.set_child(body)
        self.error = Gtk.Label(wrap=True, visible=False, css_classes=["error"])
        body.append(self.error)

        self.buffer = Gtk.TextBuffer()
        self.buffer.set_enable_undo(True)
        self.editor = NoteView(self, buffer=self.buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.editor.set_left_margin(80)
        self.editor.set_right_margin(24)
        self.editor.set_top_margin(22)
        self.editor.set_bottom_margin(40)
        self.editor.set_pixels_above_lines(3)
        self.editor.set_pixels_below_lines(4)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["paper"])
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.editor)
        overlay = Gtk.Overlay()
        overlay.set_child(scroller)
        self.placeholder = Gtk.Label(label="Paste something worth keeping.\nOr just start typing.", halign=Gtk.Align.START, valign=Gtk.Align.START, css_classes=["placeholder"])
        self.placeholder.set_can_target(False)
        overlay.add_overlay(self.placeholder)
        body.append(overlay)

        try:
            content = self.load_note()
        except (OSError, ValueError, KeyError, TypeError) as error:
            content = ""
            self.pastes = []
            self.load_error = True
            self.editor.set_editable(False)
            self.show_error("Could not open note", error)
        self.buffer.set_text(content)
        self.buffer.set_modified(False)
        self.placeholder.set_visible(not content and not self.load_error)
        self.buffer.connect("changed", self.changed)
        self.buffer.connect("insert-text", self.before_insert)
        self.buffer.connect("delete-range", self.before_delete)
        self.buffer.connect("mark-set", self.selection_changed)
        self.buffer.connect("begin-user-action", self.begin_action)
        self.buffer.connect_after("end-user-action", self.end_action)
        for action in ("undo", "redo"):
            self.buffer.connect(action, self.before_history)
            self.buffer.connect_after(action, self.after_history, action)
        self.remember()
        self.editor.connect("paste-clipboard", self.paste)
        middle = Gtk.GestureClick(button=2)
        middle.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        middle.connect("pressed", self.middle_paste)
        self.editor.add_controller(middle)
        click = Gtk.GestureClick(button=1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self.editor_clicked)
        self.editor.add_controller(click)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.key_pressed)
        self.window.add_controller(keys)

    def text(self):
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    def load_note(self):
        if self.state_path.exists():
            raw = self.state_path.read_text(encoding="utf-8")
            state = json.loads(raw)
            if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
                raise ValueError("Unsupported note format")
            content = state["text"]
            validate_text(content)
            pastes = state["pastes"]
            if not isinstance(pastes, list):
                raise ValueError("Invalid paste list")
            previous_end = 0
            for paste in pastes:
                if not isinstance(paste, dict) or type(paste.get("start")) is not int or type(paste.get("end")) is not int:
                    raise ValueError("Invalid paste position")
                if not previous_end <= paste["start"] < paste["end"] <= len(content):
                    raise ValueError("Invalid paste position")
                datetime.fromisoformat(paste["at"])
                previous_end = paste["end"]
            self.pastes = [dict(paste) for paste in pastes]
        else:
            try:
                content = self.note_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                content = ""
            validate_text(content)
        for path in (self.state_path, self.note_path):
            try:
                if path.exists():
                    write_note(path.with_name(path.name + ".bak"), path.read_bytes())
            except OSError as error:
                self.show_error("Could not update backup", error)
        return content

    def remember(self, replace=False):
        if self.action_depth:
            self.action_dirty = True
            return
        entry = (hashlib.sha256(self.text().encode()).digest(), [dict(paste) for paste in self.pastes], self.edit_count)
        if replace:
            self.history[self.history_position] = entry
        else:
            del self.history[self.history_position + 1:]
            self.history.append(entry)
            self.history_position += 1

    def begin_action(self, *_):
        self.action_depth += 1
        self.replacement = None

    def end_action(self, *_):
        self.action_depth -= 1
        if not self.action_depth:
            self.replacement = None
            if self.action_dirty and not self.restoring:
                self.remember()
            self.action_dirty = False

    def before_insert(self, buffer, location, text, length):
        if self.restoring:
            self.replay_count += len(text)
        else:
            self.edit_count += len(text)
        position = location.get_offset()
        replacement = self.replacement if not self.restoring and not self.inserting_paste else None
        replaced = replacement["paste"] if replacement and replacement["position"] == position else None
        updated = []
        for paste in self.pastes:
            if paste is replaced:
                paste["end"] += len(text)
            elif self.inserting_paste and paste["start"] < position < paste["end"]:
                updated.append({**paste, "end": position})
                paste = {**paste, "start": position + len(text), "end": paste["end"] + len(text)}
            elif paste["start"] >= position:
                paste["start"] += len(text)
                paste["end"] += len(text)
            elif paste["end"] > position:
                paste["end"] += len(text)
            updated.append(paste)
        if replaced is not None and not any(paste is replaced for paste in updated):
            updated.append({"start": position, "end": position + len(text), "at": replaced["at"]})
        self.pastes = sorted(updated, key=lambda paste: paste["start"])
        self.replacement = None

    def before_delete(self, buffer, start, end):
        first, last = start.get_offset(), end.get_offset()
        if self.restoring:
            self.replay_count += last - first
        else:
            self.edit_count += last - first
        self.replacement = None
        if self.action_depth and not self.restoring and not self.inserting_paste:
            for paste in self.pastes:
                if paste["start"] <= first < last <= paste["end"]:
                    self.replacement = {"paste": paste, "position": first}
                    break
        for paste in self.pastes:
            for key in ("start", "end"):
                paste[key] -= min(max(paste[key] - first, 0), last - first)
        self.pastes = [paste for paste in self.pastes if paste["end"] > paste["start"]]

    def before_history(self, *_):
        self.clear_marking()
        self.restoring = True
        self.history_changed = False
        self.replay_count = 0

    def after_history(self, buffer, action):
        if not self.history_changed:
            self.restoring = False
            return
        digest = hashlib.sha256(self.text().encode()).digest()
        step = -1 if action == "undo" else 1
        # GTK can merge actions. Replayed character counts distinguish repeated text.
        target = self.edit_count + step * self.replay_count
        stop = -1 if step == -1 else len(self.history)
        for index in range(self.history_position + step, stop, step):
            if self.history[index][0] == digest and self.history[index][2] == target:
                self.pastes = [dict(paste) for paste in self.history[index][1]]
                self.history_position = index
                self.edit_count = target
                break
        else:
            self.restoring = False
            self.load_error = True
            self.editor.set_editable(False)
            self.show_error("Undo failed - copy the note before reopening", "The timestamp history could not be restored; saving is paused to protect the stored note")
            return
        self.restoring = False
        self.editor.queue_draw()

    def prepare_line(self):
        self.clear_marking()
        if self.load_error:
            return
        end = self.buffer.get_end_iter()
        line = end.copy()
        line.set_line_offset(0)
        if self.buffer.get_text(line, end, True).strip():
            self.buffer.insert(end, "\n")
        self.buffer.place_cursor(self.buffer.get_end_iter())

    def scroll_to_cursor(self):
        self.editor.scroll_to_mark(self.buffer.get_insert(), 0.1, False, 0, 0)
        return GLib.SOURCE_REMOVE

    def changed(self, *_):
        self.clear_marking()
        self.placeholder.set_visible(self.buffer.get_char_count() == 0)
        self.dirty = True
        if self.restoring:
            self.history_changed = True
        else:
            self.remember()
        self.editor.queue_draw()
        self.queue_gutter()
        if self.save_source:
            GLib.source_remove(self.save_source)
        self.save_source = GLib.timeout_add(150, self.save)

    def save(self):
        if self.save_source:
            GLib.source_remove(self.save_source)
            self.save_source = 0
        if not self.dirty or self.load_error:
            return GLib.SOURCE_REMOVE
        try:
            state = {"version": 1, "text": self.text(), "pastes": self.pastes}
            write_note(self.state_path, json.dumps(state, ensure_ascii=False))
            write_note(self.note_path, self.text())
        except OSError as error:
            self.show_error("Save failed - Ctrl+S to retry", error)
            return GLib.SOURCE_REMOVE
        self.dirty = False
        self.error.set_visible(False)
        return GLib.SOURCE_REMOVE

    def hide(self, *_):
        self.save()
        if not self.dirty:
            self.window.set_visible(False)
        return True

    def request_quit(self):
        self.quit_requested = True
        if self.pending_pastes:
            return GLib.SOURCE_CONTINUE
        self.save()
        if self.dirty:
            self.quit_requested = False
            self.window.present()
        else:
            self.quit()
        return GLib.SOURCE_CONTINUE

    def show_error(self, message, error):
        self.error.set_text(message)
        self.error.set_tooltip_text(str(error))
        self.error.set_visible(True)

    def paste(self, editor):
        editor.stop_emission_by_name("paste-clipboard")
        self.read_clipboard(editor.get_clipboard())

    def middle_paste(self, gesture, count, x, y):
        self.clear_marking()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        x, y = self.editor.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        found, location = self.editor.get_iter_at_location(x, y)
        mark = self.buffer.create_mark(None, location, True)
        self.read_clipboard(self.editor.get_primary_clipboard(), mark)

    def read_clipboard(self, clipboard, mark=None):
        if self.load_error:
            if mark:
                self.buffer.delete_mark(mark)
            return
        if mark:
            start = end = self.buffer.get_iter_at_mark(mark)
            self.buffer.delete_mark(mark)
        else:
            bounds = self.buffer.get_selection_bounds()
            start, end = bounds if bounds else (self.buffer.get_iter_at_mark(self.buffer.get_insert()),) * 2
        request = {
            "start": self.buffer.create_mark(None, start, False),
            "end": self.buffer.create_mark(None, end, start.get_offset() != end.get_offset()),
            "original": self.buffer.get_text(start, end, True),
            "ready": False,
        }
        self.pending_pastes.append(request)
        clipboard.read_text_async(None, self.clipboard_ready, request)

    def clipboard_ready(self, clipboard, result, request):
        try:
            request["text"] = clipboard.read_text_finish(result)
        except GLib.Error as error:
            request["error"] = error
        request["ready"] = True
        while self.pending_pastes and self.pending_pastes[0]["ready"]:
            pending = self.pending_pastes.pop(0)
            try:
                if "error" in pending:
                    self.show_error("Could not paste clipboard", pending["error"])
                    continue
                if not pending["text"]:
                    continue
                start = self.buffer.get_iter_at_mark(pending["start"])
                end = self.buffer.get_iter_at_mark(pending["end"])
                if start.compare(end) > 0 or self.buffer.get_text(start, end, True) != pending["original"]:
                    self.show_error("Paste target changed - paste again", "The selected text was edited while the clipboard was loading")
                    continue
                self.buffer.select_range(end, start)
                self.insert_paste(pending["text"])
            finally:
                self.buffer.delete_mark(pending["start"])
                self.buffer.delete_mark(pending["end"])
        if not self.pending_pastes and self.quit_requested:
            self.request_quit()

    def insert_paste(self, text):
        if not text or self.load_error:
            return
        try:
            validate_text(text)
        except ValueError as error:
            self.show_error("Could not paste clipboard", error)
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.buffer.begin_user_action()
        self.inserting_paste = True
        try:
            self.buffer.delete_selection(True, True)
            cursor = self.buffer.get_iter_at_mark(self.buffer.get_insert())
            prefix = "" if cursor.starts_line() else "\n"
            suffix = "\n" if not cursor.is_end() and cursor.get_char() != "\n" and not text.endswith("\n") else ""
            start = cursor.get_offset() + len(prefix)
            self.buffer.insert_at_cursor(prefix + text + suffix, -1)
            self.pastes.append({"start": start, "end": start + len(text), "at": datetime.now().astimezone().isoformat(timespec="seconds")})
            self.pastes.sort(key=lambda paste: paste["start"])
            self.remember(replace=True)
        finally:
            self.inserting_paste = False
            self.buffer.end_user_action()
        self.editor.queue_draw()
        self.scroll_to_cursor()

    def clear_marking(self):
        self.marked_paste = None
        self.marked_word = None
        self.word_column = None
        self.editor.queue_draw()

    def selection_changed(self, buffer, location, mark):
        if not self.selecting and mark.get_name() in ("insert", "selection_bound"):
            self.clear_marking()

    def stamp_layout(self):
        previous_y = -26
        for paste in self.pastes:
            location = self.editor.get_iter_location(self.buffer.get_iter_at_offset(paste["start"]))
            y = max(location.y, previous_y + 26)
            yield paste, y
            previous_y = y

    def queue_gutter(self):
        if not self.gutter_source:
            self.gutter_source = GLib.idle_add(self.update_gutter)

    def update_gutter(self):
        self.gutter_source = 0
        last_y = max((y for paste, y in self.stamp_layout()), default=0)
        end_y = self.editor.get_iter_location(self.buffer.get_end_iter()).y
        margin = max(40, last_y + 26 - end_y + 12)
        if self.editor.get_bottom_margin() != margin:
            self.editor.set_bottom_margin(margin)
        return GLib.SOURCE_REMOVE

    def editor_clicked(self, gesture, count, x, y):
        x, y = self.editor.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        if 0 <= x < 62:
            for index, (paste, stamp_y) in enumerate(self.stamp_layout()):
                if stamp_y - 2 <= y < stamp_y + 24:
                    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                    self.select_paste(index)
                    return
        self.clear_marking()

    def select_offsets(self, start, end):
        self.selecting = True
        try:
            self.editor.grab_focus()
            self.buffer.select_range(self.buffer.get_iter_at_offset(start), self.buffer.get_iter_at_offset(end))
            self.scroll_to_cursor()
        finally:
            self.selecting = False
        self.editor.queue_draw()

    def select_paste(self, index):
        if not self.pastes:
            self.clear_marking()
            return
        self.marked_paste = self.pastes[max(0, min(index, len(self.pastes) - 1))]
        self.marked_word = None
        self.word_column = None
        self.select_offsets(self.marked_paste["start"], self.marked_paste["end"])

    def cycle_paste(self, direction):
        if self.marked_paste is not None:
            index = (self.pastes.index(self.marked_paste) + direction) % len(self.pastes)
        else:
            position = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()
            if direction > 0:
                index = next((i for i, paste in enumerate(self.pastes) if paste["start"] >= position), 0)
            else:
                index = next((i for i in range(len(self.pastes) - 1, -1, -1) if self.pastes[i]["start"] <= position), len(self.pastes) - 1)
        self.select_paste(index)

    def word_ranges(self, paste):
        cursor = self.buffer.get_iter_at_offset(paste["start"])
        clusters = []
        while cursor.get_offset() < paste["end"]:
            start = cursor.copy()
            cursor.forward_cursor_position()
            if cursor.get_offset() > paste["end"]:
                cursor = self.buffer.get_iter_at_offset(paste["end"])
            clusters.append((start.get_offset(), cursor.get_offset(), self.buffer.get_text(start, cursor, True)))
        words = []
        joining = False
        for index, (start, end, text) in enumerate(clusters):
            is_word = text[0].isalnum() or text[0] == "_"
            connector = text in ("'", "\u2019", "-") and joining and index + 1 < len(clusters) and (clusters[index + 1][2][0].isalnum() or clusters[index + 1][2][0] == "_")
            if text.isspace():
                joining = False
            elif joining and (is_word or connector):
                words[-1] = (words[-1][0], end)
                joining = True
            else:
                words.append((start, end))
                joining = is_word
        return words

    def select_word(self, index, keep_column=False):
        words = self.word_ranges(self.marked_paste)
        if not words:
            self.select_paste(self.pastes.index(self.marked_paste))
            return
        self.marked_word = max(0, min(index, len(words) - 1))
        if not keep_column:
            self.word_column = None
        self.select_offsets(*words[self.marked_word])

    def move_word_vertical(self, direction):
        words = self.word_ranges(self.marked_paste)
        locations = [self.editor.get_iter_location(self.buffer.get_iter_at_offset(start)) for start, end in words]
        current = locations[self.marked_word]
        if self.word_column is None:
            self.word_column = current.x
        candidates = [(i, location) for i, location in enumerate(locations) if (location.y - current.y) * direction > 0]
        if candidates:
            index, _ = min(candidates, key=lambda item: (abs(item[1].y - current.y), abs(item[1].x - self.word_column)))
            self.select_word(index, keep_column=True)

    def copy_selection(self):
        bounds = self.buffer.get_selection_bounds()
        if bounds:
            self.editor.get_clipboard().set(self.buffer.get_text(*bounds, True))

    def delete_marked(self):
        if self.load_error:
            return
        bounds = self.buffer.get_selection_bounds()
        if not bounds:
            self.clear_marking()
            return
        paste = self.marked_paste
        index = self.pastes.index(paste)
        word = self.marked_word
        start, end = bounds
        if word is None and start.starts_line():
            def belongs_to_other(position):
                return any(item is not paste and item["start"] <= position < item["end"] for item in self.pastes)
            if end.get_char() == "\n" and not end.starts_line() and not belongs_to_other(end.get_offset()):
                end.forward_char()
            elif end.is_end() and start.get_offset() > 0 and not belongs_to_other(start.get_offset() - 1):
                start.backward_char()
        self.buffer.begin_user_action()
        self.buffer.delete(start, end)
        self.buffer.end_user_action()
        if word is not None and any(item is paste for item in self.pastes):
            self.marked_paste = paste
            self.select_word(word)
        else:
            self.select_paste(index)

    def key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            if self.marked_word is not None:
                self.select_paste(self.pastes.index(self.marked_paste))
            elif self.marked_paste is not None:
                self.clear_marking()
                self.buffer.place_cursor(self.buffer.get_iter_at_mark(self.buffer.get_insert()))
            else:
                self.hide()
            return True
        if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.save()
            return True
        modifiers = state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK)
        if self.pastes and keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab) and modifiers in (0, Gdk.ModifierType.SHIFT_MASK):
            self.cycle_paste(-1 if keyval == Gdk.KEY_ISO_Left_Tab or modifiers else 1)
            return True
        if self.marked_paste is not None:
            if not modifiers:
                if keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
                    direction = -1 if keyval == Gdk.KEY_Up else 1
                    if self.marked_word is None:
                        self.cycle_paste(direction)
                    else:
                        self.move_word_vertical(direction)
                    return True
                if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
                    if keyval == Gdk.KEY_Left and self.marked_word == 0:
                        self.select_paste(self.pastes.index(self.marked_paste))
                        return True
                    if self.marked_word is None:
                        index = 0 if keyval == Gdk.KEY_Right else len(self.word_ranges(self.marked_paste)) - 1
                    else:
                        index = self.marked_word + (-1 if keyval == Gdk.KEY_Left else 1)
                    self.select_word(index)
                    return True
                if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                    self.copy_selection()
                    return True
                if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
                    self.delete_marked()
                    return True
            if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_c, Gdk.KEY_C, Gdk.KEY_x, Gdk.KEY_X):
                return False
            if keyval not in (Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R):
                self.clear_marking()
        return False


if __name__ == "__main__":
    app = PerfectNote()
    for signum in (signal.SIGTERM, signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, app.request_quit)
    sys.exit(app.run(sys.argv))
