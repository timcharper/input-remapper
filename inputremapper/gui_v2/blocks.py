# input-remapper - GUI for device specific keyboard mappings
# Copyright (C) 2026 sezanzeb <4t1pzast9@mozmail.com>
#
# This file is part of input-remapper.
#
# input-remapper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# input-remapper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with input-remapper.  If not, see <https://www.gnu.org/licenses/>.


"""A Scratch-style block editor for a macro.

The editor shows the tree it is given and reports edits as a new tree through
`on_change`; it never keeps its own state beyond what is on screen. All edits are
computed by `block_ops`, which has no GTK.

Like the rest of the window, it updates in place: when only values changed, fields
are rewritten (unless focused); only a change of structure rebuilds the widgets.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from evdev import ecodes
from gi.repository import Gdk, Gtk

from inputremapper.gui_v2.block_ops import (
    ChainPath,
    call_gap,
    can_move,
    default_call,
    full_args,
    get_chain,
    insert_call,
    move_call,
    remove_call,
    remove_spread_arg,
    set_arg,
    slot_chain,
)
from inputremapper.gui_v2.macro_text import parse_arg, print_arg
from inputremapper.gui_v2.schema import SCHEMA, ArgKind, ArgSpec
from inputremapper.gui_v2.tree import Call, Chain, Value
from inputremapper.logging.logger import logger

DRAG_TARGET = "application/x-input-remapper-block"

# (title, color, blocks) per palette section.
CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Keys", "#4c97ff", ("key", "key_down", "key_up", "hold_keys", "modify", "hold")),
    ("Timing", "#3fb8c9", ("wait", "repeat")),
    ("Mouse", "#59c059", ("mouse", "mouse_xy", "wheel")),
    (
        "Control",
        "#ff8c1a",
        (
            "if_eq",
            "if_tap",
            "if_single",
            "if_numlock",
            "if_capslock",
            "while_eq",
            "toggle",
            "parallel",
            "mod_tap",
        ),
    ),
    ("Variables", "#9966ff", ("set", "add")),
    ("Advanced", "#8a8a8a", ("event",)),
)
CATEGORY_COLORS = {op: color for _title, color, ops in CATEGORIES for op in ops} | {
    "ifeq": "#ff8c1a",
    "symbol": "#4c97ff",
    "raw": "#8a8a8a",
}
CATEGORY_CLASS = {
    color: f"v2-cat-{index}" for index, (_, color, _) in enumerate(CATEGORIES)
}

TITLES = {
    "key": "press",
    "key_down": "key down",
    "key_up": "key up",
    "hold_keys": "hold keys",
    "modify": "while holding",
    "hold": "hold while pressed",
    "wait": "wait",
    "repeat": "repeat",
    "mouse": "move mouse",
    "mouse_xy": "move mouse by",
    "wheel": "scroll",
    "if_eq": "if equal",
    "ifeq": "if variable equals",
    "if_tap": "if tapped",
    "if_single": "if pressed alone",
    "if_numlock": "if numlock on",
    "if_capslock": "if capslock on",
    "while_eq": "while equal",
    "toggle": "toggle",
    "parallel": "in parallel",
    "mod_tap": "tap or hold",
    "set": "set",
    "add": "add to",
    "event": "raw event",
    "symbol": "output key",
    "raw": "other code",
}

# Labels for arguments, where the engine's own name is too terse. Macro slots that
# aren't named `then`/`else` read "do".
ARG_LABELS = {
    ("modify", "modifier"): "key",
    ("repeat", "repeats"): "times",
    ("wait", "max_time"): "up to",
    ("set", "variable"): "variable",
    ("set", "value"): "to",
    ("add", "value"): "by",
    ("mod_tap", "default"): "tap",
    ("mod_tap", "modifier"): "hold",
    ("if_eq", "value_1"): "if",
    ("if_eq", "value_2"): "=",
    ("while_eq", "value_1"): "while",
    ("while_eq", "value_2"): "=",
    ("ifeq", "variable"): "if",
    ("ifeq", "value"): "=",
}


def _all_key_names() -> list[str]:
    return sorted(name for name in ecodes.ecodes if name.startswith(("KEY_", "BTN_")))


_MOUSE_BUTTONS = {
    "BTN_LEFT",
    "BTN_RIGHT",
    "BTN_MIDDLE",
    "BTN_SIDE",
    "BTN_EXTRA",
    "BTN_FORWARD",
    "BTN_BACK",
    "BTN_TASK",
}

# Titles of the key blocks by what they act on: a key, a mouse button or another
# (gamepad) button.
_KEY_BLOCK_TITLES = {
    "key": ("press", "click", "press button"),
    "key_down": ("key down", "mouse down", "button down"),
    "key_up": ("key up", "mouse up", "button up"),
}


# The palette says what a block can act on, since it isn't chosen yet.
PALETTE_TITLES = {
    "key": "press key / click / button",
    "key_down": "key / mouse / button down",
    "key_up": "key / mouse / button up",
}

# Extra words that find a block when searching the palette.
PALETTE_KEYWORDS = {
    "key": "tap type click mouse button",
    "key_down": "hold press mouse button",
    "key_up": "release mouse button",
    "hold_keys": "press together chord",
    "wait": "delay sleep pause",
    "mouse": "cursor pointer move",
    "mouse_xy": "cursor pointer move",
    "wheel": "scroll",
    "if_eq": "variable compare condition",
    "ifeq": "variable compare condition",
    "set": "variable assign",
    "add": "variable increase counter",
    "if_tap": "short press condition",
    "if_single": "alone condition",
    "mod_tap": "hold tap",
    "repeat": "loop times",
    "while_eq": "loop condition",
    "parallel": "together",
}


def palette_title(op: str) -> str:
    return PALETTE_TITLES.get(op, TITLES.get(op, op))


def palette_matches(op: str, query: str) -> bool:
    """Whether every word of the query is in the block's name, op or keywords."""
    haystack = f"{palette_title(op)} {op} {PALETTE_KEYWORDS.get(op, '')}".lower()
    return all(word in haystack for word in query.split())


def block_title(call: Call) -> str:
    """What a block is called; key blocks say "mouse" for mouse buttons."""
    titles = _KEY_BLOCK_TITLES.get(call.op)
    symbol = call.args[0] if call.args else None
    if titles is None or not isinstance(symbol, str):
        return TITLES.get(call.op, call.op)
    name = symbol.upper()
    if name in _MOUSE_BUTTONS:
        return titles[1]
    if name.startswith("BTN_"):
        return titles[2]
    return titles[0]


def _contains(
    completion: Gtk.EntryCompletion, key: str, row: Gtk.TreeIter, *_: Any
) -> bool:
    model = completion.get_model()
    return model is not None and key.lower() in str(model[row][0]).lower()


# A number, or a variable like $name that stands for one.
_NUMBER_OR_VARIABLE = re.compile(r"^(-?\d*\.?\d*|\$\w*)$")


def _is_numeric(arg: ArgSpec) -> bool:
    """Arguments that are numbers (or variables) and never text."""
    return arg.kind is ArgKind.VALUE and arg.accepts_number and not arg.accepts_str


def _only_numbers(entry: Gtk.Entry, text: str, _length: int, position: int) -> None:
    current = entry.get_text()
    result = current[:position] + text + current[position:]
    if not _NUMBER_OR_VARIABLE.match(result):
        entry.stop_emission_by_name("insert-text")


def shape(chain: Chain) -> tuple[Any, ...]:
    """What the widgets depend on: blocks and nesting, but no values."""
    result: list[Any] = []
    for call in chain:
        spec = SCHEMA.get(call.op)
        slots: list[Any] = []
        if spec is not None:
            for index, value in enumerate(full_args(call)):
                arg = spec.args[0 if spec.spread else index]
                if arg.kind is ArgKind.MACRO:
                    slots.append(shape(slot_chain(value)))
        count = len(call.args) if spec is not None and spec.spread else 0
        result.append((call.op, count, tuple(slots)))
    return tuple(result)


class _Field:
    """One argument's entry, kept so that it can be rewritten in place."""

    def __init__(self, entry: Gtk.Entry, arg_index: int, spec: ArgSpec) -> None:
        self.entry = entry
        self.arg_index = arg_index
        self.spec = spec

    def show(self, value: Value) -> None:
        text = print_arg(value, self.spec)
        if self.entry.get_text() != text and not self.entry.has_focus():
            self.entry.set_text(text)


class _Block:
    def __init__(self) -> None:
        self.frame: Gtk.Widget | None = None
        self.title: Gtk.Label | None = None
        self.fields: list[_Field] = []
        self.slots: list[tuple[int, _Stack]] = []


class _Stack:
    def __init__(self, editor: BlockEditor, path: ChainPath) -> None:
        self.editor = editor
        self.path = path
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.blocks: list[_Block] = []

    def build(self, chain: Chain) -> None:
        for child in self.box.get_children():
            self.box.remove(child)
        self.blocks = []
        self.box.pack_start(self.editor.make_gap(self.path, 0), False, False, 0)
        for index, call in enumerate(chain):
            block, widget = self.editor.make_block(self.path, index, call)
            self.blocks.append(block)
            self.box.pack_start(widget, False, False, 0)
            gap = self.editor.make_gap(self.path, index + 1)
            self.box.pack_start(gap, False, False, 0)
        self.box.pack_start(
            self.editor.make_add_button(self.path, len(chain)), False, False, 0
        )
        self.box.show_all()

    def update(self, chain: Chain) -> None:
        for block, call in zip(self.blocks, chain, strict=True):
            args = full_args(call)
            if block.frame is not None:
                # Outline blocks that can't be applied yet, and say why on hover.
                gap = call_gap(call)
                context = block.frame.get_style_context()
                if (gap is not None) != context.has_class("v2-incomplete"):
                    if gap is None:
                        context.remove_class("v2-incomplete")
                    else:
                        context.add_class("v2-incomplete")
                block.frame.set_tooltip_text(
                    gap[0].upper() + gap[1:] if gap is not None else None
                )
            if block.title is not None:
                title = block_title(call)
                if block.title.get_text() != title:
                    block.title.set_text(title)
            for field in block.fields:
                if field.arg_index < len(args):
                    field.show(args[field.arg_index])
            for arg_index, stack in block.slots:
                stack.update(slot_chain(args[arg_index]))


class BlockEditor(Gtk.ScrolledWindow):
    def __init__(
        self,
        on_change: Callable[[Chain, bool], None],
        variables: Callable[[], Iterable[str]] = lambda: (),
    ) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._on_change = on_change
        self._variables = variables
        self._base: Chain = ()
        self._updating = False
        self._rendered: Chain | None = None
        self._shape: tuple[Any, ...] | None = None
        self._root = _Stack(self, ())
        self._key_names = Gtk.ListStore(str)
        for name in _all_key_names():
            self._key_names.append([name])
        self._variable_names = Gtk.ListStore(str)

        self.get_style_context().add_class("v2-blocks")
        self._root.box.set_margin_start(8)
        self._root.box.set_margin_end(8)
        self._root.box.set_margin_top(8)
        self._root.box.set_margin_bottom(8)
        self.add(self._root.box)

    # Showing -------------------------------------------------------------------

    def set_chain(self, chain: Chain) -> None:
        self._base = chain
        if chain == self._rendered:
            return
        self._variable_names.clear()
        for name in self._variables():
            self._variable_names.append([name])

        # Writing into the fields must not look like the user editing them.
        self._updating = True
        try:
            new_shape = shape(chain)
            if new_shape != self._shape:
                self._root.build(chain)
                self._shape = new_shape
            self._root.update(chain)
        finally:
            self._updating = False
        self._rendered = chain

    # Editing -------------------------------------------------------------------

    def _commit(self, chain: Chain, typing: bool = False) -> None:
        self._base = chain
        self._on_change(chain, typing)

    def add_block(self, path: ChainPath, index: int, op: str) -> None:
        self._commit(insert_call(self._base, path, index, default_call(op)))

    def delete_block(self, path: ChainPath, index: int) -> None:
        self._commit(remove_call(self._base, path, index))

    def move_block(
        self, src: ChainPath, src_index: int, dst: ChainPath, dst_index: int
    ) -> None:
        if not can_move(src, src_index, dst):
            return
        moved = move_call(self._base, src, src_index, dst, dst_index)
        if moved != self._base:
            self._commit(moved)

    # Building ------------------------------------------------------------------

    def make_gap(self, path: ChainPath, index: int) -> Gtk.Widget:
        """A thin strip between blocks that accepts dropped blocks."""
        gap = Gtk.EventBox(visible_window=True)
        gap.set_size_request(-1, 6)
        gap.get_style_context().add_class("v2-gap")
        targets = [Gtk.TargetEntry.new(DRAG_TARGET, Gtk.TargetFlags.SAME_APP, 0)]
        gap.drag_dest_set(Gtk.DestDefaults.ALL, targets, Gdk.DragAction.MOVE)

        def motion(widget: Gtk.Widget, *_: Any) -> bool:
            widget.get_style_context().add_class("v2-gap-hot")
            return True

        def leave(widget: Gtk.Widget, *_: Any) -> None:
            widget.get_style_context().remove_class("v2-gap-hot")

        def received(
            widget: Gtk.Widget,
            _context: Gdk.DragContext,
            _x: int,
            _y: int,
            data: Gtk.SelectionData,
            *_: Any,
        ) -> None:
            widget.get_style_context().remove_class("v2-gap-hot")
            try:
                payload = json.loads(data.get_data().decode())
                src = tuple((int(a), int(b)) for a, b in payload["path"])
                src_index = int(payload["index"])
            except (ValueError, KeyError, TypeError):
                return
            self.move_block(src, src_index, path, index)

        gap.connect("drag-motion", motion)
        gap.connect("drag-leave", leave)
        gap.connect("drag-data-received", received)
        return gap

    def make_add_button(self, path: ChainPath, index: int) -> Gtk.Widget:
        button = Gtk.MenuButton(label="+ block")
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_halign(Gtk.Align.START)
        button.get_style_context().add_class("v2-add-block")
        button.set_popover(self._make_palette(button, path, index))
        return button

    def _make_palette(
        self, button: Gtk.MenuButton, path: ChainPath, index: int
    ) -> Gtk.Popover:
        popover = Gtk.Popover.new(button)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for margin in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{margin}")(8)

        search = Gtk.SearchEntry(placeholder_text="Search blocks")
        outer.pack_start(search, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            min_content_height=200,
            max_content_height=420,
        )
        scrolled.add(box)
        outer.pack_start(scrolled, True, True, 0)

        def choose(_button: Gtk.Button | None, op: str) -> None:
            popover.popdown()
            # Appended to the stack: its end may have moved since this was built.
            self.add_block(path, len(get_chain(self._base, path)), op)

        sections: list[tuple[Gtk.Label, list[tuple[Gtk.Button, str]]]] = []
        for title, color, ops in CATEGORIES:
            header = Gtk.Label(label=title, xalign=0)
            header.get_style_context().add_class("dim-label")
            box.pack_start(header, False, False, 0)
            items: list[tuple[Gtk.Button, str]] = []
            for op in ops:
                item = Gtk.Button(label=palette_title(op))
                item.set_relief(Gtk.ReliefStyle.NONE)
                item.set_halign(Gtk.Align.FILL)
                item.get_child().set_xalign(0)  # type: ignore[union-attr]
                item.get_style_context().add_class("v2-palette-item")
                item.get_style_context().add_class(CATEGORY_CLASS[color])
                item.connect("clicked", choose, op)
                box.pack_start(item, False, False, 0)
                items.append((item, op))
            sections.append((header, items))

        def visible_ops() -> list[str]:
            return [
                op
                for _header, items in sections
                for item, op in items
                if item.get_visible()
            ]

        def filter_items(entry: Gtk.SearchEntry) -> None:
            query = entry.get_text().strip().lower()
            for header, items in sections:
                shown = 0
                for item, op in items:
                    match = palette_matches(op, query)
                    item.set_visible(match)
                    shown += match
                header.set_visible(shown > 0)

        def pick_first(entry: Gtk.SearchEntry) -> None:
            # The entry reports changes after a short delay; don't act on stale ones.
            filter_items(entry)
            ops = visible_ops()
            if ops:
                choose(None, ops[0])

        def opened(*_: Any) -> None:
            search.set_text("")
            filter_items(search)
            search.grab_focus()

        search.connect("search-changed", filter_items)
        search.connect("activate", pick_first)
        search.connect("stop-search", lambda *_: popover.popdown())
        popover.connect("show", opened)
        outer.show_all()
        popover.add(outer)
        return popover

    def make_block(
        self, path: ChainPath, index: int, call: Call
    ) -> tuple[_Block, Gtk.Widget]:
        block = _Block()
        spec = SCHEMA.get(call.op)
        color = CATEGORY_COLORS.get(call.op, "#8a8a8a")

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        block.frame = frame
        frame.get_style_context().add_class("v2-block")
        frame.get_style_context().add_class(CATEGORY_CLASS.get(color, "v2-cat-raw"))

        header = Gtk.Box(spacing=6)
        handle = Gtk.EventBox()
        handle.add(Gtk.Label(label="⠿"))
        handle.set_tooltip_text("Drag to move")
        handle.get_style_context().add_class("v2-handle")
        self._make_drag_source(handle, frame, path, index)
        header.pack_start(handle, False, False, 0)
        title = Gtk.Label(label=block_title(call), xalign=0)
        block.title = title
        title.get_style_context().add_class("v2-block-title")
        header.pack_start(title, False, False, 0)

        slots_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        if spec is None:
            self._add_field(
                header,
                block,
                path,
                index,
                0,
                ArgSpec("code", ArgKind.VALUE, True, accepts_str=True),
                None,
            )
        elif spec.spread:
            for arg_index in range(len(call.args)):
                self._add_spread_item(
                    header, slots_box, block, path, index, arg_index, call, spec.args[0]
                )
            plus = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.MENU)
            plus.set_relief(Gtk.ReliefStyle.NONE)
            plus.connect(
                "clicked",
                lambda *_: self._commit(
                    set_arg(
                        self._base,
                        path,
                        index,
                        len(call.args),
                        default_call(call.op).args[0],
                    )
                ),
            )
            header.pack_start(plus, False, False, 0)
        else:
            for arg_index, arg in enumerate(spec.args):
                if arg.kind is ArgKind.MACRO:
                    self._add_slot(slots_box, block, path, index, arg_index, call, arg)
                else:
                    label = ARG_LABELS.get((call.op, arg.name))
                    if label is None and len(spec.args) > 1 and arg.name != "symbol":
                        label = arg.name.replace("_", " ")
                    self._add_field(header, block, path, index, arg_index, arg, label)

        delete = Gtk.Button.new_from_icon_name(
            "window-close-symbolic", Gtk.IconSize.MENU
        )
        delete.set_relief(Gtk.ReliefStyle.NONE)
        delete.set_tooltip_text("Remove this block")
        delete.connect("clicked", lambda *_: self.delete_block(path, index))
        header.pack_end(delete, False, False, 0)

        frame.pack_start(header, False, False, 0)
        if slots_box.get_children():
            frame.pack_start(slots_box, False, False, 0)
        return block, frame

    def _add_slot(
        self,
        container: Gtk.Box,
        block: _Block,
        path: ChainPath,
        index: int,
        arg_index: int,
        call: Call,
        arg: ArgSpec,
    ) -> None:
        name = arg.name if arg.name in ("then", "else") else "do"
        label = Gtk.Label(label=name, xalign=0)
        label.get_style_context().add_class("dim-label")
        stack = _Stack(self, (*path, (index, arg_index)))
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.get_style_context().add_class("v2-slot")
        inner.pack_start(stack.box, False, False, 0)
        container.pack_start(label, False, False, 0)
        container.pack_start(inner, False, False, 0)
        block.slots.append((arg_index, stack))
        stack.build(slot_chain(full_args(call)[arg_index]))

    def _add_spread_item(
        self,
        header: Gtk.Box,
        container: Gtk.Box,
        block: _Block,
        path: ChainPath,
        index: int,
        arg_index: int,
        call: Call,
        arg: ArgSpec,
    ) -> None:
        if arg.kind is ArgKind.MACRO:
            self._add_slot(container, block, path, index, arg_index, call, arg)
            return
        self._add_field(header, block, path, index, arg_index, arg, None)
        minus = Gtk.Button.new_from_icon_name("list-remove-symbolic", Gtk.IconSize.MENU)
        minus.set_relief(Gtk.ReliefStyle.NONE)
        minus.connect(
            "clicked",
            lambda *_: self._commit(
                remove_spread_arg(self._base, path, index, arg_index)
            ),
        )
        header.pack_start(minus, False, False, 0)

    def _add_field(
        self,
        header: Gtk.Box,
        block: _Block,
        path: ChainPath,
        index: int,
        arg_index: int,
        arg: ArgSpec,
        label: str | None,
    ) -> None:
        if label:
            text = Gtk.Label(label=label)
            text.get_style_context().add_class("dim-label")
            header.pack_start(text, False, False, 0)

        widget: Gtk.Widget
        if arg.kind is ArgKind.SYMBOL:
            # A drop-down of every key, which also narrows as you type.
            combo = Gtk.ComboBox.new_with_model_and_entry(self._key_names)
            combo.set_entry_text_column(0)
            entry = combo.get_child()
            assert isinstance(entry, Gtk.Entry)
            entry.set_width_chars(18)
            entry.set_completion(self._completion(self._key_names))
            widget = combo
        else:
            entry = Gtk.Entry(width_chars=6)
            widget = entry
            if arg.kind is ArgKind.VARIABLE:
                entry.set_completion(self._completion(self._variable_names))
            elif _is_numeric(arg):
                entry.connect("insert-text", _only_numbers)
        entry.get_style_context().add_class("v2-field")
        header.pack_start(widget, False, False, 0)
        if arg.unit:
            unit = Gtk.Label(label=arg.unit)
            unit.get_style_context().add_class("dim-label")
            header.pack_start(unit, False, False, 0)

        def changed(widget: Gtk.Entry) -> None:
            if self._updating:
                return
            value = parse_arg(widget.get_text(), arg)
            self._commit(set_arg(self._base, path, index, arg_index, value), True)

        entry.connect("changed", changed)
        block.fields.append(_Field(entry, arg_index, arg))

    @staticmethod
    def _completion(model: Gtk.ListStore) -> Gtk.EntryCompletion:
        completion = Gtk.EntryCompletion(model=model)
        # The method, not the property: only it adds the renderer that draws the text.
        completion.set_text_column(0)
        completion.set_inline_completion(False)
        completion.set_popup_single_match(False)
        # Match anywhere in the name, so "left" finds BTN_LEFT and KEY_LEFTSHIFT.
        completion.set_match_func(_contains, None)
        return completion

    def _make_drag_source(
        self, handle: Gtk.EventBox, frame: Gtk.Widget, path: ChainPath, index: int
    ) -> None:
        targets = [Gtk.TargetEntry.new(DRAG_TARGET, Gtk.TargetFlags.SAME_APP, 0)]
        handle.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK, targets, Gdk.DragAction.MOVE
        )

        def get_data(
            _widget: Gtk.Widget,
            _context: Gdk.DragContext,
            data: Gtk.SelectionData,
            *_: Any,
        ) -> None:
            payload = {"path": [list(step) for step in path], "index": index}
            data.set(data.get_target(), 8, json.dumps(payload).encode())

        def begin(_widget: Gtk.Widget, context: Gdk.DragContext) -> None:
            try:
                import cairo

                allocation = frame.get_allocation()
                surface = cairo.ImageSurface(
                    cairo.FORMAT_ARGB32, allocation.width, allocation.height
                )
                frame.draw(cairo.Context(surface))
                Gtk.drag_set_icon_surface(context, surface)
            except Exception as error:
                logger.debug("No custom drag icon: %s", error)

        handle.connect("drag-data-get", get_data)
        handle.connect("drag-begin", begin)


_BASE_CSS = """
.v2-block { border-radius: 6px; padding: 6px 8px; border-left: 5px solid #8a8a8a;
    background-color: alpha(#8a8a8a, 0.14); }
.v2-block-title { font-weight: bold; }
.v2-block.v2-incomplete { box-shadow: inset 0 0 0 2px #e01b24; }
.v2-slot { margin-left: 14px; padding: 2px 0 2px 8px;
    border-left: 3px solid alpha(currentColor, 0.2); }
.v2-gap { min-height: 6px; border-radius: 3px; background: transparent; }
.v2-gap-hot { min-height: 22px; background-color: alpha(#4c97ff, 0.35); }
.v2-handle { opacity: 0.55; }
.v2-handle:hover { opacity: 1; }
.v2-palette-item { padding: 4px 10px; border-left: 4px solid #8a8a8a;
    border-radius: 3px; }
"""


def stylesheet() -> bytes:
    """CSS for the editor; installed together with the window's."""
    rules = [_BASE_CSS]
    for index, (_title, color, _ops) in enumerate(CATEGORIES):
        name = f"v2-cat-{index}"
        rules.append(
            f".v2-block.{name} {{ border-left-color: {color};"
            f" background-color: alpha({color}, 0.16); }}"
        )
        rules.append(f".v2-palette-item.{name} {{ border-left-color: {color}; }}")
    return "\n".join(rules).encode()
