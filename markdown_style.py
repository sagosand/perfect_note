import re

import gi

gi.require_version("Pango", "1.0")
from gi.repository import GLib, Pango


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)")
QUOTE = re.compile(r"^ {0,3}(?:>[ \t]?)+")
LIST = re.compile(r"^[ \t]*(?:[-+*]|[0-9]+[.)])[ \t]+(?:\[([ xX])\][ \t]+)?")
RULE = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
INLINE = (
    ("strong_em", re.compile(r"(?<![\w*])\*\*\*(?=\S)[^*]+?(?<=\S)\*\*\*(?!\*)"), 3),
    ("strong", re.compile(r"(?<!\*)\*\*(?=\S)[^*]+?(?<=\S)\*\*(?!\*)|(?<!\w)__(?=\S)[^_]+?(?<=\S)__(?!\w)"), 2),
    ("em", re.compile(r"(?<!\*)\*(?=\S)[^*]+?(?<=\S)\*(?!\*)|(?<!\w)_(?=\S)[^_]+?(?<=\S)_(?!\w)"), 1),
    ("strike", re.compile(r"(?<!~)~~(?=\S)[^~]+?(?<=\S)~~(?!~)"), 2),
)
LINK = re.compile(r"!?\[[^\[\]\n]+\]\((?:[^\s()\\]|\\.|\([^\s()]*\))+\)")
URL = re.compile(r"https?://[^\s<>\x00]+")


def markdown_spans(text):
    spans = []
    fence = None
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        end = offset + len(line)
        match = FENCE.match(line)
        if fence is not None:
            spans.append(("code", offset, end))
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                spans.append(("syntax", offset, end))
                fence = None
            offset += len(raw_line)
            continue
        if match and (match[1][0] != '`' or '`' not in match[2]):
            fence = (match[1][0], len(match[1]))
            spans.extend((("code", offset, end), ("syntax", offset, end)))
            offset += len(raw_line)
            continue
        if RULE.fullmatch(line):
            spans.append(("marker", offset, end))
            offset += len(raw_line)
            continue
        heading = HEADING.match(line)
        quote = QUOTE.match(line)
        bullet = LIST.match(line)
        if heading:
            spans.extend(((f"h{len(heading[1])}", offset, end), ("syntax", offset, offset + heading.end())))
        elif quote:
            spans.extend((("quote", offset, end), ("marker", offset, offset + quote.end())))
        elif bullet:
            spans.append(("marker", offset, offset + bullet.end()))
            if bullet[1] in ('x', 'X'):
                spans.append(("completed", offset + bullet.end(), end))
        # Mask escapes and code before looking for inline markup.
        masked = list(line)
        escaped = set()
        for escape in re.finditer(r"\\[!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~]", line):
            escaped.add(escape.start() + 1)
            masked[escape.start():escape.end()] = '\x00' * (escape.end() - escape.start())
        runs = list(re.finditer(r"`+", line))
        next_run = {}
        following = {}
        for run in reversed(runs):
            length = run.end() - run.start()
            if length in next_run:
                following[run.start()] = next_run[length]
            next_run[length] = run
        consumed = -1
        for run in runs:
            if run.start() < consumed or run.start() in escaped:
                continue
            closing = following.get(run.start())
            if closing is None:
                continue
            consumed = closing.end()
            spans.append(("code", offset + run.start(), offset + consumed))
            masked[run.start():consumed] = '\x00' * (consumed - run.start())
        inline = ''.join(masked)
        for match in LINK.finditer(inline):
            if '\x00' not in match[0]:
                spans.append(("link", offset + match.start(), offset + match.end()))
                # URL underscores must not turn into emphasis.
                start = inline.find('](', match.start(), match.end()) + 2
                masked[start:match.end()] = '\x00' * (match.end() - start)
        inline = ''.join(masked)
        for match in URL.finditer(inline):
            stop = match.end()
            while stop > match.start() and line[stop - 1] in '.,;:!?)]}':
                stop -= 1
            spans.append(("link", offset + match.start(), offset + stop))
            masked[match.start():match.end()] = '\x00' * (match.end() - match.start())
        inline = ''.join(masked)
        for kind, pattern, delimiter in INLINE:
            for match in pattern.finditer(inline):
                if '\x00' in match[0]:
                    continue
                first, last = offset + match.start(), offset + match.end()
                spans.extend(((kind, first, last), ("syntax", first, first + delimiter), ("syntax", last - delimiter, last)))
        offset += len(raw_line)
    return spans


class MarkdownStyler:
    def __init__(self, buffer, view, palette, layout_changed):
        self.buffer = buffer
        self.view = view
        self.layout_changed = layout_changed
        self.source = 0
        self.tags = {}
        for level, scale in enumerate((1.35, 1.2, 1.1, 1.0, 1.0, 1.0), 1):
            self.tags[f'h{level}'] = buffer.create_tag(f'md-h{level}', weight=Pango.Weight.BOLD, scale=scale)
        styles = {
            'quote': {'style': Pango.Style.ITALIC},
            'strong': {'weight': Pango.Weight.BOLD},
            'em': {'style': Pango.Style.ITALIC},
            'strong_em': {'weight': Pango.Weight.BOLD, 'style': Pango.Style.ITALIC},
            'strike': {'strikethrough': True},
            'completed': {'strikethrough': True},
            'marker': {'weight': Pango.Weight.BOLD},
            'link': {'underline': Pango.Underline.SINGLE},
            'code': {'family': 'monospace', 'weight': Pango.Weight.NORMAL, 'style': Pango.Style.NORMAL, 'scale': 1.0},
            'syntax': {},
        }
        for name, properties in styles.items():
            self.tags[name] = buffer.create_tag('md-' + name, **properties)
        self.update_palette(palette)
        view.connect('unrealize', self.stop)
        self.refresh()

    def update_palette(self, palette):
        for name, tag in self.tags.items():
            key = 'heading' if name.startswith('h') or name in ('strong', 'strong_em') else {
                'quote': 'muted', 'em': 'foreground', 'strike': 'muted', 'completed': 'muted',
                'marker': 'marker', 'link': 'link', 'code': 'code', 'syntax': 'muted',
            }[name]
            tag.set_property('foreground', palette[key])
        self.tags['code'].set_property('background', palette['code_background'])
        self.view.queue_draw()

    def schedule(self):
        if not self.source:
            self.source = GLib.timeout_add(100, self.refresh)

    def refresh(self):
        if self.source:
            GLib.source_remove(self.source)
            self.source = 0
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, True)
        for tag in self.tags.values():
            self.buffer.remove_tag(tag, start, end)
        for kind, first, last in markdown_spans(text):
            if first < last:
                self.buffer.apply_tag(self.tags[kind], self.buffer.get_iter_at_offset(first), self.buffer.get_iter_at_offset(last))
        self.layout_changed()
        return GLib.SOURCE_REMOVE

    def stop(self, *_):
        if self.source:
            GLib.source_remove(self.source)
            self.source = 0
