#!/usr/bin/env python3
"""CLI for the legacy-catalogue -> DSR migration utility
(catalogues/legacy_dsr_migration.py). See that module's docstring for the
three-stage design (crosswalk / apply-references|apply-register / validate,
promote).

Usage:
    python3 migrate_legacy_to_dsr.py crosswalk [--dry-run|--apply]
        Reads instance/catalogue.db READ-ONLY, classifies each legacy
        record's file through the real DSR classifier, mints DSR entries in
        instance/catalogue_dsr.db, writes the crosswalk CSV. --dry-run runs
        against disposable copies of both databases (nothing real is
        touched, including the crosswalk CSV, which is written to a temp
        path printed at the end). --apply writes for real.

    python3 migrate_legacy_to_dsr.py apply-references --target PATH --out PATH
            [--crosswalk PATH]
        Rewrites [INTERNAL EVIDENCE - <legacy-id>] marker tokens in --target
        to the new DSR stable_id, writing the result to --out (a copy;
        --target is never modified). Always runs - there's no dry-run here
        because it never writes to --target, only to --out.

    python3 migrate_legacy_to_dsr.py apply-register --target PATH --out PATH
            [--crosswalk PATH] [--rename-log PATH]
        Same, for a citation_evidence_register.md-style file: rewrites the
        trailing "(LEGACY-ID)" parenthetical on lines starting with
        instance/catalogued_files/. --rename-log (a rename-files output,
        "renamed" rows only) also updates the embedded filename itself so
        the line matches what rename-files actually did on disk.

    python3 migrate_legacy_to_dsr.py rename-files [--dry-run|--apply]
        Renames each active, non-excluded, non-rollup catalogued file in
        place: finds the legacy id token already embedded in its current
        filename and replaces just that token with its DSR stable_id -
        nothing else in the filename changes. Updates
        file_name/relative_path/source_path in catalogue_dsr.db to match.
        Writes a rename_log.csv (every row, whatever its status) next to
        the crosswalk CSV - feed that to apply-register's --rename-log.
        Dry-run by default.

    python3 migrate_legacy_to_dsr.py validate --original PATH --updated PATH
        Strips catalogue-reference spans from both files and asserts the
        remaining text is byte-identical. Prints a diff and exits non-zero
        if not.

    python3 migrate_legacy_to_dsr.py promote --live PATH --new PATH
            [--archive-dir PATH] [--label TEXT] [--dry-run|--apply]
        Backs --live up to a timestamped file under --archive-dir (default:
        <live's parent>/archive/), then overwrites --live with --new's
        content. --label (default "pre-dsr-migration") names what's being
        promoted in the backup filename - override it for a non-DSR
        promotion (e.g. --label "pre-gap-audit-fixes") so the backup
        doesn't misleadingly claim to be a DSR-migration snapshot.
        Dry-run by default; requires --apply to write anything.

    python3 migrate_legacy_to_dsr.py report [--crosswalk PATH]
        Prints crosswalk coverage stats from an already-built crosswalk CSV.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from catalogues import dsr_catalogue, legacy_dsr_migration as migration

ROOT_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = ROOT_DIR / "instance"


def load_json(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"ERROR: required file missing: {path}. Run setup.py first.")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _flag_value(args: list[str], flag: str) -> str | None:
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            return args[idx + 1]
    return None


def cmd_crosswalk(args: list[str]) -> int:
    dry_run = "--dry-run" in args
    apply = "--apply" in args
    if dry_run == apply:
        raise SystemExit("crosswalk requires exactly one of --dry-run or --apply")

    project_config = load_json(INSTANCE_DIR / "project_config.json")

    if apply:
        legacy_conn = migration.open_legacy_db_readonly()
        dsr_conn = dsr_catalogue.get_dsr_db()
        rows, stats = migration.build_crosswalk(legacy_conn, dsr_conn, project_config)
        dsr_conn.commit()
        dsr_conn.close()
        legacy_conn.close()
        migration.write_crosswalk_csv(rows, migration.CROSSWALK_CSV_PATH)
        _print_crosswalk_report(stats, migration.CROSSWALK_CSV_PATH)
        return 0

    tmp_dir = Path(tempfile.mkdtemp(prefix="legacy_dsr_crosswalk_dry_run_"))
    tmp_dsr_db = tmp_dir / "catalogue_dsr.db"
    if dsr_catalogue.DSR_DB_PATH.exists():
        shutil.copy2(dsr_catalogue.DSR_DB_PATH, tmp_dsr_db)
    legacy_conn = migration.open_legacy_db_readonly()  # read-only regardless of dry-run/apply
    dsr_conn = dsr_catalogue.get_dsr_db(tmp_dsr_db)
    rows, stats = migration.build_crosswalk(legacy_conn, dsr_conn, project_config)
    dsr_conn.commit()
    dsr_conn.close()
    legacy_conn.close()
    tmp_csv = tmp_dir / "legacy_dsr_crosswalk.csv"
    migration.write_crosswalk_csv(rows, tmp_csv)
    _print_crosswalk_report(stats, tmp_csv, dry_run=True)
    print(f"Nothing written to {migration.display_path(dsr_catalogue.DSR_DB_PATH)} or "
          f"{migration.display_path(migration.CROSSWALK_CSV_PATH)}. Working copies left at {tmp_dir} for inspection.")
    return 0


def _print_crosswalk_report(stats: dict, csv_path: Path, dry_run: bool = False) -> None:
    verb = "WOULD BE" if dry_run else "IS"
    print(f"Legacy records seen: {stats['legacy_total']} (of which {stats['rollups']} repo rollups)")
    print(f"Migrated to DSR: {stats['migrated']} ({stats['requires_review']} flagged Requires Review)")
    print(f"Not migrated (no usable file on disk): {stats['no_file']}")
    coverage_denominator = stats["legacy_total"] - stats["no_file"]
    coverage = (stats["migrated"] / coverage_denominator * 100) if coverage_denominator else 0.0
    print(f"Coverage of migratable records: {stats['migrated']}/{coverage_denominator} ({coverage:.1f}%)")
    print(f"Crosswalk {verb} at {csv_path}")


def cmd_apply_references(args: list[str], register: bool) -> int:
    target = _flag_value(args, "--target")
    out = _flag_value(args, "--out")
    crosswalk_path = _flag_value(args, "--crosswalk") or str(migration.CROSSWALK_CSV_PATH)
    if not target or not out:
        raise SystemExit("apply-references/apply-register requires --target PATH --out PATH")

    crosswalk = migration.load_crosswalk_map(Path(crosswalk_path))
    if register:
        rename_log = _flag_value(args, "--rename-log")
        filename_renames = migration.load_rename_map(Path(rename_log)) if rename_log else None
        report = migration.apply_register(Path(target), Path(out), crosswalk, filename_renames=filename_renames)
    else:
        report = migration.apply_references(Path(target), Path(out), crosswalk)
    print(f"Rewrote {sum(report['replaced'].values())} reference token(s) "
          f"({len(report['replaced'])} distinct legacy ID(s)) in {migration.display_path(Path(out))}")
    if report.get("filenames_replaced"):
        print(f"Also updated {sum(report['filenames_replaced'].values())} embedded filename reference(s) "
              f"({len(report['filenames_replaced'])} distinct file(s))")
    if report["unmapped"]:
        print(f"WARNING: {sum(report['unmapped'].values())} reference token(s) had no crosswalk entry, left as-is:")
        for token, count in sorted(report["unmapped"].items()):
            print(f"  {token} (x{count})")
    if report.get("non_standard_lines"):
        print(f"WARNING: {report['non_standard_lines']} Source file: line(s) don't end in a clean id "
              f"(e.g. an editorial annotation after the id) - left completely untouched, needs manual attention")
    return 0


def cmd_rename_files(args: list[str]) -> int:
    dry_run = "--dry-run" in args
    apply = "--apply" in args
    if dry_run == apply:
        raise SystemExit("rename-files requires exactly one of --dry-run or --apply")

    conn = dsr_catalogue.get_dsr_db()
    plan = migration.build_rename_plan(conn)
    plan = migration.apply_rename_plan(conn, plan, execute=apply)
    conn.close()

    by_status: dict = {}
    for item in plan:
        by_status.setdefault(item["status"], 0)
        by_status[item["status"]] += 1
    verb = "renamed" if apply else "WOULD be renamed"
    print(f"{by_status.get('renamed' if apply else 'pending', 0)} file(s) {verb}")
    for status, count in sorted(by_status.items()):
        if status not in ("renamed", "pending"):
            print(f"  {count} {status}")

    log_path = migration.CROSSWALK_CSV_PATH.parent / "rename_log.csv"
    migration.write_rename_log_csv(plan, log_path)
    print(f"Full plan written to {migration.display_path(log_path)}")
    if not apply:
        print("Nothing renamed on disk or in the database - pass --apply to execute this plan.")
    return 0


def cmd_validate(args: list[str]) -> int:
    original = _flag_value(args, "--original")
    updated = _flag_value(args, "--updated")
    if not original or not updated:
        raise SystemExit("validate requires --original PATH --updated PATH")
    ok, detail = migration.validate_content_preserved(Path(original), Path(updated))
    if ok:
        print(f"OK: {detail}")
        return 0
    print("FAILED: content changed outside catalogue-reference tokens:")
    print(detail)
    return 1


def cmd_promote(args: list[str]) -> int:
    live = _flag_value(args, "--live")
    new = _flag_value(args, "--new")
    if not live or not new:
        raise SystemExit("promote requires --live PATH --new PATH")
    archive_dir = _flag_value(args, "--archive-dir")
    label = _flag_value(args, "--label") or "pre-dsr-migration"
    live_path = Path(live)
    archive_path = Path(archive_dir) if archive_dir else live_path.parent / "archive"

    dry_run = "--dry-run" in args
    apply = "--apply" in args
    if dry_run == apply:
        raise SystemExit("promote requires exactly one of --dry-run or --apply")

    if not apply:
        print(f"--dry-run: WOULD back up {migration.display_path(live_path)} to "
              f"{migration.display_path(archive_path)}/<name>_{label}_<timestamp>{live_path.suffix}, "
              f"then WOULD overwrite it with {migration.display_path(Path(new))}. Nothing written.")
        return 0

    backup_path = migration.promote(live_path, Path(new), archive_path, label=label)
    print(f"Backed up {migration.display_path(live_path)} -> {migration.display_path(backup_path)}")
    print(f"{migration.display_path(live_path)} is now the content from {migration.display_path(Path(new))}")
    return 0


def cmd_report(args: list[str]) -> int:
    crosswalk_path = Path(_flag_value(args, "--crosswalk") or migration.CROSSWALK_CSV_PATH)
    if not crosswalk_path.exists():
        raise SystemExit(f"No crosswalk found at {crosswalk_path}. Run 'crosswalk --apply' first.")
    mapping = migration.load_crosswalk_map(crosswalk_path)
    print(f"Crosswalk at {crosswalk_path}: {len(mapping)} legacy ID(s) mapped to a DSR stable ID")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "crosswalk":
        return cmd_crosswalk(args)
    if command == "apply-references":
        return cmd_apply_references(args, register=False)
    if command == "apply-register":
        return cmd_apply_references(args, register=True)
    if command == "rename-files":
        return cmd_rename_files(args)
    if command == "validate":
        return cmd_validate(args)
    if command == "promote":
        return cmd_promote(args)
    if command == "report":
        return cmd_report(args)

    print(f"Unknown command: {command}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
