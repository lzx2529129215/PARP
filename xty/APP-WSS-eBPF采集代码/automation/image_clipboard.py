#!/usr/bin/env python3
import sys
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gtk  # noqa: E402

pixbuf = GdkPixbuf.Pixbuf.new_from_file(sys.argv[1])
clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
clipboard.set_image(pixbuf)
clipboard.store()
print("READY", flush=True)
Gtk.main()
