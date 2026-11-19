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


"""Convert between v2 profiles and v1 preset files.

Rendering produces ordinary v1 presets, named so the old UI shows they are
generated. They are overwritten on every Apply and never read back.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from inputremapper.configs.paths import PathUtils
from inputremapper.gui_v2.document import Entry, InputRef, Profile, Trigger
from inputremapper.gui_v2.macro_text import (
    MacroTextError,
    parse_text,
    print_chain,
)
from inputremapper.gui_v2.schema import SCHEMA, ArgKind
from inputremapper.gui_v2.tree import Call, Chain, Var

RENDERED_PREFIX = "[v2 read-only] "

# Mapping fields that v2 models itself, everything else is carried in v1_extra.
_HANDLED_FIELDS = {"input_combination", "name", "output_symbol", "target_uinput"}


class RenderError(ValueError):
    """Rendering failed. With an `entry_id`, the problem is in that entry, and the
    message is written to follow "In this mapping, "."""

    def __init__(self, message: str, entry_id: str | None = None) -> None:
        super().__init__(message)
        self.entry_id = entry_id


def rendered_name(profile_name: str) -> str:
    return f"{RENDERED_PREFIX}{profile_name}"


def is_rendered_name(preset_name: str) -> bool:
    return preset_name.startswith(RENDERED_PREFIX)


def _first_gap(chain: Chain) -> str | None:
    """Describe the first block that is missing something, or None if complete."""
    for call in chain:
        spec = SCHEMA.get(call.op)
        if spec is None:
            continue
        for index, value in enumerate(call.args):
            arg = spec.args[0 if spec.spread else index]
            if arg.kind is ArgKind.SYMBOL and value == "":
                return f'a "{call.op}" block has no key chosen'
            if (
                arg.kind is ArgKind.VARIABLE
                and isinstance(value, Var)
                and value.name == ""
            ):
                return f'a "{call.op}" block has no variable chosen'
            if isinstance(value, tuple):
                if arg.required and not value:
                    return f'a "{call.op}" block is empty'
                inner = _first_gap(cast("Chain", value))
                if inner is not None:
                    return inner
    return None


def entry_to_mapping(entry: Entry) -> dict[str, Any]:
    """The v1 mapping dict for an entry, as it is stored in a preset file."""
    if not entry.trigger.inputs:
        raise RenderError("no keys are set to trigger it", entry.id)

    gap = _first_gap(entry.then)
    if gap is not None:
        raise RenderError(gap, entry.id)

    mapping: dict[str, Any] = {
        "input_combination": [i.to_json() for i in entry.trigger.inputs],
        "target_uinput": entry.target,
    }
    if entry.name:
        mapping["name"] = entry.name
    if entry.then:
        mapping["output_symbol"] = print_chain(entry.then)
    mapping.update(entry.v1_extra)
    return mapping


def render_profile(profile: Profile) -> dict[str, list[dict[str, Any]]]:
    """Group the profile's mappings by device. Keys are device group keys."""
    by_device: dict[str, list[dict[str, Any]]] = {}
    for entry in profile.entries:
        if entry.trigger.device is None:
            raise RenderError("no device is chosen", entry.id)
        by_device.setdefault(entry.trigger.device, []).append(entry_to_mapping(entry))
    return by_device


def write_rendered(
    profile: Profile,
    group_name_of: Callable[[str], str | None],
    presets_dir: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Write v1 presets for the profile. Returns {device group key: preset name}.

    `group_name_of` maps a group key to the directory name the daemon looks in,
    or None for devices that are not connected. Those are skipped.
    Rendering is checked completely before anything is written.
    """
    rendered = render_profile(profile)
    base = presets_dir if presets_dir is not None else PathUtils.get_preset_path()
    preset_name = rendered_name(profile.name)

    targets: dict[str, tuple[Path, list[dict[str, Any]]]] = {}
    for group_key, mappings in rendered.items():
        group_name = group_name_of(group_key)
        if group_name is None:
            continue
        path = (
            Path(base)
            / PathUtils.sanitize_path_component(group_name)
            / f"{preset_name}.json"
        )
        targets[group_key] = (path, mappings)

    for path, mappings in targets.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(mappings, indent=4) + "\n")
        os.replace(temporary, path)

    return {key: preset_name for key in targets}


# Import ---------------------------------------------------------------------------


def entry_from_mapping(mapping: dict[str, Any], device: str | None) -> Entry:
    inputs = tuple(InputRef.from_json(i) for i in mapping.get("input_combination", []))

    then: tuple[Call, ...] = ()
    output = mapping.get("output_symbol")
    if output:
        try:
            then = parse_text(output)
        except MacroTextError:
            # Keep the original text; the block editor shows it as a grey block.
            then = (Call("raw", (output,)),)

    return Entry(
        name=mapping.get("name"),
        trigger=Trigger(inputs=inputs, device=device),
        then=then,
        target=mapping.get("target_uinput") or "keyboard",
        v1_extra={k: v for k, v in mapping.items() if k not in _HANDLED_FIELDS},
    )


def import_preset_file(path: str | os.PathLike[str], device: str | None) -> list[Entry]:
    """Read a v1 preset file and convert each mapping to an entry."""
    data: Any = json.loads(Path(path).read_text() or "[]")
    if not isinstance(data, list):
        raise RenderError(f'"{path}" is not a preset file')
    items = cast("list[Any]", data)
    return [
        entry_from_mapping(cast("dict[str, Any]", m), device)
        for m in items
        if isinstance(m, dict)
    ]
