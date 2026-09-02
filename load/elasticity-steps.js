import exec from "k6/execution";
import { SharedArray } from "k6/data";
import http from "k6/http";
import { check } from "k6";

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

if (!Array.isArray(workload.steps) || !Array.isArray(workload.event_offsets)) {
  throw new Error("workload definition is missing steps or event offsets");
}
if (manifestEvents.length !== workload.expected_request_count) {
  throw new Error(
    `manifest has ${manifestEvents.length} events; expected ${workload.expected_request_count}`,
  );
}

let startOffsetSeconds = 0;
const scenarios = {};
const thresholds = { dropped_iterations: ["count==0"] };
for (const [index, step] of workload.steps.entries()) {
  const activeDurationMilliseconds = step.duration_seconds * 1000 - 1;
  scenarios[step.name] = {
    executor: "constant-arrival-rate",
    rate: step.offered_rate_per_second,
    timeUnit: "1s",
    duration: `${activeDurationMilliseconds}ms`,
    startTime: `${startOffsetSeconds}s`,
    preAllocatedVUs: step.offered_rate_per_second,
    maxVUs: step.offered_rate_per_second,
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
  startOffsetSeconds += step.duration_seconds;
}

export const options = {
  discardResponseBodies: true,
  scenarios,
  thresholds,
};

export default function () {
  const eventOffset = Number(__ENV.EVENT_OFFSET);
  const manifestEvent = manifestEvents[eventOffset + exec.scenario.iterationInTest];
  if (!manifestEvent) {
    throw new Error(`scenario ${__ENV.STEP_NAME} exceeded its manifest slice`);
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
        offered_rate: __ENV.OFFERED_RATE,
        step: __ENV.STEP_NAME,
      },
    },
  );
  check(
    response,
    { "event was durably accepted": (result) => result.status === 201 },
    { step: __ENV.STEP_NAME },
  );
}

export function handleSummary(data) {
  return {
    [summaryPath]: JSON.stringify(data, null, 2),
  };
}
