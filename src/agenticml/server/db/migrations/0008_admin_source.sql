ALTER TABLE accounts ADD COLUMN server_admin_source TEXT CHECK (server_admin_source IN ('list','manual'));
