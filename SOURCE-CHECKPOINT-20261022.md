# Source checkpoint - 2026-10-22

This is a source-control checkpoint requested by the owner, not a production release or a claim of complete acceptance.

## Scope

Preserve the accumulated source, tests, configuration templates, deployment scripts and documentation in this repository. Includes the institution-scoped its-relay path contract, account discovery, message/command/receipt routing, recent history/media and local mock-gateway integration work. No live credentials, runtime databases, native account state, private captures, installer binaries or scratch backups are included.

## Evidence boundary

The last completed mock checkpoint reported 37 joint HTTP/media/browser checks, 128 affected collector checks and 16 wheel-media checks passing. Sets overlap and are not a full-system acceptance count. The reproducible joint harness is in the xunjia repository at `qa/its_relay_joint/run.py`.

The earlier full regression reported 12 collector contract-test failures and 2 mail/Dify compatibility failures; these are not closed by the mock subset. Real gateway upload was last observed returning HTTP 502. Actual native image/file examples, live model/native receipt end-to-end acceptance, old UNKNOWN lineage migration, same-version runtime activation and Windows installer/UAC/cold-start validation remain separate release gates.

Committing or pushing this checkpoint does not install candidates, switch runtime configuration, clear UNKNOWN records, or authorize claiming those gates passed.
