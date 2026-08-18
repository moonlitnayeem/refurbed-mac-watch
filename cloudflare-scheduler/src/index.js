const DISPATCH_URL =
  "https://api.github.com/repos/moonlitnayeem/refurbed-mac-watch/actions/workflows/watch.yml/dispatches";

export async function dispatchWorkflow(env, fetchImpl = fetch) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN secret is not configured");
  }

  const response = await fetchImpl(DISPATCH_URL, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "refurbed-watch-cloudflare-scheduler",
      "X-GitHub-Api-Version": "2026-03-10",
    },
    body: JSON.stringify({ ref: "main" }),
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(
      `GitHub workflow dispatch failed (${response.status}): ${detail}`,
    );
  }

  return response;
}

export default {
  async scheduled(_controller, env, _ctx, fetchImpl = fetch) {
    return dispatchWorkflow(env, fetchImpl);
  },
};
