/**
 * CineVote vote worker.
 * Pops votes from the Redis LIST "votes", writes them to
 * PostgreSQL, then clears the cached leaderboard.
 * Talks to: Redis (same server) and PostgreSQL (database
 * server). Nothing else.
 */
const fs = require('fs');
const { createClient } = require('redis');
const { Pool } = require('pg');

const QUEUE = 'votes';
const HEARTBEAT_FILE = '/tmp/heartbeat';
const WEEK_PATTERN = /^\d{4}-W\d{2}$/;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The Docker healthcheck reads this file:
// a fresh timestamp means the loop is alive.
function heartbeat() {
  const now = String(Math.floor(Date.now() / 1000));
  fs.writeFileSync(HEARTBEAT_FILE, now);
}

// Turn one raw queue message into a vote object.
// Returns null when the message is broken or incomplete.
function parseVote(raw) {
  let vote;
  try {
    vote = JSON.parse(raw);
  } catch {
    return null;
  }
  const valid = vote !== null
    && Number.isInteger(vote.user_id)
    && Number.isInteger(vote.movie_id)
    && WEEK_PATTERN.test(vote.week);
  return valid ? vote : null;
}

// Save one vote, then clear that week's cached leaderboard.
// db and cache are passed in, so the tests can use fakes.
// Returns true if saved, false if the user had already voted.
async function saveVote(db, cache, vote) {
  const result = await db.query(
    `INSERT INTO votes (user_id, movie_id, week)
     VALUES ($1, $2, $3)
     ON CONFLICT (user_id, week) DO NOTHING`,
    [vote.user_id, vote.movie_id, vote.week],
  );
  await cache.del(`leaderboard:${vote.week}`);
  return result.rowCount === 1;
}

async function main() {
  const redisHost = process.env.REDIS_HOST || 'redis';
  const redis = createClient({ url: `redis://${redisHost}:6379` });
  redis.on('error', (err) => console.error('[redis]', err.message));

  const pool = new Pool({
    host: process.env.DB_HOST,
    port: 5432,
    database: process.env.DB_NAME || 'votingdb',
    user: process.env.DB_USER || 'votinguser',
    password: process.env.DB_PASSWORD,
    max: 3,
    connectionTimeoutMillis: 5000,
  });
  pool.on('error', (err) => console.error('[postgres]', err.message));

  let running = true;
  process.on('SIGTERM', () => {
    console.log('SIGTERM received, finishing current vote');
    running = false;
  });
  process.on('SIGINT', () => { running = false; });

  await redis.connect();
  console.log('Worker connected to Redis; waiting for votes on list "votes"');

  while (running) {
    heartbeat();

    let item;
    try {
      // Wait up to 5 s for a vote. Flask LPUSHes on the left,
      // we BRPOP from the right: first in, first out.
      item = await redis.brPop(QUEUE, 5);
    } catch (err) {
      console.error('Could not read from Redis:', err.message);
      await sleep(2000);
      continue;
    }
    if (!item) continue; // no vote in 5 s: loop and refresh heartbeat

    const vote = parseVote(item.element);
    if (!vote) {
      console.error('Dropping malformed message:', item.element);
      continue;
    }

    try {
      const saved = await saveVote(pool, redis, vote);
      const who = `user=${vote.user_id} week=${vote.week}`;
      console.log(saved
        ? `Saved vote: ${who} movie=${vote.movie_id}`
        : `Duplicate ignored: ${who} already voted`);
    } catch (err) {
      if (err.code && err.code.startsWith('23')) {
        // 23xxx = constraint error: a retry can never succeed
        console.error(`Dropping invalid vote (${err.code}):`, item.element);
        continue;
      }
      // Database unreachable: put the vote back and retry shortly
      console.error('Database write failed, re-queueing vote:', err.message);
      try {
        await redis.rPush(QUEUE, item.element);
      } catch (e) {
        console.error('Re-queue failed:', e.message);
      }
      await sleep(2000);
    }
  }

  await redis.quit();
  await pool.end();
  console.log('Worker stopped cleanly');
}

// Run the loop only when started with "node worker.js".
// When the tests require() this file, nothing connects.
if (require.main === module) {
  main().catch((err) => {
    console.error('Worker crashed:', err);
    process.exit(1);
  });
}

module.exports = { parseVote, saveVote };
