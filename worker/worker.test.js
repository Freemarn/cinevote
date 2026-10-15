// Unit tests for worker.js. No Redis or PostgreSQL needed:
// the database and cache are replaced with Jest fakes.
const { parseVote, saveVote } = require('./worker');

const goodVote = { user_id: 7, movie_id: 3, week: '2026-W41' };

describe('parseVote', () => {
  test('accepts a valid vote', () => {
    const raw = JSON.stringify({ ...goodVote, ts: 1 });
    expect(parseVote(raw)).toEqual({ ...goodVote, ts: 1 });
  });

  test('rejects text that is not JSON', () => {
    expect(parseVote('{not json')).toBeNull();
  });

  test('rejects a vote with a missing field', () => {
    const raw = JSON.stringify({ user_id: 7, week: '2026-W41' });
    expect(parseVote(raw)).toBeNull();
  });

  test('rejects a badly formed week', () => {
    const raw = JSON.stringify({ ...goodVote, week: 'week 41' });
    expect(parseVote(raw)).toBeNull();
  });
});

describe('saveVote', () => {
  test('saves the vote and clears the leaderboard cache', async () => {
    const db = { query: jest.fn().mockResolvedValue({ rowCount: 1 }) };
    const cache = { del: jest.fn().mockResolvedValue(1) };

    const saved = await saveVote(db, cache, goodVote);

    expect(saved).toBe(true);
    expect(db.query).toHaveBeenCalledWith(
      expect.stringContaining('INSERT INTO votes'),
      [7, 3, '2026-W41'],
    );
    expect(cache.del).toHaveBeenCalledWith('leaderboard:2026-W41');
  });

  test('reports a second vote in the same week as not saved', async () => {
    const db = { query: jest.fn().mockResolvedValue({ rowCount: 0 }) };
    const cache = { del: jest.fn().mockResolvedValue(1) };

    expect(await saveVote(db, cache, goodVote)).toBe(false);
  });
});
