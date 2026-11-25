"""tools/check_real.py, run on the synthetic fixtures only."""

import json
import re
import shutil
from pathlib import Path

from tools.check_real import check_folder, main

FIXTURES = Path(__file__).parent / "fixtures" / "vakifbank"


def test_prints_only_counts_and_status():
    lines = check_folder(FIXTURES)
    assert len(lines) == 15
    pattern = re.compile(
        r"^\d{3}  parser=tr_vakifbank_worldcard  transactions=\d+  plans=\d+  reconciliation=ok  difference=0$"
    )
    assert all(pattern.match(line) for line in lines), lines
    merchants = {tx["description"] for p in FIXTURES.glob("*.json")
                 for tx in json.loads(p.read_text(encoding="utf-8"))["transactions"]}
    blob = "\n".join(lines)
    assert not any(m in blob for m in merchants)
    assert "vb-" not in blob  # file names hidden by default


def test_names_flag_and_errors(tmp_path):
    shutil.copy(sorted(FIXTURES.glob("vb-01-*.pdf"))[0], tmp_path / "a.pdf")
    (tmp_path / "b.pdf").write_bytes(b"not a pdf")
    lines = check_folder(tmp_path, show_names=True)
    assert lines[0].startswith("001 a.pdf  parser=")
    assert re.match(r"^002 b\.pdf  error=\w+$", lines[1])


def test_main_usage(capsys):
    assert main([]) == 2
    assert main(["does-not-exist-folder"]) == 2
