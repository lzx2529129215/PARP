"use strict";

const { Gio, GLib, Meta } = imports.gi;

const BUS_NAME = "org.huawei.RuntimeAppMonitor";
const OBJECT_PATH = "/org/huawei/RuntimeAppMonitor";
const INTERFACE = "org.huawei.RuntimeAppMonitor";
const SIGNAL = "WindowEvent";

class RuntimeAppMonitorExtension {
    constructor() {
        this._displaySignals = [];
        this._windowSignals = new Map();
        this._lastFocusId = null;
        this._busNameId = 0;
    }

    enable() {
        try {
            global.log("[runtime-monitor] enable() starting");
        } catch (e) {}

        this._busNameId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.REPLACE,
            null,
            null,
            null
        );
        try {
            global.log("[runtime-monitor] bus_own_name done, id=" + this._busNameId);
        } catch (e) {}

        this._displaySignals.push(
            global.display.connect("window-created", (_display, window) => {
                this._trackWindow(window);
                this._emitWindowEvent("Opened", window);
            })
        );
        this._displaySignals.push(
            global.display.connect("notify::focus-window", () => {
                this._handleFocusChanged();
            })
        );

        for (const actor of global.get_window_actors()) {
            this._trackWindow(actor.meta_window);
        }
        this._handleFocusChanged();
        try {
            global.log("[runtime-monitor] enable() complete, tracked " + this._windowSignals.size + " windows");
        } catch (e) {}
    }

    disable() {
        for (const signalId of this._displaySignals) {
            global.display.disconnect(signalId);
        }
        this._displaySignals = [];

        for (const [window, signalIds] of this._windowSignals.entries()) {
            for (const signalId of signalIds) {
                try {
                    window.disconnect(signalId);
                } catch (_error) {
                    // Window may already be unmanaged.
                }
            }
        }
        this._windowSignals.clear();
        this._lastFocusId = null;

        if (this._busNameId) {
            Gio.bus_unown_name(this._busNameId);
            this._busNameId = 0;
        }
    }

    _trackWindow(window) {
        if (!this._isAppWindow(window) || this._windowSignals.has(window)) {
            return;
        }

        const signalIds = [];
        signalIds.push(
            window.connect("unmanaged", () => {
                this._emitWindowEvent("Closed", window);
                this._disconnectWindow(window);
            })
        );
        signalIds.push(
            window.connect("notify::minimized", () => {
                if (this._safeBool(window, "minimized")) {
                    this._emitWindowEvent("Minimized", window);
                }
            })
        );
        this._windowSignals.set(window, signalIds);
    }

    _disconnectWindow(window) {
        const signalIds = this._windowSignals.get(window);
        if (!signalIds) {
            return;
        }
        for (const signalId of signalIds) {
            try {
                window.disconnect(signalId);
            } catch (_error) {
                // Window may already be unmanaged.
            }
        }
        this._windowSignals.delete(window);
    }

    _handleFocusChanged() {
        const window = global.display.focus_window;
        if (!this._isAppWindow(window)) {
            return;
        }
        this._trackWindow(window);
        const focusId = this._windowId(window);
        if (focusId === this._lastFocusId) {
            return;
        }
        this._lastFocusId = focusId;
        this._emitWindowEvent("Switched", window);
    }

    _emitWindowEvent(eventType, window) {
        if (!this._isAppWindow(window)) {
            return;
        }
        const payload = {
            event_type: eventType,
            timestamp_ms: Date.now(),
            window_id: String(this._windowId(window)),
            title: this._safeCallString(window, "get_title"),
            wm_class: this._safeCallString(window, "get_wm_class"),
            gtk_app_id: this._safeCallString(window, "get_gtk_application_id"),
            pid: this._safeCallInt(window, "get_pid"),
            is_minimized: this._safeBool(window, "minimized")
        };

        Gio.DBus.session.emit_signal(
            null,
            OBJECT_PATH,
            INTERFACE,
            SIGNAL,
            GLib.Variant.new("(s)", [JSON.stringify(payload)])
        );
    }

    _isAppWindow(window) {
        if (!window) {
            return false;
        }
        try {
            if (window.get_window_type() !== Meta.WindowType.NORMAL) {
                return false;
            }
            if (window.skip_taskbar) {
                return false;
            }
            return true;
        } catch (_error) {
            return false;
        }
    }

    _windowId(window) {
        const stable = this._safeCallInt(window, "get_stable_sequence");
        if (stable > 0) {
            return stable;
        }
        return `${this._safeCallInt(window, "get_pid")}:${this._safeCallString(window, "get_title")}`;
    }

    _safeCallString(object, method) {
        try {
            if (object && typeof object[method] === "function") {
                const value = object[method]();
                return value === null || value === undefined ? "" : String(value);
            }
        } catch (_error) {
            return "";
        }
        return "";
    }

    _safeCallInt(object, method) {
        try {
            if (object && typeof object[method] === "function") {
                const value = Number(object[method]());
                return Number.isFinite(value) ? Math.trunc(value) : 0;
            }
        } catch (_error) {
            return 0;
        }
        return 0;
    }

    _safeBool(object, property) {
        try {
            return Boolean(object[property]);
        } catch (_error) {
            return false;
        }
    }
}

function init() {
    return new RuntimeAppMonitorExtension();
}
