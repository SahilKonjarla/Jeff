"""Declared SQLite DDL and stable column orders; no schema change."""

_LEDGER = "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"


_PROJECTS = (
    "CREATE TABLE projects (project_id TEXT NOT NULL PRIMARY KEY, "
    "canonical_root TEXT NOT NULL UNIQUE)"
)


_RECEIPTS = (
    "CREATE TABLE request_receipts (request_id TEXT NOT NULL PRIMARY KEY, "
    "owner_instance_id TEXT NOT NULL, operation TEXT NOT NULL "
    "CHECK(operation IN ('init','ask.branch','ask.changes')), "
    "canonical_root TEXT NOT NULL, project_id TEXT, "
    "state TEXT NOT NULL CHECK(state IN ('accepted','running','succeeded','failed','unknown')), "
    "accepted_at_ms INTEGER NOT NULL CHECK(accepted_at_ms>=0), finished_at_ms INTEGER, "
    "outcome_code TEXT CHECK(outcome_code IN "
    "('ok','operation_failed','timeout','interrupted','partial','target_mismatch')), "
    "CHECK(project_id IS NOT NULL OR operation='init'), "
    "CHECK((state IN ('accepted','running') AND finished_at_ms IS NULL "
    "AND outcome_code IS NULL) OR (state IN ('succeeded','failed','unknown') "
    "AND finished_at_ms IS NOT NULL AND outcome_code IS NOT NULL)))"
)


_RECEIPT_INDEX = "CREATE INDEX receipts_unfinished ON request_receipts(state,owner_instance_id)"


_COLUMNS = (
    "request_id,owner_instance_id,operation,canonical_root,project_id,"
    "state,accepted_at_ms,finished_at_ms,outcome_code"
)


_ACTIVITY_META = (
    "CREATE TABLE activity_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "epoch TEXT NOT NULL, "
    "coverage_started_at_ms INTEGER NOT NULL CHECK(coverage_started_at_ms>=0), "
    "high_water INTEGER NOT NULL CHECK(high_water>=0), "
    "generation INTEGER NOT NULL CHECK(generation>=0))"
)


_FLOOR_COLUMN = "replay_floor INTEGER NOT NULL DEFAULT 0 CHECK(replay_floor>=0)"


_ACTIVITY_META_V4 = _ACTIVITY_META[:-1] + ", " + _FLOOR_COLUMN + ")"


_TOMBSTONES = "CREATE TABLE request_tombstones (request_id TEXT NOT NULL PRIMARY KEY)"


_EXPIRABLE_INDEX = (
    "CREATE INDEX receipts_expirable ON request_receipts(finished_at_ms,request_id) "
    "WHERE state IN ('succeeded','failed')"
)


_ACTIVITY_REQUESTS = (
    "CREATE TABLE activity_requests ("
    "request_id TEXT PRIMARY KEY REFERENCES request_receipts(request_id), "
    "canonical_root TEXT NOT NULL, project_id TEXT, accepted_sequence INTEGER NOT NULL UNIQUE)"
)


_ACTIVITY_EVENTS = (
    "CREATE TABLE activity_events (event_id INTEGER PRIMARY KEY, "
    "request_id TEXT NOT NULL REFERENCES request_receipts(request_id), "
    "owner_instance_id TEXT NOT NULL, "
    "canonical_root TEXT NOT NULL, project_id TEXT, scope_project_id TEXT, kind TEXT NOT NULL, "
    "recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0), code TEXT, git_category TEXT, "
    "elapsed_ms INTEGER, output_bytes INTEGER, git_started INTEGER CHECK(git_started IN (0,1)), "
    "registration_created INTEGER CHECK(registration_created IN (0,1)))"
)


_ACTIVITY_INDEXES = {
    "activity_trace": "CREATE INDEX activity_trace ON activity_events(request_id,event_id)",
    "activity_replay": (
        "CREATE INDEX activity_replay ON activity_events(scope_project_id,canonical_root,event_id)"
    ),
    "activity_history": (
        "CREATE INDEX activity_history "
        "ON activity_requests(project_id,canonical_root,accepted_sequence)"
    ),
}


_EVENT_COLUMNS = (
    "event_id,request_id,owner_instance_id,canonical_root,project_id,kind,recorded_at_ms,"
    "code,git_category,elapsed_ms,output_bytes,git_started,registration_created"
)


_REVIEWS = (
    "CREATE TABLE execution_reviews (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "proposal_id TEXT NOT NULL UNIQUE, approval_id TEXT NOT NULL UNIQUE, "
    "job_id TEXT NOT NULL UNIQUE, "
    "project_id TEXT NOT NULL, canonical_root TEXT NOT NULL, owner_instance_id TEXT NOT NULL, "
    "manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB))<=32768), "
    "manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64), "
    "configuration_digest TEXT NOT NULL CHECK(length(configuration_digest)=64), "
    "state TEXT NOT NULL CHECK(state IN ('pending','approved','denied','expired','invalidated')), "
    "created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0), "
    "expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms>=created_at_ms))"
)


_REVIEW_OPS = (
    "CREATE TABLE execution_operations (request_id TEXT PRIMARY KEY, "
    "project_id TEXT NOT NULL, canonical_root TEXT NOT NULL, operation TEXT NOT NULL "
    "CHECK(operation IN ('propose','approve','deny')), payload_digest TEXT NOT NULL "
    "CHECK(length(payload_digest)=64), proposal_id TEXT NOT NULL, "
    "recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0), "
    "outcome_state TEXT NOT NULL CHECK(outcome_state IN ('pending','approved','denied')))"
)


_REVIEW_COLUMNS = (
    "sequence,proposal_id,approval_id,job_id,project_id,canonical_root,owner_instance_id,"
    "manifest_json,manifest_digest,configuration_digest,state,created_at_ms,expires_at_ms"
)


_AUTHORITIES = (
    "CREATE TABLE execution_authorities (proposal_id TEXT PRIMARY KEY, "
    "approval_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL UNIQUE, "
    "project_id TEXT NOT NULL, canonical_root TEXT NOT NULL, owner_instance_id TEXT NOT NULL, "
    "manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB))<=32768), "
    "manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64), "
    "state TEXT NOT NULL CHECK(state IN "
    "('pending','approved','denied','expired','invalidated','consumed')), "
    "created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0), "
    "expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms>=created_at_ms), "
    "claim_id TEXT UNIQUE, claim_state TEXT "
    "CHECK(claim_state IN ('claimed','unknown','finished')), "
    "claimed_at_ms INTEGER, CHECK((state='consumed' AND claim_id IS NOT NULL "
    "AND claim_state IS NOT NULL AND claimed_at_ms IS NOT NULL "
    "AND claimed_at_ms>=created_at_ms) OR "
    "(state!='consumed' AND claim_id IS NULL AND claim_state IS NULL AND claimed_at_ms IS NULL)))"
)


_AUTHORITY_OPS = (
    "CREATE TABLE execution_authority_operations (request_id TEXT PRIMARY KEY, "
    "payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64), "
    "proposal_id TEXT NOT NULL REFERENCES execution_authorities(proposal_id))"
)


_AUTHORITY_COLUMNS = (
    "proposal_id,approval_id,job_id,project_id,canonical_root,owner_instance_id,manifest_json,"
    "manifest_digest,state,created_at_ms,expires_at_ms,claim_id,claim_state,claimed_at_ms"
)


_LAUNCHES = (
    "CREATE TABLE execution_launches (attempt_id TEXT PRIMARY KEY, "
    "claim_id TEXT NOT NULL UNIQUE REFERENCES execution_authorities(claim_id), "
    "intent_json TEXT NOT NULL CHECK(length(CAST(intent_json AS BLOB))<=131072), "
    "intent_digest TEXT NOT NULL CHECK(length(intent_digest)=64), "
    "container_id TEXT, created_at TEXT, observed_container_id TEXT, exit_code INTEGER, "
    "CHECK((container_id IS NULL)=(created_at IS NULL)), "
    "CHECK(container_id IS NULL OR length(container_id)=64), "
    "CHECK(created_at IS NULL OR length(created_at)<=64), "
    "CHECK(observed_container_id IS NULL OR length(observed_container_id)=64), "
    "CHECK(exit_code IS NULL OR exit_code BETWEEN 0 AND 255))"
)


_LAUNCH_EVENTS = (
    "CREATE TABLE execution_launch_events (attempt_id TEXT NOT NULL "
    "REFERENCES execution_launches(attempt_id), phase TEXT NOT NULL "
    "CHECK(phase IN ('create_intent','created','start_intent','exited','cleaned','unknown')), "
    "sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 6), "
    "event_digest TEXT NOT NULL CHECK(length(event_digest)=64), "
    "PRIMARY KEY(attempt_id,phase), UNIQUE(attempt_id,sequence))"
)


_RESULTS = (
    "CREATE TABLE execution_results (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "attempt_id TEXT NOT NULL UNIQUE REFERENCES execution_launches(attempt_id), "
    "disposition TEXT NOT NULL CHECK(disposition IN ('completed','quarantined')), "
    "recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0), "
    "metadata_json TEXT NOT NULL CHECK(length(CAST(metadata_json AS BLOB))<=8192), "
    "completion_digest TEXT NOT NULL CHECK(length(completion_digest)=64), "
    "stdout TEXT, stderr TEXT, retirement_reason TEXT "
    "CHECK(retirement_reason IN ('age','capacity')), "
    "CHECK((stdout IS NULL)=(stderr IS NULL)), "
    "CHECK((stdout IS NULL)=(retirement_reason IS NOT NULL)), "
    "CHECK(stdout IS NULL OR length(CAST(stdout AS BLOB))+length(CAST(stderr AS BLOB))<=1048576))"
)


_RESULT_PAYLOADS = (
    "CREATE INDEX execution_result_payloads ON execution_results(sequence) WHERE stdout IS NOT NULL"
)


_RESULT_QUARANTINE = (
    "CREATE UNIQUE INDEX execution_result_quarantine ON execution_results(disposition) "
    "WHERE disposition='quarantined' AND stdout IS NOT NULL"
)


_STOP_PAYLOADS = (
    "CREATE TABLE execution_stop_payloads ("
    "proposal_id TEXT PRIMARY KEY NOT NULL REFERENCES execution_lifecycles(proposal_id), "
    "disposition TEXT NOT NULL CHECK(disposition IN ('known','quarantined')), "
    "recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0), "
    "clock_confirmed INTEGER NOT NULL CHECK(clock_confirmed IN (0,1)), "
    "output_facts_json TEXT NOT NULL CHECK(length(CAST(output_facts_json AS BLOB))<=8192), "
    "output_digest TEXT NOT NULL CHECK(length(output_digest)=64), "
    "stdout TEXT, stderr TEXT, retirement_reason TEXT, "
    "CHECK((stdout IS NULL AND stderr IS NULL AND retirement_reason IS NOT NULL "
    "AND retirement_reason IN ('age','capacity')) OR "
    "(stdout IS NOT NULL AND stderr IS NOT NULL AND retirement_reason IS NULL "
    "AND length(CAST(stdout AS BLOB))+length(CAST(stderr AS BLOB))<=1048576)))"
)


_LIFECYCLES = (
    "CREATE TABLE execution_lifecycles (proposal_id TEXT PRIMARY KEY "
    "REFERENCES execution_authorities(proposal_id), record_json TEXT NOT NULL "
    "CHECK(length(CAST(record_json AS BLOB))<=32768), record_digest TEXT NOT NULL "
    "CHECK(length(record_digest)=64))"
)
