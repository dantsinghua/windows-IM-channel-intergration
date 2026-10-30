ALTER TABLE messages ADD COLUMN reply_json TEXT CHECK (reply_json IS NULL OR json_valid(reply_json));
