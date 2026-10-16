/*
 * Poke every friend on RemiliaNET.
 *
 * Usage: open https://www.remilia.net while logged in, open the DevTools
 * console (Cmd+Option+J in Chrome, Cmd+Option+K in Firefox), paste this whole
 * file, and press Enter. Keep the tab open until it prints "Done".
 *
 * Pokes have a 24h cooldown per person, so anyone already poked today is
 * skipped. Safe to re-run at any time.
 */
(async () => {
  const DELAY_MS = 500;
  const RATE_LIMIT_BACKOFF_MS = 10000;
  const MAX_RETRIES = 3;
  const PAGE_SIZE = 50;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /* The site keeps a fresh auth cookie on remilia.net, so same-origin fetches are logged in as you. */
  const api = async (method, path) => {
    const res = await fetch(path, {
      method,
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
    let body = null;
    try { body = await res.json(); } catch {}
    return { status: res.status, body };
  };

  if (!location.hostname.endsWith('remilia.net')) {
    console.error('Run this from a remilia.net tab.');
    return;
  }

  const me = await api('GET', '/api/profile/whoami');
  if (me.status !== 200 || !me.body?.userHandle) {
    alert(`Not logged in (whoami returned ${me.status}). Log in, reload the page, and run again.`);
    return;
  }
  const myHandle = me.body.userHandle;

  const friends = [];
  for (let page = 1; ; page++) {
    const query = new URLSearchParams({ username: myHandle, page, limit: PAGE_SIZE });
    const { status, body } = await api('GET', `/api/profile/friends?${query}`);
    if (status !== 200) {
      console.error(`Could not load friends page ${page} (HTTP ${status}).`);
      return;
    }
    friends.push(...body.friends.map((f) => f.user.userHandle));
    if (!body.pagination?.hasMore) break;
  }
  console.log(`~${myHandle} has ${friends.length} friends. Poking...`);

  /* A 429 is either the 24h per-person cooldown or a general rate limit; the profile's canPoke tells them apart. */
  const isOnCooldown = async (handle) => {
    const { status, body } = await api('GET', `/api/profile/~${encodeURIComponent(handle)}`);
    return status === 200 && body?.viewerContext?.canPoke === false;
  };

  const result = { poked: [], cooldown: [], failed: [] };
  let retries = 0;
  let sessionExpired = false;
  for (let i = 0; i < friends.length; i++) {
    const handle = friends[i];
    const tag = `[${i + 1}/${friends.length}] ~${handle}`;
    const { status, body } = await api('POST', `/api/users/${encodeURIComponent(handle)}/poke`);

    if (status >= 200 && status < 300) {
      result.poked.push(handle);
      console.log(`${tag} poked`);
    } else if (status === 429 && await isOnCooldown(handle)) {
      result.cooldown.push(handle);
      console.log(`${tag} already poked in the last 24h`);
    } else if (status === 429 && retries < MAX_RETRIES) {
      retries++;
      console.warn(`${tag} rate limited, waiting ${RATE_LIMIT_BACKOFF_MS / 1000}s and retrying`);
      await sleep(RATE_LIMIT_BACKOFF_MS);
      i--;
      continue;
    } else if (status === 401) {
      sessionExpired = true;
      break;
    } else {
      result.failed.push(handle);
      console.warn(`${tag} failed: HTTP ${status} ${body?.error?.message ?? ''}`);
    }
    retries = 0;
    await sleep(DELAY_MS);
  }

  let summary = sessionExpired
    ? 'Stopped early: session expired. Reload the page and run again; already-poked friends will be skipped.\n'
    : 'Done. ';
  summary += `Poked ${result.poked.length}, skipped ${result.cooldown.length} on cooldown, ${result.failed.length} failed.`;
  if (result.failed.length) summary += `\nFailed: ${result.failed.join(', ')}`;
  console.log(summary);
  alert(summary);
  return result;
})();
