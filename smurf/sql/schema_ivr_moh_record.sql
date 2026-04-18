-- Office hours: one rule per extension (weekday bitmap + local time window)
CREATE TABLE IF NOT EXISTS office_hours (
    extension_number VARCHAR(32) PRIMARY KEY REFERENCES extensions (number) ON DELETE CASCADE,
    weekday_mask     INT NOT NULL DEFAULT 127 CHECK (weekday_mask >= 0 AND weekday_mask <= 127),
    time_start         TIME NOT NULL DEFAULT '09:00',
    time_end           TIME NOT NULL DEFAULT '18:00',
    outside_target     VARCHAR(64) NOT NULL DEFAULT '*1000',
    timezone           TEXT NOT NULL DEFAULT 'UTC'
);

-- IVR menus: digit -> SIP user part or *voicemail or queue:support
CREATE TABLE IF NOT EXISTS ivr_menus (
    slug         VARCHAR(64) PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    welcome_file TEXT NOT NULL DEFAULT '/var/lib/smurf/moh/default.wav',
    timeout_sec  INT NOT NULL DEFAULT 10 CHECK (timeout_sec > 0 AND timeout_sec <= 120)
);

CREATE TABLE IF NOT EXISTS ivr_options (
    menu_slug VARCHAR(64) NOT NULL REFERENCES ivr_menus (slug) ON DELETE CASCADE,
    digit       CHAR(1) NOT NULL CHECK (digit IN ('0','1','2','3','4','5','6','7','8','9','*','#')),
    action      VARCHAR(128) NOT NULL,
    PRIMARY KEY (menu_slug, digit)
);
