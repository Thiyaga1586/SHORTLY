# SHORTLY - URL Shortener

## Problem Statement
Long URLs can be cumbersome to share, difficult to remember, and sometimes exceed character limits in certain platforms. A URL shortener transforms long URLs into shorter, manageable links while maintaining redirection functionality. Additionally, having an expiration mechanism for short URLs helps in scenarios where temporary access is needed.

## Introduction
Shortly is a simple yet efficient URL shortener built using Flask and SQLite. It allows users to shorten long URLs, store them in a database, and retrieve the original URL upon request. This system also supports expiration dates for generated short URLs, ensuring temporary access when required.

The system follows these key steps:
1. **User submits a long URL** – The API receives the original URL and an optional expiry duration.
2. **Short URL generation** – A unique identifier (UUID) is generated for the URL and stored in an SQLite database.
3. **Storage and retrieval** – The short URL and expiry date (if provided) are saved, and users can later retrieve the original URL by accessing the short link.
4. **Redirection** – When a user visits a short URL, they are redirected to the original link if it is still valid.
5. **Expiration Handling** – If a URL has expired, it is deleted from the database and an appropriate error response is returned.

## Features
- **Shortens long URLs** into concise, easy-to-share links.
- **Supports URL expiration** to restrict access beyond a certain time.
- **Redirects users** to the original URL when a short link is accessed.
- **Stores URLs in SQLite**, ensuring persistence across sessions.
- **Prevents duplicate entries**, reusing short URLs for the same long URL.
- **Handles expired URLs**, automatically removing them from the database.

## Prerequisites
- Python 3.x (Recommended: Python 3.8+)
- Flask (Install using `pip install flask`)
- SQLite3 (Included by default in Python)
- Curl or any API testing tool (Postman, HTTPie, etc.)

## Installation and Setup
1. Clone the repository:
   ```sh
   git clone https://github.com/your-repository/shortly.git
   cd shortly
   ```

2. Install dependencies:
   ```sh
   pip install flask
   ```

3. Run the application:
   ```sh
   python app.py
   ```

By default, the application runs on `http://localhost:5000`.

## API Endpoints

### Shorten a URL
**Endpoint:** `POST /shorten`

#### Request Format (JSON)
```json
{
  "org_url": "https://www.example.com",
  "expiry_days": 7
}
```
- `org_url`: The long URL to be shortened (Required)
- `expiry_days`: Optional, defaults to 7 days

#### Response Format (JSON)
```json
{
  "short_url": "http://localhost:5000/abc123",
  "expires_at": "2025-02-28 12:00:00.000000"
}
```

### Retrieve Original URL
**Endpoint:** `GET /<short_id>`

- Redirects to the original URL if valid.
- Returns an error if the URL is expired or does not exist.

## Input Format for Different Operating Systems

### Windows (Command Prompt / PowerShell)
```sh
curl -X POST http://localhost:5000/shorten -H "Content-Type: application/json" -d "{\"org_url\": \"https://www.example.com\", \"expiry_days\": 7}"
```

### Linux / macOS (Terminal)
```sh
curl -X POST http://localhost:5000/shorten -H 'Content-Type: application/json' -d '{"org_url": "https://www.example.com", "expiry_days": 7}'
```

## Conclusion
Shortly provides a simple and efficient solution for URL shortening while incorporating essential features like expiration handling and duplicate prevention. It is lightweight, easy to set up, and suitable for both personal and small-scale business use cases. By leveraging Flask and SQLite, Shortly ensures quick deployment and reliable performance. Future enhancements could include analytics, custom short URLs, and enhanced security features to improve usability and functionality.

## License
This project is licensed under the MIT License.

