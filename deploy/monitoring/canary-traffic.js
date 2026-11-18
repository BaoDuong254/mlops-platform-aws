// Bounded dev traffic only. HTTP errors are reported to Argo metrics, not treated as generator failure.
import http from 'k6/http';
import { check } from 'k6';
export const options = {
  scenarios: { canary: {
    executor: 'constant-arrival-rate', rate: 40, timeUnit: '1s', duration: '600s',
    preAllocatedVUs: 20, maxVUs: 20, gracefulStop: '10s',
  } },
  thresholds: {
    // Generator availability budget, distinct from application error/latency gates.
    dropped_iterations: [{ threshold: 'count<1200', abortOnFail: true, delayAbortEval: '60s' }],
    iterations: ['count>=22800'],
  },
};
export function setup() {
  const response = http.get(`${__ENV.LOAD_BASE_URL}/healthz`, { timeout: '10s' });
  if (response.status !== 200) { throw new Error('Internal ALB baseline unavailable; no release started'); }
  console.log('CANARY_TRAFFIC_READY');
}
export default function () {
  const response = http.post(`${__ENV.LOAD_BASE_URL}/predict`, JSON.stringify({
    pickup_location_id: 1, dropoff_location_id: 2, pickup_datetime: '2025-04-07T16:00:00Z',
  }), { headers: { 'Content-Type': 'application/json' }, timeout: '10s' });
  check(response, { 'prediction 200': (r) => r.status === 200 });
}
export function handleSummary(data) {
  // Capture counters/errors even on threshold failure. No forged application errors or payloads.
  return { stdout: `CANARY_TRAFFIC_RESULT=${JSON.stringify(data)}\n` };
}
