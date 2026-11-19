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
from gi.repository import Gtk

from inputremapper.configs.paths import PathUtils
from inputremapper.daemon import Daemon
from inputremapper.groups import _Groups
from inputremapper.gui_v2.controller import Controller
from inputremapper.gui_v2.store import default_profiles_dir
from inputremapper.gui_v2.window import Window
from inputremapper.logging.logger import logger


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("-d", "--debug", action="store_true", dest="debug")
    options = parser.parse_args(sys.argv[1:])
    logger.update_verbosity(options.debug)
    logger.log_info("input-remapper beta GUI")

    daemon = Daemon.connect()
    controller = Controller(
        daemon,
        _Groups(),
        default_profiles_dir(),
        PathUtils.get_preset_path(),
    )

    window = Window(controller)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
