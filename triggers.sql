-- Graylore triggers and stored functions (PostgreSQL / PL/pgSQL)
--
-- Unlike MySQL, PostgreSQL lets a trigger modify the table it fires on, so
-- both business rules below live entirely in the database.

-- ---------------------------------------------------------------------------
-- Trigger 1: Device limit enforcement.
-- Fires before a new device session is inserted. If the user already has as
-- many active sessions as their plan allows, the oldest active session(s)
-- are force-logged-out so the new device can sign in. The app checks
-- Sessions.is_active on every request and signs out a kicked device.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_device_limit() RETURNS TRIGGER AS $$
DECLARE
    active_count INT;
    device_cap   INT;
BEGIN
    -- Plan cap; a user with no active plan is treated as a 1-device Basic user.
    SELECT device_limit INTO device_cap
    FROM Subscriptions
    WHERE user_id = NEW.user_id AND status = 'active'
    ORDER BY start_date DESC, sub_id DESC
    LIMIT 1;
    device_cap := COALESCE(device_cap, 1);

    -- Active sessions on OTHER devices (re-logging the same device never counts twice).
    SELECT COUNT(*) INTO active_count
    FROM Sessions
    WHERE user_id = NEW.user_id AND is_active = TRUE AND device_id <> NEW.device_id;

    IF active_count >= device_cap THEN
        UPDATE Sessions
        SET is_active = FALSE
        WHERE session_id IN (
            SELECT session_id FROM Sessions
            WHERE user_id = NEW.user_id AND is_active = TRUE AND device_id <> NEW.device_id
            ORDER BY login_time ASC, session_id ASC
            LIMIT (active_count - device_cap + 1)
        );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_device_limit ON Sessions;
CREATE TRIGGER trg_device_limit
BEFORE INSERT ON Sessions
FOR EACH ROW EXECUTE FUNCTION fn_device_limit();


-- ---------------------------------------------------------------------------
-- Trigger 2: Single active subscription per user.
-- Inserting a new subscription cancels any previous active one for that user
-- and stamps its end_date, so a user is never on two plans at once.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_single_active_sub() RETURNS TRIGGER AS $$
BEGIN
    UPDATE Subscriptions
    SET status = 'cancelled',
        end_date = CURRENT_DATE
    WHERE user_id = NEW.user_id AND status = 'active';
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_single_active_sub ON Subscriptions;
CREATE TRIGGER trg_single_active_sub
BEFORE INSERT ON Subscriptions
FOR EACH ROW EXECUTE FUNCTION fn_single_active_sub();


-- ---------------------------------------------------------------------------
-- Stored function: royalty payout snapshot.
-- Aggregates SUM(watch_minutes) × rate per title into the denormalised
-- Payouts reporting table for a named period (e.g. '2026-10'). Re-running
-- for the same period refreshes the existing rows. Returns rows written.
-- Called from the admin dashboard's "Snapshot payouts" button.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION refresh_payouts(p_period VARCHAR, p_rate DECIMAL DEFAULT 0.05)
RETURNS INT AS $$
DECLARE
    n INT;
BEGIN
    INSERT INTO Payouts (content_id, total_minutes, rate_per_min, payout_amount, period)
    SELECT c.content_id,
           COALESCE(SUM(w.watch_minutes), 0),
           p_rate,
           ROUND(COALESCE(SUM(w.watch_minutes), 0) * p_rate, 2),
           p_period
    FROM Content c
    LEFT JOIN WatchHistory w ON w.content_id = c.content_id
    GROUP BY c.content_id
    ON CONFLICT (content_id, period) DO UPDATE
        SET total_minutes = EXCLUDED.total_minutes,
            rate_per_min  = EXCLUDED.rate_per_min,
            payout_amount = EXCLUDED.payout_amount;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$ LANGUAGE plpgsql;
