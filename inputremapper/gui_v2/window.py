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


"""The GTK window of the beta GUI. Only displays the controller's state.

Widgets are created once and updated in place, like a keyed virtual DOM: lists add,
remove and reorder rows by key, and text fields are only written to when their value
differs and they aren't being typed in. Refreshes run from an idle callback, never
inside a signal handler, so a handler never destroys the widget that is emitting.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from inputremapper.gui_v2.controller import Controller
from inputremapper.gui_v2.document import Entry
from inputremapper.gui_v2.macro_text import (
    MacroTextError,
    parse_text,
    print_chain,
)
from inputremapper.gui_v2.trigger_text import (
    parse_trigger,
    print_trigger,
)

ACTIVE_MARK = "▶"
NEW_PROFILE = "<new profile>"


def entry_title(entry: Entry) -> str:
    if entry.name:
        return entry.name
    keys = print_trigger(entry.trigger.inputs)
    return keys or "New entry"


class KeyedRow(Gtk.ListBoxRow):
    """A list row that remembers which item it shows."""

    def __init__(self, key: str, label: Gtk.Label, *extra: Gtk.Widget) -> None:
        super().__init__()
        self.key = key
        self.label = label
        box = Gtk.Box(spacing=4)
        label.set_margin_top(4)
        label.set_margin_bottom(4)
        label.set_margin_start(4)
        label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        box.pack_start(label, True, True, 0)
        for widget in extra:
            box.pack_end(widget, False, False, 0)
        self.add(box)
        self.show_all()


def sync_rows(
    listbox: Gtk.ListBox,
    rows: dict[str, KeyedRow],
    keys: Sequence[str],
    make: Callable[[str], KeyedRow],
) -> None:
    """Make the list show exactly `keys`, in order, reusing existing rows."""
    for key in [k for k in rows if k not in keys]:
        listbox.remove(rows.pop(key))
    for index, key in enumerate(keys):
        row = rows.get(key)
        if row is None:
            row = rows[key] = make(key)
            listbox.insert(row, index)
        elif listbox.get_row_at_index(index) is not row:
            listbox.remove(row)
            listbox.insert(row, index)


class Window(Gtk.Window):
    def __init__(self, controller: Controller) -> None:
        super().__init__(title="Input Remapper (beta)")
        self.controller = controller
        self._refreshing = False
        self._refresh_scheduled = False
        self._profile_keys: tuple[str, ...] = ()
        self._entry_rows: dict[str, KeyedRow] = {}
        self._device_keys: tuple[str, ...] = ()
        self._record_dialog: Gtk.Dialog | None = None
        self._record_label: Gtk.Label | None = None
        self.set_default_size(980, 640)
        self._install_css()

        self._build_header()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self._build_profile_bar(), False, False, 0)
        outer.pack_start(Gtk.Separator(), False, False, 0)
        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        body.pack1(self._build_entries(), resize=False, shrink=False)
        body.pack2(self._build_entry_form(), resize=True, shrink=False)
        body.set_position(260)
        outer.pack_start(body, True, True, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_margin_start(8)
        self.status.set_margin_bottom(4)
        outer.pack_start(self.status, False, False, 0)
        self.add(outer)

        controller.subscribe(self.schedule_refresh)
        self.refresh()

    @staticmethod
    def _install_css() -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b".v2-record image { color: #e01b24; }"
            b".v2-input-frame { border: 1px solid @borders; border-radius: 4px; }"
            b".v2-input { background-color: @theme_base_color; }"
            b".v2-input-frame:focus-within { border-color: @theme_selected_bg_color; }"
        )
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # Building ------------------------------------------------------------------

    def _build_header(self) -> None:
        bar = Gtk.HeaderBar(show_close_button=True, title="Input Remapper (beta)")
        self.undo_button = Gtk.Button.new_from_icon_name(
            "edit-undo-symbolic", Gtk.IconSize.BUTTON
        )
        self.redo_button = Gtk.Button.new_from_icon_name(
            "edit-redo-symbolic", Gtk.IconSize.BUTTON
        )
        self.undo_button.set_tooltip_text("Undo")
        self.redo_button.set_tooltip_text("Redo")
        self.undo_button.connect("clicked", lambda *_: self.controller.undo())
        self.redo_button.connect("clicked", lambda *_: self.controller.redo())
        bar.pack_start(self.undo_button)
        bar.pack_start(self.redo_button)
        self.set_titlebar(bar)

    def _build_profile_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(8)

        self.profile_store = Gtk.ListStore(str, str)
        self.profile_combo = Gtk.ComboBox(model=self.profile_store)
        renderer = Gtk.CellRendererText()
        self.profile_combo.pack_start(renderer, True)
        self.profile_combo.add_attribute(renderer, "text", 1)
        self.profile_combo.set_size_request(240, -1)
        self.profile_combo.connect("changed", self._on_profile_chosen)
        bar.pack_start(self.profile_combo, False, False, 0)

        def icon_button(icon: str, tip: str, handler: Callable[[], None]) -> Gtk.Button:
            button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            button.set_tooltip_text(tip)
            button.connect("clicked", lambda *_: handler())
            return button

        self.rename_button = icon_button(
            "document-edit-symbolic", "Rename profile", self._on_rename
        )
        self.variables_button = icon_button(
            "x-office-spreadsheet-symbolic", "Variables…", self._on_variables
        )
        self.clone_button = icon_button(
            "edit-copy-symbolic", "Clone profile", self.controller.copy_profile
        )
        self.delete_button = icon_button(
            "user-trash-symbolic", "Delete profile", self.controller.delete_profile
        )
        for button in (
            self.rename_button,
            self.variables_button,
            self.clone_button,
            self.delete_button,
        ):
            bar.pack_start(button, False, False, 0)

        self.play_button = Gtk.Button()
        self.play_button.connect("clicked", lambda *_: self.controller.toggle_apply())
        bar.pack_end(self.play_button, False, False, 0)
        return bar

    def _build_entries(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start"):
            getattr(box, f"set_margin_{side}")(8)
        self.entry_list = Gtk.ListBox()
        self.entry_list.connect("row-selected", self._on_entry_selected)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.add(self.entry_list)
        box.pack_start(scroll, True, True, 0)
        box.pack_start(self._build_add_button(), False, False, 0)
        return box

    def _build_add_button(self) -> Gtk.Widget:
        combo = Gtk.Box(spacing=0)
        combo.get_style_context().add_class("linked")
        add = Gtk.Button(label="+ Add")
        add.connect("clicked", lambda *_: self.controller.new_entry())
        self.add_menu_button = Gtk.MenuButton()
        # A menu button without a popup is insensitive, so attach one right away
        # and refill it each time it opens, so new presets show up.
        self.add_menu = Gtk.Menu()
        self.add_menu.connect("show", lambda *_: self._populate_add_menu())
        self.add_menu_button.set_popup(self.add_menu)
        combo.pack_start(add, False, False, 0)
        combo.pack_start(self.add_menu_button, False, False, 0)
        combo.set_halign(Gtk.Align.START)
        return combo

    def _build_entry_form(self) -> Gtk.Widget:
        self.form = Gtk.Grid(row_spacing=6, column_spacing=8)
        for side in ("top", "bottom", "end", "start"):
            getattr(self.form, f"set_margin_{side}")(8)

        def row(index: int, label: str, widget: Gtk.Widget) -> None:
            self.form.attach(Gtk.Label(label=label, xalign=1), 0, index, 1, 1)
            widget.set_hexpand(True)
            self.form.attach(widget, 1, index, 1, 1)

        self.entry_name = Gtk.Entry(placeholder_text="Name (optional)")
        self.entry_name.connect("changed", self._on_name_changed)
        row(0, "Name", self.entry_name)

        when = Gtk.Box(spacing=6)
        self.trigger_entry = Gtk.Entry(placeholder_text="KEY_LEFTCTRL + KEY_A")
        self.trigger_entry.connect("changed", self._on_trigger_changed)
        self.record_button = Gtk.Button()
        self.record_button.get_style_context().add_class("v2-record")
        self.record_button.connect(
            "clicked", lambda *_: self.controller.toggle_recording()
        )
        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.connect("changed", self._on_device_changed)
        when.pack_start(self.trigger_entry, True, True, 0)
        when.pack_start(self.record_button, False, False, 0)
        when.pack_start(Gtk.Label(label="is pressed on"), False, False, 0)
        when.pack_start(self.device_combo, False, False, 0)
        row(1, "When", when)

        self.then_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.then_view.set_monospace(True)
        self.then_view.get_buffer().connect("changed", self._on_then_changed)
        frame = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        frame.add(self.then_view)
        frame.set_vexpand(True)
        row(2, "Then", frame)

        self.entry_error = Gtk.Label(xalign=0)
        self.entry_error.get_style_context().add_class("error")
        self.form.attach(self.entry_error, 1, 3, 1, 1)
        return self.form

    # Refreshing ----------------------------------------------------------------

    def schedule_refresh(self) -> None:
        """Refresh once the current event is handled. Several requests become one."""
        if not self._refresh_scheduled:
            self._refresh_scheduled = True
            GLib.idle_add(self._run_scheduled_refresh)

    def _run_scheduled_refresh(self) -> bool:
        self._refresh_scheduled = False
        self.refresh()
        return False

    def refresh(self) -> None:
        self._refreshing = True
        try:
            self._refresh_header()
            self._refresh_profiles()
            self._refresh_entries()
            self._refresh_form()
            self._refresh_record_dialog()
        finally:
            self._refreshing = False

    def _refresh_header(self) -> None:
        self.undo_button.set_sensitive(self.controller.can_undo)
        self.redo_button.set_sensitive(self.controller.can_redo)
        self.status.set_text(self.controller.message)

    def _refresh_profiles(self) -> None:
        c = self.controller
        names = [p.name for p in c.workspace.profiles]
        keys = (*names, NEW_PROFILE)
        store = self.profile_store
        if tuple(keys) != self._profile_keys:
            self._profile_keys = tuple(keys)
            store.clear()
            for name in names:
                store.append([name, name])
            store.append([NEW_PROFILE, "+ New profile"])
        for row, name in zip(store, names, strict=False):
            mark = f"{ACTIVE_MARK} " if name == c.active_profile else ""
            if row[1] != f"{mark}{name}":
                row[1] = f"{mark}{name}"

        wanted = c.selected_profile
        current = self.profile_combo.get_active()
        index = names.index(wanted) if wanted in names else -1
        if current != index:
            self.profile_combo.set_active(index)

        has_profile = c.profile is not None
        for button in (
            self.rename_button,
            self.variables_button,
            self.clone_button,
            self.delete_button,
            self.play_button,
        ):
            button.set_sensitive(has_profile)
        playing = (
            c.selected_profile is not None and c.selected_profile == c.active_profile
        )
        self.play_button.set_image(
            Gtk.Image.new_from_icon_name(
                (
                    "media-playback-stop-symbolic"
                    if playing
                    else "media-playback-start-symbolic"
                ),
                Gtk.IconSize.BUTTON,
            )
        )
        self.play_button.set_tooltip_text("Stop" if playing else "Apply")

    def _make_entry_row(self, entry_id: str) -> KeyedRow:
        delete = Gtk.Button.new_from_icon_name(
            "window-close-symbolic", Gtk.IconSize.MENU
        )
        delete.set_relief(Gtk.ReliefStyle.NONE)
        delete.set_tooltip_text("Delete this entry")
        delete.connect("clicked", lambda *_: self.controller.delete_entry(entry_id))
        return KeyedRow(entry_id, Gtk.Label(xalign=0), delete)

    def _refresh_entries(self) -> None:
        c = self.controller
        profile = c.profile
        entries = profile.entries if profile is not None else ()
        sync_rows(
            self.entry_list,
            self._entry_rows,
            [e.id for e in entries],
            self._make_entry_row,
        )
        for entry in entries:
            self._set_label(self._entry_rows[entry.id].label, entry_title(entry))
        self._select(self.entry_list, self._entry_rows.get(c.selected_entry or ""))

    def _refresh_form(self) -> None:
        c = self.controller
        entry = c.entry
        self.form.set_sensitive(entry is not None)
        self._refresh_devices()
        self.record_button.set_image(
            Gtk.Image.new_from_icon_name(
                (
                    "media-playback-stop-symbolic"
                    if c.recording
                    else "media-record-symbolic"
                ),
                Gtk.IconSize.BUTTON,
            )
        )
        self.record_button.set_tooltip_text(
            "Stop recording" if c.recording else "Record keys from the device"
        )
        if entry is None:
            self._set_text(self.entry_name, "")
            self._set_text(self.trigger_entry, "")
            self._set_buffer(self.then_view, "")
            self.entry_error.set_text("")
            self.device_combo.set_active(-1)
            return

        self._set_text(self.entry_name, entry.name or "")
        self._set_text(self.trigger_entry, print_trigger(entry.trigger.inputs))
        self._set_buffer(self.then_view, print_chain(entry.then))
        if self.device_combo.get_active_id() != entry.trigger.device:
            self.device_combo.set_active_id(entry.trigger.device)

    def _refresh_record_dialog(self) -> None:
        """Show a modal while recording. It is also the click surface for mouse
        buttons: those arrive from the device, not from GTK."""
        c = self.controller
        if not c.recording:
            if self._record_dialog is not None:
                dialog, self._record_dialog = self._record_dialog, None
                dialog.destroy()
            return

        entry = c.entry
        keys = print_trigger(entry.trigger.inputs) if entry else ""
        if self._record_dialog is None:
            self._record_dialog = self._build_record_dialog()
            self._record_dialog.show_all()
        if self._record_label is not None:
            self._set_label(self._record_label, keys or "…")

    def _build_record_dialog(self) -> Gtk.Dialog:
        dialog = Gtk.Dialog(
            title="Record keys",
            transient_for=self,
            modal=True,
            use_header_bar=True,
        )
        dialog.set_default_size(420, 220)
        box = dialog.get_content_area()
        box.set_spacing(12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(24)

        title = Gtk.Label(label="Press your key chord now")
        title.get_style_context().add_class("title-2")
        hint = Gtk.Label(
            label="Release all keys to finish.\n"
            "Mouse buttons count too: click here with the mouse you chose.\n"
            "To abort, close this dialog."
        )
        hint.set_justify(Gtk.Justification.CENTER)
        self._record_label = Gtk.Label(label="…")
        self._record_label.get_style_context().add_class("title-3")
        box.pack_start(title, False, False, 0)
        box.pack_start(hint, False, False, 0)
        box.pack_start(self._record_label, True, True, 0)

        dialog.connect("response", self._on_record_dialog_closed)
        dialog.connect("delete-event", self._on_record_dialog_closed)
        return dialog

    def _on_record_dialog_closed(self, *_: Any) -> bool:
        # Only user-initiated closes land here: refresh sets the attribute to None
        # before it destroys the dialog.
        if self._record_dialog is not None:
            self._record_dialog = None
            self.controller.stop_recording()
        return False

    def _refresh_devices(self) -> None:
        groups = self.controller.groups.get_groups()
        keys = tuple(g.key for g in groups)
        if keys == self._device_keys:
            return
        self._device_keys = keys
        self.device_combo.remove_all()
        for group in groups:
            self.device_combo.append(group.key, group.name)

    # In-place update helpers. Never touch a widget whose value is already right,
    # or that the user is typing in.

    @staticmethod
    def _set_label(label: Gtk.Label, text: str) -> None:
        if label.get_text() != text:
            label.set_text(text)

    @staticmethod
    def _select(listbox: Gtk.ListBox, row: KeyedRow | None) -> None:
        if row is None:
            if listbox.get_selected_row() is not None:
                listbox.unselect_all()
        elif listbox.get_selected_row() is not row:
            listbox.select_row(row)

    @staticmethod
    def _set_text(widget: Gtk.Entry, text: str) -> None:
        if widget.get_text() != text and not widget.has_focus():
            widget.set_text(text)

    @staticmethod
    def _buffer_text(buffer: Gtk.TextBuffer) -> str:
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def _set_buffer(self, view: Gtk.TextView, text: str) -> None:
        buffer = view.get_buffer()
        if self._buffer_text(buffer) != text and not view.has_focus():
            buffer.set_text(text)

    # Handlers ------------------------------------------------------------------

    def _on_profile_chosen(self, combo: Gtk.ComboBox) -> None:
        if self._refreshing:
            return
        tree_iter = combo.get_active_iter()
        if tree_iter is None:
            return
        key = self.profile_store[tree_iter][0]
        if key == NEW_PROFILE:
            self.controller.new_profile()
        else:
            self.controller.select_profile(key)

    def _on_rename(self) -> None:
        profile = self.controller.profile
        if profile is None:
            return
        dialog = Gtk.Dialog(title="Rename profile", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        field = Gtk.Entry(text=profile.name, activates_default=True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.get_content_area().add(field)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            self.controller.rename_profile(field.get_text())
        dialog.destroy()

    def _on_entry_selected(self, _list: Gtk.ListBox, row: Any) -> None:
        if self._refreshing:
            return
        self.controller.select_entry(row.key if row is not None else None)

    def _on_name_changed(self, widget: Gtk.Entry) -> None:
        if self._refreshing:
            return
        text = widget.get_text() or None
        self.controller.edit_entry(lambda e: replace(e, name=text), merge_key="name")

    def _on_trigger_changed(self, widget: Gtk.Entry) -> None:
        if self._refreshing:
            return
        try:
            inputs = parse_trigger(widget.get_text())
        except ValueError as error:
            self.entry_error.set_text(str(error))
            return
        self.entry_error.set_text("")
        self.controller.edit_entry(
            lambda e: replace(e, trigger=replace(e.trigger, inputs=inputs)),
            merge_key="trigger",
        )

    def _on_device_changed(self, widget: Gtk.ComboBoxText) -> None:
        if self._refreshing:
            return
        key = widget.get_active_id()
        if key is None:
            return
        self.controller.edit_entry(
            lambda e: replace(e, trigger=replace(e.trigger, device=key))
        )

    def _on_then_changed(self, buffer: Gtk.TextBuffer) -> None:
        if self._refreshing:
            return
        try:
            chain = parse_text(self._buffer_text(buffer))
        except MacroTextError as error:
            self.entry_error.set_text(str(error))
            return
        self.entry_error.set_text("")
        self.controller.edit_entry(lambda e: replace(e, then=chain), merge_key="then")

    def _populate_add_menu(self) -> None:
        menu = self.add_menu
        for old in menu.get_children():
            menu.remove(old)
        new = Gtk.MenuItem(label="New")
        new.connect("activate", lambda *_: self.controller.new_entry())
        menu.append(new)

        importer = Gtk.MenuItem(label="Import from v1 preset…")
        submenu = Gtk.Menu()
        presets = self.controller.list_v1_presets()
        for preset in presets:
            item = Gtk.MenuItem(label=f"{preset.group_name} / {preset.name}")
            item.connect("activate", lambda _i, p=preset: self.controller.import_v1(p))
            submenu.append(item)
        if not presets:
            empty = Gtk.MenuItem(label="No presets found")
            empty.set_sensitive(False)
            submenu.append(empty)
        importer.set_submenu(submenu)
        menu.append(importer)
        menu.show_all()

    def _on_variables(self) -> None:
        profile = self.controller.profile
        if profile is None:
            return
        dialog = Gtk.Dialog(title="Variables", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(8)
        for side in ("top", "bottom", "start", "end"):
            getattr(area, f"set_margin_{side}")(12)
        label = Gtk.Label(label="One variable name per line", xalign=0)
        label.get_style_context().add_class("dim-label")
        area.add(label)
        view = Gtk.TextView(
            monospace=True,
            left_margin=8,
            right_margin=8,
            top_margin=6,
            bottom_margin=6,
        )
        view.get_style_context().add_class("v2-input")
        view.get_buffer().set_text("\n".join(profile.variables))
        scrolled = Gtk.ScrolledWindow(
            shadow_type=Gtk.ShadowType.IN,
            min_content_width=300,
            min_content_height=180,
        )
        scrolled.get_style_context().add_class("v2-input-frame")
        scrolled.add(view)
        area.pack_start(scrolled, True, True, 0)
        view.grab_focus()
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            self.controller.set_variables(
                self._buffer_text(view.get_buffer()).split("\n")
            )
        dialog.destroy()
