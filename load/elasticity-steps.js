import exec from "k6/execution";
import { SharedArray } from "k6/data";
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const manifestPath = __ENV.K6_MANIFEST_PATH;
const workloadPath = __ENV.K6_WORKLOAD_PATH;
if (!manifestPath || !workloadPath) {
  throw new Error("K6_MANIFEST_PATH and K6_WORKLOAD_PATH are required");
}

const manifest = JSON.parse(open(manifestPath));
const workload = JSON.parse(open(workloadPath));
const manifestEvents = new SharedArray("elasticity manifest events", () =>
  JSON.parse(open(manifestPath)).expected_events,
);
const apiUrl = __ENV.TRACKRELAY_API_URL || "http://host.docker.internal:8000";
const summaryPath = __ENV.K6_SUMMARY_PATH || "/results/k6-summary.json";

if (
  !Array.isArray(workload.steps) ||
  !Array.isArray(workload.event_offsets) ||
  workload.steps.length === 0 ||
  workload.steps.length !== workload.event_offsets.length
) {
  throw new Error("workload definition is missing steps or event offsets");
}
if (manifestEvents.length !== workload.expected_request_count) {
  throw new Error(
    `manifest has ${manifestEvents.length} events; expected ${workload.expected_request_count}`,
  );
}

let startOffsetSeconds = 0;
let nextEventOffset = 0;
const scenarios = {};
const stepsByName = new Map();
const boundaryIterations = new Counter("driver_boundary_iterations");
const driverErrors = new Counter("driver_errors");
const thresholds = {
  dropped_iterations: ["count==0"],
  driver_errors: ["count==0"],
};
for (const [index, step] of workload.steps.entries()) {
  if (
    typeof step.name !== "string" ||
    !/^[a-z][a-z0-9-]*$/.test(step.name) ||
    stepsByName.has(step.name) ||
    !Number.isSafeInteger(step.offered_rate_per_second) ||
    step.offered_rate_per_second <= 0 ||
    !Number.isSafeInteger(step.duration_seconds) ||
    step.duration_seconds <= 0 ||
    !Number.isSafeInteger(step.expected_request_count) ||
    step.expected_request_count !==
      step.offered_rate_per_second * step.duration_seconds ||
    workload.event_offsets[index] !== nextEventOffset
  ) {
    throw new Error(`invalid manifest slice for step ${step.name}`);
  }
  stepsByName.set(step.name, { ...step, eventOffset: nextEventOffset });
  nextEventOffset += step.expected_request_count;
  scenarios[step.name] = {
    executor: "constant-arrival-rate",
    rate: step.offered_rate_per_second,
    timeUnit: "1s",
    duration: `${step.duration_seconds}s`,
    startTime: `${startOffsetSeconds}s`,
    preAllocatedVUs: step.offered_rate_per_second * (workload.name === "aws-elasticity-demo-v6" ? 5 : 1),
    maxVUs: step.offered_rate_per_second * (workload.name === "aws-elasticity-demo-v6" ? 5 : 1),
    gracefulStop: "10s",
    env: {
      EVENT_OFFSET: String(workload.event_offsets[index]),
      OFFERED_RATE: String(step.offered_rate_per_second),
      STEP_NAME: step.name,
    },
  };
  thresholds[`http_req_duration{step:${step.name}}`] = [
    `p(95)<${workload.ingestion_p95_limit_ms}`,
  ];
  thresholds[`http_reqs{step:${step.name}}`] = [
    `count==${step.expected_request_count}`,
  ];
  thresholds[`http_req_failed{step:${step.name}}`] = [
    `rate<${workload.ingestion_error_limit_percent / 100}`,
  ];
  thresholds[`checks{step:${step.name}}`] = ["rate==1"];
  thresholds[`driver_boundary_iterations{step:${step.name}}`] = ["count<=1"];
  startOffsetSeconds += step.duration_seconds;
}
if (nextEventOffset !== manifestEvents.length) {
  throw new Error("step slices do not cover the complete manifest");
}

export const options = {
  discardResponseBodies: true,
  scenarios,
  thresholds,
};

export default function () {
  const step = stepsByName.get(exec.scenario.name);
  const iteration = exec.scenario.iterationInTest;
  if (!step || !Number.isSafeInteger(iteration) || iteration < 0) {
    driverErrors.add(1);
    throw new Error("invalid driver scenario or iteration index");
  }
  const tags = { step: step.name };
  if (iteration === 0) {
    driverErrors.add(0);
    boundaryIterations.add(0, tags);
  }
  // A time-based executor can dispatch at the closing boundary. Cap HTTP work
  // by this step's count, not the whole array: the next element belongs to the
  // next step. Keep the skipped boundary visible and reject larger overruns.
  if (iteration >= step.expected_request_count) {
    boundaryIterations.add(1, tags);
    if (iteration > step.expected_request_count) {
      driverErrors.add(1);
      throw new Error(`scenario ${step.name} exceeded its boundary allowance`);
    }
    return;
  }
  const manifestEvent = manifestEvents[step.eventOffset + iteration];
  if (!manifestEvent) {
    driverErrors.add(1);
    throw new Error(`scenario ${step.name} has a missing manifest event`);
  }
  const response = http.post(
    `${apiUrl}/api/v1/partners/${manifestEvent.partner_id}/events`,
    JSON.stringify(manifestEvent.payload),
    {
      headers: {
        "Content-Type": "application/json",
        "X-Test-Run-ID": manifest.test_run_id,
      },
      tags: {
        name: "POST partner event",
        offered_rate: String(step.offered_rate_per_second),
        step: step.name,
        ...(["aws-elasticity-demo-v5", "aws-elasticity-demo-v6"].includes(workload.name) ? {sample_sequence: String(iteration)} : {}),
      },
    },
  );
  check(
    response,
    { "event was durably accepted": (result) => result.status === 201 },
    tags,
  );
}

export function handleSummary(data) {
  return {
    [summaryPath]: JSON.stringify(data, null, 2),
  };
}
