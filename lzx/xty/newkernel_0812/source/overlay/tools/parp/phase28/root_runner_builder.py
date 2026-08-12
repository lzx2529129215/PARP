#!/usr/bin/env python3
"""Derive the reviewed Phase2.8 root collector from the proven Phase2.7B runner.

The derivation is hash-pinned and every textual edit is cardinality checked.
It does not execute the generated script or invoke sudo.
"""

import argparse
import hashlib
import os
from pathlib import Path


SOURCE_SHA256 = "b82f0e03613fd764746da48b99792392c987e31681719f57533dbfc75094bd1c"
BASELINE = "01995ef7e5e523edb44b26aa84015bf09e385776"


def replace_once(text, old, new):
    count = text.count(old)
    if count != 1:
        raise AssertionError("expected one occurrence, found %d: %s" % (count, old[:100]))
    return text.replace(old, new, 1)


def build(source, destination):
    payload = source.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != SOURCE_SHA256:
        raise SystemExit("source collector hash mismatch: %s" % actual)
    text = payload.decode("utf-8")
    text = text.replace("Phase2.7B", "Phase2.8")
    text = text.replace("phase27b", "phase28")
    text = text.replace("PHASE27B", "PHASE28")
    text = text.replace("RUNTIME_PHASE28_REAL_FRESH_REUSED", "RUNTIME_PHASE28_REAL_FRESH")
    text = replace_once(text,
        "expected_head=146bbe66a42c98715a5a8c447923cb1b3859074c",
        "expected_head=" + BASELINE)
    text = replace_once(text,
        'tree="$project/MGLRU-test/v4-parp/work/linux-6.17.13-parp-v4-phase27-page-segment"',
        'tree="$project/MGLRU-test/v4-parp/work/linux-6.17.13-parp-v4-phase28-refault-prediction"\n'
        'sampler="$tree/tools/parp/phase28/cgroup_sampler.py"')
    text = replace_once(text,
        'wps_scenario="$script_dir/phase28_wps_real.json"\n'
        'files_scenario="$script_dir/phase28_files_real.json"',
        'wps_scenario_1="$script_dir/phase28_wps_repeated_01.json"\n'
        'wps_scenario_2="$script_dir/phase28_wps_repeated_02.json"\n'
        'wps_scenario_3="$script_dir/phase28_wps_repeated_03.json"\n'
        'files_scenario_1="$script_dir/phase28_files_repeated_01.json"\n'
        'files_scenario_2="$script_dir/phase28_files_repeated_02.json"')
    text = replace_once(text, "active_trace=\ntrace_reader_pid=\nactive_unit=",
                        "active_trace=\ntrace_reader_pid=\nsampler_pid=\nactive_unit=")
    text = replace_once(text,
        "\tif [[ -n ${trace_reader_pid:-} ]]; then\n",
        "\tif [[ -n ${sampler_pid:-} ]]; then\n"
        "\t\tkill -INT \"$sampler_pid\" 2>/dev/null || true\n"
        "\t\twait \"$sampler_pid\" 2>/dev/null || true\n"
        "\t\tsampler_pid=\n"
        "\tfi\n"
        "\tif [[ -n ${trace_reader_pid:-} ]]; then\n")
    text = replace_once(text,
        '[[ -f $state_file && -f $automation && -f $wps_scenario && -f $files_scenario ]]',
        '[[ -f $state_file && -f $automation && -f $sampler ]]\n'
        '[[ -f $wps_scenario_1 && -f $wps_scenario_2 && -f $wps_scenario_3 ]]\n'
        '[[ -f $files_scenario_1 && -f $files_scenario_2 ]]')
    text = replace_once(text,
        '[[ $(git -C "$tree" rev-parse HEAD) == "$expected_head" ]]\n'
        '[[ -z $(git -C "$tree" status --porcelain=v1) ]]',
        '[[ $(git -C "$tree" branch --show-current) == parp-kernel-page-prediction-phase28 ]]\n'
        'git -C "$tree" merge-base --is-ancestor "$expected_head" HEAD\n'
        '[[ -z $(git -C "$tree" status --porcelain=v1) ]]\n'
        'phase28_head=$(git -C "$tree" rev-parse HEAD)')
    text = replace_once(text,
        '[[ $(python3 -c \'import json,sys; print(json.load(open(sys.argv[1]))["stage"])\' "$state_file") == REAL_COLLECTION ]]\n'
        '[[ $(python3 -c \'import json,sys; print(str(json.load(open(sys.argv[1]))["schema_smoke_pass"]).lower())\' "$state_file") == true ]]',
        '[[ $(python3 -c \'import json,sys; print(str(json.load(open(sys.argv[1]))["stage"] in ("AWAITING_COLLECTION_AUTHORIZATION", "REAL_COLLECTION")).lower())\' "$state_file") == true ]]\n'
        '[[ $(python3 -c \'import json,sys; print(str(json.load(open(sys.argv[1]))["collection_runner_prepared"]).lower())\' "$state_file") == true ]]')
    text = replace_once(text,
        'source_wps="$project/samples/wps/word_200m.docx"\n'
        '[[ -f $source_wps ]]',
        'source_wps_small="$script_dir/fixtures/wps_small.docx"\n'
        'source_wps_medium="$script_dir/fixtures/wps_medium.docx"\n'
        'source_wps_large="$script_dir/fixtures/wps_large.docx"\n'
        '[[ -f $source_wps_small && -f $source_wps_medium && -f $source_wps_large ]]')
    text = replace_once(text,
        'install -o "$run_uid" -g "$run_gid" -m 0600 "$source_wps" "$fixtures/wps_session_01.docx"\n'
        '\tinstall -o "$run_uid" -g "$run_gid" -m 0600 "$source_wps" "$fixtures/wps_session_02.docx"\n'
        '\tinstall -o "$run_uid" -g "$run_gid" -m 0600 "$source_wps" "$fixtures/wps_session_03.docx"',
        'install -o "$run_uid" -g "$run_gid" -m 0600 "$source_wps_small" "$fixtures/wps_session_01.docx"\n'
        '\tinstall -o "$run_uid" -g "$run_gid" -m 0600 "$source_wps_medium" "$fixtures/wps_session_02.docx"\n'
        '\tinstall -o "$run_uid" -g "$run_gid" -m 0600 "$source_wps_large" "$fixtures/wps_session_03.docx"')
    text = replace_once(text,
        'cp "$wps_scenario" "$files_scenario" "$output/config/"',
        'cp "$wps_scenario_1" "$wps_scenario_2" "$wps_scenario_3" \\\n'
        '\t"$files_scenario_1" "$files_scenario_2" "$script_dir/phase28_collection_manifest.json" \\\n'
        '\t"$output/config/"')
    text = text.replace('deadline=$((SECONDS + 900))', 'deadline=$((SECONDS + 10800))')
    text = text.replace("printf '%s %s 900000 1 27\\n'", "printf '%s %s 10800000 1 27\\n'")
    text = replace_once(text,
        '\t[[ -e $cgroup_path/memory.current ]] || {\n'
        '\t\tlog "ERROR: memory controller did not reach session scope: $control_group"\n'
        '\t\treturn 90\n'
        '\t}',
        '\t[[ -e $cgroup_path/memory.current ]] || {\n'
        '\t\tlog "ERROR: memory controller did not reach session scope: $control_group"\n'
        '\t\treturn 90\n'
        '\t}\n'
        '\tpython3 "$sampler" --cgroup "$cgroup_path" \\\n'
        '\t\t--output "$session_dir/runtime/kernel_metrics.jsonl" --interval-ms 1000 &\n'
        '\tsampler_pid=$!')
    text = replace_once(text,
        '\tcollection_end_ns=$(mono_ns)\n\twall_end_ns=$(wall_ns)\n\tstop_damon_if_running',
        '\tcollection_end_ns=$(mono_ns)\n\twall_end_ns=$(wall_ns)\n'
        '\tkill -INT "$sampler_pid" 2>/dev/null || true\n'
        '\twait "$sampler_pid"\n'
        '\tsampler_pid=\n'
        '\t[[ -s $session_dir/runtime/kernel_metrics.jsonl ]]\n'
        '\tstop_damon_if_running')
    text = replace_once(text,
        '"kernel_source_head": "$expected_head",',
        '"kernel_source_head": "$phase28_head",')
    text = replace_once(text,
        '[[ -f $output/raw/wps/wps_01/state/session.json ]] || collect_session WPS 1 1 "$wps_scenario" "$fixtures/wps_session_01.docx"\n'
        '[[ -f $output/raw/wps/wps_02/state/session.json ]] || collect_session WPS 1 2 "$wps_scenario" "$fixtures/wps_session_02.docx"\n'
        '[[ -f $output/raw/wps/wps_03/state/session.json ]] || collect_session WPS 1 3 "$wps_scenario" "$fixtures/wps_session_03.docx"\n'
        '[[ -f $output/raw/files/files_01/state/session.json ]] || collect_session FILES 3 1 "$files_scenario" "$files_root"\n'
        '[[ -f $output/raw/files/files_02/state/session.json ]] || collect_session FILES 3 2 "$files_scenario" "$files_root"',
        'python3 - "$state_file" <<\'PY\'\n'
        'import datetime, json, os, sys\n'
        'path = sys.argv[1]\n'
        'with open(path, encoding="utf-8") as stream: state = json.load(stream)\n'
        'state["stage"] = "REAL_COLLECTION"\n'
        'state["collection_authorized_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")\n'
        'temporary = path + ".tmp"\n'
        'with open(temporary, "w", encoding="utf-8") as stream:\n'
        '    json.dump(state, stream, ensure_ascii=False, indent=2); stream.write("\\n")\n'
        'os.replace(temporary, path)\n'
        'PY\n'
        '[[ -f $output/raw/wps/wps_01/state/session.json ]] || collect_session WPS 1 1 "$wps_scenario_1" "$fixtures/wps_session_01.docx"\n'
        '[[ -f $output/raw/wps/wps_02/state/session.json ]] || collect_session WPS 1 2 "$wps_scenario_2" "$fixtures/wps_session_02.docx"\n'
        '[[ -f $output/raw/wps/wps_03/state/session.json ]] || collect_session WPS 1 3 "$wps_scenario_3" "$fixtures/wps_session_03.docx"\n'
        '[[ -f $output/raw/files/files_01/state/session.json ]] || collect_session FILES 3 1 "$files_scenario_1" "$files_root"\n'
        '[[ -f $output/raw/files/files_02/state/session.json ]] || collect_session FILES 3 2 "$files_scenario_2" "$files_root"')
    text = text.replace('"kernel_write": False,', '"kernel_write": False,\n    "kernel_metrics_interval_ms": 1000,')
    text = replace_once(text,
        '- WPS operates only on per-session copies under raw/fixtures.',
        '- WPS operates only on per-session small/medium/large copies under raw/fixtures.\n'
        '- automation markers are offline labels only and never model features.\n'
        '- cgroup kernel metrics are sampled every 1000 ms without labels.')
    text = text.replace("#!/usr/bin/env bash\n",
                        "#!/usr/bin/env bash\n# GENERATED BY tools/parp/phase28/root_runner_builder.py\n", 1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o755)
    os.replace(temporary, destination)
    return hashlib.sha256(text.encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.source, args.destination))


if __name__ == "__main__":
    main()
