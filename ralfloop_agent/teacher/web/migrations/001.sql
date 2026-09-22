BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS students(
 id TEXT PRIMARY KEY, membership_card_id TEXT NOT NULL UNIQUE,
 display_name TEXT NOT NULL, school_level TEXT NOT NULL, grade INTEGER NOT NULL,
 school_track TEXT NOT NULL DEFAULT '', track_detail TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, last_activity REAL, demo INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS credentials(
 student TEXT PRIMARY KEY REFERENCES students(id), salt TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_sessions(
 token_hash TEXT PRIMARY KEY, student TEXT NOT NULL REFERENCES students(id), expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS session_student ON web_sessions(student);
CREATE TABLE IF NOT EXISTS login_limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mastery(
 student TEXT NOT NULL REFERENCES students(id), topic TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 consecutive INTEGER NOT NULL DEFAULT 0, difficulty INTEGER NOT NULL DEFAULT 1,
 last_seen REAL, due REAL, PRIMARY KEY(student,topic));
CREATE TABLE IF NOT EXISTS activities(
 id TEXT PRIMARY KEY, student TEXT NOT NULL REFERENCES students(id), topic TEXT NOT NULL,
 kind TEXT NOT NULL, content TEXT NOT NULL, fingerprint TEXT NOT NULL,
 source TEXT NOT NULL, reason TEXT NOT NULL, difficulty INTEGER NOT NULL,
 created REAL NOT NULL, completed REAL, simulation TEXT, material TEXT);
CREATE INDEX IF NOT EXISTS activities_student ON activities(student,created);
CREATE TABLE IF NOT EXISTS attempts(
 id INTEGER PRIMARY KEY, student TEXT NOT NULL REFERENCES students(id), activity TEXT NOT NULL REFERENCES activities(id),
 request_key TEXT NOT NULL, correct INTEGER NOT NULL, error TEXT, result TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(student,request_key));
CREATE INDEX IF NOT EXISTS attempts_activity ON attempts(activity,created);
CREATE TABLE IF NOT EXISTS xp_events(
 id INTEGER PRIMARY KEY, student TEXT NOT NULL REFERENCES students(id), points INTEGER NOT NULL CHECK(points>=0),
 reason TEXT NOT NULL, deduplication_key TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(student,deduplication_key));
CREATE TABLE IF NOT EXISTS badges(
 student TEXT NOT NULL REFERENCES students(id), badge TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(student,badge));
CREATE TABLE IF NOT EXISTS daily_challenges(
 student TEXT NOT NULL REFERENCES students(id), day TEXT NOT NULL, target INTEGER NOT NULL DEFAULT 5,
 completed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(student,day));
CREATE TABLE IF NOT EXISTS materials(
 id TEXT PRIMARY KEY, student TEXT NOT NULL REFERENCES students(id), title TEXT NOT NULL,
 kind TEXT NOT NULL, chapter TEXT NOT NULL, pages TEXT NOT NULL, text TEXT NOT NULL,
 rights TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS material_mapping(
 material TEXT NOT NULL REFERENCES materials(id), topic TEXT NOT NULL, PRIMARY KEY(material,topic));
CREATE TABLE IF NOT EXISTS audio_assets(
 id TEXT PRIMARY KEY, student TEXT NOT NULL REFERENCES students(id), material TEXT NOT NULL REFERENCES materials(id),
 provider TEXT NOT NULL, tracks TEXT NOT NULL, chapter INTEGER NOT NULL DEFAULT 0,
 position REAL NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS study_plans(
 student TEXT PRIMARY KEY REFERENCES students(id), topics TEXT NOT NULL, minutes INTEGER NOT NULL, updated REAL NOT NULL);
INSERT OR IGNORE INTO schema_version VALUES(1);
COMMIT;
