import getpass
import hashlib
import hmac
import json
import requests

base = "https://mock-api.roostoo.com"
key = getpass.getpass("Testing API key (hidden): ").strip()
secret = getpass.getpass("Testing API secret (hidden): ").strip()

try:
    response = requests.get(base + "/v3/serverTime", timeout=20)
    response.raise_for_status()
    timestamp = response.json()["ServerTime"]
    payload = f"timestamp={timestamp}"
    signature = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    response = requests.get(
        base + "/v3/balance",
        params={"timestamp": timestamp},
        headers={"RST-API-KEY": key, "MSG-SIGNATURE": signature},
        timeout=20
    )
    response.raise_for_status()
    result = response.json()

    if result.get("Success"):
        print("Testing account connected")
        print(json.dumps(result, indent=2))
    else:
        print("Authentication failed:", result.get("ErrMsg", "Unknown error"))
except (requests.RequestException, ValueError, KeyError) as error:
    print("Connection check failed:", str(error))