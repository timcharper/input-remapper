# Beta GUI (`inputremapper/gui_v2`)

A profile-first rewrite of the GUI that lives next to the existing one. Design
sketches: https://claude.ai/artifact/C3tBp3f8xrta4TLMp3QuLw

Rules that were decided on purpose:

- **Independent of `inputremapper/gui`.** It may import the daemon, `Preset`,
  `Mapping` and config paths, and nothing from the old GUI package. The one exception
  is `inputremapper.gui.reader_service`, the root helper that reads keys; `recorder.py`
  uses its pipe protocol. It is launched
  separately, from source.
- **A profile is the set of presets with the same name across devices.** The daemon
  only takes `(group_key, preset_name)` and reads the preset file itself, so nothing
  in the daemon changes.
- **v2 files are the source of truth.** One JSON file per profile under
  `<config>/v2/profiles/`. On Apply they are rendered to ordinary v1 presets named
  `[v2 read-only] <profile>`, which are overwritten every time and never read back.
  Apply is `stop_all()` followed by `start_injecting` per device. Do not use
  `set_suspended`: it queues the new injection too.
- **Macros are stored as a tree, not text** (`tree.py`). `macro_text.print_chain`
  produces the v1 macro string. The text parser exists only for importing v1
  presets and for the "Edit as text" box. Functions the editor doesn't know are kept
  as `raw` calls and survive unchanged.
- **Block definitions come from the engine** (`schema.py` reads each task's
  `argument_configs`). Only units and aliases are written by hand.
- **v1 fields the editor doesn't expose** (`deadzone`, `release_timeout`, ...) are
  carried in `Entry.v1_extra`. Import followed by render must stay lossless; there is
  a test for it.
- **The document is immutable.** Edits create a new `Workspace`, and undo/redo is a
  linear stack of snapshots (`undo.py`) with no command objects. Typing coalesces via
  `merge_key`.
- Variables are only declared names. The engine has one global variable store and no
  initial values.
- Autoload is not supported yet. A hotplug autoload of a v1 preset can replace a
  running profile on that device; this is a known limitation.
- Only "pressed" triggers exist, because the engine has no trigger-on-release.
- Analog axes are out of scope for now.

## Type checking

This package is also kept clean under pyright in strict mode (the same
checks Pylance reports in an editor). To check it, put a `pyrightconfig.json` with
`{"include": ["inputremapper/gui_v2"], "typeCheckingMode": "strict"}` at the repo root,
run `pyright --pythonpath /usr/bin/python3`, and delete the file again. Note that
`include` paths must be relative.

## Running the beta GUI

`/usr/bin/python3 -m inputremapper.bin.input_remapper_beta` (system Python, it has `gi`; the service must be running, or
it offers to start it). `controller.py` holds all behaviour and has no GTK imports;
`window.py` only displays its state and forwards actions, so add logic there and test
it in `tests/unit/test_gui_v2/test_controller.py`.

Keys are recorded from the entry's device via the reader service (Record button, needs
a device chosen first; it starts the service with pkexec on first use). The typed key
names (`trigger_text.py`) stay as a fallback. Only keys and buttons are recorded, no
axes.

## Window conventions

- Widgets are created once and updated in place (keyed rows, write a field only if its
  value differs and it isn't focused). Never rebuild a list on refresh.
- Refreshes are scheduled with `GLib.idle_add` (`Window.schedule_refresh`), never run
  inside a signal handler: destroying a widget in its own handler segfaulted.
- Handlers check `self._refreshing` first, so programmatic updates don't become edits.
- Layout: profile bar on top (combo with "+ New profile", ▶ on the running one, rename,
  variables, clone, delete, play/stop), entries list on the left, entry form on the right.

## The block editor

`blocks.py` (GTK) shows the `then` tree as Scratch-style blocks: colored by category,
macro arguments as nested C-shaped stacks, blocks reordered by dragging the handle
into any gap, new ones from each stack's "+ block" palette. All edits are computed by
`block_ops.py` (no GTK, tested): stacks are addressed by a chain path of
`(call index, argument index)` steps. The editor keeps no state of its own; it reports
a new tree through `on_change(chain, typing)` and is told what to show with
`set_chain`, which only rebuilds widgets when the *shape* (blocks and nesting)
changes. "Text" in the form switches to the macro text box.

Known gaps: blocks can't be dragged out of the palette, only inserted by click; an
output that is a bare key (`symbol`) is one block that can't be mixed with others
until it's converted by hand; variables used in blocks are not auto-declared.
