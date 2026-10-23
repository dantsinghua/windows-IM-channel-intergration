-- Preserve native history while isolating an account slot after identity changes.
ALTER TABLE messages ADD COLUMN identity_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN identity_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE commands ADD COLUMN identity_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE idempotency ADD COLUMN identity_epoch INTEGER NOT NULL DEFAULT 0;
DROP INDEX ux_messages_ext;
CREATE UNIQUE INDEX ux_messages_ext ON messages(account_id, identity_epoch, ext_msg_id) WHERE ext_msg_id IS NOT NULL;
