-- Which kind of pass wrote a platform_health row: "harvest" (whole boards)
-- or "crawl" (locality-scoped). The two read the same boards to very
-- different counts (SmartRecruiters: 5,161 jobs, 0 empty a harvest; 143 jobs,
-- 5 of 11 empty a crawl), and one trailing median over both raised false
-- alarms each way (2026-10-08). Each pass is now judged against its own kind.
--
-- Backfill: only the unambiguous harvests (every one so far read 94k+ jobs;
-- every crawl 36k or fewer). Other old rows stay NULL, no pass's baseline.

ALTER TABLE platform_health ADD COLUMN kind TEXT;

UPDATE platform_health SET kind = 'harvest'
WHERE kind IS NULL AND pass_at IN (
    SELECT pass_at FROM platform_health GROUP BY pass_at HAVING SUM(jobs) >= 60000);
