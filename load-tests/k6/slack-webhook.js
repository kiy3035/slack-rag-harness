import crypto from 'k6/crypto';
import exec from 'k6/execution';
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate } from 'k6/metrics';

const BASE_URL = (__ENV.BASE_URL || 'http://api:8000').replace(/\/$/, '');
const SIGNING_SECRET = __ENV.SLACK_SIGNING_SECRET || 'local_test_signing_secret';
const REQUESTS = positiveInteger(__ENV.REQUESTS, 20, 'REQUESTS');
const VUS = Math.min(positiveInteger(__ENV.VUS, 10, 'VUS'), REQUESTS);
const RUN_ID = safeRunId(__ENV.RUN_ID || `local-${Date.now()}`);
const MODE = __ENV.MODE || 'unique';
const SUMMARY_PATH = __ENV.SUMMARY_PATH || '/results/slack-webhook-summary.json';

if (!['unique', 'duplicate'].includes(MODE)) {
  throw new Error('MODE는 unique 또는 duplicate여야 합니다.');
}

const ackUnderThreeSeconds = new Rate('slack_ack_under_3s');
const createdJobs = new Counter('slack_jobs_created');
const duplicateJobs = new Counter('slack_jobs_duplicate');

export const options = {
  scenarios: {
    webhook_burst: {
      executor: 'shared-iterations',
      vus: VUS,
      iterations: REQUESTS,
      maxDuration: '2m',
    },
  },
  thresholds: {
    checks: ['rate>0.99'],
    http_req_failed: ['rate<0.01'],
    'http_req_duration{endpoint:slack_events}': ['p(95)<3000'],
    slack_ack_under_3s: ['rate>0.99'],
  },
};

// 양의 정수 환경변수를 검증해 잘못된 부하 조건을 즉시 거절한다.
function positiveInteger(rawValue, fallback, name) {
  const value = Number(rawValue || fallback);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${name}은 양의 정수여야 합니다.`);
  }
  return value;
}

// 외부 이벤트 ID에 안전하게 사용할 수 있도록 실행 식별자를 정규화한다.
function safeRunId(value) {
  const normalized = String(value).replace(/[^A-Za-z0-9_-]/g, '-').slice(0, 80);
  if (!normalized) {
    throw new Error('RUN_ID에 사용할 수 있는 문자가 없습니다.');
  }
  return normalized;
}

// 실행 모드에 따라 매 요청의 고유 ID 또는 하나의 중복 ID를 만든다.
function eventIdFor(iteration) {
  const suffix = MODE === 'duplicate' ? 'same' : String(iteration);
  return `Ev_LOAD_${RUN_ID}_${suffix}`;
}

// 실제 Slack app_mention 계약과 같은 합성 Payload를 만든다.
function buildSlackPayload(iteration, timestamp) {
  const eventId = eventIdFor(iteration);
  const messageSuffix = String(iteration % 1000000).padStart(6, '0');
  return {
    type: 'event_callback',
    event_id: eventId,
    event: {
      type: 'app_mention',
      text: '<@B_LOAD_TEST> 정산 배치 마감 전에 무엇을 확인하나요?',
      channel: 'C_LOAD_TEST',
      ts: `${timestamp}.${messageSuffix}`,
    },
  };
}

// Slack v0 규격에 맞춰 원본 JSON 본문에 HMAC-SHA256 서명을 계산한다.
function signSlackBody(body, timestamp) {
  return `v0=${crypto.hmac('sha256', SIGNING_SECRET, `v0:${timestamp}:${body}`, 'hex')}`;
}

// 응답 JSON을 안전하게 읽고 계약 위반이면 빈 객체로 처리한다.
function parseAck(response) {
  try {
    return response.json();
  } catch (error) {
    return {};
  }
}

// Slack Endpoint가 성공 상태를 반환했는지 검사한다.
function isHttpOk(response) {
  return response.status === 200;
}

// ACK 본문이 저장된 작업 식별자를 포함하는지 검사한다.
function hasAcceptedJob(ack) {
  return ack.ok === true && typeof ack.job_id === 'string' && ack.job_id.length > 0;
}

// 각 VU 반복에서 서명된 Webhook을 한 건 보내고 ACK 지표만 기록한다.
export default function sendSlackWebhook() {
  const iteration = exec.scenario.iterationInTest;
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const body = JSON.stringify(buildSlackPayload(iteration, timestamp));
  const response = http.post(`${BASE_URL}/api/v1/slack/events`, body, {
    headers: {
      'Content-Type': 'application/json',
      'X-Slack-Request-Timestamp': timestamp,
      'X-Slack-Signature': signSlackBody(body, timestamp),
    },
    tags: { endpoint: 'slack_events', mode: MODE },
  });
  const ack = parseAck(response);

  check(response, { 'Slack ACK HTTP 200': isHttpOk });
  check(ack, { 'Slack ACK에 job_id 존재': hasAcceptedJob });
  ackUnderThreeSeconds.add(response.timings.duration < 3000);
  if (ack.duplicate === true) {
    duplicateJobs.add(1);
  } else if (ack.job_id) {
    createdJobs.add(1);
  }
}

// 원시 k6 요약을 실행별 JSON 파일로 보존한다.
export function handleSummary(data) {
  const metrics = data.metrics;
  const visibleSummary = {
    run_id: RUN_ID,
    mode: MODE,
    requests: metricValue(metrics, 'http_reqs', 'count'),
    requests_per_second: metricValue(metrics, 'http_reqs', 'rate'),
    ack_p95_ms: metricValue(metrics, 'http_req_duration', 'p(95)'),
    ack_max_ms: metricValue(metrics, 'http_req_duration', 'max'),
    failed_rate: metricValue(metrics, 'http_req_failed', 'rate'),
    checks_rate: metricValue(metrics, 'checks', 'rate'),
    ack_under_3s_rate: metricValue(metrics, 'slack_ack_under_3s', 'rate'),
    created_jobs: metricValue(metrics, 'slack_jobs_created', 'count'),
    duplicate_jobs: metricValue(metrics, 'slack_jobs_duplicate', 'count'),
  };
  return {
    [SUMMARY_PATH]: JSON.stringify(
      {
        run_id: RUN_ID,
        mode: MODE,
        requests: REQUESTS,
        vus: VUS,
        generated_at: new Date().toISOString(),
        k6: data,
      },
      null,
      2,
    ),
    stdout: `${JSON.stringify(visibleSummary, null, 2)}\n`,
  };
}

// k6 요약에서 필요한 지표가 없을 때도 명시적인 0을 반환한다.
function metricValue(metrics, metricName, valueName) {
  const metric = metrics[metricName];
  if (!metric || !metric.values || metric.values[valueName] === undefined) {
    return 0;
  }
  return metric.values[valueName];
}
