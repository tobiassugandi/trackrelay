import http from "k6/http";
import { check } from "k6";

const apiUrl = __ENV.TRACKRELAY_API_URL || "http://host.docker.internal:8000";
const summaryPath = __ENV.K6_SUMMARY_PATH || "/results/smoke-summary.json";

export const options = {
  scenarios: {
    smoke: {
      executor: "shared-iterations",
      vus: 1,
      iterations: 5,
      maxDuration: "30s",
    },
  },
  thresholds: {
    checks: ["rate==1"],
  },
};

export default function () {
  const response = http.get(`${apiUrl}/health/live`, {
    tags: { name: "GET /health/live" },
  });

  check(response, {
    "liveness returned HTTP 200": (result) => result.status === 200,
    "liveness reported ok": (result) => result.json("status") === "ok",
  });
}

export function handleSummary(data) {
  return {
    [summaryPath]: JSON.stringify(data, null, 2),
  };
}
