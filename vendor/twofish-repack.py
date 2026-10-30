"""Reproduce QTrade's CPython 3.12 compatibility repack (not an upstream wheel)."""

import argparse
import base64
import csv
import difflib
import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile


ORIGINAL_SHA256 = "a39acd6fa6480232d159c29eb4d03025a10a794d60797250f1156c6121c66a0d"
SOURCE_SHA256 = "b09d8bb50d33b23ff34cafb1f9209f858f752935c6a5c901efb92a41acb830fa"
BUILD = "1qtrade1"
FILENAME = f"twofish-0.3.0-{BUILD}-cp312-cp312-linux_x86_64.whl"
DIST_INFO = "twofish-0.3.0.dist-info/"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original_wheel", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    original = args.original_wheel.read_bytes()
    if sha256(original) != ORIGINAL_SHA256:
        raise SystemExit("Original wheel SHA256 mismatch")
    source = json.loads(Path(__file__).with_name("twofish-source.json").read_text())
    if source["archive_info"]["hashes"]["sha256"] != SOURCE_SHA256:
        raise SystemExit("Upstream source SHA256 mismatch")
    with ZipFile(io.BytesIO(original)) as archive:
        entries = archive.infolist()
        contents = {entry.filename: archive.read(entry) for entry in entries}
    before = contents["twofish.py"]
    after = before
    for old, new in (
        (b"import imp\n", b"import importlib.util\n"),
        (b"imp.find_module('_twofish')[1]", b"importlib.util.find_spec('_twofish').origin"),
    ):
        if after.count(old) != 1:
            raise SystemExit("Unexpected upstream Python source")
        after = after.replace(old, new)
    contents["twofish.py"] = after
    wheel_metadata = contents[DIST_INFO + "WHEEL"]
    if b"Build:" in wheel_metadata:
        raise SystemExit("Original wheel already has a build tag")
    contents[DIST_INFO + "WHEEL"] = wheel_metadata.replace(
        b"Wheel-Version: 1.0\n", b"Wheel-Version: 1.0\nBuild: 1qtrade1\n", 1
    )
    record_name = DIST_INFO + "RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for entry in entries:
        name = entry.filename
        if name != record_name:
            data = contents[name]
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            writer.writerow((name, "sha256=" + digest.decode("ascii"), len(data)))
    writer.writerow((record_name, "", ""))
    contents[record_name] = record.getvalue().encode()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = args.output_dir / FILENAME
    with ZipFile(wheel_path, "w") as archive:
        for entry in entries:
            archive.writestr(entry, contents[entry.filename])
    patch = "".join(difflib.unified_diff(
        before.decode().splitlines(keepends=True),
        after.decode().splitlines(keepends=True),
        fromfile="a/twofish.py", tofile="b/twofish.py",
    )).encode()
    patch_name = "twofish-cpython312.patch"
    (args.output_dir / patch_name).write_bytes(patch)
    source.update({
        "distribution": "QTrade compatibility repack; not an unmodified upstream artifact",
        "version": "0.3.0",
        "build": BUILD,
        "original_wheel_filename": "twofish-0.3.0-cp312-cp312-linux_x86_64.whl",
        "original_wheel_sha256": ORIGINAL_SHA256,
        "wheel_filename": FILENAME,
        "wheel_sha256": sha256(wheel_path.read_bytes()),
        "patch": {"filename": patch_name, "sha256": sha256(patch)},
        "python_source": {"before_sha256": sha256(before), "after_sha256": sha256(after)},
        "preserved_members_sha256": {
            name: sha256(data) for name, data in contents.items()
            if name not in ("twofish.py", DIST_INFO + "WHEEL", record_name)
        },
        "repack_script": {"filename": Path(__file__).name,
                          "sha256": sha256(Path(__file__).read_bytes())},
        "validation": "Isolated CPython 3.12 import and upstream 128/192/256-bit self_test required; target-image validation remains a separate release gate",
    })
    (args.output_dir / "twofish-source.json").write_text(json.dumps(source, indent=2) + "\n")
    print(json.dumps({"filename": FILENAME, "sha256": source["wheel_sha256"]}))


if __name__ == "__main__":
    main()
