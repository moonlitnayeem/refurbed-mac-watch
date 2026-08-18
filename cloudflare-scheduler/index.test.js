import assert from "node:assert/strict";
import test from "node:test";

import { dispatchWorkflow } from "./src/index.js";

test("dispatchWorkflow triggers watch.yml on main", async () => {
  const requests = [];
  const fakeFetch = async (url, options) => {
    requests.push({ url, options });
    return new Response(JSON.stringify({ workflow_run_id: 123 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  await dispatchWorkflow({ GITHUB_TOKEN: "secret-token" }, fakeFetch);

  assert.equal(requests.length, 1);
  assert.equal(
    requests[0].url,
    "https://api.github.com/repos/moonlitnayeem/refurbed-mac-watch/actions/workflows/watch.yml/dispatches",
  );
  assert.equal(requests[0].options.method, "POST");
  assert.equal(requests[0].options.headers.Authorization, "Bearer secret-token");
  assert.deepEqual(JSON.parse(requests[0].options.body), { ref: "main" });
});

test("dispatchWorkflow fails loudly when GitHub rejects the request", async () => {
  const fakeFetch = async () => new Response("Bad credentials", { status: 401 });

  await assert.rejects(
    dispatchWorkflow({ GITHUB_TOKEN: "bad-token" }, fakeFetch),
    /GitHub workflow dispatch failed \(401\): Bad credentials/,
  );
});

test("scheduled waits for the GitHub dispatch", async () => {
  let completed = false;
  const fakeFetch = async () => {
    completed = true;
    return new Response(null, { status: 204 });
  };
  const worker = (await import("./src/index.js")).default;

  await worker.scheduled({}, { GITHUB_TOKEN: "secret-token" }, {}, fakeFetch);

  assert.equal(completed, true);
});
