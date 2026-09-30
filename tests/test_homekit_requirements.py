"""HomeKit's libraries are installed only when the option is on (review of #32).

In the manifest they were installed for every user, and a failed install (or a
clash with the core HomeKit integration's pin) stopped the whole integration
from loading, not just HomeKit.
"""
import ast
import json
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"
HOMEKIT_LIBS = {"HAP-python", "PyQRCode", "base36"}


def _names(reqs):
    return {r.split(">")[0].split("=")[0].split("<")[0] for r in reqs}


def test_the_manifest_does_not_install_the_homekit_libraries():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert not _names(manifest["requirements"]) & HOMEKIT_LIBS


def test_the_homekit_requirements_are_the_core_homekit_libraries():
    tree = ast.parse((COMPONENT / "const.py").read_text())
    reqs = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "HOMEKIT_REQUIREMENTS")
    assert _names(reqs) == HOMEKIT_LIBS


def test_setup_installs_them_before_importing_the_accessory():
    source = (COMPONENT / "__init__.py").read_text()
    install = source.index("async_process_requirements(hass, f\"{DOMAIN}.homekit\"")
    load = source.index("from .homekit_accessory import async_setup_homekit")
    assert install < load


def test_removing_an_entry_needs_no_pyhap():
    tree = ast.parse((COMPONENT / "homekit_files.py").read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any("pyhap" in m or "homekit_accessory" in m for m in imported)
    assert "from .homekit_files import remove_homekit_files" in (COMPONENT / "__init__.py").read_text()
