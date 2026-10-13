"""Generate COM wrappers for the target Windows system, without writing the bundle."""
import sys

# Match pywinauto's default MTA when this hook imports COM before pywinauto.
# Restore an absent flag so its normal initialization still owns that setting.
_has_coinit_flags = hasattr(sys, "coinit_flags")
if not _has_coinit_flags:
    sys.coinit_flags = 0
try:
    import comtypes.client

    comtypes.client.gen_dir = None
finally:
    if not _has_coinit_flags:
        del sys.coinit_flags
