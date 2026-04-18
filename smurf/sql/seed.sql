INSERT INTO extensions (number, secret, display_name, max_concurrent, record_calls)
VALUES ('1000', 'smurf1000', 'Demo Extension', 4, true)
ON CONFLICT (number) DO NOTHING;

INSERT INTO extensions (number, secret, display_name, max_concurrent, record_calls)
VALUES ('1001', 'smurf1001', 'Second Extension', 4, true)
ON CONFLICT (number) DO NOTHING;

UPDATE extensions SET record_calls = true WHERE number IN ('1000', '1001');

-- bcrypt hash of "smurfadmin" (cost 10)
INSERT INTO admin_users (username, password_hash, role)
VALUES (
    'admin',
    '$2a$10$wh9IkooBbOdxvYST10wBFOg.EO6B/M1FKIUsAzNCwqZh/qIUNoOri',
    'superadmin'
)
ON CONFLICT (username) DO NOTHING;
