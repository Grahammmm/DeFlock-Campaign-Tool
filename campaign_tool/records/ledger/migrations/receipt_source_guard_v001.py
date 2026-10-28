"""Additive receipt admission guard; preserve captured source and gap history."""

VERSION = 'receipt-source-success-1'
TRIGGER = 'rp_item_source_success'
SQL = '''
CREATE TRIGGER rp_item_source_success BEFORE INSERT ON rp_items
WHEN NOT EXISTS(SELECT 1 FROM rp_sources
                WHERE id=NEW.source_id AND error IS NULL)
BEGIN
 SELECT RAISE(ABORT,'receipt item requires successful source');
END;
'''
