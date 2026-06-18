"""Tests for modules.inventory.share_inventory."""

from __future__ import annotations

import json
from pathlib import Path

from modules.inventory import share_inventory as si
from tests.fakes import make_context


def test_parse_share_cfg() -> None:
    parsed = si.parse_share_cfg('a="1"\n# comment\nbad line\nb=2\n')
    assert parsed == {"a": "1", "b": "2"}


def test_run_inventories_shares(tmp_path: Path) -> None:
    shares = tmp_path / "shares"
    shares.mkdir()
    (shares / "media.cfg").write_text(
        'shareUseCache="no"\nshareInclude="disk1,disk2"\nshareComment="Media"\n', encoding="utf-8"
    )
    (shares / "appdata.cfg").write_text('shareUseCache="prefer"\n', encoding="utf-8")

    ctx = make_context(tmp_path, paths={"shares_cfg_dir": str(shares)})
    result = si.run(ctx)

    assert result.actions == 2
    data = json.loads(
        (tmp_path / "reports" / "inventory" / "share_inventory.json").read_text(encoding="utf-8")
    )
    media = next(s for s in data if s["name"] == "media")
    assert media["cache"] == "array-only"
    assert media["include"] == ["disk1", "disk2"]
    appdata = next(s for s in data if s["name"] == "appdata")
    assert appdata["cache"] == "prefer-cache"
