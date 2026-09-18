import copy
import os
import sys
import tempfile
import time
from pathlib import Path

from perfect_note import PerfectNote, GLib, Gio, Gdk, Gtk


def settle():
    until = time.monotonic() + 0.25
    while time.monotonic() < until:
        while GLib.MainContext.default().iteration(False):
            pass
        time.sleep(0.01)


callback_errors = []
def callback_exception(kind, value, traceback):
    callback_errors.append(value)
    sys.__excepthook__(kind, value, traceback)
sys.excepthook = callback_exception

instances = 0

def create(root, application_id=None, palette_paths=()):
    global instances
    instances += 1
    app = PerfectNote(root, application_id or f'io.github.sagosand.PerfectNote.Check{instances}', palette_paths=palette_paths)
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    app.build_window()
    app.window.realize()
    return app


with tempfile.TemporaryDirectory(prefix='perfect-note-stamps-check-') as directory:
    root = Path(directory)
    (root / 'note.txt').write_text('Existing note\n')
    app = create(root)
    assert app.text() == 'Existing note\n' and app.pastes == []
    app.prepare_line()
    app.insert_paste('First paste\nwith two lines')
    assert len(app.pastes) == 1
    assert app.text() == 'Existing note\nFirst paste\nwith two lines'
    first = copy.deepcopy(app.pastes)
    app.insert_paste('Second paste')
    assert app.text().endswith('with two lines\nSecond paste')
    assert len(app.pastes) == 2
    second = copy.deepcopy(app.pastes)
    app.buffer.undo()
    assert app.pastes == first, (app.pastes, first)
    assert app.text().endswith('with two lines')
    app.buffer.redo()
    assert app.pastes == second
    app.buffer.insert_at_cursor('\nManually typed\n', -1)
    assert app.pastes == second, 'typing created or extended stamp'
    app.prepare_line()
    count = app.buffer.get_char_count()
    app.prepare_line()
    assert app.buffer.get_char_count() == count, 'extra newline'
    app.buffer.insert(app.buffer.get_start_iter(), 'Header\n')
    assert app.pastes[0]['start'] == first[0]['start'] + 7
    assert app.pastes[0]['at'] == first[0]['at']
    paste = dict(app.pastes[0])
    app.buffer.begin_user_action()
    app.buffer.delete(app.buffer.get_iter_at_offset(paste['start']), app.buffer.get_iter_at_offset(paste['end']))
    app.buffer.end_user_action()
    assert len(app.pastes) == 1, 'deleted paste left stamp'
    app.buffer.undo()
    assert len(app.pastes) == 2, 'undo did not restore stamp'
    app.buffer.redo()
    assert len(app.pastes) == 1
    app.buffer.undo()
    app.insert_paste('Swedish: åäö\nEmoji: 🌿')
    assert app.text()[app.pastes[-1]['start']:app.pastes[-1]['end']] == 'Swedish: åäö\nEmoji: 🌿'
    class Clipboard:
        def read_text_async(self, cancellable, callback, mark):
            callback(self, None, mark)
        def read_text_finish(self, result):
            return 'Pasted through the GTK action'
    app.editor.get_clipboard = lambda: Clipboard()
    count = len(app.pastes)
    app.editor.emit('paste-clipboard')
    assert len(app.pastes) == count + 1, 'paste signal bypassed stamps'
    count = len(app.pastes)
    assert app.editor.activate_action('clipboard.paste', None)
    assert len(app.pastes) == count + 1, 'context-menu paste bypassed stamps'
    before = app.text()
    app.insert_paste('')
    assert app.text() == before
    settle()
    assert not app.dirty
    assert app.note_path.read_text() == app.text()
    assert app.state_path.stat().st_mode & 0o777 == 0o600
    app2 = create(root)
    assert app2.text() == app.text()
    assert app2.pastes == app.pastes
    assert app2.state_path.with_suffix('.json.bak').exists()
    assert not app2.error.get_visible()
    app2.buffer.insert_at_cursor('Unsaved', -1)
    old_path = app2.state_path
    blocker = root / 'blocker'
    blocker.write_text('not a directory')
    app2.state_path = blocker / 'note.json'
    app2.hide()
    assert app2.dirty and app2.error.get_visible()
    app2.state_path = old_path
    app2.save()
    assert not app2.dirty and not app2.error.get_visible()
    # A broken state must never be silently replaced by an empty note.
    bad = root / 'bad'
    bad.mkdir()
    (bad / 'note.json').write_text('{')
    app3 = create(bad)
    assert app3.load_error and not app3.editor.get_editable()
    app3.save()
    assert (bad / 'note.json').read_text() == '{'
    nav = create(root / 'navigation', 'io.github.sagosand.PerfectNote')
    clips = ['First small paste', 'Second clip\nwith another line', "Swedish: åäö can't stop 🌿"]
    for clip in clips:
        nav.insert_paste(clip)
    original_text = nav.text()
    original_stamps = copy.deepcopy(nav.pastes)
    def selected():
        bounds = nav.buffer.get_selection_bounds()
        return nav.buffer.get_text(*bounds, True) if bounds else ''
    def key(value, state=0):
        return nav.key_pressed(None, value, 0, state)
    class CopyClipboard:
        copied = None
        def set(self, text):
            self.copied = text
    clipboard = CopyClipboard()
    nav.editor.get_clipboard = lambda: clipboard
    nav.select_paste(0)
    assert selected() == clips[0]
    assert key(Gdk.KEY_Return) and clipboard.copied == clips[0]
    assert key(Gdk.KEY_Up) and selected() == clips[2]
    assert key(Gdk.KEY_Down) and selected() == clips[0]
    assert key(Gdk.KEY_Down) and selected() == clips[1]
    assert key(Gdk.KEY_Right) and selected() == 'Second'
    assert key(Gdk.KEY_Right) and selected() == 'clip'
    assert key(Gdk.KEY_Return) and clipboard.copied == 'clip'
    assert key(Gdk.KEY_Left) and selected() == 'Second'
    assert key(Gdk.KEY_Left) and selected() == clips[1]
    assert key(Gdk.KEY_Down) and selected() == clips[2]
    assert key(Gdk.KEY_Left) and selected() == '🌿'
    key(Gdk.KEY_Left)
    key(Gdk.KEY_Left)
    assert selected() == "can't"
    assert key(Gdk.KEY_Up) and selected() == "can't"
    assert key(Gdk.KEY_ISO_Left_Tab, Gdk.ModifierType.SHIFT_MASK) and selected() == clips[1]
    key(Gdk.KEY_Right)
    key(Gdk.KEY_Right)
    assert selected() == 'clip'
    assert key(Gdk.KEY_Delete)
    assert 'Second \nwith another line' in nav.text()
    assert selected() == 'with', 'word deletion should select next word'
    nav.buffer.undo()
    assert nav.text() == original_text and nav.pastes == original_stamps
    nav.select_paste(1)
    assert key(Gdk.KEY_Delete)
    assert nav.text() == clips[0] + '\n' + clips[2]
    assert selected() == clips[2], 'segment deletion should select next segment'
    nav.buffer.undo()
    assert nav.text() == original_text and nav.pastes == original_stamps
    nav.buffer.redo()
    assert nav.text() == clips[0] + '\n' + clips[2]
    nav.buffer.undo()
    nav.select_paste(2)
    key(Gdk.KEY_BackSpace)
    assert nav.text() == clips[0] + '\n' + clips[1]
    nav.buffer.undo()
    nav.select_paste(0)
    key(Gdk.KEY_Delete)
    assert nav.text() == clips[1] + '\n' + clips[2]
    nav.buffer.undo()
    nav.select_paste(0)
    key(Gdk.KEY_Escape)
    assert nav.marked_paste is None and not selected()
    nav.select_paste(0)
    assert not key(Gdk.KEY_Right, Gdk.ModifierType.SHIFT_MASK)
    assert nav.marked_paste is None, 'modified arrows must allow normal selection'
    assert selected() == clips[0], 'leaving navigation must preserve text selection'
    # Test the gutter callback with actual Gtk text coordinates.
    nav.select_paste(2)
    location = nav.editor.get_iter_location(nav.buffer.get_iter_at_offset(nav.pastes[0]['start']))
    x, y = nav.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, 30, location.y + 5)
    class Click:
        claimed = False
        def set_state(self, state):
            self.claimed = state == Gtk.EventSequenceState.CLAIMED
    click = Click()
    nav.editor_clicked(click, 1, x, y)
    assert click.claimed and selected() == clips[0]
    # Render the temporary note so wrapped-line geometry is available.
    nav.window.present()
    settle()
    for sample in ('CAPS LOCK ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'ÅÄÖ ÉÈÊ Ç Ñ Ü', 'Mixed case gjpqy'):
        layout = nav.editor.create_pango_layout(sample)
        ink, logical = layout.get_pixel_extents()
        assert layout.get_baseline() % 1024 == 0, 'fractional text baseline'
        assert ink.y >= logical.y - nav.editor.get_pixels_above_lines(), 'glyph tops exceed line padding'
        assert ink.y + ink.height <= logical.y + logical.height + nav.editor.get_pixels_below_lines(), 'glyph bottoms exceed line padding'

    # Vertical arrows stay inside the segment and retain the horizontal column.
    nav.select_paste(1)
    key(Gdk.KEY_Right)
    assert selected() == 'Second'
    key(Gdk.KEY_Down)
    assert selected() == 'with', (selected(), nav.editor.get_width(), [(nav.text()[a:b], nav.editor.get_iter_location(nav.buffer.get_iter_at_offset(a)).x, nav.editor.get_iter_location(nav.buffer.get_iter_at_offset(a)).y) for a,b in nav.word_ranges(nav.marked_paste)])
    assert nav.marked_paste is nav.pastes[1]
    key(Gdk.KEY_Down)
    assert selected() == 'with', (selected(), nav.editor.get_width(), [(nav.text()[a:b], nav.editor.get_iter_location(nav.buffer.get_iter_at_offset(a)).x, nav.editor.get_iter_location(nav.buffer.get_iter_at_offset(a)).y) for a,b in nav.word_ranges(nav.marked_paste)])
    key(Gdk.KEY_Up)
    assert selected() == 'Second'
    key(Gdk.KEY_Right)
    assert selected() == 'clip'
    key(Gdk.KEY_Down)
    assert selected() == 'another'
    key(Gdk.KEY_Up)
    assert selected() == 'clip'
    key(Gdk.KEY_Escape)
    assert selected() == clips[1] and nav.marked_word is None
    # Tab cycles whole segments from either navigation depth, including wraparound.
    key(Gdk.KEY_Right)
    key(Gdk.KEY_Tab)
    assert selected() == clips[2] and nav.marked_word is None
    key(Gdk.KEY_Tab)
    assert selected() == clips[0]
    key(Gdk.KEY_Tab, Gdk.ModifierType.SHIFT_MASK)
    assert selected() == clips[2]
    key(Gdk.KEY_ISO_Left_Tab)
    assert selected() == clips[1]
    key(Gdk.KEY_Escape)
    assert nav.marked_paste is None
    nav.buffer.place_cursor(nav.buffer.get_end_iter())
    key(Gdk.KEY_Tab)
    assert selected() == clips[0]
    key(Gdk.KEY_Escape)
    nav.buffer.place_cursor(nav.buffer.get_end_iter())
    key(Gdk.KEY_ISO_Left_Tab, Gdk.ModifierType.SHIFT_MASK)
    assert selected() == clips[2]
    # A long first paste must remain one selection, even when wrapped.
    nav.prepare_line()
    nav.insert_paste('long word ' * 100)
    nav.select_paste(3)
    assert selected() == 'long word ' * 100
    settle()
    key(Gdk.KEY_Right)
    first_word = nav.marked_word
    first_row = nav.editor.get_iter_location(nav.buffer.get_iter_at_mark(nav.buffer.get_insert())).y
    key(Gdk.KEY_Down)
    next_row = nav.editor.get_iter_location(nav.buffer.get_iter_at_mark(nav.buffer.get_insert())).y
    assert next_row > first_row, 'Down must follow soft-wrapped lines'
    assert nav.marked_paste is nav.pastes[3]
    key(Gdk.KEY_Up)
    assert nav.marked_word == first_word, 'vertical navigation should preserve column'
    key(Gdk.KEY_Escape)
    assert selected() == 'long word ' * 100
    key(Gdk.KEY_Tab)
    assert selected() == clips[0], 'Tab wraps from the final segment'
    nav.prepare_line()
    nav.insert_paste('alpha\n\nbeta')
    settle()
    nav.select_paste(4)
    key(Gdk.KEY_Right)
    key(Gdk.KEY_Down)
    assert selected() == 'beta', 'skip blank lines within the current segment'
    key(Gdk.KEY_Down)
    assert selected() == 'beta', 'stay within the segment at its last line'
    key(Gdk.KEY_Up)
    assert selected() == 'alpha'
    key(Gdk.KEY_Up)
    assert selected() == 'alpha', 'stay within the segment at its first line'
    key(Gdk.KEY_Left)
    assert selected() == 'alpha\n\nbeta', 'Left from the first word returns to the segment'
    nav.hide()
    for instance in (app, app2, app3, nav):
        instance.markdown.stop()
        instance.stop_theme()
        instance.window.destroy()
print('PASS: migration, one stamp per paste, Unicode, position tracking, delete/undo/redo, autosave, restart, backups, errors, segment/word navigation, copy, delete, click, vertical/wrapped navigation, Tab cycling')

with tempfile.TemporaryDirectory(prefix='perfect-note-audit-') as directory:
    from unittest.mock import patch
    import json
    import random
    import perfect_note
    root = Path(directory)
    audit_instances = []
    def audit_app(name):
        app = create(root / name)
        audit_instances.append(app)
        return app
    def snapshot(app):
        return app.text(), copy.deepcopy(app.pastes)
    def valid_ranges(app):
        end = 0
        for paste in app.pastes:
            assert end <= paste['start'] < paste['end'] <= len(app.text()), (app.text(), app.pastes)
            end = paste['end']

    split = audit_app('split')
    split.insert_paste('alpha beta gamma')
    original = snapshot(split)
    split.buffer.place_cursor(split.buffer.get_iter_at_offset(6))
    split.insert_paste('new')
    valid_ranges(split)
    assert len(split.pastes) == 3
    assert split.pastes[0]['at'] == split.pastes[2]['at'] == original[1][0]['at']
    split.buffer.undo()
    assert snapshot(split) == original
    split.buffer.redo()
    valid_ranges(split)
    split.save()
    reloaded = audit_app('split')
    assert snapshot(reloaded) == snapshot(split)

    grouped = audit_app('grouped')
    grouped.insert_paste('abc')
    grouped.pastes[0]['at'] = '2000-01-01T00:00:00+00:00'
    grouped.remember(replace=True)
    original = snapshot(grouped)
    grouped.buffer.begin_user_action()
    grouped.select_paste(0)
    grouped.insert_paste('abc')
    grouped.insert_paste('tail')
    grouped.buffer.end_user_action()
    final = snapshot(grouped)
    grouped.buffer.undo()
    assert snapshot(grouped) == original, 'grouped undo must restore the original timestamp'
    grouped.buffer.redo()
    assert snapshot(grouped) == final

    edited = audit_app('replace')
    edited.insert_paste('old word')
    original = snapshot(edited)
    edited.select_paste(0)
    edited.select_word(0)
    edited.buffer.begin_user_action()
    edited.buffer.delete_selection(True, True)
    edited.buffer.insert_at_cursor('new', -1)
    edited.buffer.end_user_action()
    edited.select_paste(0)
    bounds = edited.buffer.get_selection_bounds()
    assert edited.buffer.get_text(*bounds, True) == 'new word'
    assert edited.pastes[0]['at'] == original[1][0]['at']
    edited.buffer.undo()
    assert snapshot(edited) == original
    edited.select_paste(0)
    edited.buffer.begin_user_action()
    edited.buffer.delete_selection(True, True)
    edited.buffer.insert_at_cursor('entire replacement', -1)
    edited.buffer.end_user_action()
    assert len(edited.pastes) == 1 and edited.pastes[0]['start'] == 0
    assert edited.pastes[0]['end'] == len(edited.text())
    edited.select_paste(0)
    edited.buffer.place_cursor(edited.buffer.get_end_iter())
    assert edited.marked_paste is None, 'native cursor changes must clear navigation state'

    separators = audit_app('separators')
    separators.insert_paste('keep\n')
    separators.insert_paste('delete')
    separators.select_paste(1)
    separators.delete_marked()
    assert separators.text() == 'keep\n', 'do not remove another paste\'s newline'
    separators.buffer.insert_at_cursor('  ', -1)
    before = separators.text()
    separators.prepare_line()
    separators.prepare_line()
    assert separators.text() == before, 'reuse an indented empty line'

    joined = audit_app('joined')
    joined.insert_paste('one')
    joined.insert_paste('two')
    joined.buffer.begin_user_action()
    joined.buffer.delete(joined.buffer.get_iter_at_offset(3), joined.buffer.get_iter_at_offset(4))
    joined.buffer.end_user_action()
    layout = list(joined.stamp_layout())
    assert layout[1][1] >= layout[0][1] + 26
    x, y = joined.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, 30, layout[1][1] + 5)
    click = Click()
    joined.editor_clicked(click, 1, x, y)
    assert click.claimed and joined.marked_paste is joined.pastes[1]
    assert joined.buffer.get_text(*joined.buffer.get_selection_bounds(), True) == 'two'

    unicode = audit_app('unicode')
    unicode.insert_paste('e\u0301clair 👨\u200d👩\u200d👧\u200d👦 🇸🇪')
    words = [unicode.text()[a:b] for a, b in unicode.word_ranges(unicode.pastes[0])]
    assert words == ['e\u0301clair', '👨\u200d👩\u200d👧\u200d👦', '🇸🇪'], words
    original = snapshot(unicode)
    unicode.select_paste(0)
    unicode.select_word(1)
    unicode.delete_marked()
    assert '\u200d' not in unicode.text()
    unicode.buffer.undo()
    assert snapshot(unicode) == original
    unicode.insert_paste('invalid\0text')
    assert snapshot(unicode) == original
    whitespace = audit_app('whitespace')
    whitespace.insert_paste('only ')
    whitespace.select_paste(0)
    whitespace.select_word(0)
    whitespace.delete_marked()
    assert whitespace.marked_word is None and whitespace.buffer.get_has_selection()

    class DelayedClipboard:
        def __init__(self, text):
            self.text = text
        def read_text_async(self, cancellable, callback, request):
            self.callback, self.request = callback, request
        def read_text_finish(self, result):
            if isinstance(self.text, Exception):
                raise self.text
            return self.text
        def finish(self):
            self.callback(self, None, self.request)
    delayed = audit_app('delayed')
    delayed.insert_paste('keep this safe')
    clipboard = DelayedClipboard('incoming')
    delayed.read_clipboard(clipboard)
    delayed.select_paste(0)
    clipboard.finish()
    assert delayed.text() == 'keep this safe\nincoming'
    delayed.prepare_line()
    first, second = DelayedClipboard('one'), DelayedClipboard('two')
    delayed.read_clipboard(first)
    delayed.read_clipboard(second)
    second.finish()
    assert 'two' not in delayed.text()
    first.finish()
    assert delayed.text().endswith('one\ntwo'), 'preserve clipboard request order'
    delayed.select_paste(0)
    clipboard = DelayedClipboard('replacement')
    delayed.read_clipboard(clipboard)
    delayed.buffer.delete_selection(True, True)
    before = snapshot(delayed)
    clipboard.finish()
    assert snapshot(delayed) == before and delayed.error.get_visible()
    failed = DelayedClipboard(GLib.Error('clipboard unavailable'))
    delayed.read_clipboard(failed)
    failed.finish()
    assert not delayed.pending_pastes

    valid = {'version': 1, 'text': 'abc', 'pastes': [{'start': 0, 'end': 3, 'at': '2026-09-18T12:00:00+02:00'}]}
    invalid = [
        {**valid, 'version': True}, {**valid, 'text': 'a\0b'}, {**valid, 'text': '\ud800'},
        {**valid, 'pastes': None}, {**valid, 'pastes': [{'start': 0.5, 'end': 3, 'at': valid['pastes'][0]['at']}]},
        {**valid, 'pastes': [{'start': False, 'end': 3, 'at': valid['pastes'][0]['at']}]},
        {**valid, 'pastes': [{'start': 0, 'end': 4, 'at': valid['pastes'][0]['at']}]},
        {**valid, 'pastes': [{'start': 0, 'end': 3, 'at': 'not a date'}]},
        {**valid, 'pastes': valid['pastes'] * 2}, [], None,
    ]
    for index, value in enumerate(invalid):
        data = root / f'invalid-{index}'
        data.mkdir()
        encoded = json.dumps(value)
        (data / 'note.json').write_text(encoded)
        app = audit_app(data.name)
        assert app.load_error and not app.pastes and not app.editor.get_editable()
        app.save()
        assert (data / 'note.json').read_text() == encoded
    data = root / 'backup-failure'
    data.mkdir()
    (data / 'note.json').write_text(json.dumps(valid))
    (data / 'note.json.bak').mkdir()
    (data / 'note.txt').write_bytes(b'\xff')
    app = audit_app(data.name)
    assert app.text() == 'abc' and app.pastes == valid['pastes'] and not app.load_error
    assert app.error.get_visible() and (data / 'note.txt.bak').read_bytes() == b'\xff'
    data = root / 'legacy-nul'
    data.mkdir()
    (data / 'note.txt').write_bytes(b'abc\0def')
    assert audit_app(data.name).load_error

    target = root / 'atomic' / 'note.txt'
    perfect_note.write_note(target, 'original')
    target.with_suffix('.tmp').write_text('unrelated')
    with patch('perfect_note.os.replace', side_effect=OSError('replace failed')):
        try:
            perfect_note.write_note(target, 'changed')
        except OSError:
            pass
        else:
            raise AssertionError('expected write failure')
    assert target.read_text() == 'original'
    assert target.with_suffix('.tmp').read_text() == 'unrelated'
    assert not list(target.parent.glob('note.txt.*.tmp'))
    perfect_note.write_note(target, 'changed')
    assert target.stat().st_mode & 0o777 == 0o600

    quitting = audit_app('quit')
    quitting.insert_paste('keep unsaved edits')
    destination = quitting.state_path
    blocker = root / 'not-a-directory'
    blocker.write_text('blocker')
    quitting.state_path = blocker / 'note.json'
    with patch.object(quitting, 'quit') as quit_call, patch.object(quitting.window, 'present'):
        quitting.request_quit()
        assert quitting.dirty and not quit_call.called
        quitting.state_path = destination
        quitting.request_quit()
        assert not quitting.dirty and quit_call.called
    quitting = audit_app('pending-quit')
    pending = DelayedClipboard('finish this paste')
    quitting.read_clipboard(pending)
    with patch.object(quitting, 'quit') as quit_call:
        quitting.request_quit()
        assert not quit_call.called
        pending.finish()
        assert quit_call.called and quitting.note_path.read_text() == 'finish this paste'

    fuzz = audit_app('randomized')
    cycles = int(os.environ.get('PERFECT_NOTE_TEST_CYCLES', '150'))
    rng = random.Random(int(os.environ.get('PERFECT_NOTE_TEST_SEED', '91826')))
    for iteration in range(cycles):
        size = fuzz.buffer.get_char_count()
        start, end = sorted((rng.randrange(size + 1), rng.randrange(size + 1)))
        fuzz.buffer.select_range(fuzz.buffer.get_iter_at_offset(start), fuzz.buffer.get_iter_at_offset(end))
        operation = rng.randrange(3)
        if operation == 0:
            fuzz.insert_paste(rng.choice(['alpha', 'a\nb', 'åäö', 'x', '  ']))
        else:
            fuzz.buffer.begin_user_action()
            fuzz.buffer.delete_selection(True, True)
            if operation == 1:
                fuzz.buffer.insert_at_cursor(rng.choice(['same', 'x', '\n', ' ']), -1)
            fuzz.buffer.end_user_action()
        valid_ranges(fuzz)
        expected = snapshot(fuzz)
        if fuzz.buffer.get_can_undo():
            fuzz.buffer.undo()
            valid_ranges(fuzz)
            fuzz.buffer.redo()
            assert snapshot(fuzz) == expected, ('undo/redo round trip', iteration, expected, snapshot(fuzz))
    fuzz.save()
    assert snapshot(audit_app('randomized')) == snapshot(fuzz)
    for app in audit_instances:
        if app.save_source:
            GLib.source_remove(app.save_source)
            app.save_source = 0
        if app.gutter_source:
            GLib.source_remove(app.gutter_source)
            app.gutter_source = 0
        app.markdown.stop()
        app.stop_theme()
        app.window.destroy()
print(f'PASS: audit regressions, delayed clipboard, safe persistence, Unicode, grouped undo, and {cycles} randomized edit/undo/redo cycles')

with tempfile.TemporaryDirectory(prefix='perfect-note-theme-check-') as directory:
    from perfect_note import DEFAULT_PALETTE, read_theme, contrast
    root = Path(directory)
    theme = root / 'theme'
    theme.mkdir()
    colors = theme / 'colors.toml'
    themed = create(root / 'note', palette_paths=(colors,))
    assert themed.palette == DEFAULT_PALETTE
    themed.insert_paste('The selected words and their timestamp must survive a theme switch.')
    themed.select_paste(0)
    themed.select_word(2)
    before = (themed.text(), copy.deepcopy(themed.pastes), themed.marked_word,
              tuple(i.get_offset() for i in themed.buffer.get_selection_bounds()), themed.history_position)
    css_errors = []
    themed.style_provider.connect('parsing-error', lambda provider, section, error: css_errors.append(error))
    colors.write_text('background = "#181818"\nforeground = "#eeeeee"\naccent = "#55aaff"\n')
    deadline = time.monotonic() + 4
    while themed.palette['background'] != '#181818' and time.monotonic() < deadline:
        settle()
    assert themed.palette['background'] == '#181818', 'live theme timer did not reload while hidden'
    assert not themed.window.get_visible()
    theme.rename(root / 'old-theme')
    themed.refresh_theme()
    assert themed.palette['background'] == '#181818', 'missing theme discarded last good palette'
    theme.mkdir()
    colors.write_text('background = "#fafafa"\nforeground = "#202020"\nselection = "#222222"\nmuted = "#dddddd"\n')
    deadline = time.monotonic() + 4
    while themed.palette['background'] != '#fafafa' and time.monotonic() < deadline:
        settle()
    assert themed.palette['background'] == '#fafafa', 'directory replacement stopped live theme updates'
    assert contrast(themed.palette['selected'], themed.palette['selection']) >= 4.5
    assert contrast(themed.palette['muted'], themed.palette['background']) >= 4.5
    after = (themed.text(), copy.deepcopy(themed.pastes), themed.marked_word,
             tuple(i.get_offset() for i in themed.buffer.get_selection_bounds()), themed.history_position)
    assert after == before, 'theme change disturbed editing state'
    good = themed.palette.copy()
    for content in ('background = [', 'background = "#333333"', 'background = "bad; }"\nforeground = "#eeeeee"'):
        colors.write_text(content)
        themed.refresh_theme()
        assert themed.palette == good, 'invalid theme discarded last good palette'
    colors.write_text('bg = "#101010"\nfg = "#f0f0f0"\ncolor4 = "#33aaff"\nselection_background = "#444444"\n')
    themed.refresh_theme()
    assert themed.palette['background'] == '#101010' and themed.palette['selection'] == '#444444'
    legacy = root / 'legacy.toml'
    colors.rename(legacy)
    assert read_theme((colors, legacy))['foreground'] == '#f0f0f0'
    assert not css_errors, css_errors
    themed.shutdown()
    assert themed.theme_source == 0 and themed.style_provider is None
    assert themed.note_path.read_text() == before[0]
    themed.window.destroy()
print('PASS: live dark/light themes, directory replacement, legacy colors, contrast, invalid themes, and editing-state preservation')

from markdown_style import markdown_spans


def styled_parts(text, kind):
    return [text[start:end] for name, start, end in markdown_spans(text) if name == kind]


assert styled_parts('# Heading\n## Smaller\n', 'h1') == ['# Heading']
assert styled_parts('**bold** *italic* ~~old~~ ***both***', 'strong') == ['**bold**']
assert styled_parts('**bold** *italic* ~~old~~ ***both***', 'em') == ['*italic*']
assert styled_parts('**bold** *italic* ~~old~~ ***both***', 'strong_em') == ['***both***']
assert styled_parts('- [x] Finished\n- [ ] Next', 'completed') == ['Finished']
assert styled_parts('`**literal**` and **bold**', 'strong') == ['**bold**']
assert styled_parts('``a ` tick``', 'code') == ['``a ` tick``']
assert not styled_parts(r'\*literal\* snake_case_value', 'em')
assert not styled_parts('https://example.org/a_b_c [link](https://example.org/a_b_c)', 'em')
assert len(styled_parts('https://example.org/a_b_c [link](https://example.org/a_b_c)', 'link')) == 2
assert styled_parts('🌿 **ÅÄÖ**', 'strong') == ['**ÅÄÖ**']
assert not styled_parts('~~~python\n# not a heading\n```\n# still code', 'h1')
assert styled_parts('~~~\n# code\n~~~\n# heading', 'h1') == ['# heading']
assert not styled_parts('```\nhttps://example.org\n```', 'link')
assert not styled_parts('####### plain', 'h6')

with tempfile.TemporaryDirectory(prefix='perfect-note-markdown-check-') as directory:
    root = Path(directory)
    note = create(root)
    sample = '# CAPS LOCK ÅÄÖ\n**Keep** *these* words.\n- [x] Done\n> A quote\n`literal **stars**`\nhttps://example.org'
    note.insert_paste(sample)
    note.save()
    note.select_paste(0)
    before = (note.text(), copy.deepcopy(note.pastes), len(note.history), note.history_position,
              tuple(i.get_offset() for i in note.buffer.get_selection_bounds()), note.buffer.get_modified())
    note.markdown.refresh()
    after = (note.text(), copy.deepcopy(note.pastes), len(note.history), note.history_position,
             tuple(i.get_offset() for i in note.buffer.get_selection_bounds()), note.buffer.get_modified())
    assert after == before and not note.dirty, 'styling modified note or selection state'
    assert note.note_path.read_text() == sample
    def has_style(word, name):
        return note.buffer.get_iter_at_offset(note.text().index(word)).has_tag(note.markdown.tags[name])
    assert has_style('CAPS', 'h1') and has_style('Keep', 'strong') and has_style('these', 'em')
    assert has_style('literal', 'code') and not has_style('stars', 'strong')
    class MarkdownClipboard:
        copied = None
        def set(self, text):
            self.copied = text
    clipboard = MarkdownClipboard()
    note.editor.get_clipboard = lambda: clipboard
    note.copy_selection()
    assert clipboard.copied == sample, 'copy changed Markdown source'
    note.buffer.begin_user_action()
    note.buffer.delete(note.buffer.get_start_iter(), note.buffer.get_iter_at_offset(2))
    note.buffer.end_user_action()
    note.markdown.refresh()
    assert not has_style('CAPS', 'h1'), 'removed heading kept its style'
    note.buffer.undo()
    note.markdown.refresh()
    assert note.text() == sample and note.pastes == before[1] and has_style('CAPS', 'h1')
    note.buffer.redo()
    note.markdown.refresh()
    assert not has_style('CAPS', 'h1')
    note.buffer.undo()
    light = root / 'light.toml'
    light.write_text('background = "#fafafa"\nforeground = "#202020"\naccent = "#b0c0ff"\ngreen = "#c0ffc0"\n')
    note.palette_paths = (light,)
    note.refresh_theme()
    for key in ('heading', 'link', 'marker'):
        assert contrast(note.palette[key], note.palette['background']) >= 5
    assert contrast(note.palette['code'], note.palette['code_background']) >= 5
    expected = Gdk.RGBA()
    expected.parse(note.palette['heading'])
    assert note.markdown.tags['h1'].get_property('foreground-rgba').equal(expected)
    note.shutdown()
    assert note.markdown.source == 0 and note.note_path.read_text() == sample
    note.window.destroy()
print('PASS: Markdown styles, literal code/escapes, Unicode offsets, theme contrast, exact copy/save, selection, and undo/redo')

with tempfile.TemporaryDirectory(prefix='perfect-note-word-delete-check-') as directory:
    for number, (sample, cursor, expected) in enumerate((
        ('one two three', 13, 'one two '),
        ('one two   ', 10, 'one '),
        ('one\ntwo', 7, 'one\n'),
        ('one two', 5, 'one wo'),
        ('one two', 0, 'one two'),
        ('ÅÄÖ cafe\u0301', 9, 'ÅÄÖ '),
    )):
        note = create(Path(directory) / str(number))
        note.insert_paste(sample)
        before = copy.deepcopy(note.pastes)
        note.buffer.place_cursor(note.buffer.get_iter_at_offset(cursor))
        assert note.key_pressed(None, Gdk.KEY_BackSpace, 0, Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.LOCK_MASK)
        assert note.text() == expected, (sample, note.text())
        if sample != expected:
            note.buffer.undo()
            assert note.text() == sample and note.pastes == before
            note.buffer.redo()
            assert note.text() == expected
        note.shutdown()
        note.window.destroy()
    note = create(Path(directory) / 'selection')
    note.insert_paste('one two three')
    before = copy.deepcopy(note.pastes)
    note.select_paste(0)
    note.select_word(1)
    note.key_pressed(None, Gdk.KEY_BackSpace, 0, Gdk.ModifierType.ALT_MASK)
    assert note.text() == 'one  three' and note.marked_paste is None
    note.buffer.undo()
    assert note.text() == 'one two three' and note.pastes == before
    note.buffer.place_cursor(note.buffer.get_end_iter())
    for expected in ('one two ', 'one ', ''):
        note.key_pressed(None, Gdk.KEY_BackSpace, 0, Gdk.ModifierType.ALT_MASK)
        assert note.text() == expected
    note.buffer.undo()
    note.editor.set_editable(False)
    before = (note.text(), copy.deepcopy(note.pastes))
    note.key_pressed(None, Gdk.KEY_BackSpace, 0, Gdk.ModifierType.ALT_MASK)
    assert (note.text(), note.pastes) == before, 'word deletion changed read-only text'
    note.shutdown()
    note.window.destroy()
print('PASS: Alt+Backspace words, whitespace, Unicode, selection, repeated deletion, undo/redo timestamps, and read-only text')

assert not callback_errors, callback_errors
