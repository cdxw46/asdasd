INSERT INTO ivr_menus (slug, name, welcome_file, timeout_sec)
VALUES ('main', 'Main IVR', '/var/lib/smurf/moh/default.wav', 15)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO ivr_options (menu_slug, digit, action)
VALUES
    ('main', '1', '1000'),
    ('main', '2', 'support'),
    ('main', '0', '*1000')
ON CONFLICT DO NOTHING;

INSERT INTO office_hours (extension_number, weekday_mask, time_start, time_end, outside_target, timezone)
VALUES ('1000', 127, '00:00', '23:59', 'main', 'UTC')
ON CONFLICT (extension_number) DO NOTHING;
