"""Container healthcheck: the app is healthy if it answers, even with a 401
(which just means dashboard auth is enabled)."""
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8000/api/config"

try:
    sys.exit(0 if urllib.request.urlopen(URL, timeout=4).status == 200 else 1)
except urllib.error.HTTPError as e:
    sys.exit(0 if e.code == 401 else 1)
except Exception:
    sys.exit(1)
