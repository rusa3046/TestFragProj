-- `subreddit` was named for the only source that existed. With Reddit API
-- access refused and YouTube added, a column that must hold a channel ID
-- while being called `subreddit` is the kind of naming that quietly rots:
-- every future reader has to know it lies.
--
-- Renamed to `source_channel` — whatever subdivision the source uses.
-- Reddit stores the subreddit, YouTube the channel ID.

ALTER TABLE comments RENAME COLUMN subreddit TO source_channel;
