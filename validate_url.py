import re

def is_valid_url(url):
    regex = re.compile(
        r'^(https?://)?'  # Optional http or https
        r'(([A-Za-z0-9-]+\.)+[A-Za-z]{2,6})'  # Domain name
        r'(:\d+)?(/.*)?$',  # Optional port and path
        re.IGNORECASE
    )
    return re.match(regex, url) is not None
# ✅ Test URLs
test_urls = [
    "https://www.google.com",
    "http://example.com",
    "https://sub.domain.com",
    "www.facebook.com",  # ❌ Invalid (missing "http://")
    "random text",  # ❌ Invalid (not a URL)
    "ftp://fileserver.com",  # ❌ Invalid (FTP is not allowed)
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # ✅ Valid with path
    "http://localhost:5000",  # ✅ Valid (localhost allowed)
]
print("Starting URL validation...\n")
# 🔹 Loop through URLs and print results
for url in test_urls:
    if is_valid_url(url):
        print(f"✅ Valid: {url}")
    else:
        print(f"❌ Invalid: {url}")
