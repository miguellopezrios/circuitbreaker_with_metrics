import time
import threading
import http.server
import httpx
from collections import deque
from circuitbreaker import CircuitBreaker, CircuitBreakerError
from prometheus_client import Gauge, Counter, Summary, start_http_server

# Mock server
class MockRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # Respond based on the URL path
        if self.path == "/status/200":
            self.send_response(200)
            self.end_headers()
        elif self.path == "/status/500":
            self.send_response(500)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
    
    # Override log_message to suppress the logs
    def log_message(self, format, *args):
        return

def run_mock_server():
    server_address = ('localhost', 8080)
    httpd = http.server.HTTPServer(server_address, MockRequestHandler)
    httpd.serve_forever()

# Start the mock server in a separate thread
mock_server_thread = threading.Thread(target=run_mock_server, daemon=True)
mock_server_thread.start()

STATE = Gauge('feature_store_circuitbreaker_state', 'The states of the circuit breaker (0=closed, 1=open, 2=half_open)')
CALLS_SUMMARY = Summary('feature_store_circuitbreaker_calls_seconds', 'Circuit breaker call durations')
FAILURE_RATE_GAUGE = Gauge('feature_store_circuitbreaker_failure_rate', 'Failure rate of the circuit breaker over a rolling window')
NOT_PERMITTED_CALLS_COUNTER = Counter('feature_store_circuitbreaker_not_permitted_calls_total', 'Total number of calls which have not been permitted')

def our_fs_serving_api_call(should_fail: bool):
    url = "http://localhost:8080/status/500" if should_fail else "http://localhost:8080/status/200"
    
    response = httpx.get(url, timeout=4)
    response.raise_for_status()
    return f"Service call successful! Status: {response.status_code}"

circuit_breaker = CircuitBreaker(
    failure_threshold=3,
    recovery_timeout=5,
    expected_exception=(httpx.TimeoutException, httpx.HTTPStatusError)
)

class MetricsRecorder:
    def __init__(self, window_size: int = 10):
        self._call_history = deque(maxlen=window_size)

    def record_success(self):
        self._call_history.append(0)
        self._update_metrics()

    def record_failure(self):
        self._call_history.append(1)
        self._update_metrics()

    def _update_metrics(self):
        if len(self._call_history) > 0:
            failure_rate = sum(self._call_history) / len(self._call_history)
            FAILURE_RATE_GAUGE.set(failure_rate)

metrics_recorder = MetricsRecorder(window_size=10)

def circuit_breaker_with_metrics(func, *args, **kwargs):
    with CALLS_SUMMARY.time():
        try:
            result = circuit_breaker.decorate(func)(*args, **kwargs)
            metrics_recorder.record_success()
            STATE.set(0) # Closed
            return result
        except CircuitBreakerError:
            STATE.set(1) # Open
            NOT_PERMITTED_CALLS_COUNTER.inc()
            return "Call not permitted by circuit breaker."
        except httpx.HTTPError:
            metrics_recorder.record_failure()
            return "Call failed and was recorded as a failure."
        except Exception:
            metrics_recorder.record_failure()
            return "An unexpected error occurred."


if __name__ == '__main__':
    start_http_server(8000)
    print("Prometheus metrics server started at http://localhost:8000")
    print("\n")
    print("Starting POC: Monitoring a circuit breaker with HTTPX")
    print("\n")
    print("Phase 1: Successful calls")
    print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=False))
    print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=False))
    print("Circuit breaker should now be in the closed state. The state is: " + STATE._value._value.__str__())
    print("\n")
    print("Phase 2: Failed calls")
    print("Simulating 3 failures...")
    print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=True))
    print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=True))
    print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=True))
    print("\n")
    print("Phase 3: Circuit is open (Calls not permitted)")
    print("Attempting calls while circuit is open...")
    print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=False))
    print("Circuit breaker should now be in the open state. The state is: " + STATE._value._value.__str__())
    print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=False))
    print("Circuit breaker should now be in the open state. The state is: " + STATE._value._value.__str__())
    print("\n")
    print("Phase 4: Wait for half-open state and retry")
    print("Waiting for 5 seconds for recovery...")
    time.sleep(5)
    print("Retrying call in half-open state...")
    print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=False))
    print("Circuit breaker should now be in the closed state. The state is: " + STATE._value._value.__str__())
    print("\n")
    print("Final Metric Values")
    print(f"Final Failure Rate Gauge: {FAILURE_RATE_GAUGE._value._value}")
    print(f"Final Not Permitted Calls Counter: {NOT_PERMITTED_CALLS_COUNTER._value._value}")