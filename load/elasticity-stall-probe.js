// Local regression only: never use this abbreviated scenario as cloud evidence.
import execute, { options as fullOptions } from './elasticity-steps.js';
import exec from 'k6/execution';
import { Counter } from 'k6/metrics';
export { handleSummary } from './elasticity-steps.js';

const boundary = new Counter('probe_boundary_iterations');

export const options = {
  ...fullOptions,
  scenarios: {
    'peak-25': { ...fullOptions.scenarios['peak-25'], startTime: '0s', duration: '20s' },
  },
  thresholds: {
    dropped_iterations: ['count==0'],
    http_reqs: ['count==500'],
    checks: ['rate==1'],
    probe_boundary_iterations: ['count<=1'],
  },
};
export default function () {
  // The production manifest has 4,500 peak events. This abbreviated probe
  // needs the same one-closing-iteration allowance at its own 500-event edge.
  const index = exec.scenario.iterationInTest;
  if (index === 0) boundary.add(0);
  if (index >= 500) {
    boundary.add(1);
    if (index > 500) throw new Error('probe exceeded its boundary allowance');
    return;
  }
  execute();
}
