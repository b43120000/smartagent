"""Prepare a harmless offline wheel and scripts for manually scoped PR-B/C/D Live tests.

No network, pip, runtime changes or automatic installation. Defaults to
E:\SmartAgent_PR_BCD_LIVE only on Windows; explicitly supply --root elsewhere.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import zipfile
from pathlib import Path

DIST = "smartagent-pr-bcd-live"
MODULE = "smartagent_pr_bcd_live"
VERSION = "1.0"
WHEEL = "smartagent_pr_bcd_live-1.0-py3-none-any.whl"


def prepare(root: str | Path) -> dict:
    base = Path(root).expanduser().resolve()
    if base == base.anchor or str(base) in {"/","."}:
        raise ValueError("fixture_root_must_be_dedicated_directory")
    if base.exists() and base.is_symlink():
        raise ValueError("fixture_root_symlink_not_allowed")
    if not re.search(r"SmartAgent_PR_BCD_LIVE$",base.name,re.I):
        raise ValueError("fixture_must_use_SmartAgent_PR_BCD_LIVE_directory")
    wheelhouse = base / "wheelhouse"
    deps = base / "deps"
    wheelhouse.mkdir(parents=True,exist_ok=True)
    deps.mkdir(parents=True,exist_ok=True)
    wheel = wheelhouse / WHEEL
    meta = "smartagent_pr_bcd_live-1.0.dist-info"
    files = {
        f"{MODULE}.py": b"VALUE = 'PR_BCD_DEPENDENCY_PRESENT'\n",
        f"{meta}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {DIST}\nVersion: {VERSION}\n"
        ).encode(),
        f"{meta}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: SmartAgent PR-BCD Live Fixture\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode(),
    }
    record = io.StringIO(newline="")
    writer=csv.writer(record,lineterminator="\n")
    for name in files: writer.writerow((name,"",""))
    writer.writerow((f"{meta}/RECORD","",""))
    files[f"{meta}/RECORD"]=record.getvalue().encode()
    # Idempotent preparation only before install; never overwrite user results.
    if not wheel.exists():
        with zipfile.ZipFile(wheel,"w",zipfile.ZIP_DEFLATED) as z:
            for name,data in files.items(): z.writestr(name,data)
    inspect_script=(
        "import importlib,pathlib,sys\n"
        "sys.path.insert(0,str(pathlib.Path(__file__).parent/'deps'))\n"
        "try:\n"
        f"    dep=importlib.import_module('{MODULE}')\n"
        "except ModuleNotFoundError:\n"
        "    print('PR_BCD_DEPENDENCY_MISSING')\n"
        "else:\n"
        "    print(dep.VALUE)\n"
    )
    verify_script=(
        "import importlib,pathlib,sys\n"
        "sys.path.insert(0,str(pathlib.Path(__file__).parent/'deps'))\n"
        f"dep=importlib.import_module('{MODULE}')\n"
        "assert dep.VALUE=='PR_BCD_DEPENDENCY_PRESENT'\n"
        "print('PR_BCD_VERIFY_PASS')\n"
    )
    for name,body in (("inspect_dependency.py",inspect_script),
                      ("verify_dependency.py",verify_script)):
        path=base/name
        if not path.exists(): path.write_text(body,encoding="utf-8")
    output={
        "schema":"SMARTAGENT_PR_BCD_LIVE_FIXTURE_V1",
        "root":str(base),"wheel":str(wheel),"distribution":DIST,
        "initial_probe":"python -I -B inspect_dependency.py",
        "remediate":f"python -m pip install --no-index --no-deps --disable-pip-version-check --target deps wheelhouse/{WHEEL}",
        "verify":"python -I -B verify_dependency.py",
        "resume":"python -I -B inspect_dependency.py",
    }
    return output


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",default=r"E:\SmartAgent_PR_BCD_LIVE")
    args=parser.parse_args()
    print(json.dumps(prepare(args.root),ensure_ascii=False,indent=2))
