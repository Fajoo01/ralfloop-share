BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS oidc_identities(
 subject_hash TEXT PRIMARY KEY,
 student TEXT NOT NULL UNIQUE REFERENCES students(id) ON DELETE CASCADE,
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oidc_pending_links(
 token_hash TEXT PRIMARY KEY,
 subject_hash TEXT NOT NULL,
 expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS oidc_pending_expires ON oidc_pending_links(expires);
INSERT OR IGNORE INTO schema_version VALUES(3);
COMMIT;
