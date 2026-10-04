"""The changelog and release notes composed from the PR descriptions (tools/release_notes.py)."""
import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release_notes", Path(__file__).resolve().parents[1] / "tools" / "release_notes.py")
rn = importlib.util.module_from_spec(_SPEC)
sys.modules["release_notes"] = rn  # dataclasses look the module up while the class is built
_SPEC.loader.exec_module(rn)

PR = rn.PR


def prs():
    return [
        PR(127, "chore: require Home Assistant 2025.10 or newer",
           "Changelog: Other changes - Home Assistant 2025.10 or newer is now required\n"
           "Before you update: Home Assistant 2025.10 or newer is now required\n", "chore/ha-min-2025-10"),
        PR(120, "fix(door): no second door open", "- Changelog: Bug fixes — No second open over the cloud\n"
           "- Changelog: Bug fixes — A 202 is not an open door (#14)\n", "fix/door-no-double-open"),
        PR(116, "docs(github): issue templates point to Discussions Q&A", "No line here.", "docs/issue-templates"),
        PR(117, "test(homekit): own clock", "Changelog: none", "test/flaky-port-reservation"),
        PR(119, "Keep the call visible", "Changelog: Enhancements - The card keeps the call up", "fix/status"),
        PR(118, "chore: contributing", "", "lollox80/docs/contributing-version-bump"),
    ]


def test_entries_go_to_their_sections_in_pr_order():
    notes = rn.collect(prs())
    assert notes.sections["Bug fixes"] == ["No second open over the cloud (#120)", "A 202 is not an open door (#14) (#120)"]
    assert notes.sections["Enhancements"] == ["The card keeps the call up (#119)"]
    assert notes.before_you_update == ["Home Assistant 2025.10 or newer is now required (#127)"]


def test_the_number_goes_before_a_final_full_stop():
    notes = rn.collect([PR(119, "t", "Changelog: Bug fixes - The status stays up.", "fix/x")])
    assert notes.sections["Bug fixes"] == ["The status stays up (#119)."]


def test_a_pr_number_already_in_the_text_is_not_added_again():
    notes = rn.collect([PR(132, "x", "Changelog: Bug fixes - Door results translated (#132, fixes #128)", "fix/x")])
    assert notes.sections["Bug fixes"] == ["Door results translated (#132, fixes #128)"]


def test_none_is_skipped_and_a_missing_line_is_deduced_and_reported():
    notes = rn.collect(prs())
    assert not any("#117" in e for es in notes.sections.values() for e in es), "Changelog: none"
    assert "docs(github): issue templates point to Discussions Q&A (#116)" in notes.sections["Documentation"]
    assert "chore: contributing (#118)" in notes.sections["Other changes"], "unknown prefix: Other changes"
    assert [p.split(":")[0] for p in notes.problems] == ["#116", "#118"]


def test_a_bad_line_next_to_good_ones_is_reported():
    notes = rn.collect([PR(5, "t", "Changelog: Bug fixes - ok\nChangelog: Fixed - bad", "fix/x")])
    assert notes.sections["Bug fixes"] == ["ok (#5)"]
    assert any("#5" in p and "unknown section" in p for p in notes.problems)


def test_changelog_section_order_box_and_links():
    md = rn.changelog_section("1.0.20", "2026-10-05", rn.collect(prs()), repo="ha-vimar/ha-vimar-intercom")
    lines = md.splitlines()
    assert lines[0] == "## [1.0.20] - 2026-10-05"
    assert lines[2] == "> **⚠ Before you update**"
    heads = [line for line in lines if line.startswith("### ")]
    assert heads == ["### Enhancements", "### Bug fixes", "### Documentation", "### Other changes"]
    assert "[#120](https://github.com/ha-vimar/ha-vimar-intercom/pull/120)" in md
    assert "### Security" not in md, "an empty section does not show"


def test_release_notes_have_highlights_and_the_box():
    rel = rn.release_notes(rn.collect(prs()), highlights=2)
    assert rel.startswith("## Highlights")
    assert rel.count("\n- ", 0, rel.index("**⚠ Before you update:**")) == 2
    assert "- Home Assistant 2025.10 or newer is now required (#127)" in rel
    assert "](https://" not in rel, "GitHub links #N on the release page by itself"


def test_insert_keeps_unreleased_on_top():
    log = "# Changelog\n\n## [Unreleased]\n\n## [1.0.19] - 2026-10-02\n"
    out = rn.insert_section(log, "## [1.0.20] - 2026-10-05\n\n### Bug fixes\n\n- x\n")
    assert out.index("## [Unreleased]") < out.index("## [1.0.20]") < out.index("## [1.0.19]")
    with pytest.raises(ValueError):
        rn.insert_section("# Changelog\n", "x")


def test_only_merge_commits_of_prs_count():
    log = ("Merge pull request #124 from Apeiv/fix/card-mic\n"
           "Merge branch 'main' into fix/srtp-wrap-retry\n"
           "Merge pull request #116 from lollox80/docs/issue-templates\n")
    assert rn.merged_numbers(log) == [124, 116]


def test_main_reads_git_and_gh_and_writes_the_files(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_run(*cmd):
        calls.append(cmd)
        if cmd[:2] == ("git", "describe"):
            return "v1.0.19\n"
        if cmd[:2] == ("git", "log"):
            return "Merge pull request #120 from Apeiv/fix/door\n"
        return ('{"number": 120, "title": "fix(door)", "headRefName": "fix/door", '
                '"body": "Changelog: Bug fixes - One open only\\nBefore you update: check your automations"}')

    monkeypatch.setattr(rn, "_run", fake_run)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [1.0.19] - x\n", encoding="utf-8")
    assert rn.main(["--version", "1.0.20", "--date", "2026-10-05", "--write-changelog", "--notes-file", "n.md"]) == 0
    assert ("git", "log", "--first-parent", "--merges", "--format=%s", "v1.0.19..origin/main") in calls
    assert "## [1.0.20] - 2026-10-05" in (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "One open only (#120)" in (tmp_path / "n.md").read_text(encoding="utf-8")
    assert "Range v1.0.19..origin/main: 1 merged PRs (#120)" in capsys.readouterr().out
