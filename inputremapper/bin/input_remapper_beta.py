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


"""Starts the beta user interface: `python3 -m inputremapper.bin.input_remapper_beta`."""

from __future__ import annotations

import sys
from argparse import ArgumentParser

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from inputremapper.bin.process_utils import ProcessUtils
from inputremapper.configs.paths import PathUtils
from inputremapper.daemon import Daemon
from inputremapper.groups import _Groups
from inputremapper.gui.reader_service import ReaderService, ReaderServiceError
from inputremapper.gui_v2.controller import Controller
from inputremapper.gui_v2.recorder import Recorder, RecorderError
from inputremapper.gui_v2.store import default_profiles_dir
from inputremapper.gui_v2.window import Window
from inputremapper.ipc.pipe import Pipe
from inputremapper.logging.logger import logger


def ensure_reader_service() -> None:
    """Start the root helper that reads keys, if it isn't running yet."""
    if ProcessUtils.count_python_processes("input-remapper-reader-service") >= 1:
        return
    try:
        ReaderService.pkexec_reader_service()
    except ReaderServiceError as error:
        raise RecorderError(str(error)) from error


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("-d", "--debug", action="store_true", dest="debug")
    options = parser.parse_args(sys.argv[1:])
    logger.update_verbosity(options.debug)
    logger.log_info("input-remapper beta GUI")

    daemon = Daemon.connect()

    # The pipes must be created by this (unprivileged) process before the reader
    # service starts, otherwise the service owns them and we can't read them.
    results_path, commands_path = ReaderService.get_pipe_paths()
    recorder = Recorder(Pipe(results_path), Pipe(commands_path), ensure_reader_service)
    GLib.timeout_add(30, lambda: recorder.poll() or True)
    controller = Controller(
        daemon,
        _Groups(),
        default_profiles_dir(),
        PathUtils.get_preset_path(),
        recorder=recorder,
    )

    window = Window(controller)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
