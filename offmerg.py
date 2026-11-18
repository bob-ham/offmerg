# offmerg.py
# fetches offsets from multiple websites
# currently from three websites: theos offsets, offsetmanager (github), credosdev
# im great at programming python right guys

import argparse
import json
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from colorama import init as colorama_init, Fore, Style

colorama_init(autoreset=True)

HEADERS = {"User-Agent": "WEAO-3PService"}
WEAO_CURRENT  = "https://weao.xyz/api/versions/current"
THEO_JSON     = "https://offsets.imtheo.lol/offsets.json"
OFFSETMGR_RAW = "https://raw.githubusercontent.com/Offsetmanager/Roblox-Internal-External-Offsets/refs/heads/main/Windows/{version}/{file}"
OFFSETMGR_FILES = [
    "Internal_Offsets.hpp",
    "Internal_Offsets_Thorioum.hpp",
    "External_Offsets.hpp",
    "Struct.hpp",
    "Struct_Thorioum.hpp",
]
CREDOS_RAW = "https://offsets.credosdev.lol/assets/{version}/offsets.hpp"

OUTPUT_JSON         = "offsets.json"
OUTPUT_H            = "offsets.h"
OUTPUT_STRUCTS_JSON = "structs.json"
OUTPUT_STRUCTS_H    = "structs.h"
OUTPUT_CONFLICTS    = "conflicts.json"
OUTPUT_SOURCES      = "sources.txt"

SOURCE_PRIORITY = ["theo", "offsetmgr", "credos"]

TIMEOUT = 10


def ok(msg):
    print(f"{Fore.GREEN}[+]{Style.RESET_ALL} {msg}")

def info(msg):
    print(f"{Fore.CYAN}[*]{Style.RESET_ALL} {msg}")

def warn(msg):
    print(f"{Fore.YELLOW}[!]{Style.RESET_ALL} {msg}")

def err(msg):
    print(f"{Fore.RED}[x]{Style.RESET_ALL} {msg}")

def src_ok(name, msg):
    print(f"  {Fore.GREEN}[{name}]{Style.RESET_ALL} {msg}")

def src_warn(name, msg):
    print(f"  {Fore.YELLOW}[{name}]{Style.RESET_ALL} {msg}")

def src_err(name, msg):
    print(f"  {Fore.RED}[{name}]{Style.RESET_ALL} {msg}")


def fetch_roblox_version():
    r = requests.get(WEAO_CURRENT, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["Windows"]


def fetch_theo():
    r = requests.get(THEO_JSON, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("Offsets", {}), data.get("Roblox Version", "unknown")


def fetch_credosdev(version):
    url = CREDOS_RAW.format(version=version)
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    offsets = parse_hpp(r.text)
    return offsets, version


def parse_hpp(text):
    result = {}

    ns_blocks = re.finditer(
        r'namespace\s+(\w+)\s*\{([^}]*)\}',
        text, re.DOTALL
    )
    for match in ns_blocks:
        ns_name = match.group(1)
        ns_body = match.group(2)
        entries = re.findall(r'(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)', ns_body)
        if not entries:
            continue
        result[ns_name] = {}
        for name, val in entries:
            result[ns_name][name] = int(val, 16) if val.startswith("0x") else int(val)

    if not result:
        enum_body = re.search(r'enum\s+\w+\s*:\s*\w+\s*\{([^}]*)\}', text, re.DOTALL)
        if enum_body:
            entries = re.findall(r'(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)', enum_body.group(1))
            result["Global"] = {}
            for name, val in entries:
                result["Global"][name] = int(val, 16) if val.startswith("0x") else int(val)

    return result


def parse_enums(text):
    result = {}
    enum_blocks = re.finditer(
        r'enum\s+(\w+)\s*(?::\s*\w+)?\s*\{([^}]*)\}',
        text, re.DOTALL
    )
    for match in enum_blocks:
        enum_name = match.group(1)
        body      = match.group(2)
        entries   = re.findall(r'(\w+)\s*=\s*(0x[0-9a-fA-F]+|-?\d+)', body)
        if not entries:
            continue
        result[enum_name] = {}
        for name, val in entries:
            result[enum_name][name] = int(val, 16) if val.startswith("0x") else int(val)
    return result


def parse_rebase_rvas(text):
    result = {}
    ns_blocks = re.finditer(r'namespace\s+(\w+)\s*\{(.*?)\n\}', text, re.DOTALL)
    for match in ns_blocks:
        ns_name = match.group(1)
        body    = match.group(2)
        entries = re.findall(
            r'(?:const\s+ULONGLONG|constexpr\s+ULONGLONG)\s+(\w+)\s*=\s*REBASE_(?:RBX|HYPERION)\s*\(\s*(0x[0-9a-fA-F]+)\s*\)',
            body
        )
        if not entries:
            continue
        result[ns_name] = {}
        for name, val in entries:
            result[ns_name][name] = int(val, 16)
    return result


def parse_constexpr_flat(text):
    result = {}
    ns_blocks = re.finditer(r'namespace\s+(\w+)\s*\{(.*?)\n\}', text, re.DOTALL)
    for match in ns_blocks:
        ns_name = match.group(1)
        body    = match.group(2)
        entries = re.findall(
            r'constexpr\s+\w+\s+(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*;',
            body
        )
        if not entries:
            continue
        result[ns_name] = {}
        for name, val in entries:
            result[ns_name][name] = int(val, 16) if val.startswith("0x") else int(val)
    return result


def fetch_offsetmgr(version):
    def get_file(filename):
        url = OFFSETMGR_RAW.format(version=version, file=filename)
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    offsets_merged = {}
    structs = {
        "rvas":        {},
        "constants":   {},
        "raw_structs": "",
        "defines":     [],
    }

    for filename in OFFSETMGR_FILES:
        try:
            text = get_file(filename)
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                src_warn("offsetmgr", f"{filename} not found for {version}, skipping")
                continue
            raise

        is_thorioum = "Thorioum" in filename
        is_struct   = "Struct" in filename

        if is_thorioum or is_struct:
            for ns, vals in parse_rebase_rvas(text).items():
                if ns not in structs["rvas"]:
                    structs["rvas"][ns] = {}
                structs["rvas"][ns].update(vals)

            for ns, vals in parse_constexpr_flat(text).items():
                if ns not in structs["constants"]:
                    structs["constants"][ns] = {}
                structs["constants"][ns].update(vals)

            if is_struct:
                cleaned = re.sub(r'/\*.*?\*/', '', text, count=1, flags=re.DOTALL).lstrip()
                cleaned = re.sub(r'#pragma once\s*\n', '', cleaned)
                cleaned = re.sub(r'#include\s*<[^>]+>\s*\n', '', cleaned)

                defines = re.findall(r'(#define\s+(?:[^\n]|\\\n)+)', cleaned)
                body    = re.sub(r'#define\s+(?:[^\n]|\\\n)+\n?', '', cleaned)

                for d in defines:
                    if d not in structs["defines"]:
                        structs["defines"].append(d)

                structs["raw_structs"] += body + "\n"
        else:
            parsed = parse_hpp(text)
            for ns, fields in parsed.items():
                if ns not in offsets_merged:
                    offsets_merged[ns] = {}
                offsets_merged[ns].update(fields)

    return offsets_merged, version, structs


def merge_sources(sources):
    all_data     = {}
    zero_map     = {}
    ns_canon     = {}
    winner_tally = {s: 0 for s in SOURCE_PRIORITY}

    for source_name, offsets in sources:
        src_pri = SOURCE_PRIORITY.index(source_name) if source_name in SOURCE_PRIORITY else len(SOURCE_PRIORITY)
        for ns, fields in offsets.items():
            ns_key = ns.lower()
            if ns_key not in all_data:
                all_data[ns_key] = {}
                zero_map[ns_key] = {}
                ns_canon[ns_key] = (ns, src_pri)
            else:
                if src_pri < ns_canon[ns_key][1]:
                    ns_canon[ns_key] = (ns, src_pri)

            for name, val in fields.items():
                if name not in all_data[ns_key]:
                    all_data[ns_key][name] = {}
                    zero_map[ns_key][name] = []
                if val == 0:
                    zero_map[ns_key][name].append(source_name)
                else:
                    all_data[ns_key][name][source_name] = val

    merged    = {}
    conflicts = {}

    for ns_key, fields in all_data.items():
        canonical_ns = ns_canon[ns_key][0]
        merged[canonical_ns] = {}

        for name, source_map in fields.items():
            if not source_map:
                zero_srcs = zero_map[ns_key].get(name, [])
                if zero_srcs:
                    src_warn("warn", f"offset {canonical_ns}::{name} is 0x0 on all sources ({', '.join(zero_srcs)}), skipping")
                continue

            values = list(source_map.values())
            best = max(source_map, key=lambda s: (
                values.count(source_map[s]),
                -SOURCE_PRIORITY.index(s) if s in SOURCE_PRIORITY else 0
            ))
            merged[canonical_ns][name] = source_map[best]

            if best in winner_tally:
                winner_tally[best] += 1

            for bad_src in zero_map[ns_key].get(name, []):
                src_warn("warn", f"offset {canonical_ns}::{name} wasn't found for {bad_src}, using {best} (0x{source_map[best]:X})")

            if len(set(values)) > 1:
                if canonical_ns not in conflicts:
                    conflicts[canonical_ns] = {}
                conflicts[canonical_ns][name] = source_map

    return merged, conflicts, winner_tally


def export_json(merged, versions, conflicts, export_conflicts):
    def to_hex(obj):
        if isinstance(obj, dict):
            return {k: to_hex(v) for k, v in obj.items()}
        if isinstance(obj, int):
            return hex(obj)
        return obj

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "versions":     versions,
        "Offsets":      to_hex(merged),
    }
    if conflicts and not export_conflicts:
        out["conflicts"] = to_hex(conflicts)

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    ok(f"wrote {OUTPUT_JSON}")


def export_conflicts_file(conflicts):
    def to_hex(obj):
        if isinstance(obj, dict):
            return {k: to_hex(v) for k, v in obj.items()}
        if isinstance(obj, int):
            return hex(obj)
        return obj

    total = sum(len(v) for v in conflicts.values())
    out = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "total_conflicts": total,
        "conflicts":       to_hex(conflicts),
    }
    with open(OUTPUT_CONFLICTS, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    ok(f"wrote {OUTPUT_CONFLICTS} ({total} conflicting offsets)")


def export_sources_file(winner_tally, versions, errors):
    total = sum(winner_tally.values())
    ts    = datetime.now(timezone.utc).isoformat()

    lines = [
        "offset sources report",
        f"generated: {ts}",
        "",
    ]

    for src in SOURCE_PRIORITY:
        ver = versions.get(src, "n/a")
        lines.append(f"  {src:<12} version: {ver}")
    lines.append("")

    if errors:
        lines.append(f"  failed sources: {', '.join(errors.keys())}")
        lines.append("")

    lines.append(f"total offsets: {total}")
    lines.append("")

    label_w = max(len(s) for s in SOURCE_PRIORITY)
    for src in SOURCE_PRIORITY:
        count = winner_tally.get(src, 0)
        if src in errors:
            lines.append(f"  {src:<{label_w}} : FAILED")
        else:
            pct = (count / total * 100) if total else 0
            lines.append(f"  {src:<{label_w}} : {count:>5}  ({pct:.1f}%)")

    lines.append("")

    with open(OUTPUT_SOURCES, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    ok(f"wrote {OUTPUT_SOURCES}")


def export_header(merged, versions):
    lines = [
        "#pragma once",
        "// auto-generated by offmerg.py, creator of file is @_exthk",
        f"// generated: {datetime.now(timezone.utc).isoformat()}",
        "// sources: theo (offsets.imtheo.lol), offsetmanager (github), credos (offsets.credosdev.lol)",
        "",
    ]
    for source, ver in versions.items():
        lines.append(f"// {source}: {ver}")
    lines += [
        "",
        "#include <cstdint>",
        "",
        "namespace Offsets {",
        "",
    ]

    for ns in sorted(merged.keys()):
        fields = merged[ns]
        if not fields:
            continue
        lines.append(f"    namespace {ns} {{")
        for name in sorted(fields.keys()):
            lines.append(f"        inline constexpr uintptr_t {name} = 0x{fields[name]:X};")
        lines.append("    }")
        lines.append("")

    lines += [
        "} // namespace Offsets",
        "",
    ]

    with open(OUTPUT_H, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    ok(f"wrote {OUTPUT_H}")


def export_structs_json(structs):
    def to_hex(obj):
        if isinstance(obj, dict):
            return {k: to_hex(v) for k, v in obj.items()}
        if isinstance(obj, int):
            return hex(obj)
        return obj

    with open(OUTPUT_STRUCTS_JSON, "w", encoding="utf-8") as f:
        json.dump(to_hex(structs), f, indent=2)
    ok(f"wrote {OUTPUT_STRUCTS_JSON}")


def export_structs_header(structs):
    lines = [
        "#pragma once",
        "// auto-generated structs, enums, and rvas from offsetmanager",
        "",
        "#include <cstdint>",
        "#include <Windows.h>",
        "",
        "#define REBASE_RBX(x) ((ULONGLONG)GetModuleHandle(0) + x)",
        "#define REBASE_HYPERION(x) ((ULONGLONG)GetModuleHandle(\"RobloxPlayerBeta.dll\") + x)",
        "",
    ]

    if structs.get("rvas"):
        lines.append("// rva function pointers")
        lines.append("")
        for ns_name, fields in sorted(structs["rvas"].items()):
            lines.append(f"namespace {ns_name} {{")
            for fname, fval in sorted(fields.items()):
                lines.append(f"    const ULONGLONG {fname} = REBASE_RBX(0x{fval:X});")
            lines.append("}")
            lines.append("")

    if structs.get("constants"):
        lines.append("// constants")
        lines.append("")
        for ns_name, fields in sorted(structs["constants"].items()):
            lines.append(f"namespace {ns_name} {{")
            for fname, fval in sorted(fields.items()):
                lines.append(f"    constexpr ULONG {fname} = 0x{fval:X};")
            lines.append("}")
            lines.append("")

    if structs.get("defines"):
        lines.append("// vm encoding macros and struct helpers")
        lines.append("")
        for d in structs["defines"]:
            lines.append(d)
        lines.append("")

    if structs.get("raw_structs"):
        lines.append("// raw struct definitions")
        lines.append("")
        lines.append(structs["raw_structs"])

    with open(OUTPUT_STRUCTS_H, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    ok(f"wrote {OUTPUT_STRUCTS_H}")


def parse_args():
    parser = argparse.ArgumentParser(description="offmerg - roblox offset aggregator")
    parser.add_argument(
        "--export-conflicts",
        action="store_true",
        help=f"write conflicting offsets to {OUTPUT_CONFLICTS} (removes them from {OUTPUT_JSON})",
    )
    parser.add_argument(
        "--export-sources",
        action="store_true",
        help=f"write per-source offset winner counts to {OUTPUT_SOURCES}",
    )
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument(
        "--export-json",
        action="store_true",
        help="export only json files (offsets.json, structs.json)",
    )
    fmt.add_argument(
        "--export-c++",
        dest="export_cpp",
        action="store_true",
        help="export only c++ header files (offsets.h, structs.h)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    do_json = not args.export_cpp
    do_cpp  = not args.export_json

    info("fetching current roblox version...")
    try:
        version = fetch_roblox_version()
        src_ok("weao", f"version: {version}")
    except Exception as e:
        src_err("weao", f"FAILED to get version: {e}")
        err("cannot proceed without version string (needed for offsetmgr + credos)")
        return

    info("fetching offsets from all sources in parallel...")

    results  = {}
    versions = {}
    errors   = {}
    structs  = {}

    fetch_map = {
        "theo":      fetch_theo,
        "offsetmgr": lambda: fetch_offsetmgr(version),
        "credos":    lambda: fetch_credosdev(version),
    }

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): name for name, fn in fetch_map.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                if name == "offsetmgr":
                    offsets, ver, s = result
                    structs = s
                else:
                    offsets, ver = result
                results[name]  = offsets
                versions[name] = ver
                offset_count   = sum(len(v) for v in offsets.values())
                src_ok(name, f"ok — {len(offsets)} namespaces, {offset_count} offsets ({ver})")
            except Exception as e:
                errors[name] = str(e)
                src_err(name, f"FAILED: {e}")

    if not results:
        err("all sources failed, nothing to write")
        return

    source_list = [(name, results[name]) for name in results]
    merged, conflicts, winner_tally = merge_sources(source_list)

    total = sum(len(v) for v in merged.values())
    info(f"merged {len(merged)} namespaces, {total} total offsets")

    if conflicts:
        total_conflicts = sum(len(v) for v in conflicts.values())
        if args.export_conflicts:
            warn(f"{total_conflicts} conflicting offsets — writing to {OUTPUT_CONFLICTS}")
        else:
            warn(f"{total_conflicts} conflicting offsets (majority vote used, see conflicts key in JSON)")

    if errors:
        warn(f"failed sources: {', '.join(errors.keys())} — output is partial")

    if do_json:
        export_json(merged, versions, conflicts, args.export_conflicts)
    if do_cpp:
        export_header(merged, versions)

    if args.export_conflicts and conflicts:
        export_conflicts_file(conflicts)

    if args.export_sources:
        export_sources_file(winner_tally, versions, errors)

    if structs:
        rva_count = sum(len(v) for v in structs.get("rvas", {}).values())
        info(f"structs — {rva_count} rvas")
        if do_json:
            export_structs_json(structs)
        if do_cpp:
            export_structs_header(structs)

    print(f"\n{Fore.GREEN}[done]{Style.RESET_ALL}")


if __name__ == "__main__":
    main()