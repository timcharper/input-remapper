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


import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evdev import ecodes

from inputremapper.gui.reader_service import CMD_STOP_READING, MSG_EVENT
from inputremapper.gui_v2.controller import Controller
from inputremapper.gui_v2.document import InputRef
from inputremapper.gui_v2.recorder import Accumulator, Recorder

CTRL = ecodes.KEY_LEFTCTRL
A = ecodes.KEY_A


class TestAccumulator(unittest.TestCase):
    def test_combination_finishes_when_everything_is_released(self) -> None:
        acc = Accumulator()
        self.assertTrue(acc.feed(1, CTRL, None, True))
        self.assertTrue(acc.feed(1, A, None, True))
        self.assertFalse(acc.feed(1, A, None, False))
        self.assertFalse(acc.finished)
        acc.feed(1, CTRL, None, False)
        self.assertTrue(acc.finished)
        self.assertEqual(acc.inputs, [InputRef(1, CTRL), InputRef(1, A)])

    def test_releases_of_unseen_keys_are_ignored(self) -> None:
        # The key that started the recording (e.g. a mouse click) is released first.
        acc = Accumulator()
        acc.feed(1, ecodes.BTN_LEFT, None, False)
        self.assertFalse(acc.finished)
        acc.feed(1, A, None, True)
        acc.feed(1, A, None, False)
        self.assertTrue(acc.finished)

    def test_repeats_do_not_add_twice(self) -> None:
        acc = Accumulator()
        self.assertTrue(acc.feed(1, A, None, True))
        self.assertFalse(acc.feed(1, A, None, True))
        self.assertEqual(len(acc.inputs), 1)

    def test_axes_are_ignored(self) -> None:
        acc = Accumulator()
        self.assertFalse(acc.feed(ecodes.EV_REL, ecodes.REL_X, None, True))
        self.assertEqual(acc.inputs, [])

    def test_origin_hash_is_kept(self) -> None:
        acc = Accumulator()
        acc.feed(1, A, "abc", True)
        self.assertEqual(acc.inputs, [InputRef(1, A, origin_hash="abc")])


class FakePipe:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def poll(self) -> bool:
        return bool(self.messages)

    def recv(self) -> Any:
        return self.messages.pop(0)

    def send(self, message: Any) -> None:
        self.messages.append(message)


def event(code: int, pressed: bool, type_: int = 1) -> dict[str, Any]:
    body = {"type": type_, "code": code, "value": int(pressed), "origin_hash": None}
    return {"type": MSG_EVENT, "message": body}


class TestRecorder(unittest.TestCase):
    def setUp(self) -> None:
        self.results = FakePipe()
        self.commands = FakePipe()
        self.updates: list[tuple[InputRef, ...]] = []
        self.done = 0
        self.recorder = Recorder(self.results, self.commands)

    def _start(self) -> None:
        self.recorder.start("kbd", self.updates.append, self._done)

    def _done(self) -> None:
        self.done += 1

    def test_records_and_stops_the_reader(self) -> None:
        self._start()
        self.assertEqual(self.commands.messages, ["kbd"])
        self.results.messages += [
            event(CTRL, True),
            event(A, True),
            event(A, False),
            event(CTRL, False),
        ]
        self.recorder.poll()
        self.assertEqual(len(self.updates), 2)
        self.assertEqual(self.done, 1)
        self.assertFalse(self.recorder.recording)
        self.assertEqual(self.commands.messages[-1], CMD_STOP_READING)

    def test_old_events_from_before_start_are_dropped(self) -> None:
        self.results.messages.append(event(A, True))
        self._start()
        self.recorder.poll()
        self.assertEqual(self.updates, [])

    def test_events_after_stop_are_ignored(self) -> None:
        self._start()
        self.recorder.stop()
        self.results.messages.append(event(A, True))
        self.recorder.poll()
        self.assertEqual(self.updates, [])


class FakeRecorder:
    def __init__(self) -> None:
        self.recording = False
        self.group: str | None = None
        self.on_update: Callable[[tuple[InputRef, ...]], None] = lambda _: None
        self.on_done: Callable[[], None] = lambda: None

    def start(
        self,
        group_key: str,
        on_update: Callable[[tuple[InputRef, ...]], None],
        on_done: Callable[[], None],
    ) -> None:
        self.recording = True
        self.group = group_key
        self.on_update = on_update
        self.on_done = on_done

    def stop(self) -> None:
        self.recording = False


class FakeGroups:
    def get_groups(self) -> list[Any]:
        return []


class TestControllerRecording(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.recorder = FakeRecorder()
        self.c = Controller(
            None,
            FakeGroups(),
            Path(self._tmp.name) / "p",
            Path(self._tmp.name) / "pre",
            recorder=self.recorder,
        )
        self.c.new_profile()
        self.c.new_entry()

    def _set_device(self) -> None:
        from dataclasses import replace

        self.c.edit_entry(
            lambda e: replace(e, trigger=replace(e.trigger, device="kbd"))
        )

    def test_needs_a_device(self) -> None:
        self.c.start_recording()
        self.assertFalse(self.recorder.recording)
        self.assertIn("device", self.c.message)

    def test_recorded_keys_become_one_undo_step(self) -> None:
        self._set_device()
        self.c.start_recording()
        self.assertEqual(self.recorder.group, "kbd")
        self.recorder.on_update((InputRef(1, CTRL),))
        self.recorder.on_update((InputRef(1, CTRL), InputRef(1, A)))
        entry = self.c.entry
        assert entry is not None
        self.assertEqual(len(entry.trigger.inputs), 2)
        self.c.undo()
        entry = self.c.entry
        assert entry is not None
        self.assertEqual(entry.trigger.inputs, ())
        self.assertEqual(entry.trigger.device, "kbd")

    def test_navigation_cancels_recording(self) -> None:
        self._set_device()
        self.c.start_recording()
        self.c.select_entry(None)
        self.assertFalse(self.recorder.recording)

    def test_toggle(self) -> None:
        self._set_device()
        self.c.toggle_recording()
        self.assertTrue(self.c.recording)
        self.c.toggle_recording()
        self.assertFalse(self.c.recording)


if __name__ == "__main__":
    unittest.main()
