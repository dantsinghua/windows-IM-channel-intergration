"""公网文件网关第一期。纯逻辑，不连 OSS、不发邮件、不操作三通道。"""

from .bind import Dedup, command_allowed, deposit_allows, xunjia_allows
from .bridge import POLL_INTERVAL_SEC, PlannedUpload, plan_uploads
from .crypto import open_sealed, seal
from .envelope import pack_piece_envelope
from .imap_out import render_selected_group, should_send_imap
from .keys import (
    ack_object_name,
    catalog_object_name,
    in_object_name,
    out_object_name,
    validate_object_name,
)
from .schema import validate_catalog, validate_command, validate_message, validate_receipt

__all__ = [
    "Dedup",
    "POLL_INTERVAL_SEC",
    "PlannedUpload",
    "ack_object_name",
    "catalog_object_name",
    "command_allowed",
    "deposit_allows",
    "in_object_name",
    "open_sealed",
    "out_object_name",
    "pack_piece_envelope",
    "plan_uploads",
    "render_selected_group",
    "seal",
    "should_send_imap",
    "validate_catalog",
    "validate_command",
    "validate_message",
    "validate_object_name",
    "validate_receipt",
    "xunjia_allows",
]
