import time
import httpx
from collections import deque
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from circuitbreaker import CircuitBreaker, CircuitBreakerError
from prometheus_client import Gauge, Counter, Histogram, start_http_server

# Mock server
class MockRequestHandler(BaseHTTPRequestHandler):
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
    httpd = HTTPServer(server_address, MockRequestHandler)
    httpd.serve_forever()

# Start the mock server in a separate thread
mock_server_thread = Thread(target=run_mock_server, daemon=True)
mock_server_thread.start()

STATE = Gauge('feature_store_circuitbreaker_state', 'The states of the circuit breaker (0=closed, 1=open, 2=half_open)')
CALLS_HISTOGRAM = Histogram(
    'feature_store_circuitbreaker_calls_seconds',
    'Circuit breaker call durations',
    buckets=[.005, .01, .025, .05, .1, .25, .5, 1.0, 2.5, 5.0]
)
FAILURE_RATE_GAUGE = Gauge('feature_store_circuitbreaker_failure_rate', 'Failure rate of the circuit breaker over a rolling window')
NOT_PERMITTED_CALLS_COUNTER = Counter('feature_store_circuitbreaker_not_permitted_calls_total', 'Total number of calls which have not been permitted')

def our_fs_serving_api_call(should_fail: bool):
    url = "http://localhost:8080/status/500" if should_fail else "http://localhost:8080/status/200"
    
    response = httpx.get(url, timeout=0.5)
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
    with CALLS_HISTOGRAM.time():
        try:
            print(f"Current internal circuit breaker state: {circuit_breaker.state}")

            result = circuit_breaker.decorate(func)(*args, **kwargs)
            metrics_recorder.record_success()
            return result
        except CircuitBreakerError:
            NOT_PERMITTED_CALLS_COUNTER.inc()
            return "Call not permitted by circuit breaker."
        except httpx.HTTPError:
            metrics_recorder.record_failure()
            return "Call failed and was recorded as a failure."
        except Exception:
            metrics_recorder.record_failure()
            return "An unexpected error occurred."
        finally:
            if circuit_breaker.state == "closed":
                STATE.set(0)
            elif circuit_breaker.state == "open":
                STATE.set(1)
            else:
                STATE.set(2)

if __name__ == '__main__':
    start_http_server(8000)
    print("Prometheus metrics server started at http://localhost:8000")
    print("Circuit breaker application is now running continuously. Press Ctrl+C to exit.")
    
    call_count = 0
    try:
        print("\nPhase 1: Triggering failures to open the circuit")
        for i in range(3): # 3 consecutive failures
            print(f"[{time.strftime('%H:%M:%S')}] Attempting to fail call {i+1}...")
            print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=True))
            time.sleep(1)

        print("Circuit breaker should now be in the open state. The state is: " + STATE._value._value.__str__())

        print("\nPhase 2: Circuit is now open")
        for i in range(2): # 2 calls that should be rejected
            print(f"[{time.strftime('%H:%M:%S')}] Attempting a call while circuit is open...")
            print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=False))
            time.sleep(1)

        print("\nPhase 3: Waiting for recovery timeout")
        print("Waiting for 5 seconds...")
        time.sleep(5) # circuit breaker recovers and go to half_open state
        
        print(f"[{time.strftime('%H:%M:%S')}] Attempting a successful call to close the circuit...")
        print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=False))
        print("Circuit breaker should now be in the closed state. The state is: " + STATE._value._value.__str__())
        time.sleep(2)

        print("\nPhase 4: Back to normal operations")
        while True:
            print(f"[{time.strftime('%H:%M:%S')}] Making a regular call...")
            print(circuit_breaker_with_metrics(our_fs_serving_api_call, should_fail=False))
            time.sleep(2)

    except KeyboardInterrupt:
        print("\nExiting application.")