# SHORTLY – URL Shortener

## Live Deployment

**Production URL:**  
https://shortly-0ngb.onrender.com/

---

## Problem Statement

Long URLs are difficult to share, remember, and often exceed character limits across platforms. A URL shortener converts long URLs into compact, manageable links while preserving correct redirection behavior. Adding expiration support enables time-bound access control, which is useful for temporary sharing and security-sensitive use cases.

---

## Overview

**SHORTLY** is a production-ready URL shortener built using Flask and SQLite, designed as an API-first backend service with a minimal web interface.

The project focuses on real-world backend engineering concerns such as:

- Correctness and validation
- Persistent storage
- HTTP semantics
- Rate limiting and abuse prevention
- Automated testing
- Production deployment

Rather than being a toy application, the system is structured to resemble how a real backend service is designed, deployed, and maintained.

---

## System Architecture & Workflow

### URL Submission
Users submit a long URL through the REST API or the web UI, optionally specifying an expiration duration.

### Validation & Normalization
URLs are validated and normalized (scheme enforcement, host validation) to ensure correctness and consistency before storage.

### Short ID Generation
A unique short identifier is generated and mapped to the original URL.

### Persistent Storage
URL mappings are stored in a disk-backed SQLite database, ensuring persistence across restarts and redeployments.

### Redirection
Visiting a short URL triggers an HTTP 302 redirect to the original URL if it is valid.

### Expiration Handling
Expired URLs are invalidated and return an HTTP 410 Gone response. A background cleanup process removes expired entries.

---

## Features

- Shortens long URLs into concise, shareable links
- URL validation and normalization for correctness
- Configurable expiration for shortened URLs
- HTTP 302 redirection for valid URLs
- HTTP 410 responses for expired URLs
- Duplicate URL detection and reuse
- Click count tracking for basic analytics
- Background cleanup of expired URLs
- Rate limiting on URL creation endpoint
- Health check endpoint for deployment monitoring
- Minimal frontend UI served directly by Flask
- API-first design for extensibility

---

## Technology Stack

### Core
- **Language:** Python
- **Backend Framework:** Flask
- **Database:** SQLite

### Persistence
- SQLite configured with disk-backed persistent storage
- Database path controlled via environment variables

### Web Servers
- **Waitress** – production-style server for local Windows development
- **Gunicorn** – production WSGI server for Linux/cloud deployment

### Testing
- Pytest
- Isolated test databases
- API correctness and edge-case validation

### Containerization
- Docker
- Reproducible builds
- Production execution using Gunicorn

### Deployment
- Render
- Linux environment
- Persistent disk attached
- Environment-based configuration

---

## Persistence & Storage

In production, the application uses SQLite with a mounted disk to ensure data durability.

This guarantees:

- Shortened URLs persist across restarts
- Redeployments do not wipe stored data
- Configuration remains environment-agnostic

**Database configuration:**
```
SHORTLY_DB=/var/data/urls.db
```

---

## API Endpoints

### Shorten a URL

**POST** `/shorten`

**Request:**
```json
{
  "org_url": "https://www.example.com",
  "expiry_days": 7
}
```

**Response:**
```json
{
  "short_url": "https://shortly-0ngb.onrender.com/abc123",
  "expires_at": "2025-02-28 12:00:00.000000"
}
```

### Redirect to Original URL

**GET** `/<short_id>`

- Redirects to original URL if valid
- Returns 410 Gone if expired
- Returns 404 Not Found if the ID does not exist

### Analytics

**GET** `/api/info/<short_id>`

Returns metadata such as:
- Original URL
- Expiration timestamp
- Click count

### Health Check

**GET** `/health`

Used for uptime and deployment monitoring.

---

## Frontend

A minimal frontend is available at:

**GET** `/`

The UI allows users to:
- Submit long URLs
- Specify expiration duration
- Receive and copy shortened URLs

The frontend communicates directly with the backend API and requires no separate frontend deployment.

---

## Testing

Automated tests are written using pytest.

Coverage includes:
- URL normalization logic
- `/shorten` endpoint correctness
- Redirect behavior (HTTP 302)
- Expired URL handling (HTTP 410)
- Isolated database usage per test run

**Run tests:**
```bash
pytest -q
```

---

## Docker Support

The application includes Docker support for reproducible execution.

**Build and run locally:**
```bash
docker build -t shortly .
docker run -p 5000:5000 shortly
```

Docker runs the application using Gunicorn in production mode.

---

## Deployment

The application is deployed on Render using:

- Gunicorn as the production WSGI server
- Disk-backed SQLite for persistence
- Environment-variable-driven configuration
- Dynamic host-based URL generation

**Live Deployment:**  
https://shortly-0ngb.onrender.com/

---

## Conclusion

SHORTLY is a clean, production-oriented backend project that demonstrates end-to-end system building—from API design and validation to persistence, testing, and cloud deployment.

The project emphasizes correctness, simplicity, and real-world engineering practices, making it a strong representation of backend development skills relevant to Google STEP and Associate Software Developer roles.

---

## License

**MIT License**  
This project is licensed under the MIT License.
