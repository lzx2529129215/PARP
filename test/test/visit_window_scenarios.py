#!/usr/bin/env python3
"""Five real-GUI, output-only scenarios for the nested visit predictor.

Reuses PARP app specifications, private fixtures and UI primitives. Prediction
and ground truth come from independently observed owned windows, never from
the action schedule. All deadlines use an unscaled monotonic clock.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
import random
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import torch

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "lzx/service/runtime_monitor"
PREDICTOR = REPO / "lzx/tool/operation_predictor"
sys.path[:0] = [str(RUNTIME), str(PREDICTOR)]
from online_visit_window import OnlineVisitWindowRunner
from collectors.foreground import _X11PropertyReader
from v3.train.train_app_lstm_visit_window import metrics


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


AUTO = load_module("visit_gui_automation", REPO / "test/automation/app_automation.py")
ACCEPT = load_module("visit_app_specs", REPO / "test/test/parp-acceptance-lzx.py")
CONFIG = Path(__file__).with_name("visit-window-scenarios.json")
DEFAULT_CHECKPOINT = PREDICTOR / "outputs/lsapp_expanded/visit_window_v1/app_lstm_visit_window.pt"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def gui_environment():
    env = dict(os.environ)
    text = subprocess.check_output(["systemctl", "--user", "show-environment"], text=True, timeout=5)
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in {"DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}:
            env[key] = value
    env.update(GDK_BACKEND="x11", MOZ_ENABLE_WAYLAND="0", WAYLAND_DISPLAY="", QT_QPA_PLATFORM="xcb")
    return env


@contextlib.contextmanager
def desktop(mode, out):
    """Own an isolated X server; never stop or reconfigure the user's desktop."""
    original = dict(os.environ)
    os.environ.update(gui_environment())
    processes, logs = [], []
    try:
        if mode == "isolated":
            for executable in ("Xvfb", "openbox"):
                if not shutil.which(executable):
                    raise RuntimeError(f"missing executable: {executable}")
            xlog = (out / "xvfb.log").open("w")
            logs.append(xlog)
            server = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "1280x900x24",
                                       "-nolisten", "tcp", "-ac"], stdout=subprocess.PIPE, stderr=xlog)
            processes.append(server)
            if not select.select([server.stdout], [], [], 10)[0]:
                raise RuntimeError("Xvfb startup timeout")
            number = server.stdout.readline().decode().strip()
            if not number.isdigit():
                raise RuntimeError("Xvfb did not provide a display number")
            os.environ.update(DISPLAY=f":{number}", XAUTHORITY="/dev/null",
                              XDG_CURRENT_DESKTOP="Openbox", XDG_SESSION_TYPE="x11")
            wlog = (out / "openbox.log").open("w")
            logs.append(wlog)
            processes.append(subprocess.Popen(["openbox", "--sm-disable"], stdout=wlog, stderr=wlog))
            for _ in range(50):
                check = subprocess.run(["wmctrl", "-m"], capture_output=True)
                if check.returncode == 0:
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("Openbox startup timeout")
        write_json(out / "desktop.json", {"mode": mode, "display": os.environ["DISPLAY"],
                                         "owned_pids": [p.pid for p in processes]})
        yield
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
        os.environ.clear()
        os.environ.update(original)


def score_actions(name, config, seed):
    """Schedule intent is stored separately from predicted hot/cold identities."""
    rng = random.Random(seed)
    duration = config["score_s"]
    actions = []
    if name in {"v1_short_return", "v2_cold_retire"}:
        elapsed, i = 0., 0
        while elapsed < duration:
            actions.append({"at_s": elapsed, "app": ("FIREFOX", "LIBREOFFICE")[i % 2]})
            elapsed += rng.choice((10, 15, 20)) if name == "v1_short_return" else 20
            i += 1
    elif name == "v3_medium_return":
        # Two fixed reference epochs: target enters at +90s and +150s.
        for base, delay in ((0, 90), (180, 150)):
            for offset in range(0, 180, 15):
                app = "THUNDERBIRD" if offset == delay else ("FIREFOX", "LIBREOFFICE")[(offset // 15) % 2]
                actions.append({"at_s": base + offset, "app": app})
    elif name == "v4_staggered_return":
        actions = [{"at_s": t, "app": app} for t, app in (
            (0, "FIREFOX"), (15, "LIBREOFFICE"), (60, "THUNDERBIRD"),
            (120, "VLC"), (210, "GIMP"), (240, "FIREFOX"), (270, "LIBREOFFICE"),
            (300, "FIREFOX"), (330, "LIBREOFFICE"))]
    elif name == "v5_active_set_shift":
        for offset in range(0, duration, 15):
            pair = ("FIREFOX", "LIBREOFFICE") if offset < 180 else ("GIMP", "EVINCE")
            actions.append({"at_s": offset, "app": pair[(offset // 15) % 2]})
    else:
        raise ValueError(f"unknown scenario: {name}")
    return actions


def make_plan(name, config, round_number, smoke=False):
    seed = config["seed"] + round_number - 1
    warmup = [{"at_s": i * 10, "app": app} for i, app in enumerate(config["apps"])]
    if smoke:
        warmup = [{"at_s": i * 2, "app": app} for i, app in enumerate(config["apps"])]
        actions = [{"at_s": t, "app": app} for t, app in ((0, "FIREFOX"), (15, "LIBREOFFICE"), (30, "FIREFOX"))]
        warmup_s, score_s, followup_s = 12, 35, 35
    else:
        actions = score_actions(name, config, seed)
        warmup_s, score_s, followup_s = config["warmup_s"], config["score_s"], config["followup_s"]
    return {"scenario": name, "round": round_number, "seed": seed, "smoke": smoke,
            "apps": config["apps"], "warmup": warmup, "actions": actions,
            "warmup_s": warmup_s, "score_s": score_s, "followup_s": followup_s,
            "time_scale": 1, "kernel_sink": "off", "pressure": "none", "prediction_roles": "model_outputs_only"}


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[19])  # ppid, starttime
    except (OSError, IndexError, ValueError):
        return None


def owned_root_app(pid, run_dir):
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        executable = Path(f"/proc/{pid}/exe").resolve().name.lower()
    except OSError:
        return ""
    if str(run_dir) + "/" not in command:
        return ""
    names = {"epiphany":"FIREFOX", "soffice.bin":"LIBREOFFICE", "oosplash":"LIBREOFFICE",
             "thunderbird":"THUNDERBIRD", "vlc":"VLC", "gimp":"GIMP", "gimp-2.10":"GIMP", "evince":"EVINCE"}
    return names.get(executable, "")


def cleanup_owned(run_dir):
    processes = {}
    owned = set()
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            pid = int(path.name)
            identity = process_identity(pid)
            if identity:
                processes[pid] = identity
                if owned_root_app(pid, run_dir):
                    owned.add(pid)
    while True:
        children = {pid for pid, (ppid, _) in processes.items() if ppid in owned}
        if children <= owned:
            break
        owned |= children
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in sorted(owned, reverse=True):
            current = process_identity(pid)
            if current and current[1] == processes[pid][1]:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        time.sleep(.3)


class Windows:
    def __init__(self, units, run_dir):
        self.units = units
        self.run_dir = run_dir
        self.reader = _X11PropertyReader()

    def app_for_pid(self, pid):
        try:
            cg = Path(f"/proc/{pid}/cgroup").read_text()
        except OSError:
            return ""
        return next((app for app, unit in self.units.items() if f"/{unit}\n" in cg), "") or owned_root_app(pid, self.run_dir)

    def snapshot(self):
        windows = {}
        for wid in self.reader.client_window_ids():
            prop = self.reader.window_properties(wid)
            if prop is None or not prop.is_normal_window:
                continue
            app = self.app_for_pid(prop.pid)
            if app:
                windows.setdefault(app, []).append(prop)
        active_id = self.reader.active_window_id()
        active = self.reader.window_properties(active_id) if active_id else None
        foreground = self.app_for_pid(active.pid) if active else ""
        return foreground, windows, active


class Observer:
    def __init__(self, run_dir, units, config, checkpoint):
        self.run_dir, self.config = run_dir, config
        self.windows = Windows(units, run_dir)
        self.stop = threading.Event()
        self.error = ""
        self.entries, self.calls = [], []
        self.latest_state = {}
        self.observed_until = 0.
        self.started = 0.
        self.maximum_sample_gap = 0.
        self.unknown_seconds = 0.
        self.score_begin = None
        self.score_end = None
        self.startup_ready = threading.Event()
        args = argparse.Namespace(session_id=run_dir.name, lstm_checkpoint=checkpoint,
            app_vocab=PREDICTOR / "data/vocab/lsapp_expanded/app_vocab_duration.json",
            group_vocab=PREDICTOR / "data/vocab/lsapp_expanded/user_group_vocab.json",
            user_group="通用用户", device="cpu", history_len=5, duration_cap_s=600.,
            app_key_to_vocab_name=config["vocab_names"], visit_hot_threshold=config["hot_threshold"],
            visit_cold_threshold=config["cold_threshold"])
        self.runner = OnlineVisitWindowRunner(args, run_dir / "model", run_dir / "review")
        if self.runner.predictor is None:
            raise RuntimeError(self.runner.predictor_error)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        previous_fg, previous_opened, previous_t = None, None, None
        try:
            with (self.run_dir / "observations.jsonl").open("w") as trace, (self.run_dir / "prediction_bundles.jsonl").open("w") as pred:
                while not self.stop.is_set():
                    foreground, windows, active = self.windows.snapshot()
                    opened = sorted(windows)
                    now = time.monotonic()
                    if not self.started:
                        self.started = now
                    when = dt.datetime.now().isoformat(sep=" ")
                    gap = now - previous_t if previous_t else 0
                    self.maximum_sample_gap = max(self.maximum_sample_gap, gap)
                    if not foreground:
                        self.unknown_seconds += gap
                    if foreground != previous_fg and foreground:
                        self.entries.append((now, self.config["vocab_names"][foreground]))
                    state = {"monotonic_s": now, "timestamp": when, "foreground_app": foreground,
                             "open_apps": opened, "window_id": active.window_id if active else "",
                             "pid": active.pid if active else 0}
                    self.latest_state = state
                    trace.write(json.dumps(state) + "\n")
                    trace.flush()
                    feature = {"timestamp": when, "foreground_app": foreground, "open_apps": "|".join(opened),
                               "feature_window_id": str(len(self.calls)), "session_id": self.run_dir.name}
                    changed = foreground != previous_fg or opened != previous_opened
                    result = self.runner.process_event(feature, "APP_SWITCH" if foreground != previous_fg else "APP_OPEN") if changed else self.runner.process_sample(feature)
                    if result.get("inference_executed"):
                        call = {"monotonic_s": now, "observed_state": state, "result": result}
                        self.calls.append(call)
                        pred.write(json.dumps(call, ensure_ascii=False) + "\n")
                        pred.flush()
                        if result["status"] != "success":
                            raise RuntimeError(result.get("skip_reason", "prediction failed"))
                    if set(opened) != set(self.config["apps"]):
                        raise RuntimeError(f"owned application windows missing: {set(self.config['apps']) - set(opened)}")
                    self.observed_until = now
                    previous_fg, previous_opened, previous_t = foreground, opened, now
                    self.startup_ready.set()
                    self.stop.wait(self.config["observer_interval_s"])
        except Exception as exc:
            self.error = str(exc)
            self.startup_ready.set()
        finally:
            self.windows.reader.close()
            self.runner.close()

    def wait_until(self, deadline):
        while time.monotonic() < deadline:
            if self.error:
                raise RuntimeError(self.error)
            time.sleep(min(.1, max(0, deadline - time.monotonic())))


def evaluate_calls(calls, entries, observed_until, start, end, vocab, hot=.90, cold=.20):
    predictions, labels, valid, eligible = [], [], [], []
    cold_counts, hot_counts, latencies = [], [], []
    for call in calls:
        t, result = call["monotonic_s"], call["result"]
        if not start <= t < end or result["status"] != "success":
            continue
        p, y, m = [np.zeros((len(vocab), 2), np.float32) for _ in range(3)]
        bg = np.zeros(len(vocab), bool)
        for row in result["all_probabilities"]:
            aid = row["app_id"]
            p[aid] = [row["p_visit_30s"], row["p_visit_180s"]]
            bg[aid] = row["thermal_state"] in {"hot", "cold", "neutral"}
            for i, horizon in enumerate((30, 180)):
                entered = any(t < e <= min(t + horizon, observed_until) and a == row["app"] for e, a in entries)
                y[aid, i] = entered
                m[aid, i] = entered or observed_until >= t + horizon
        predictions.append(p); labels.append(y); valid.append(m); eligible.append(bg)
        hot_counts.append(len(result["hot_apps"])); cold_counts.append(len(result["cold_apps"]))
        latencies.append(result["predict_latency_ms"])
    if not predictions:
        raise ValueError("no successful predictions in scoring period")
    arrays = {"probabilities": np.array(predictions), "labels": np.array(labels),
              "valid": np.array(valid), "eligible": np.array(eligible)}
    report = metrics(arrays["probabilities"], arrays["labels"], arrays["valid"], arrays["eligible"], vocab, hot, cold)
    report.update(cold_count_mean=float(np.mean(cold_counts)), cold_count_max=max(cold_counts),
        hot_count_mean=float(np.mean(hot_counts)), hot_count_max=max(hot_counts),
        inference_latency_p95_ms=float(np.percentile(latencies, 95)),
        cold_count_distribution={str(n):cold_counts.count(n) for n in sorted(set(cold_counts))})
    return report, arrays


def preflight(config, checkpoint):
    env = os.environ
    missing = [exe for exe in ("epiphany-browser", "libreoffice", "thunderbird", "vlc", "gimp", "evince", "xdotool", "wmctrl", "systemd-run", "dbus-run-session") if not shutil.which(exe)]
    if missing:
        raise RuntimeError(f"missing executables: {missing}")
    if not checkpoint.is_file():
        raise FileNotFoundError(checkpoint)
    subprocess.run(["xdotool", "getdisplaygeometry"], check=True, capture_output=True, timeout=5)
    if not AUTO._cgroup_available():
        raise RuntimeError("systemd user scopes unavailable; cannot isolate application ownership")
    return {"display": env["DISPLAY"], "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            "source_sha256": {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (Path(__file__).resolve(), REPO / "test/automation/app_automation.py",
                                           REPO / "test/test/parp-acceptance-lzx.py", RUNTIME / "online_visit_window.py",
                                           RUNTIME / "predictor_visit_window.py")},
            "kernel_sink": "off", "applications": config["apps"], "status": "PASS"}


def prepare(run_dir, config):
    ACCEPT.write_local_app_fixtures(run_dir)
    assets = REPO / "test/outputs/real_pc_assets_r8"
    names = ["local-page.html", "writer-test.odt", "mail-test.eml", "audio-test.wav", "document-test.pdf", "image-test.png"]
    names += [f"image-test-{i:02d}.png" for i in range(1, 9)]
    if any(not (assets / n).is_file() for n in names):
        subprocess.run([sys.executable, str(REPO / "test/automation/create_real_pc_assets_lzx.py"), "--output", str(assets)], check=True)
    for name in names:
        shutil.copy2(assets / name, run_dir / "fixtures" / name)
    write_json(run_dir / "asset_hashes.json", {n:hashlib.sha256((run_dir / "fixtures" / n).read_bytes()).hexdigest() for n in names})


def content_windows(app, candidates, run_dir):
    pattern = ACCEPT.app_specs(run_dir)[app].window_title
    return [p for p in candidates.get(app, []) if re.search(pattern, p.net_wm_name, re.IGNORECASE)]


def owned_switch(app, windows, ctx):
    _, candidates, _ = windows.snapshot()
    content = content_windows(app, candidates, windows.run_dir)
    if not content:
        raise RuntimeError(f"no owned content window for {app}")
    candidate = max(content, key=lambda p:len(p.net_wm_name))
    for attempt in range(6):
        AUTO.run(["xdotool", "windowactivate", candidate.window_id], ctx, check=False)
        AUTO.run(["xdotool", "windowraise", candidate.window_id], ctx, check=False)
        time.sleep(.15)
        fg, _, active = windows.snapshot()
        if fg == app and active.window_id == candidate.window_id:
            AUTO.verify_foreground({"app_key": app}, ctx)
            return {"window_id": active.window_id, "pid": active.pid, "app": app, "verified_at": time.monotonic()}
    raise RuntimeError(f"cannot activate owned window: {app}")


def operate(app, index, ctx):
    # All edits affect this round's private fixtures. No account/network actions.
    keys = {"FIREFOX": "Page_Down", "LIBREOFFICE": "Page_Down", "THUNDERBIRD": "Page_Down",
            "VLC": "space", "GIMP": "plus", "EVINCE": "Page_Down"}
    AUTO.key({"key": keys[app]}, ctx)
    if app == "LIBREOFFICE" and index % 3 == 0:
        AUTO.key({"key": "ctrl+End"}, ctx)
        AUTO.type_text({"text": f" PARP visit scenario step {index}. "}, ctx)
    if app == "FIREFOX" and index % 3 == 0:
        AUTO.key({"key": "ctrl+f"}, ctx)
        AUTO.type_text({"text": "Project section"}, ctx)
        AUTO.key({"key": "Escape"}, ctx)


def run_round(run_dir, plan, config, checkpoint):
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "scenario.json", plan)
    prepare(run_dir, config)
    specs = ACCEPT.app_specs(run_dir)
    ctx = AUTO.Context(dry_run=False, test_slice="parp-visit.slice")
    units = {}
    observer = None
    windows = None
    actions = []
    result = {"scenario": plan["scenario"], "round": plan["round"], "status": "INVALID", "path": str(run_dir), "smoke": plan["smoke"]}
    try:
        for app in config["apps"]:
            command = specs[app].command
            if app == "GIMP":
                command = command.replace("gimp ", "gimp --new-instance ", 1)
                command = f"env GIMP2_DIRECTORY={shlex.quote(str(run_dir / 'fixtures/gimp-profile'))} " + command
            if app == "EVINCE":
                command = "dbus-run-session -- " + command
            if app == "VLC":
                command += " --no-audio"
            unique = f"visit-{run_dir.parent.name[-12:]}-{run_dir.name}-{app.lower()}"
            AUTO.launch({"name": unique, "scope_name": unique, "command": command}, ctx)
            units[app] = ctx.processes[unique]
        write_json(run_dir / "owned_scopes.json", units)
        windows = Windows(units, run_dir)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            _, found, _ = windows.snapshot()
            if all(content_windows(app, found, run_dir) for app in config["apps"]):
                break
            time.sleep(.5)
        else:
            raise RuntimeError(f"startup missing owned windows: {set(config['apps']) - set(found)}")
        ownership = []
        for app, items in found.items():
            for item in items:
                identity = process_identity(item.pid)
                ownership.append({"app": app, "pid": item.pid, "starttime_ticks": identity[1] if identity else None,
                    "window_id": item.window_id, "original_scope": units[app],
                    "current_cgroup": Path(f"/proc/{item.pid}/cgroup").read_text().strip(),
                    "private_root_match": owned_root_app(item.pid, run_dir) == app})
        write_json(run_dir / "ownership.json", ownership)
        owned_switch(config["apps"][0], windows, ctx)
        observer = Observer(run_dir, units, config, checkpoint)
        observer.thread.start()
        if not observer.startup_ready.wait(10) or observer.error:
            raise RuntimeError(observer.error or "observer startup timeout")
        warmup_start = time.monotonic()
        for phase, events, start in (("warmup", plan["warmup"], warmup_start),
                                     ("score", plan["actions"], warmup_start + plan["warmup_s"])):
            if phase == "score":
                observer.score_begin = start
                observer.score_end = start + plan["score_s"]
            for index, event in enumerate(events):
                observer.wait_until(start + event["at_s"])
                actual = owned_switch(event["app"], windows, ctx)
                lateness = actual["verified_at"] - start - event["at_s"]
                record = {**event, **actual, "phase": phase, "planned_monotonic_s": start + event["at_s"], "lateness_s": lateness}
                actions.append(record)
                write_json(run_dir / "verified_actions.json", actions)
                if lateness > config["maximum_action_lateness_s"]:
                    raise RuntimeError(f"action missed deadline by {lateness:.3f}s")
                operate(event["app"], index, ctx)
        observer.wait_until(observer.score_end + plan["followup_s"])
        observer.stop.set(); observer.thread.join(5)
        if observer.error:
            raise RuntimeError(observer.error)
        if observer.maximum_sample_gap > 1.:
            raise RuntimeError(f"observer sampling gap {observer.maximum_sample_gap:.3f}s exceeds 1s")
        if observer.unknown_seconds > 2.:
            raise RuntimeError(f"unmapped foreground for {observer.unknown_seconds:.3f}s")
        report, arrays = evaluate_calls(observer.calls, observer.entries, observer.observed_until,
            observer.score_begin, observer.score_end, observer.runner.vocab, config["hot_threshold"], config["cold_threshold"])
        report.update(maximum_sample_gap_s=observer.maximum_sample_gap, unknown_foreground_s=observer.unknown_seconds,
            score_start_monotonic_s=observer.score_begin, score_end_monotonic_s=observer.score_end,
            observed_until_monotonic_s=observer.observed_until,
            observation_interval_s=config["observer_interval_s"],
            periodic_refresh_calls=sum(c['result']['trigger_type']=='periodic_refresh_30s' for c in observer.calls))
        write_json(run_dir / "evaluation.json", report)
        np.savez_compressed(run_dir / "scored_predictions.npz", **arrays)
        write_json(run_dir / "foreground_entries.json", observer.entries)
        result.update(status="PASS", report=report)
    except KeyboardInterrupt:
        result["error"] = "execution interrupted"
        raise
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if observer:
            observer.stop.set(); observer.thread.join(5)
        if windows:
            windows.reader.close()
        # Track private roots even after service routing, then stop our scopes.
        cleanup_owned(run_dir)
        AUTO.cleanup_tracked_processes(ctx)
        write_json(run_dir / "result.json", result)
    return result


def summarize(out, results, vocab, config):
    aggregate = {}
    for name in sorted({r["scenario"] for r in results}):
        good = [r for r in results if r["scenario"] == name and r["status"] == "PASS" and not r["smoke"]]
        if not good:
            continue
        chunks = [np.load(Path(r["path"]) / "scored_predictions.npz") for r in good]
        arrays = {k:np.concatenate([x[k] for x in chunks]) for k in ("probabilities", "labels", "valid", "eligible")}
        report = metrics(arrays["probabilities"], arrays["labels"], arrays["valid"], arrays["eligible"], vocab,
                         config["hot_threshold"], config["cold_threshold"])
        counts = (arrays["eligible"] & (arrays["probabilities"][:, :, 1] < config["cold_threshold"])).sum(1)
        report.update(cold_count_mean=float(counts.mean()), cold_count_min=int(counts.min()),
                      cold_count_max=int(counts.max()),
                      cold_count_distribution={str(n):int((counts == n).sum()) for n in np.unique(counts)})
        aggregate[name] = report
    write_json(out / "summary.json", {"rounds": results, "scenarios": aggregate,
        "kernel_sink": "off", "model_unchanged": True,
        "hot_threshold": config["hot_threshold"], "cold_threshold": config["cold_threshold"]})
    lines = ["# V1–V5 双窗口预测真实 GUI 场景", "", "所有指标来自实际前台观测。未接入内核回收，模型与阈值未修改。", "",
             "| 场景 | 有效轮次 | 热准确率 | 热召回率 | 冷实际访问率 | 冷覆盖率 |", "|---|---:|---:|---:|---:|---:|"]
    for name, r in aggregate.items():
        thermal = r["thermal"]
        vals = [thermal[k] for k in ("hot_precision", "hot_recall", "cold_actual_visit_rate", "cold_coverage")]
        lines.append('| '+name+' | '+str(sum(x['scenario']==name and x['status']=='PASS' and not x['smoke'] for x in results))+' | '+' | '.join('N/A' if v is None else f'{v:.2%}' for v in vals)+' |')
    for r in results:
        if r["status"] != "PASS":
            lines.append(f"\nINVALID {r['scenario']} round {r['round']}: {r.get('error')}")
    lines.extend(["", "| 场景 | 预测次数 | 30s PR-AUC / Brier | 180s PR-AUC / Brier | 每次冷应用数（均值 / 最少 / 最多） |",
                  "|---|---:|---:|---:|---:|"])
    def number(value):
        return "N/A" if value is None else f"{value:.4f}"
    for name, r in aggregate.items():
        windows = [r["windows"][str(h)] for h in (30, 180)]
        scores = [number(w["pr_auc"]) + " / " + number(w["brier"]) for w in windows]
        lines.append(f"| {name} | {r['samples']} | {' | '.join(scores)} | {r['cold_count_mean']:.2f} / {r['cold_count_min']} / {r['cold_count_max']} |")
    lines.extend(["", "PR-AUC 为 AP；窗口指标覆盖词表中的全部真实应用，冷热指标只覆盖当时运行的后台应用。",
        "冷实际访问率的分母为预测冷且180秒标签有效的应用-预测时刻对；空热名单的准确率为 N/A。",
        "这些是人工 GUI 场景结果，不替代原始 LSApp 独立测试集的准确率。应用数为6（最多5个后台），词表未扩展。",
        "每轮有效标签数、名单覆盖率、单调性检查见 evaluation.json；汇总详见 summary.json。",
        "smoke 只验证链路，后续观察不足的负标签被掩码排除，不纳入正式汇总。"])
    (out / "REPORT.md").write_text('\n'.join(lines)+'\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["plan", "preflight", "run", "smoke"])
    p.add_argument("--config", type=Path, default=CONFIG)
    p.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--scenario", default="all")
    p.add_argument("--rounds", type=int)
    p.add_argument("--display-mode", choices=["isolated", "host"], default="isolated")
    args = p.parse_args()
    torch.set_num_threads(1)
    config = json.loads(args.config.read_text())
    names = list(config["scenarios"]) if args.scenario == "all" else [args.scenario]
    if any(name not in config["scenarios"] for name in names):
        p.error("unknown scenario")
    rounds = config["rounds"] if args.rounds is None else args.rounds
    if rounds < 1:
        p.error("rounds must be positive")
    if args.command == "smoke":
        names, rounds = names[:1], 1
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    plans = [make_plan(name, config, r, args.command == "smoke") for name in names for r in range(1, rounds + 1)]
    write_json(out / "plans.json", plans)
    write_json(out / "config.json", config)
    if args.command == "plan":
        print(json.dumps({"plans":len(plans), "nominal_seconds":sum(x['warmup_s']+x['score_s']+x['followup_s'] for x in plans)}))
        return
    with desktop(args.display_mode, out):
        execute(args, config, plans, out)


def execute(args, config, plans, out):
    write_json(out / "preflight.json", preflight(config, args.checkpoint))
    if args.command == "preflight":
        return
    results = []
    vocab = json.loads((PREDICTOR / "data/vocab/lsapp_expanded/app_vocab_duration.json").read_text())
    def interrupted(*_):
        raise KeyboardInterrupt("scenario execution interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    for plan in plans:
        run_dir = out / f"{plan['scenario']}-r{plan['round']:02d}"
        write_json(out / "progress.json", {"state":"running", "scenario":plan['scenario'], "round":plan['round'], "completed":len(results), "total":len(plans)})
        try:
            result = run_round(run_dir, plan, config, args.checkpoint)
        except KeyboardInterrupt:
            write_json(out / "progress.json", {"state": "interrupted", "completed": len(results), "total": len(plans)})
            raise
        results.append(result)
        summarize(out, results, vocab, config)
        print(json.dumps({k:v for k,v in result.items() if k!='report'},ensure_ascii=False),flush=True)
        if result["status"] != "PASS":
            write_json(out / "progress.json", {"state":"stopped_invalid", "result":result, "completed":len(results), "total":len(plans)})
            raise SystemExit(1)
    write_json(out / "progress.json", {"state":"complete", "completed":len(results), "total":len(plans)})


if __name__ == "__main__":
    main()
