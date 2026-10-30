# Collector runtime

`xunjia_collector_runtime-2.2.12-py3-none-any.whl` is the shared protocol, receiver and transport runtime built by xunjia-agent/scripts/build_collector_runtime.py. It contains no web application, customer configuration or account data.

SHA256: `699d16cc6d2f799f4b3400b135f4ca2e9e61b055342351d39f72460e2e3552e6`.

All 18 runtime source files were compared byte-for-byte with the peer source on 2026-10-30. The wheel includes server/_collector_build.py with its source hashes. Install through the offline wheelhouse; never rely on an external checkout in PYTHONPATH. Retain the exact wheel as a release input.

# Twofish: QTrade CPython 3.12 compatibility repack

`twofish-0.3.0-1qtrade1-cp312-cp312-linux_x86_64.whl` is a QTrade compatibility repack, **not an unmodified upstream artifact**. Upstream version remains `0.3.0`; the PEP 427 build tag and WHEEL Build field are `1qtrade1`.

SHA256: `cfec53851d499629d7db06b218db5d3c085d051696427d7cfe47406fd519557f`.

The only runtime change replaces `imp.find_module('_twofish')[1]` with `importlib.util.find_spec('_twofish').origin` and the corresponding import. No global `imp` shim is installed. The native C extension, upstream 128/192/256-bit self-test vectors, METADATA, and BSD 3-Clause license are retained byte-for-byte. WHEEL and RECORD are regenerated as appropriate; the highest required GLIBC symbol version remains 2.4.

`twofish-source.json` preserves the upstream PyPI source URL/hash, original cached wheel hash, patch hash, repack script hash, and unchanged-member hashes. `twofish-cpython312.patch` contains the complete Python source change. Reproduce from the original wheel with:

```sh
python3 vendor/twofish-repack.py /path/to/original/twofish-0.3.0-cp312-cp312-linux_x86_64.whl /path/to/output
```

The script rejects any original wheel whose SHA256 is not `a39acd6fa6480232d159c29eb4d03025a10a794d60797250f1156c6121c66a0d`. Keep the original wheel in the release evidence archive, outside vendor and the active wheelhouse. The upstream source archive SHA256 is `b09d8bb50d33b23ff34cafb1f9209f858f752935c6a5c901efb92a41acb830fa`.

Isolated CPython 3.12.3 import and the unmodified built-in vectors passed on 2026-10-30; see `outputs/windows-im-release-20261030/twofish-remediation.md`. This does not replace independent acceptance or target-rootfs validation. Retain binary-only installation, exact filename checking, and the new wheel hash pin.
