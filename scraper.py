import time
import httpx

PROMETHEUS_ENDPOINT = "http://localhost:8000/metrics"

print(f"Starting Prometheus scraper. Polling {PROMETHEUS_ENDPOINT} every 5 seconds.")

try:
    while True:
        try:
            response = httpx.get(PROMETHEUS_ENDPOINT, timeout=3)
            response.raise_for_status()
            
            print(f"\nScrape successful at {time.strftime('%H:%M:%S')}")
            print(response.text)
        except httpx.HTTPError as e:
            print(f"\nScrape failed at {time.strftime('%H:%M:%S')}")
            print(f"HTTP Error: {e}")
        except httpx.RequestError as e:
            print(f"\nScrape failed at {time.strftime('%H:%M:%S')}")
            print(f"Network Request Error: {e}")
            
        time.sleep(5)

except KeyboardInterrupt:
    print("\nExiting scraper.")