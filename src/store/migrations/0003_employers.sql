-- Employers: one company may own several boards. A `companies` row stays the
-- crawl unit (one board, its schedule and counts); `employer_id` says which
-- employer a board belongs to, and jobs display under the employer's name.
--
-- Backfill (all SQL): one employer per `employer:<key>` tag group, named for
-- the group's board with the most open postings (what restamp.link_employers
-- picked), one per remaining row, named for it. The tag is then retired.

CREATE TABLE IF NOT EXISTS employers (
    id         INTEGER PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    created_at TEXT
);

ALTER TABLE companies ADD COLUMN employer_id INTEGER REFERENCES employers(id);

CREATE INDEX IF NOT EXISTS ix_companies_employer ON companies(employer_id);

-- A board's employer, for the places that show or group by employer.
CREATE VIEW IF NOT EXISTS board_employers AS
SELECT c.id AS company_id, c.employer_id, COALESCE(e.name, c.name) AS employer_name
FROM companies c LEFT JOIN employers e ON e.id = c.employer_id;

CREATE TEMP TABLE _emp_tag AS
SELECT c.id AS company_id,
       (SELECT substr(j.value, 10)
        FROM json_each('["' || replace(c.tags, ',', '","') || '"]') j
        WHERE j.value LIKE 'employer:%' ORDER BY j.key LIMIT 1) AS key
FROM companies c
WHERE c.tags LIKE '%employer:%' AND json_valid('["' || replace(c.tags, ',', '","') || '"]');

CREATE TEMP TABLE _emp_pick AS
SELECT key, name FROM (
    SELECT t.key, c.name,
           ROW_NUMBER() OVER (PARTITION BY t.key
                              ORDER BY COALESCE(n.jobs, 0) DESC, c.id) AS rn
    FROM _emp_tag t
    JOIN companies c ON c.id = t.company_id
    LEFT JOIN (SELECT company_id, COUNT(*) AS jobs FROM open_jobs
               WHERE company_id IN (SELECT company_id FROM _emp_tag)
               GROUP BY company_id) n ON n.company_id = c.id
) WHERE rn = 1;

INSERT OR IGNORE INTO employers (name, created_at)
SELECT c.name, c.created_at FROM companies c
WHERE c.employer_id IS NULL
  AND (c.id NOT IN (SELECT company_id FROM _emp_tag) OR c.name IN (SELECT name FROM _emp_pick));

UPDATE companies SET employer_id = (
    SELECT e.id FROM _emp_tag t JOIN _emp_pick p ON p.key = t.key
    JOIN employers e ON e.name = p.name WHERE t.company_id = companies.id)
WHERE employer_id IS NULL AND id IN (SELECT company_id FROM _emp_tag);

UPDATE companies SET employer_id = (SELECT e.id FROM employers e WHERE e.name = companies.name)
WHERE employer_id IS NULL;

UPDATE companies SET tags = (
    SELECT group_concat(j.value, ',')
    FROM json_each('["' || replace(companies.tags, ',', '","') || '"]') j
    WHERE j.value NOT LIKE 'employer:%')
WHERE id IN (SELECT company_id FROM _emp_tag);

UPDATE jobs SET company_name = (
    SELECT e.name FROM companies c JOIN employers e ON e.id = c.employer_id
    WHERE c.id = jobs.company_id)
WHERE company_id IN (SELECT company_id FROM _emp_tag);

DROP TABLE _emp_tag;
DROP TABLE _emp_pick;

-- Every board row has an employer, whichever path inserts it; an employer
-- goes with its last deleted row (one whose boards were moved away is swept by
-- store.dedup_companies, so restamp.link_employers can be undone meanwhile); a
-- board that is its employer's namesake renames it. Plain SQL: no function a bare sqlite3 connection lacks.
CREATE TRIGGER IF NOT EXISTS companies_employer_new AFTER INSERT ON companies
WHEN NEW.employer_id IS NULL
BEGIN
    INSERT OR IGNORE INTO employers (name, created_at) VALUES (NEW.name, NEW.created_at);
    UPDATE companies SET employer_id = (SELECT id FROM employers WHERE name = NEW.name)
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS companies_employer_gone AFTER DELETE ON companies
BEGIN
    DELETE FROM employers WHERE id = OLD.employer_id
      AND NOT EXISTS (SELECT 1 FROM companies WHERE employer_id = OLD.employer_id);
END;

CREATE TRIGGER IF NOT EXISTS companies_employer_renamed AFTER UPDATE OF name ON companies
WHEN NEW.name IS NOT OLD.name
BEGIN
    UPDATE employers SET name = NEW.name
    WHERE id = NEW.employer_id AND name = OLD.name
      AND NOT EXISTS (SELECT 1 FROM employers x WHERE x.name = NEW.name);
END;
