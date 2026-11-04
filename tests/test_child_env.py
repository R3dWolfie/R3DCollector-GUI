"""A frozen Linux build must never hand its bundled LD_LIBRARY_PATH to the
programs it launches: system flatpak/ostree/curl then load our older
libssl/libcrypto and die ("OPENSSL_3.4.0 not found")."""
import ast
from pathlib import Path

import osu_collector_gui as g

SRC = Path(g.__file__).read_text()


def test_child_env_restores_original_library_path(monkeypatch):
    monkeypatch.setattr(g.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/app/_internal")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    assert "LD_LIBRARY_PATH" not in g._child_env()
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    assert g._child_env()["LD_LIBRARY_PATH"] == "/usr/local/lib"


def test_every_external_launch_passes_an_env():
    """Each subprocess.run/Popen either passes env= or forwards **kwargs that
    were built with one (checked by the call sites' own code)."""
    missing = []
    for node in ast.walk(ast.parse(SRC)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in ("run", "Popen")):
            continue
        kws = {k.arg for k in node.keywords}
        if "env" in kws or None in kws:      # None = **kwargs
            continue
        # pip runs our own interpreter; it needs our environment.
        if "sys.executable" in ast.unparse(node):
            continue
        missing.append(node.lineno)
    assert missing == [], f"subprocess calls without env= at lines {missing}"
