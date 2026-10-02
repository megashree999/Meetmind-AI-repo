import urllib.request
import json

def check_url(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            return response.status
    except urllib.error.URLError as e:
        if hasattr(e, 'code'):
            return e.code
        return str(e)

print("API root (http://127.0.0.1:2024/ok):", check_url("http://127.0.0.1:2024/ok"))
print("API root (http://127.0.0.1:2024):", check_url("http://127.0.0.1:2024"))
print("API docs (http://127.0.0.1:2024/docs):", check_url("http://127.0.0.1:2024/docs"))
