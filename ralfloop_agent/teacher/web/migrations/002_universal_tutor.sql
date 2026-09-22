BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS learner_profiles(
 student TEXT PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
 profile_json TEXT NOT NULL,
 updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kc_mastery(
 student TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
 component TEXT NOT NULL,
 probability REAL NOT NULL DEFAULT 0.15 CHECK(probability>=0.01 AND probability<=0.99),
 attempts INTEGER NOT NULL DEFAULT 0,
 successes INTEGER NOT NULL DEFAULT 0,
 due REAL,
 last_seen REAL,
 PRIMARY KEY(student,component)
);
CREATE INDEX IF NOT EXISTS kc_mastery_due ON kc_mastery(student,due);
INSERT OR IGNORE INTO schema_version VALUES(2);
COMMIT;