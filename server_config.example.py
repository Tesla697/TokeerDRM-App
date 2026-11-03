# Copy this file to `server_config.py` and set your code-store server URL.
# server_config.py is gitignored so the address never lands in the public repo;
# PyInstaller bundles it into the built exe.
SERVER_URL = "http://your-server:8091"
# Tried in order. Put a Cloudflare-proxied hostname first so ISPs that block the
# raw IP still get through; keep the raw URL last as a fallback.
SERVER_URLS = ["https://codestore.example.com", SERVER_URL]
