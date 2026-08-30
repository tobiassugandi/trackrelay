import exec from "k6/execution";
import { SharedArray } from "k6/data";
import http from "k6/http";
import { check } from "k6";

function positiveInteger(value, name) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

const manifestPath = __ENV.K6_MANIFEST_PATH;
if (!manifestPath) {
  throw new Error("K6_MANIFEST_PATH is required");
}

const manifest = JSON.parse(open(manifestPath));
const manifestEvents = new SharedArray("manifest events", () =>
  JSON.parse(open(manifestPath)).expected_events,
);
const requestRate = positiveInteger(__ENV.LOAD_RATE, "LOAD_RATE");
const durationSeconds = positiveInteger(
  __ENV.LOAD_DURATION_SECONDS,
  "LOAD_DURATION_SECONDS",
);
const expectedHttpStatus = positiveInteger(
  __ENV.EXPECTED_HTTP_STATUS,
  "EXPECTED_HTTP_STATUS",
);
const expectedRequests = requestRate * durationSeconds;
const activeDurationMilliseconds = durationSeconds * 1000 - 1;
const preAllocatedVUs = Math.max(100, requestRate * 2);
const apiUrl = __ENV.TRACKRELAY_API_URL || "http://host.docker.internal:8000";
const summaryPath =
  __ENV.K6_SUMMARY_PATH || "/results/k6-summary.json";

if (manifestEvents.length !== expectedRequests) {
  throw new Error(
    `manifest has ${manifestEvents.length} events; expected ${expectedRequests}`,
  );
}

export const options = {
  discardResponseBodies: true,
  scenarios: {
    downstream_under_load: {
      executor: "constant-arrival-rate",
      rate: requestRate,
      timeUnit: "1s",
      duration: `${activeDurationMilliseconds}ms`,
      // Preallocate two seconds of offered traffic—four times the 500 ms p95
      // SLO—with a 100-VU floor for low-rate tail-latency outliers. This keeps
      // driver startup and VU allocation from masquerading as a system limit.
      preAllocatedVUs,
      maxVUs: preAllocatedVUs,
      gracefulStop: "10s",
    },
  },
  thresholds: {
    checks: ["rate==1"],
    dropped_iterations: ["count==0"],
  },
};

export default function () {
  const manifestEvent = manifestEvents[exec.scenario.iterationInTest];
  if (!manifestEvent) {
    return;
  }
  const response = http.post(
    `${apiUrl}/api/v1/partners/${manifestEvent.partner_id}/events`,
    JSON.stringify(manifestEvent.payload),
    {
      headers: {
        "Content-Type": "application/json",
        "X-Test-Run-ID": manifest.test_run_id,
      },
      tags: { name: "POST partner event" },
    },
  );

  check(response, {
    [`response was HTTP ${expectedHttpStatus}`]:
      (result) => result.status === expectedHttpStatus,
  });
}

export function handleSummary(data) {
  return {
    [summaryPath]: JSON.stringify(data, null, 2),
  };
}
