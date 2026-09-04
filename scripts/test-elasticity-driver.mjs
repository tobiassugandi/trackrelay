// Execute the unmodified production module with in-memory k6 APIs. No HTTP/AWS.
// Run through make elasticity-driver-check, which supplies generated inputs.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";
import { createContext, SourceTextModule, SyntheticModule } from "node:vm";

const source = await readFile(
  new URL("../load/elasticity-steps.js", import.meta.url),
  "utf8",
);
const inputDirectory = process.env.ELASTICITY_TEST_INPUT_DIR;
assert.ok(inputDirectory, "ELASTICITY_TEST_INPUT_DIR must contain prepared inputs");
const originalManifest = JSON.parse(
  await readFile(join(inputDirectory, "input-manifest.json")),
);
const originalWorkload = JSON.parse(
  await readFile(join(inputDirectory, "workload-definition.json")),
);

async function driver({ mutate = () => {}, responseStatus = 201 } = {}) {
  const manifest = structuredClone(originalManifest);
  const workload = structuredClone(originalWorkload);
  mutate(manifest, workload);
  const requests = [];
  const checks = [];
  const counters = new Map();
  const execution = { scenario: { name: "baseline", iterationInTest: 0 } };
  const environment = { K6_MANIFEST_PATH: "manifest", K6_WORKLOAD_PATH: "workload" };
  const context = createContext({
    __ENV: environment,
    open: (path) => JSON.stringify(path === "manifest" ? manifest : workload),
  });
  class Counter {
    constructor(name) {
      this.name = name;
    }
    add(value, tags = {}) {
      const key = `${this.name}:${tags.step ?? "all"}`;
      counters.set(key, (counters.get(key) ?? 0) + value);
    }
  }
  const mocks = {
    "k6/execution": { default: execution },
    "k6/data": {
      SharedArray: function (_, callback) {
        return callback();
      },
    },
    "k6/http": {
      default: {
        post(url, body, options) {
          requests.push({ url, body: JSON.parse(body), options });
          return { status: responseStatus };
        },
      },
    },
    "k6": {
      check(response, predicates, tags) {
        const passed = Object.values(predicates).every((predicate) =>
          predicate(response),
        );
        checks.push({ passed, tags });
        return passed;
      },
    },
    "k6/metrics": { Counter },
  };
  const module = new SourceTextModule(source, { context });
  await module.link((specifier) => {
    const exports = mocks[specifier];
    assert.ok(exports, `unexpected import: ${specifier}`);
    return new SyntheticModule(
      Object.keys(exports),
      function () {
        for (const [name, value] of Object.entries(exports)) {
          this.setExport(name, value);
        }
      },
      { context },
    );
  });
  await module.evaluate();
  return {
    manifest,
    workload,
    requests,
    checks,
    counters,
    options: module.namespace.options,
    summary: module.namespace.handleSummary,
    run(name, iteration) {
      execution.scenario = { name, iterationInTest: iteration };
      Object.assign(environment, module.namespace.options.scenarios[name]?.env);
      module.namespace.default();
    },
  };
}

function replay(subject, extraSteps = []) {
  for (const step of subject.workload.steps) {
    for (let index = 0; index < step.expected_request_count; index++) {
      subject.run(step.name, index);
    }
    if (extraSteps.includes(step.name)) {
      subject.run(step.name, step.expected_request_count);
    }
  }
}

test("normal replay submits every manifest event exactly once with the original tags", async () => {
  const subject = await driver();
  replay(subject);
  assert.equal(subject.requests.length, 10800);
  assert.deepEqual(
    subject.requests.map((request) => request.body),
    subject.manifest.expected_events.map((event) => event.payload),
  );
  let offset = 0;
  for (const step of subject.workload.steps) {
    const stepRequests = subject.requests.slice(
      offset, offset + step.expected_request_count,
    );
    for (const request of stepRequests) {
      assert.equal(request.options.tags.step, step.name);
      assert.equal(
        request.options.tags.offered_rate, String(step.offered_rate_per_second),
      );
      assert.equal(
        request.options.headers["X-Test-Run-ID"], subject.manifest.test_run_id,
      );
      const partner = subject.manifest.expected_events[offset].partner_id;
      assert.ok(request.url.endsWith(`/api/v1/partners/${partner}/events`));
    }
    offset += step.expected_request_count;
    assert.equal(subject.counters.get(`driver_boundary_iterations:${step.name}`), 0);
  }
  assert.equal(subject.counters.get("driver_errors:all"), 0);
  assert.equal(subject.checks.length, 10800);
  assert.ok(subject.checks.every((check) => check.passed));
});

test("reproduces the failed cloud dispatches without crossing any slice or duplicating HTTP work", async () => {
  const subject = await driver();
  replay(subject, ["baseline", "fall-5", "recovery"]);
  assert.equal(subject.requests.length, 10800);
  assert.deepEqual(
    subject.requests.map((request) => request.body),
    subject.manifest.expected_events.map((event) => event.payload),
  );
  for (const name of ["baseline", "fall-5", "recovery"]) {
    assert.equal(subject.counters.get(`driver_boundary_iterations:${name}`), 1);
  }
  assert.equal(subject.counters.get("driver_errors:all"), 0);
  assert.ok(subject.checks.every((check) => check.passed));
});

test("every step's last valid event is sent but its closing boundary makes no HTTP request or check", async () => {
  const subject = await driver();
  for (const [index, step] of subject.workload.steps.entries()) {
    subject.run(step.name, step.expected_request_count - 1);
    const before = subject.requests.length;
    const lastIndex = subject.workload.event_offsets[index] + step.expected_request_count - 1;
    assert.deepEqual(
      subject.requests.at(-1).body,
      subject.manifest.expected_events[lastIndex].payload,
    );
    subject.run(step.name, step.expected_request_count);
    assert.equal(subject.requests.length, before);
    assert.equal(subject.checks.length, before);
  }
});

test("missing submissions are never backfilled and exact-count, dropped-iteration and response checks stay strict", async () => {
  const subject = await driver({ responseStatus: 200 });
  subject.run("baseline", 0);
  subject.run("baseline", 2); // simulate missing scheduled work
  assert.equal(subject.requests.length, 2);
  assert.ok(subject.checks.every((check) => !check.passed));
  for (const step of subject.workload.steps) {
    const thresholds = subject.options.thresholds;
    assert.equal(
      thresholds[`http_reqs{step:${step.name}}`][0],
      `count==${step.expected_request_count}`,
    );
    assert.equal(thresholds[`checks{step:${step.name}}`][0], "rate==1");
    assert.equal(thresholds[`http_req_duration{step:${step.name}}`][0], "p(95)<500");
    assert.equal(thresholds[`http_req_failed{step:${step.name}}`][0], "rate<0.01");
    assert.equal(
      thresholds[`driver_boundary_iterations{step:${step.name}}`][0], "count<=1",
    );
  }
  assert.equal(subject.options.thresholds.dropped_iterations[0], "count==0");
  assert.equal(subject.options.thresholds.driver_errors[0], "count==0");
});

test("larger overruns fail explicitly without sending the next step's event", async () => {
  const subject = await driver();
  for (const step of subject.workload.steps) {
    subject.run(step.name, step.expected_request_count);
    assert.throws(
      () => subject.run(step.name, step.expected_request_count + 1),
      /boundary allowance/,
    );
    assert.equal(subject.counters.get(`driver_boundary_iterations:${step.name}`), 2);
  }
  assert.equal(
    subject.counters.get("driver_errors:all"), subject.workload.steps.length,
  );
  assert.equal(subject.requests.length, 0);
});

test("unknown scenarios and invalid iteration indices fail closed", async () => {
  const subject = await driver();
  for (const [name, index] of [
    ["unknown", 0], ["baseline", -1], ["baseline", 0.5], ["baseline", NaN],
  ]) {
    assert.throws(
      () => subject.run(name, index), /invalid driver scenario or iteration/,
    );
  }
  assert.equal(subject.counters.get("driver_errors:all"), 4);
  assert.equal(subject.requests.length, 0);
});

test("malformed or overlapping slices are rejected during initialization", async () => {
  for (const mutate of [
    (_, workload) => { workload.event_offsets[1] -= 1; },
    (_, workload) => { workload.event_offsets.pop(); },
    (_, workload) => { workload.steps[0].expected_request_count = 61; },
    (_, workload) => { workload.steps[0].duration_seconds = -1; },
    (_, workload) => { workload.steps[1].name = "baseline"; },
    (_, workload) => { workload.steps = []; workload.event_offsets = []; },
    (_, workload) => { workload.steps.pop(); workload.event_offsets.pop(); },
    (manifest) => { manifest.expected_events.pop(); },
  ]) {
    await assert.rejects(driver({ mutate }), /slice|steps|manifest|events/);
  }
});

test("a hole inside an otherwise valid slice fails the driver error threshold", async () => {
  const subject = await driver({
    mutate(manifest) { manifest.expected_events[59] = null; },
  });
  assert.throws(() => subject.run("baseline", 59), /missing manifest event/);
  assert.equal(subject.requests.length, 0);
  assert.equal(subject.counters.get("driver_errors:all"), 1);
});

test("pacing and full metric windows match the frozen definition", async () => {
  const subject = await driver();
  let offset = 0;
  for (const step of subject.workload.steps) {
    const scenario = subject.options.scenarios[step.name];
    assert.equal(scenario.executor, "constant-arrival-rate");
    assert.equal(scenario.rate, step.offered_rate_per_second);
    assert.equal(scenario.timeUnit, "1s");
    assert.equal(scenario.startTime, `${offset}s`);
    assert.equal(scenario.duration, `${step.duration_seconds}s`);
    assert.equal(scenario.preAllocatedVUs, step.offered_rate_per_second);
    assert.equal(scenario.maxVUs, step.offered_rate_per_second);
    offset += step.duration_seconds;
  }
  assert.equal(offset, 1260);
});

test("boundary and error metrics remain in the raw k6 summary", async () => {
  const subject = await driver();
  const data = {
    metrics: {
      driver_boundary_iterations: { values: { count: 3 } },
      driver_errors: { values: { count: 0 } },
    },
  };
  assert.deepEqual(
    JSON.parse(subject.summary(data)["/results/k6-summary.json"]), data,
  );
});
