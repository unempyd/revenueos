"""Entry point for the RevenueOS MCP Bundle (.mcpb).

The bundle declares ``server.type = "uv"`` (MCPB manifest 0.4), so the host application
(Claude Desktop) downloads uv, resolves the bundle's ``pyproject.toml`` -- which pins
``revenueos[mcp]`` from PyPI -- and launches this file. No RevenueOS code is vendored here:
this shim only sanitises the environment the host hands us and then hands control to the
real server in ``revenueos.mcp_server``.

Why a shim at all:

* MCPB substitutes ``${user_config.KEY}`` into ``mcp_config.env``. A value the user left
  blank arrives as an empty string, and a key with no default may arrive as the literal
  ``${user_config.KEY}``. ``revenueos.paths.find_root`` treats *any* non-empty
  ``REVENUEOS_ROOT`` as authoritative, so an unsanitised placeholder would point the
  workspace at a directory literally named ``${user_config.workspace_directory}``.
* MCPB renders a boolean user_config as the string ``"true"``/``"false"``; RevenueOS reads
  ``REVENUEOS_DRY_RUN == "1"``. The translation happens here.
* If the user has not created a workspace yet we say so on stderr and let ``find_root``
  materialise ``~/.revenueos`` from the template shipped inside the wheel. The server starts
  either way -- it just has nothing to report until onboarding has run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: env vars whose value comes from ${user_config.*} and must survive being left blank
_SUBSTITUTED = ("REVENUEOS_ROOT", "ANTHROPIC_API_KEY", "REVENUEOS_MCPB_DRY_RUN")


def _is_placeholder(value: str) -> bool:
    """True when MCPB left a ``${user_config.KEY}`` placeholder instead of a real value."""
    return "${" in value


def _sanitise() -> None:
    env = os.environ
    for key in _SUBSTITUTED:
        value = env.get(key)
        if value is None:
            continue
        if not value.strip() or _is_placeholder(value):
            del env[key]

    # MCPB booleans arrive as "true"/"false"; RevenueOS wants REVENUEOS_DRY_RUN == "1".
    dry = env.pop("REVENUEOS_MCPB_DRY_RUN", "").strip().lower()
    if dry in ("true", "1", "yes", "on"):
        env["REVENUEOS_DRY_RUN"] = "1"
    elif dry in ("false", "0", "no", "off"):
        env.pop("REVENUEOS_DRY_RUN", None)

    root = env.get("REVENUEOS_ROOT")
    if root:
        resolved = Path(root).expanduser()
        env["REVENUEOS_ROOT"] = str(resolved)
        if not (resolved / "company-context").is_dir():
            print(
                f"[revenueos-mcp] {resolved} is not an onboarded RevenueOS workspace "
                "(no company-context/). The server will start, but the tools have nothing "
                "to report until you run `revenueos --root <dir> init` there.",
                file=sys.stderr,
            )
    else:
        print(
            "[revenueos-mcp] No workspace directory configured. RevenueOS will create and use "
            "~/.revenueos from its bundled template; run `revenueos init` there, or point "
            "Workspace Directory in this extension's settings at an onboarded workspace.",
            file=sys.stderr,
        )


def main() -> None:
    _sanitise()
    from revenueos.mcp_server import main as serve

    serve()


if __name__ == "__main__":
    main()
