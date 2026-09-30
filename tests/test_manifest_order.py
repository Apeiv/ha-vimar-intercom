"""hassfest's rule: manifest keys are domain, name, then alphabetical.

CI runs hassfest; this catches the same mistake in the local suite (a key
added in the wrong place failed CI on #31).
"""
import json
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom" / "manifest.json"


def test_manifest_keys_are_sorted_like_hassfest_wants():
    keys = list(json.loads(MANIFEST.read_text()))
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
