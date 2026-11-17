ALTER TABLE accounts ADD COLUMN firebase_uid TEXT;
ALTER TABLE accounts ADD COLUMN email TEXT;
ALTER TABLE accounts ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0 CHECK (email_verified IN (0,1));
CREATE UNIQUE INDEX accounts_firebase_uid ON accounts(firebase_uid);
