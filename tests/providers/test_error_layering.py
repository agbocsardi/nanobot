"""Provider error handling must not import session/compaction orchestration."""

import ast
import subprocess
import sys
from pathlib import Path

from nanobot.providers import base


def test_provider_layer_has_no_session_dependency():
    for path in Path(base.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("nanobot.session"), path
                if node.module == "nanobot":
                    assert all(alias.name != "session" for alias in node.names), path
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith("nanobot.session") for alias in node.names), path
    subprocess.run(
        [sys.executable, "-c", "import sys; import nanobot.providers.base; "
         "assert not any(n == 'nanobot.session' or n.startswith('nanobot.session.') for n in sys.modules)"],
        check=True, capture_output=True, text=True, timeout=15,
    )
