"""Una chiave doppia in strings.json/translations sovrascrive in silenzio la prima."""
import json
from pathlib import Path

import pytest

BASE = Path(__file__).parent.parent / "custom_components" / "vimar_intercom"


def _no_dup(pairs):
    keys = [k for k, _ in pairs]
    dup = {k for k in keys if keys.count(k) > 1}
    assert not dup, f"chiavi doppie: {dup}"
    return dict(pairs)


@pytest.mark.parametrize("path", [BASE / "strings.json", *sorted((BASE / "translations").glob("*.json"))],
                         ids=lambda p: p.name)
def test_nessuna_chiave_doppia(path):
    json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_dup)


def test_services_yaml_valido():
    """Hassfest carica services.yaml: un ': ' non quotato in una descrizione lo rompe."""
    import yaml
    data = yaml.safe_load((BASE / "services.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "decline" in data
