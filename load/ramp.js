import exec from "k6/execution";
import http from "k6/http";
import { check } from "k6";

const DEFAULT_RATES = [10, 25, 50, 100, 250, 500];
const P95_LATENCY_LIMIT_MS = 500;
const ERROR_RATE_LIMIT = 0.01;

function positiveInteger(value, name) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

function configuredRates() {
  if (!__ENV.RAMP_RATES) {
    return DEFAULT_RATES;
  }
  return __ENV.RAMP_RATES.split(",").map((value) =>
    positiveInteger(value.trim(), "every RAMP_RATES value"),
  );
}

const rates = configuredRates();
const tierDurationSeconds = positiveInteger(
  __ENV.RAMP_TIER_DURATION_SECONDS || "10",
  "RAMP_TIER_DURATION_SECONDS",
);
const apiUrl = __ENV.TRACKRELAY_API_URL || "http://host.docker.internal:8000";
const partnerId = __ENV.RAMP_PARTNER_ID || "load-alpha";
const runId = __ENV.RAMP_RUN_ID;
const summaryPath =
  __ENV.K6_SUMMARY_PATH || "/results/ramp-summary.json";

if (!runId) {
  throw new Error("RAMP_RUN_ID is required to keep event identities unique");
}

function tierForElapsedMilliseconds(elapsedMilliseconds) {
  const tierIndex = Math.min(
    Math.floor(elapsedMilliseconds / (tierDurationSeconds * 1000)),
    rates.length - 1,
  );
  return rates[tierIndex];
}

function abortingThreshold(expression, delaySeconds) {
  return [
    {
      threshold: expression,
      abortOnFail: true,
      delayAbortEval: `${delaySeconds}s`,
    },
  ];
}

const stages = rates.flatMap((rate) => [
  { target: rate, duration: "0s" },
  { target: rate, duration: `${tierDurationSeconds}s` },
]);

const tierThresholds = Object.fromEntries(
  rates.flatMap((rate, tierIndex) => {
    const tierEndSeconds = (tierIndex + 1) * tierDurationSeconds;
    return [
      [
        `http_req_duration{rate:${rate}}`,
        abortingThreshold(`p(95)<${P95_LATENCY_LIMIT_MS}`, tierEndSeconds),
      ],
      [
        `http_req_failed{rate:${rate}}`,
        abortingThreshold(`rate<${ERROR_RATE_LIMIT}`, tierEndSeconds),
      ],
      [
        `checks{rate:${rate}}`,
        abortingThreshold("rate==1", tierEndSeconds),
      ],
    ];
  }),
);

const maximumRate = Math.max(...rates);

export const options = {
  discardResponseBodies: true,
  scenarios: {
    ingestion_ramp: {
      executor: "ramping-arrival-rate",
      startRate: rates[0],
      timeUnit: "1s",
      preAllocatedVUs: Math.max(1, Math.ceil(maximumRate / 2)),
      maxVUs: maximumRate,
      stages,
    },
  },
  thresholds: {
    ...tierThresholds,
    dropped_iterations: [
      {
        threshold: "count==0",
        abortOnFail: true,
      },
    ],
  },
};

export default function () {
  const rate = tierForElapsedMilliseconds(exec.instance.currentTestRunDuration);
  const eventIdentity = `${runId}-${exec.scenario.iterationInTest}`;
  const payload = JSON.stringify({
    event_id: `RAMP-EVT-${eventIdentity}`,
    tracking_number: `RAMP-TRK-${eventIdentity}`,
    status: "CREATED",
    event_time: new Date().toISOString(),
  });
  const response = http.post(
    `${apiUrl}/api/v1/partners/${partnerId}/events`,
    payload,
    {
      headers: { "Content-Type": "application/json" },
      tags: { name: "POST partner event", rate: String(rate) },
    },
  );

  check(
    response,
    { "event was created": (result) => result.status === 201 },
    { rate: String(rate) },
  );
}

export function handleSummary(data) {
  return {
    [summaryPath]: JSON.stringify(data, null, 2),
  };
}
