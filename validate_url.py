from urllib.parse import urlparse  # better than regex for URL parsing

def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return None

    # if scheme missing, add https by default
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    # allow only http/https
    if parsed.scheme not in ("http", "https"):
        return None

    # must have a host
    if not parsed.netloc:
        return None

    return url

# Test URLs
test_urls = [
    "https://www.google.com",
    "http://example.com",
    "https://sub.domain.com",
    "www.facebook.com",  # now becomes https://www.facebook.com
    "random text",  # invalid
    "ftp://fileserver.com",  # invalid scheme
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # valid
    "http://localhost:5000",  # valid
]

print("Starting URL validation...\n")

for url in test_urls:
    normalized = normalize_url(url)
    if normalized:
        print(f"Valid: {url}  -> normalized: {normalized}")
    else:
        print(f"Invalid: {url}")
