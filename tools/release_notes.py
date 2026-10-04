#!/usr/bin/env python3
"""Compose a release's CHANGELOG section and GitHub release notes from the merged PRs.

PRs don't edit CHANGELOG.md: each PR description has `Changelog:` and `Before you update:` lines
(tools/pr_changelog.py, CONTRIBUTING.md). This reads them for the PRs merged into the release range and
writes them in the changelog's order: Before you update, Enhancements, Bug fixes, Documentation,
Security, Other changes. A PR without a valid line gets a section from its branch prefix and its title,
and is reported so the text can be fixed before the release.

    python tools/release_notes.py --version 1.0.20                      # print both, change nothing
    python tools/release_notes.py --version 1.0.20 --write-changelog --notes-file notes.md
    python tools/release_notes.py --version 1.0.21 --prs 140            # a hotfix: the fix PR(s) by number

Needs git and an authenticated `gh` (it reads the PR descriptions).
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_spec = importlib.util.spec_from_file_location("pr_changelog", Path(__file__).with_name("pr_changelog.py"))
pr_changelog = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("pr_changelog", pr_changelog)
_spec.loader.exec_module(pr_changelog)

SECTIONS = pr_changelog.SECTIONS
# The section a PR gets from its branch prefix when its description has no valid line.
PREFIX_SECTION = {"feat": "Enhancements", "fix": "Bug fixes", "hotfix": "Bug fixes", "docs": "Documentation",
                  "security": "Security"}
_MERGE = re.compile(r"^Merge pull request #(\d+) from ")


@dataclass
class PR:
    number: int
    title: str
    body: str
    branch: str


@dataclass
class Notes:
    sections: dict[str, list[str]]
    before_you_update: list[str]
    problems: list[str]


def _ref(text: str, number: int) -> str:
    """The text with its PR number, before a final full stop: "Fixed the ring (#60)."."""
    if re.search(rf"#{number}\b", text):
        return text
    stop = "." if text.endswith(".") else ""
    return f"{text.removesuffix('.')} (#{number}){stop}"


def collect(prs: list[PR]) -> Notes:
    """The entries of every PR, in PR number order within each section."""
    notes = Notes({s: [] for s in SECTIONS}, [], [])
    for pr in sorted(prs, key=lambda p: p.number):
        res = pr_changelog.parse(pr.body)
        notes.before_you_update += [_ref(b, pr.number) for b in res.before_you_update]
        if res.entries:
            for section, text in res.entries:
                notes.sections[section].append(_ref(text, pr.number))
        elif not (res.none and not res.problems):
            section = PREFIX_SECTION.get(pr.branch.split("/")[0], "Other changes")
            notes.sections[section].append(_ref(pr.title, pr.number))
            notes.problems.append(f"#{pr.number}: no valid 'Changelog:' line, put in {section} from its title"
                                  f" (branch {pr.branch}): {'; '.join(res.problems)}")
        for problem in res.problems if res.entries else ():
            notes.problems.append(f"#{pr.number}: {problem}")
    return notes


def _link(line: str, repo: str | None) -> str:
    """#N -> a link, for CHANGELOG.md (the release page links #N by itself)."""
    if not repo:
        return line
    return re.sub(r"(?<![\w/\[])#(\d+)\b", rf"[#\1](https://github.com/{repo}/pull/\1)", line)


def changelog_section(version: str, date: str, notes: Notes, repo: str | None = None) -> str:
    out = [f"## [{version}] - {date}", ""]
    if notes.before_you_update:
        out += ["> **⚠ Before you update**", ">"] + [f"> - {_link(b, repo)}" for b in notes.before_you_update] + [""]
    for section in SECTIONS:
        if notes.sections[section]:
            out += [f"### {section}", ""] + [f"- {_link(e, repo)}" for e in notes.sections[section]] + [""]
    return "\n".join(out)


def release_notes(notes: Notes, highlights: int = 5) -> str:
    """The GitHub release text: Highlights to edit by hand, then the same sections."""
    picks = (notes.sections["Enhancements"] + notes.sections["Bug fixes"])[:highlights]
    out = ["## Highlights", "", "<!-- Keep 2-5 of these and rewrite them for users; delete this comment. -->"]
    out += [f"- {p}" for p in picks] + [""]
    if notes.before_you_update:
        out += ["**⚠ Before you update:**", ""] + [f"- {b}" for b in notes.before_you_update] + [""]
    for section in SECTIONS:
        if notes.sections[section]:
            out += [f"### {section}", ""] + [f"- {e}" for e in notes.sections[section]] + [""]
    return "\n".join(out)


def insert_section(changelog: str, section: str) -> str:
    """Put the release right under `## [Unreleased]`, which stays (empty) on top."""
    marker = "## [Unreleased]\n"
    if marker not in changelog:
        raise ValueError("CHANGELOG.md has no '## [Unreleased]' heading")
    return changelog.replace(marker, f"{marker}\n{section.rstrip()}\n", 1)


def merged_numbers(log: str) -> list[int]:
    """PR numbers from `git log --first-parent --merges --format=%s` (merge commits on the branch only)."""
    return [int(m[1]) for line in log.splitlines() if (m := _MERGE.match(line))]


def _run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def fetch_prs(numbers: list[int], repo: str) -> list[PR]:
    prs = []
    for n in numbers:
        d = json.loads(_run("gh", "pr", "view", str(n), "-R", repo, "--json", "number,title,body,headRefName"))
        prs.append(PR(d["number"], d["title"], d.get("body") or "", d["headRefName"]))
    return prs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True, help="e.g. 1.0.20")
    ap.add_argument("--range", help="git range (default: <last tag>..origin/main)")
    ap.add_argument("--prs", help="comma-separated PR numbers instead of a range (a hotfix cherry-picks its fix)")
    ap.add_argument("--repo", default="ha-vimar/ha-vimar-intercom")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--write-changelog", action="store_true", help="insert the section into CHANGELOG.md")
    ap.add_argument("--notes-file", help="write the GitHub release notes there")
    args = ap.parse_args(argv)

    if args.prs:
        rng, numbers = "PRs given", [int(n) for n in args.prs.split(",") if n.strip()]
    else:
        rng = args.range or f"{_run('git', 'describe', '--tags', '--abbrev=0', 'origin/main').strip()}..origin/main"
        numbers = merged_numbers(_run("git", "log", "--first-parent", "--merges", "--format=%s", rng))
    notes = collect(fetch_prs(numbers, args.repo))
    section = changelog_section(args.version, args.date, notes, args.repo)
    rel = release_notes(notes)

    print(f"Range {rng}: {len(numbers)} merged PRs ({', '.join(f'#{n}' for n in sorted(numbers))})\n")
    print(section)
    print("\n----- release notes -----\n")
    print(rel)
    for p in notes.problems:
        print(f"CHECK: {p}", file=sys.stderr)
    if args.write_changelog:
        path = Path("CHANGELOG.md")
        path.write_text(insert_section(path.read_text(encoding="utf-8"), section), encoding="utf-8")
    if args.notes_file:
        Path(args.notes_file).write_text(rel, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
