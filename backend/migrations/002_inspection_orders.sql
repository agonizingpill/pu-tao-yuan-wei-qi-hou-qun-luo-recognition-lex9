CREATE TABLE IF NOT EXISTS sensor_shift (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id integer NOT NULL,
    sensor text NOT NULL,
    observed_day date NOT NULL,
    slot text NOT NULL CHECK (slot IN ('day', 'night')),
    state text NOT NULL CHECK (state IN ('insufficient', 'clear', 'anomaly')),
    judgment jsonb NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    digest text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sensor, observed_day, slot),
    FOREIGN KEY (block_id, sensor) REFERENCES vine_block(id, sensor)
);
CREATE INDEX IF NOT EXISTS sensor_shift_lookup ON sensor_shift (observed_day DESC, slot, block_id);

CREATE TABLE IF NOT EXISTS inspection_order (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id integer NOT NULL,
    sensor text NOT NULL,
    observed_day date NOT NULL,
    slot text NOT NULL CHECK (slot IN ('day', 'night')),
    state text NOT NULL CHECK (state IN ('open', 'accepted', 'pending_review', 'revoked')),
    current_revision integer NOT NULL CHECK (current_revision > 0),
    accepted_revision integer,
    accepted_by text REFERENCES account(username),
    accepted_at timestamptz,
    opened_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    review_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sensor, observed_day, slot),
    FOREIGN KEY (block_id, sensor) REFERENCES vine_block(id, sensor),
    CHECK ((accepted_revision IS NULL) = (accepted_at IS NULL)),
    CHECK ((accepted_revision IS NULL) = (accepted_by IS NULL)),
    CHECK (state NOT IN ('accepted', 'pending_review')
           OR (accepted_revision IS NOT NULL AND accepted_at IS NOT NULL AND accepted_by IS NOT NULL)),
    CHECK ((review_at IS NULL) = (state <> 'pending_review')),
    CHECK ((revoked_at IS NULL) = (state <> 'revoked'))
);
CREATE INDEX IF NOT EXISTS inspection_order_queue ON inspection_order (state, observed_day DESC, slot);

CREATE TABLE IF NOT EXISTS inspection_evidence (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id integer NOT NULL REFERENCES inspection_order(id),
    revision integer NOT NULL CHECK (revision > 0),
    state text NOT NULL CHECK (state IN ('anomaly', 'clear', 'insufficient')),
    judgment jsonb NOT NULL,
    digest text NOT NULL,
    note text NOT NULL,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (order_id, revision)
);
CREATE INDEX IF NOT EXISTS inspection_evidence_order ON inspection_evidence (order_id, revision DESC);
