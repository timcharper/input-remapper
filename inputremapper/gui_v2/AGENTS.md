# Beta GUI (`inputremapper/gui_v2`)

A profile-first rewrite of the GUI that lives next to the existing one. Design
sketches: https://claude.ai/artifact/C3tBp3f8xrta4TLMp3QuLw

Rules that were decided on purpose:

- **Independent of `inputremapper/gui`.** It may import the daemon, `Preset`,
  `Mapping` and config paths, and nothing from the old GUI package. It is launched
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

Known stand-ins: the trigger is typed as key names (`trigger_text.py`) until recording
from a device exists, and "Then" is the macro text until the block editor is built.
